"""回执：生成、幂等入队、发送前核对、崩溃恢复。

## 不新建第二套发送系统

队列直接复用 :class:`wechat_ai_assistant.agent.AgentStore` 已有的 ``outbox`` 表：
租约、重试、崩溃恢复、``recover_outbox()`` 全部是现成的。回执的命令 id 就是
**业务幂等键**（订单 + 版本 + 会话 + 回执类型），而 ``outbox.command_id`` 是主键，
所以 ``INSERT OR IGNORE`` 天然做到"同一条回执只入队一次"。

## 两条硬规则

**1. 不能"订单没保存却发出已记单"。** 回执只在订单已经写进本地库之后才入队。
顺序永远是：落库 → 生成回执 → 入队 → 发送。

**2. 不能因崩溃反复发同一条回执。** 入队用业务幂等键，发送成功后标记完成。
崩溃发生在"已发送未标记"之间时，状态是 ``sent_unknown``，此时**不重发**，
先核对。这是本地幂等，不能宣称是微信端的严格 exactly-once。

## 自身回执不再触发下单

发出去的内容带 ``outbound`` 标记并写入回执账本，解析入口遇到本程序自己发的
消息直接跳过。否则每发一条回执就会再生成一条新订单。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from ._packages import ensure_packages_on_path

ensure_packages_on_path()

from order_agent.receipt import Receipt, ReceiptItem, format_money, render_receipt  # noqa: E402

from .wechat_send import SendHeld  # noqa: E402

#: 回执类型。参与幂等键计算，所以新增类型不会与旧回执冲突。
RECEIPT_CONFIRMATION = "confirmation"
RECEIPT_CHANGE = "change"
RECEIPT_CANCEL = "cancel"
RECEIPT_TYPES = (RECEIPT_CONFIRMATION, RECEIPT_CHANGE, RECEIPT_CANCEL)

#: 历史导入 / 重复回放 / 停机补采默认不产生回执。
NO_RECEIPT_SOURCES = frozenset({"history", "replay", "backfill", "import"})


class ReceiptStatus:
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    #: 已进入发送但没有确认结果。不重发，先核对。
    SENT_UNKNOWN = "sent_unknown"
    FAILED = "failed"
    #: 商户暂停或模式不允许，留在队列里等条件满足。
    HELD = "held"
    SUPPRESSED = "suppressed"

    ALL = (QUEUED, SENDING, SENT, SENT_UNKNOWN, FAILED, HELD, SUPPRESSED)

    TERMINAL = (SENT, SUPPRESSED)


@dataclass(frozen=True)
class ReceiptRequest:
    """一条待发回执的完整描述。

    ``order_id`` + ``order_version`` + ``conversation`` + ``kind`` 共同构成
    业务幂等标识。订单改一版就换一条回执，不会把旧版本的回执发出去。
    """

    order_id: str
    order_version: int
    conversation: str
    kind: str = RECEIPT_CONFIRMATION
    text: str = ""
    #: 消息来源。历史导入类来源会被抑制。
    source: str = "live"
    price_pending: bool = False
    customer_name: str = ""

    def __post_init__(self) -> None:
        if self.kind not in RECEIPT_TYPES:
            raise ValueError(f"unknown receipt kind: {self.kind}")
        if not str(self.order_id).strip():
            raise ValueError("receipt requires an order id")
        if not str(self.conversation).strip():
            raise ValueError("receipt requires a conversation")

    @property
    def idempotency_key(self) -> str:
        raw = f"{self.order_id}|{self.order_version}|{self.conversation}|{self.kind}"
        return "rcpt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "order_version": self.order_version,
            "conversation": self.conversation,
            "kind": self.kind,
            "text": self.text,
            "source": self.source,
            "price_pending": self.price_pending,
            "customer_name": self.customer_name,
            "idempotency_key": self.idempotency_key,
        }


def build_receipt_text(
    items: Sequence[Mapping[str, Any]],
    *,
    delivery_date: str = "",
    price_pending: bool = False,
    kind: str = RECEIPT_CONFIRMATION,
) -> str:
    """按已校验的订单字段渲染回执。

    * 缺价且未标记 ``price_pending`` 时抛错 —— 绝不发「合计 0.00」。
    * 回执不含地址和电话：群里发一条带手机号的确认等于泄露客户资料。
    """
    if not price_pending:
        for entry in items:
            if "unit_price_cents" not in entry:
                raise ValueError("receipt requires a verified price, or an explicit price_pending=True")
    if kind == RECEIPT_CANCEL:
        body = "好的，这单已经取消。" if not items else "好的，已把这单取消：" + "、".join(
            f"{entry.get('name', '')} {entry.get('quantity', '')}{entry.get('unit', '')}" for entry in items
        )
        return body + "（直接回复即可改回）"
    if price_pending:
        # A whole order can have no verified price at all. Still say what was
        # ordered and that the price is unconfirmed: an empty receipt leaves the
        # customer with no idea the order was even taken.
        renderable = [{**entry, "unit_price_cents": entry.get("unit_price_cents", 0)} for entry in items]
        if not renderable:
            return "好的，已记单，价格稍后确认（直接回复即可修改）"
        return render_receipt(items=renderable, delivery_date=delivery_date, price_pending=True)
    return render_receipt(items=items, delivery_date=delivery_date, price_pending=price_pending)


class ReceiptOutbox:
    """持久化回执队列。持久化介质复用 :class:`wechat_ai_assistant.agent.AgentStore`。"""

    def __init__(self, store: Any, sender: Callable[[str, str], Any] | None = None,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.sender = sender
        self._clock = clock
        self._lock = threading.RLock()
        #: 本程序自己发出的文本指纹。解析入口据此跳过自身回执。
        self._own_fingerprints: set[str] = set()

    # ------------------------------------------------------------ enqueueing

    def enqueue(self, request: ReceiptRequest) -> bool:
        """入队。已存在同幂等键的回执时返回 False，不会产生第二条。"""
        if request.source in NO_RECEIPT_SOURCES:
            return False
        if not request.text.strip():
            return False
        return self._enqueue_with_metadata(request)

    def _enqueue_with_metadata(self, request: ReceiptRequest) -> bool:
        from .agent import AgentCommand

        command = AgentCommand(
            command_id=request.idempotency_key,
            action="send_receipt",
            target=request.conversation,
            payload=request.to_dict(),
            max_attempts=3,
            retryable=True,
        )
        with self._lock:
            return self.store.enqueue(command)

    # ------------------------------------------------------------- dispatch

    def dispatch(
        self,
        *,
        allowed_targets: Sequence[str],
        paused: bool = False,
        expected_versions: Mapping[str, int] | None = None,
        send: Callable[[str, str], Any] | None = None,
    ) -> list[dict[str, Any]]:
        """发送到期回执。

        发送前逐条核对四件事：会话在允许列表里、没暂停、订单版本还是最新的、
        回执不是被抑制的。任何一条不满足就留在队列里，不发。
        """
        sender = send or self.sender
        if sender is None:
            return []
        expected = dict(expected_versions or {})
        outcomes: list[dict[str, Any]] = []
        with self._lock:
            leases = self.store.claim_outbox_leases(limit=20, lease_seconds=30)
        for lease in leases:
            command = lease.command
            if command.action != "send_receipt":
                self.store.release_lease(command.command_id, lease.token)
                continue
            payload = dict(command.payload or {})
            version = int(payload.get("order_version", 0))
            reason = ""
            if paused:
                reason = "paused"
            elif command.target not in allowed_targets:
                reason = "conversation_not_allowed"
            elif expected and expected.get(command.target) is not None and version < expected[command.target]:
                # 订单已经更新到更高版本：旧回执会让客户看到过期内容。
                reason = "stale_order_version"
            if reason:
                self.store.mark_hold(command.command_id, reason)
                outcomes.append({"idempotency_key": command.command_id, "status": ReceiptStatus.HELD, "reason": reason})
                continue
            try:
                sender(command.target, str(payload.get("text", "")))
            except SendHeld as held:
                # Nothing was typed into WeChat. This is a hold, not an
                # unknown send: park it and let the next dispatch retry.
                self.store.mark_hold(command.command_id, held.reason)
                outcomes.append(
                    {"idempotency_key": command.command_id, "status": ReceiptStatus.HELD, "reason": held.reason}
                )
                continue
            except Exception as exc:
                self.store.mark_unknown(command.command_id, lease.token, str(exc))
                outcomes.append(
                    {"idempotency_key": command.command_id, "status": ReceiptStatus.SENT_UNKNOWN, "reason": str(exc)}
                )
                continue
            self._own_fingerprints.add(fingerprint(str(payload.get("text", ""))))
            self.store.complete(command.command_id, lease.token)
            outcomes.append({"idempotency_key": command.command_id, "status": ReceiptStatus.SENT, "reason": ""})
        return outcomes

    def reconcile_unknown(self, *, verifier: Callable[[str, str], bool] | None = None) -> list[dict[str, Any]]:
        """核对结果未知的回执。

        有 ``verifier`` 时先问它"这条是不是已经发出去了"；问不出来就保持
        ``sent_unknown``，**不重发**。盲目重发的代价是客户收到两条一样的确认。
        """
        resolved: list[dict[str, Any]] = []
        for command_id, target, text in self.store.list_unknown():
            if verifier is None:
                resolved.append({"idempotency_key": command_id, "status": ReceiptStatus.SENT_UNKNOWN, "reason": "无法确认"})
                continue
            try:
                already = bool(verifier(target, text))
            except Exception as exc:
                resolved.append({"idempotency_key": command_id, "status": ReceiptStatus.SENT_UNKNOWN, "reason": str(exc)})
                continue
            if already:
                self.store.complete_unknown(command_id)
                self._own_fingerprints.add(fingerprint(text))
                resolved.append({"idempotency_key": command_id, "status": ReceiptStatus.SENT, "reason": "已确认发出"})
            else:
                self.store.requeue_unknown(command_id)
                resolved.append({"idempotency_key": command_id, "status": ReceiptStatus.QUEUED, "reason": "确认未发出，重新入队"})
        return resolved

    # ------------------------------------------------------------- self-loop

    def is_own_receipt(self, text: str) -> bool:
        """这条消息是不是本程序自己发出去的回执。"""
        if fingerprint(text) in self._own_fingerprints:
            return True
        return bool(_RECEIPT_SHAPE.search(text or ""))

    def mark_sent(self, text: str) -> None:
        self._own_fingerprints.add(fingerprint(text))

    def forget_sent(self, text: str) -> None:
        self._own_fingerprints.discard(fingerprint(text))

    # ---------------------------------------------------------------- views

    def pending_count(self) -> int:
        return self.store.pending_count()

    def unknown_count(self) -> int:
        return self.store.unknown_count()

    def failed_count(self) -> int:
        """重试用尽、确实没发出去的条数。"""
        return self.store.failed_command_count()

    def held_reasons(self) -> dict[str, str]:
        """当前被暂缓的回执及原因，界面上要显示给商户。"""
        return dict(self.store.held_reasons())

    def close(self) -> None:
        self.store.close()


#: 识别"看起来就是本程序回执"的形状。用于冷启动后仍未发出任何回执的场景。
_RECEIPT_SHAPE = re.compile(r"^好的[，,].{0,40}(已记单|已把这单取消|已取消)")


def fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:16]


def receipt_kind_for(change_type: str) -> str:
    if change_type == "cancel":
        return RECEIPT_CANCEL
    if change_type in ("modify", "append"):
        return RECEIPT_CHANGE
    return RECEIPT_CONFIRMATION


__all__ = [
    "NO_RECEIPT_SOURCES",
    "RECEIPT_CANCEL",
    "RECEIPT_CHANGE",
    "RECEIPT_CONFIRMATION",
    "RECEIPT_TYPES",
    "ReceiptOutbox",
    "ReceiptRequest",
    "ReceiptStatus",
    "build_receipt_text",
    "fingerprint",
    "receipt_kind_for",
]
