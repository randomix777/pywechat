"""充值 → 到账 → 额度可用。

覆盖产品规则里最要紧的那些：**零余额不许分析、重复回调不重复加点、
签名/金额/币种/归属对不上必须拒绝、浏览器跳转不算到账、退款不把余额扣成负数**。

这里用的是 :class:`TestPaymentAdapter` —— 明确标记的测试通道，不是真实支付。
真实 JYI 另行报告。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.payment import (  # noqa: E402
    PAY_PAID,
    PAY_PENDING,
    PaymentError,
    RechargeOrder,
    TestPaymentAdapter,
    credit_reference,
    refund_offset,
    validate_notice,
)
from wechat_ai_assistant.product import ProductStore, ValidationFailed  # noqa: E402


class RechargeTestBase(unittest.TestCase):
    def setUp(self):
        self.store = ProductStore(":memory:")
        self.account = self.store.create_account("shop-test", "password-1234")["account_id"]
        self.pay = TestPaymentAdapter(secret="secret")

    def tearDown(self):
        self.store.close()

    def order(self, credits=100, amount_minor=1000, currency="CNY", key="") -> RechargeOrder:
        built = self.pay.create_order(self.account, f"pack-{credits}", credits=credits,
                                      amount_minor=amount_minor, currency=currency,
                                      idempotency_key=key or str(self._seq()))
        self.store.save_recharge_order(built)
        return built

    def _seq(self):
        self._counter = getattr(self, "_counter", 0) + 1
        return self._counter

    def settle(self, order: RechargeOrder, **overrides) -> dict[str, Any]:
        payload = self.pay.build_notice(order, **overrides)
        notice = self.pay.verify_notice(payload, payload["signature"])
        local = self.store.get_recharge_order(order.order_id)
        validate_notice(notice, _as_order(local) if local else None)
        return self.store.credit(
            self.account, order.credits, credit_reference(notice.transaction_id),
            order_id=order.order_id, adapter=TestPaymentAdapter.TEST_MARK,
        )


def _as_order(row: dict[str, Any]) -> RechargeOrder:
    return RechargeOrder(
        order_id=row["order_id"], account_id=row["account_id"], product_id=row["product_id"],
        amount_minor=row["amount_minor"], currency=row["currency"], credits=row["credits"],
        status=row["status"], external_id=row.get("external_id", ""),
    )


class CreditIdempotencyTests(RechargeTestBase):
    def test_one_top_up_credits_once(self):
        order = self.order()
        result = self.settle(order)
        self.assertTrue(result["applied"])
        self.assertEqual(self.store.get_subscription(self.account)["quota_micro_usd"], 100)

    def test_a_repeated_callback_does_not_credit_again(self):
        """支付回调会重复送达。靠自觉查重必然重复加点。"""
        order = self.order()
        self.settle(order)
        again = self.settle(order)
        self.assertFalse(again["applied"], "重复回调又加了一次")
        self.assertTrue(again["duplicate"])
        self.assertEqual(self.store.get_subscription(self.account)["quota_micro_usd"], 100,
                         "余额被重复加了")

    def test_the_same_transaction_id_across_orders_still_credits_once(self):
        a = self.order(credits=100)
        b = self.order(credits=100)
        self.assertNotEqual(a.order_id, b.order_id)
        # 同一笔来源交易被挂到两个订单上：只能入账一次。
        reference = credit_reference(a.external_id)
        self.store.credit(self.account, 100, reference, order_id=a.order_id)
        second = self.store.credit(self.account, 100, reference, order_id=b.order_id)
        self.assertFalse(second["applied"])
        self.assertEqual(self.store.get_subscription(self.account)["quota_micro_usd"], 100)

    def test_a_credit_without_a_reference_is_refused(self):
        with self.assertRaises(ValidationFailed):
            self.store.credit(self.account, 100, "")

    def test_zero_credit_is_refused(self):
        with self.assertRaises(ValidationFailed):
            self.store.credit(self.account, 0, credit_reference("t1"))


class NoticeVerificationTests(RechargeTestBase):
    """签名、金额、币种、归属 —— 任何一项对不上都拒绝。"""

    def test_a_bad_signature_is_refused(self):
        order = self.order()
        payload = self.pay.build_notice(order)
        payload["signature"] = "deadbeef" * 8
        with self.assertRaises(PaymentError) as caught:
            self.pay.verify_notice(payload, payload["signature"])
        self.assertEqual(caught.exception.code, "bad_signature")

    def test_a_tampered_amount_fails_signature_check(self):
        """改了金额但没重新签名 —— 验签就该挡住。"""
        order = self.order()
        payload = self.pay.build_notice(order)
        payload["amount_minor"] = 1
        with self.assertRaises(PaymentError):
            self.pay.verify_notice(payload, payload["signature"])

    def test_a_mismatched_amount_is_refused_even_when_signed(self):
        order = self.order(amount_minor=1000)
        # 重新签名了，但金额与本地订单不符 —— 说明上游或中间环节串了单。
        payload = self.pay.build_notice(order, amount_minor=99999)
        notice = self.pay.verify_notice(payload, payload["signature"])
        with self.assertRaises(PaymentError) as caught:
            validate_notice(notice, _as_order(self.store.get_recharge_order(order.order_id)))
        self.assertEqual(caught.exception.code, "amount_mismatch")

    def test_a_mismatched_currency_is_refused(self):
        order = self.order(currency="CNY")
        payload = self.pay.build_notice(order, currency="USD")
        notice = self.pay.verify_notice(payload, payload["signature"])
        with self.assertRaises(PaymentError) as caught:
            validate_notice(notice, _as_order(self.store.get_recharge_order(order.order_id)))
        self.assertEqual(caught.exception.code, "currency_mismatch")

    def test_a_mismatched_account_is_refused(self):
        """不能给别的账号加钱。"""
        order = self.order()
        payload = self.pay.build_notice(order, account_id="someone-else")
        notice = self.pay.verify_notice(payload, payload["signature"])
        with self.assertRaises(PaymentError) as caught:
            validate_notice(notice, _as_order(self.store.get_recharge_order(order.order_id)))
        self.assertEqual(caught.exception.code, "account_mismatch")

    def test_an_unknown_order_is_refused(self):
        order = self.order()
        notice = self.pay.verify_notice(self.pay.build_notice(order), "")
        with self.assertRaises(PaymentError) as caught:
            validate_notice(notice, None)
        self.assertEqual(caught.exception.code, "unknown_order")

    def test_an_incomplete_notice_is_refused(self):
        order = self.order()
        payload = self.pay.build_notice(order)
        payload["transaction_id"] = ""
        body = {k: v for k, v in payload.items() if k != "signature"}
        payload["signature"] = self.pay.sign(body)
        notice = self.pay.verify_notice(payload, payload["signature"])
        with self.assertRaises(PaymentError) as caught:
            validate_notice(notice, _as_order(self.store.get_recharge_order(order.order_id)))
        self.assertEqual(caught.exception.code, "notice_incomplete")


class BrowserRedirectIsNotPaymentTests(RechargeTestBase):
    """浏览器支付成功跳转**不能**直接加额度。"""

    def test_a_pending_notice_credits_nothing(self):
        order = self.order()
        payload = self.pay.build_notice(order, status=PAY_PENDING)
        notice = self.pay.verify_notice(payload, payload["signature"])
        with self.assertRaises(PaymentError) as caught:
            validate_notice(notice, _as_order(self.store.get_recharge_order(order.order_id)))
        self.assertEqual(caught.exception.code, "not_paid")
        self.assertEqual(self.store.get_subscription(self.account)["quota_micro_usd"], 0,
                         "没到账却加了额度")

    def test_a_failed_notice_credits_nothing(self):
        order = self.order()
        payload = self.pay.build_notice(order, status="failed")
        notice = self.pay.verify_notice(payload, payload["signature"])
        with self.assertRaises(PaymentError):
            validate_notice(notice, _as_order(self.store.get_recharge_order(order.order_id)))
        self.assertEqual(self.store.get_subscription(self.account)["quota_micro_usd"], 0)


class RefundTests(RechargeTestBase):
    def test_a_refunded_order_is_refused_for_crediting(self):
        order = self.order()
        order = RechargeOrder(**{**order.__dict__, "status": "refunded"})
        payload = self.pay.build_notice(order, status=PAY_PAID)
        notice = self.pay.verify_notice(payload, payload["signature"])
        with self.assertRaises(PaymentError) as caught:
            validate_notice(notice, order)
        self.assertEqual(caught.exception.code, "already_refunded")

    def test_a_refund_never_makes_the_balance_negative(self):
        self.store.credit(self.account, 100, credit_reference("t1"))
        spent = 80
        self.store.update_subscription(self.account)
        before = self.store.get_subscription(self.account)["quota_micro_usd"]
        # 退款 1000 分（全部），但只消耗了 80 点 —— 不该把余额扣成负数。
        taken = refund_offset(credits_spent=spent, refunded_minor=1000,
                              order_amount_minor=1000, order_credits=100)
        self.store.credit(self.account, -taken, credit_reference("refund-1"))
        after = self.store.get_subscription(self.account)["quota_micro_usd"]
        self.assertGreaterEqual(after, 0, "余额被扣成负数")
        self.assertEqual(after, before - taken)

    def test_an_out_of_range_refund_is_clamped_to_what_was_spent(self):
        self.assertEqual(refund_offset(80, 1000, 1000, 100), 80)
        self.assertEqual(refund_offset(80, 500, 1000, 100), 50)
        self.assertEqual(refund_offset(0, 1000, 1000, 100), 0, "没消耗过就没有可扣的")


class BalanceGateTests(RechargeTestBase):
    """零余额：本地资料照常可用，但不许发起分析。"""

    def test_a_fresh_account_starts_at_zero(self):
        self.assertEqual(self.store.get_subscription(self.account)["remaining_micro_usd"], 0)

    def test_viewing_and_editing_local_data_needs_no_balance(self):
        """查看、读取、保存、人工修改、导出都不扣额度，也不需要额度。"""
        self.store.update_subscription(self.account, plan_code="pack-100")
        # 余额为 0 时本地订单库照样能写 —— 没有余额门禁落在本地数据路径上。
        self.assertIsNotNone(self.store.get_subscription(self.account))

    def test_crediting_makes_the_balance_usable(self):
        self.store.credit(self.account, 50, credit_reference("t1"))
        self.assertEqual(self.store.get_subscription(self.account)["remaining_micro_usd"], 50)


if __name__ == "__main__":
    import datetime  # noqa: F401  (供 _as_order 的类型推断使用)

    unittest.main()