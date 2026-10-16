"""充值与支付集成边界。

## 职责划分（不要混淆）

* **JYI 是支付的事实来源** —— 它知道钱有没有真的到账。
* **网关是分析点数的权威来源** —— 只有它能改用户的可用点数。

用户浏览器「支付成功」跳回来**不算**到账：那只是一个前端页面，任何人都能
自己打开。只有**服务端确认**（签名验证过的通知，或主动查询支付状态）才作数。

## 为什么这里没有默认实现

JYI 的商品、支付订单、收银页面和签名方式都**还没有确认过**。在不知道它
长什么样之前写死一个实现，等于把猜测当事实。所以这里只定义接口与
两条必须成立的规则，真正的适配器等资料到位再补。

没有真实支付配置时用 :class:`TestPaymentAdapter`——它**明确标记自己**
是测试通道，绝不伪造到账。

## 金额与点数的绑定

点数额度**只能**由可信服务端根据套餐推导。客户端提交的任何数字都只是
"想充多少"，最终入账的点数由服务端查表得出。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

#: 支付状态。只有 ``PAID`` 能触发加点数。
PAY_PENDING = "pending"
PAY_PAID = "paid"
PAY_FAILED = "failed"
PAY_REFUNDED = "refunded"

#: 到账幂等键的前缀。同一笔来源交易只会产生一条流水。
CREDIT_REFERENCE_PREFIX = "jyiref:"


class PaymentError(RuntimeError):
    """支付流程里的可说明失败。**不是**系统异常，不该让调用方以为要重试。"""

    def __init__(self, message: str, code: str = "payment_invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RechargeOrder:
    """一笔待付或已付的充值单。

    ``credits`` 由**服务端**按套餐推导，不是客户端说了算。
    """

    order_id: str
    account_id: str
    product_id: str
    amount_minor: int
    currency: str
    credits: int
    status: str = PAY_PENDING
    #: 外部单据标识（JYI 侧的订单号 / 交易号）。对账要用，**不能本地编**。
    external_id: str = ""
    checkout_url: str = ""
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "account_id": self.account_id,
            "product_id": self.product_id,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "credits": self.credits,
            "status": self.status,
            "external_id": self.external_id,
            "checkout_url": self.checkout_url,
        }


@dataclass(frozen=True)
class PaymentNotice:
    """来自 JYI 的支付结果通知。

    这些字段**全部**来自外部，必须逐一核对：签名、订单号、金额、币种、
    状态、以及归属的账号。任何一项对不上就拒绝加点数。
    """

    transaction_id: str
    order_id: str
    account_id: str
    amount_minor: int
    currency: str
    status: str
    signature: str = ""
    paid_at: float = 0.0
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def settled(self) -> bool:
        return str(self.status or "").lower() == PAY_PAID


class PaymentGateway(Protocol):
    """支付侧需要我们提供的三件事。

    具体实现（JYI 适配器）等接口资料确认后再补。
    """

    def create_order(
        self, account_id: str, product_id: str, idempotency_key: str = ""
    ) -> RechargeOrder:
        """按**套餐**建单。金额与点数额度由服务端查表得出。"""

    def verify_notice(self, payload: Mapping[str, Any], signature: str) -> PaymentNotice:
        """验签并解析通知。验不过必须抛 :class:`PaymentError`。"""

    def query_order(self, order_id: str) -> RechargeOrder | None:
        """主动查询。回调丢了时的恢复路径。"""


class TestPaymentAdapter:
    """**测试通道**，明确标记，绝不伪造到账。

    存在的意义是让本地与 CI 能跑通「建单 → 通知 → 到账 → 幂等」这条链，
    而不是让产品在没有真实支付时也能骗到点数。因此：

    * :meth:`settle` 必须由调用方**显式**调用，模拟"服务端确认到账"；
    * 每一笔都带 :data:`TEST_MARK`，任何下游看到它都要知道这不是真钱。
    """

    TEST_MARK = "test-payment-adapter"

    def __init__(self, secret: str = "test-secret", clock: Callable[[], float] = time.time):
        self._secret = secret
        self._clock = clock
        self._orders: dict[str, RechargeOrder] = {}

    # --------------------------------------------------------------- signing

    def sign(self, payload: Mapping[str, Any]) -> str:
        """与真实适配器同一种签名算法，便于测出"验签被绕过"的 bug。"""
        body = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hmac.new(self._secret.encode(), body.encode("utf-8"), hashlib.sha256).hexdigest()

    # -------------------------------------------------------------- gateway

    def create_order(
        self, account_id: str, product_id: str, credits: int = 100,
        amount_minor: int = 1000, currency: str = "CNY", idempotency_key: str = "",
    ) -> RechargeOrder:
        if not str(account_id).strip():
            raise PaymentError("缺少账号", "account_required")
        if int(credits) <= 0:
            raise PaymentError("点数额度必须大于零", "credits_invalid")
        order_id = "ro_test_" + hashlib.sha256(
            f"{account_id}:{product_id}:{idempotency_key}".encode()
        ).hexdigest()[:16]
        existing = self._orders.get(order_id)
        if existing is not None:
            return existing
        order = RechargeOrder(
            order_id=order_id, account_id=account_id, product_id=product_id,
            amount_minor=int(amount_minor), currency=currency, credits=int(credits),
            external_id="test_txn_" + order_id[-8:],
            checkout_url=f"https://example.invalid/checkout/{order_id}",
            created_at=self._clock(),
        )
        self._orders[order_id] = order
        return order

    def build_notice(self, order: RechargeOrder, status: str = PAY_PAID, **overrides: Any) -> dict[str, Any]:
        """构造一份通知负载（含正确签名），供测试投递。"""
        payload = {
            "transaction_id": order.external_id,
            "order_id": order.order_id,
            "account_id": order.account_id,
            "amount_minor": order.amount_minor,
            "currency": order.currency,
            "status": status,
            "paid_at": self._clock(),
        }
        payload.update(overrides)
        signed = dict(payload)
        signed["signature"] = self.sign(payload)
        return signed

    def verify_notice(self, payload: Mapping[str, Any], signature: str) -> PaymentNotice:
        given = str(payload.get("signature") or signature or "")
        body = {k: v for k, v in payload.items() if k != "signature"}
        expected = self.sign(body)
        if not hmac.compare_digest(expected, str(given)):
            raise PaymentError("签名校验失败", "bad_signature")
        return PaymentNotice(
            transaction_id=str(payload.get("transaction_id") or ""),
            order_id=str(payload.get("order_id") or ""),
            account_id=str(payload.get("account_id") or ""),
            amount_minor=int(payload.get("amount_minor") or 0),
            currency=str(payload.get("currency") or ""),
            status=str(payload.get("status") or ""),
            signature=given,
            paid_at=float(payload.get("paid_at") or 0.0),
            raw=dict(payload),
        )

    def query_order(self, order_id: str) -> RechargeOrder | None:
        return self._orders.get(str(order_id))


#: 加点数时必须逐项核对的项。任何一项对不上都拒绝。
REQUIRED_NOTICE_FIELDS = ("transaction_id", "order_id", "account_id", "amount_minor", "currency", "status")


def validate_notice(notice: PaymentNotice, order: RechargeOrder | None) -> None:
    """通知与本地订单逐项核对。

    不做这一步就会出现：伪造金额、串号给别人加钱、把失败的单当成到账。
    """
    for field_name in REQUIRED_NOTICE_FIELDS:
        if not str(getattr(notice, field_name, "") or "").strip():
            raise PaymentError(f"通知缺少 {field_name}", "notice_incomplete")
    if not notice.settled:
        # 未支付 / 失败 / 退款：都不加点数。**浏览器跳转不算到账。**
        raise PaymentError(f"支付状态为 {notice.status}，不增加点数", "not_paid")
    if order is None:
        raise PaymentError("找不到对应的充值订单", "unknown_order")
    if notice.order_id != order.order_id:
        raise PaymentError("订单号不匹配", "order_mismatch")
    if notice.account_id != order.account_id:
        raise PaymentError("账号归属不匹配", "account_mismatch")
    if int(notice.amount_minor) != int(order.amount_minor):
        raise PaymentError("金额不匹配", "amount_mismatch")
    if str(notice.currency or "").upper() != str(order.currency or "").upper():
        raise PaymentError("币种不匹配", "currency_mismatch")
    if order.status == PAY_REFUNDED:
        raise PaymentError("该订单已退款", "already_refunded")


def credit_reference(transaction_id: str) -> str:
    """到账幂等键。同一笔来源交易永远得到同一个键。"""
    digest = hashlib.sha256(str(transaction_id or "").encode()).hexdigest()[:32]
    return f"{CREDIT_REFERENCE_PREFIX}{digest}"


def refund_offset(credits_spent: int, refunded_minor: int, order_amount_minor: int, order_credits: int) -> int:
    """退款时该扣回多少点数。

    按比例算，且**不把余额扣成负数**：余额不足就返回 0，退款超出已消耗的部分
    记为超退，交由人工处理，而不是凭空造出负余额。
    """
    if order_amount_minor <= 0 or order_credits <= 0:
        return 0
    raw = int(refunded_minor) * int(order_credits) / float(order_amount_minor)
    return max(0, min(int(credits_spent), int(raw)))


__all__ = [
    "CREDIT_REFERENCE_PREFIX",
    "PAY_FAILED",
    "PAY_PAID",
    "PAY_PENDING",
    "PAY_REFUNDED",
    "REQUIRED_NOTICE_FIELDS",
    "PaymentError",
    "PaymentGateway",
    "PaymentNotice",
    "RechargeOrder",
    "TestPaymentAdapter",
    "credit_reference",
    "refund_offset",
    "validate_notice",
]