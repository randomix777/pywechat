"""充值门禁：余额为 0 时不许发起分析。

产品规则是「先充值后使用」。这组测试盯的是 `ProductStore.create_job` ——
**真正做预留的那一处**，因为它发生在任何模型调用之前。

## 为什么不在网关再加一道

早先在 ``_resolve_model`` 里加过一道"余额为 0 就拒绝"的门禁，结果它把
`create_job` 里两个**更准确**的错误遮住了：

* ``subscription_inactive`` 套餐到期 → 要**续费**
* ``quota_exhausted``        点数用完 → 要**充值**

混成一句"请充值"是误导：套餐到期时充值并不能让套餐重新生效。所以这道门禁被
撤掉了，这里反过来把"顺序与可区分性"钉死。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyweixin.payment import TestPaymentAdapter, credit_reference  # noqa: E402
from pyweixin.product import ProductStore, QuotaExhausted, SubscriptionInactive  # noqa: E402


def expire(store, account_id) -> None:
    store._connection.execute(
        "UPDATE subscriptions SET period_end=? WHERE account_id=?", (1.0, account_id)
    )
    store._connection.commit()


def job_count(store, account_id) -> int:
    return int(store._connection.execute(
        "SELECT COUNT(*) c FROM jobs WHERE account_id=?", (account_id,)
    ).fetchone()["c"])


def s_get(store, account_id):
    row = store.get_account(account_id)
    assert row is not None
    return row


class GateTestBase(unittest.TestCase):
    def setUp(self):
        self.store = ProductStore(":memory:")
        self.account = self.store.create_account("shop-gate", "password-1234")["account_id"]
        self.merchant_id = str(s_get(self.store, self.account).get("merchant_id") or "m")
        self.device = self.store.start_device_authorization("pc-1", "windows")
        self.store.approve_device_authorization(self.device["user_code"], self.account)
        self.poll = self.store.poll_device_authorization(self.device["device_code"])

    def tearDown(self):
        self.store.close()

    def device_id(self) -> str:
        device = self.poll.get("device") or {}
        return str(device.get("id") or self.poll.get("device_id") or "")

    def top_up(self, micro_usd=50, reference="txn-1") -> dict:
        return self.store.credit(self.account, micro_usd, credit_reference(reference))


class InitialBalanceTests(GateTestBase):
    def test_a_new_account_starts_at_zero(self):
        """产品规则：初始分析余额为 0，必须先购买额度。"""
        self.assertEqual(self.store.get_subscription(self.account)["remaining_micro_usd"], 0)

    def test_the_default_quota_is_zero_not_a_free_allowance(self):
        created = self.store.create_account("shop-two", "password-1234")
        self.assertEqual(
            self.store.get_subscription(created["account_id"])["quota_micro_usd"], 0,
            "新账号带了免费额度",
        )


class ZeroBalanceRefusalTests(GateTestBase):
    def test_a_job_is_refused_when_the_balance_is_zero(self):
        with self.assertRaises(QuotaExhausted) as caught:
            self.store.create_job(
                self.account, self.device_id(), self.merchant_id, "k1", "h1", 1, 60.0,
                request_payload={"message": {"content": "两箱苹果"}},
            )
        self.assertEqual(caught.exception.code, "quota_exhausted")

    def test_no_job_row_is_created_when_refused(self):
        """被拒之后不能留下一个"看起来在跑"的任务。"""
        with self.assertRaises(QuotaExhausted):
            self.store.create_job(
                self.account, self.device_id(), self.merchant_id, "k1", "h1", 1, 60.0,
                request_payload={"message": {"content": "两箱苹果"}},
            )
        self.assertEqual(job_count(self.store, self.account), 0, "被拒却留下了任务行")

    def test_an_expired_plan_says_expire_not_recharge(self):
        """到期要说续费。"请充值"是错的 —— 充值不能让套餐重新生效。"""
        self.top_up(50)
        expire(self.store, self.account)
        with self.assertRaises(SubscriptionInactive) as caught:
            self.store.create_job(
                self.account, self.device_id(), self.merchant_id, "k1", "h1", 1, 60.0,
                request_payload={"message": {"content": "两箱苹果"}},
            )
        self.assertEqual(caught.exception.code, "subscription_inactive")
        self.assertNotIn("充值", str(caught.exception))

    def test_the_two_reasons_stay_distinguishable(self):
        with self.assertRaises(QuotaExhausted) as balance:
            self.store.create_job(self.account, self.device_id(), self.merchant_id,
                                  "k1", "h1", 1, 60.0, request_payload={})
        self.top_up(50)
        expire(self.store, self.account)
        with self.assertRaises(SubscriptionInactive) as expired:
            self.store.create_job(self.account, self.device_id(), self.merchant_id,
                                  "k2", "h2", 1, 60.0, request_payload={})
        self.assertNotEqual(balance.exception.code, expired.exception.code)


class AfterTopUpTests(GateTestBase):
    def test_the_same_message_can_be_submitted_after_top_up(self):
        with self.assertRaises(QuotaExhausted):
            self.store.create_job(self.account, self.device_id(), self.merchant_id,
                                  "k1", "h1", 1, 60.0, request_payload={})
        self.top_up(50)
        job = self.store.create_job(
            self.account, self.device_id(), self.merchant_id, "k2", "h2", 1, 60.0,
            request_payload={"message": {"content": "两箱苹果"}},
        )
        self.assertTrue(job.get("job_id"))
        self.assertEqual(self.store.get_subscription(self.account)["reserved_micro_usd"], 1)

    def test_a_refused_submission_charged_nothing(self):
        with self.assertRaises(QuotaExhausted):
            self.store.create_job(self.account, self.device_id(), self.merchant_id,
                                  "k1", "h1", 1, 60.0, request_payload={})
        self.assertEqual(self.store.get_subscription(self.account)["used_micro_usd"], 0)
        self.assertEqual(self.store.get_subscription(self.account)["reserved_micro_usd"], 0,
                         "被拒却占了预留")

    def test_topping_up_twice_with_one_transaction_credits_once(self):
        self.assertTrue(self.top_up(50, "txn-dup")["applied"])
        self.assertFalse(self.top_up(50, "txn-dup")["applied"])
        self.assertEqual(self.store.get_subscription(self.account)["quota_micro_usd"], 50)

    def test_the_test_payment_adapter_identifies_itself(self):
        """没有真实支付配置时用的通道必须自报家门，不能冒充真钱。"""
        self.assertEqual(TestPaymentAdapter.TEST_MARK, "test-payment-adapter")


class NoParallelWalletTests(GateTestBase):
    def test_crediting_goes_through_the_subscription_ledger(self):
        """充值记在**现有**额度账本里，没有第二个钱包可以绕过门禁。"""
        self.top_up(30)
        self.assertEqual(self.store.get_subscription(self.account)["quota_micro_usd"], 30)
        self.assertEqual(len(self.store.credit_history(self.account)), 1)

    def test_the_credit_ledger_records_the_source_transaction(self):
        """每笔到账都要能追回是哪笔支付交易。"""
        self.top_up(30, "txn-trace")
        row = self.store.credit_history(self.account)[0]
        self.assertTrue(row["reference"].startswith("jyiref:"))
        self.assertEqual(row["micro_usd"], 30)


if __name__ == "__main__":
    unittest.main()