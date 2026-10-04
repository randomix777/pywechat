"""The merchant's local parse loop.

This is the seam between the two halves of the product:

    local collector ──► local OrderStore (messages, customers, products, memory)
                                    │
                        LocalParseService
                          │           │
              build request │           │ apply suggestion
                          ▼           ▼
                   cloud gateway ──► local OrderStore (draft / review)

The cloud never writes to the merchant's books. It returns a *suggestion*; this
module feeds that suggestion back through the same business rules the local
model path uses, so prices, evidence checks and human confirmation stay local.

Offline behaviour is deliberate: if the gateway is unreachable, or the plan has
expired, the message stays in the local pending queue and the shop keeps working.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .client_login import (
    HELD_ERRORS,
    PRICING_CONFIRM_ERRORS,
    STATUS_NETWORK_DOWN,
    STATUS_RETRY,
    STATUS_SUBSCRIPTION_EXPIRED,
    GatewayError,
    MerchantClient,
    PendingQueue,
    should_retry,
    status_label,
)
from .orders import OrderParseResultInvalid, OrderStore, validate_order_parse_result

#: Local states for one message, as the shop owner sees them.
LOCAL_PENDING = "pending"
LOCAL_QUEUED = "queued"
LOCAL_RUNNING = "running"
LOCAL_DONE = "done"
LOCAL_NEEDS_RETRY = "needs_retry"
LOCAL_BLOCKED = "blocked"

_TERMINAL_REMOTE = {"succeeded", "failed", "cancelled"}


@dataclass
class ParseProgress:
    """What the shop-owner interface renders. Plain Chinese only."""

    state: str
    message: str
    message_id: str = ""
    job_id: str = ""
    order_id: str = ""
    #: 独立澄清任务。**不是订单**，界面要分开显示。
    clarification_id: str = ""
    needs_review: bool = False


@dataclass
class LocalParseService:
    """Composes the local order store with the cloud gateway client."""

    orders: OrderStore
    client: MerchantClient
    queue: PendingQueue
    tenant: str
    group: str = "default"
    recent_message_limit: int = 20
    history_fact_limit: int = 20
    order_limit: int = 10
    #: 客户选定的**产品模型别名**。空 = 由网关用默认免费模型。
    #: 这里只放别名；provider/model id 由服务端解析。
    model_alias: str = ""
    #: 客户端最近一次读到的计费策略版本。提交时带上，网关发现它变了会要求
    #: 重新确认，而不是按新价悄悄扣。
    pricing_version: str = ""
    clock: Callable[[], float] = time.time
    _lock: threading.RLock = field(default_factory=threading.RLock)
    #: job_id -> 网关下发的计费事实。**只读不写**，本模块不推算扣费。
    _billing: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    #: 等待商户确认的新价格。**在商户点确认之前不会写回 pricing_version**，
    #: 更不会自动重发 —— 那是替商户同意涨价。
    _pending_pricing: dict[str, Any] = field(default_factory=dict, repr=False)

    # --------------------------------------------------------- 定价确认

    def pending_pricing(self) -> dict[str, Any]:
        """需要商户确认的价格事实（空 = 没有待确认项）。"""
        with self._lock:
            return dict(self._pending_pricing)

    def confirm_pricing(self) -> dict[str, Any]:
        """商户**显式**确认后，才把新版本写进去。

        这一步必须由人触发。自动调用它等于系统替商户同意了涨价。
        """
        with self._lock:
            facts = dict(self._pending_pricing)
            if not facts:
                return {}
            version = str(facts.get("pricing_version") or "")
            if version:
                self.pricing_version = version
            self._pending_pricing = {}
        return facts

    def pricing_confirmation_text(self) -> str:
        """给商户看的一句话。**不用**这里的话算钱，只用它说明要花多少。"""
        facts = self.pending_pricing()
        if not facts:
            return ""
        line = str(facts.get("charge_line") or "")
        if not line:
            units = max(1, int(facts.get("cost_units_per_job") or 0))
            # 不再有「本次不消耗」这个分支：免费档位已被焊死，
            # 每一次分析都扣。写「不消耗」会让商户预期错误。
            line = f"每分析一条消息消耗 {units} 次分析额度"
        return line

    # ------------------------------------------------------------- context

    def build_request(self, message_id: str) -> dict[str, Any]:
        """Assemble the minimal context for one parse from local state only."""
        message = self._message(message_id)
        if message["kind"] not in ("text", "text_message"):
            raise ValueError("only text messages can be parsed")
        if not message["customer_id"]:
            raise ValueError("message has no bound customer; bind it before parsing")
        customer = self.orders.db.execute("SELECT id,name,address FROM customers WHERE merchant_id=? AND id=?", (self.tenant, message["customer_id"])).fetchone()
        if customer is None:
            raise ValueError("customer is not in this merchant's store")
        products = [
            {"sku": row["sku"], "name": row["name"], "unit": row["unit"]}
            for row in self.orders.db.execute("SELECT sku,name,unit FROM products WHERE merchant_id=? ORDER BY name", (self.tenant,))
        ]
        recent = self._recent_messages(message)
        facts = self._customer_facts(message)
        recent_orders = self._recent_orders(message)
        order_state = self._order_state(message)
        return {
            # 客户选定的**产品模型别名**。网关据此解析出真正的 provider/model。
            # 不传就由网关用它的默认免费模型 —— 但那样界面上的选择就是摆设。
            "model": self.model_alias,
            # 商户确认时看到的计费策略版本。网关发现它变了会退回 pricing_changed
            # 要求重新确认，而不是按新价扣。
            "pricing_version": self.pricing_version,
            "message": {
                "id": message["id"],
                "sender": message["sender"] or "",
                "content": message["content"],
                "kind": message["kind"],
                "sent_at": message["sent_at"] or message["observed_at"],
            },
            "customer": {
                "id": customer["id"],
                "name": customer["name"],
                "alias": message["sender"] or "",
                "address": customer["address"] or "",
            },
            "products": products,
            "history": {
                "recent_messages": recent,
                "customer_facts": facts,
                "recent_orders": recent_orders,
            },
            "order_state": order_state,
        }

    def _message(self, message_id: str):
        row = self.orders.db.execute("SELECT * FROM messages WHERE merchant_id=? AND id=?", (self.tenant, message_id)).fetchone()
        if row is None:
            raise ValueError("message is not in this merchant's store")
        return row

    def _recent_messages(self, message) -> list[dict[str, Any]]:
        if message["sent_at_epoch"] is None:
            return []
        rows = self.orders.db.execute(
            "SELECT sender,content,sent_at FROM messages WHERE merchant_id=? AND group_id=? AND customer_id=?"
            " AND sent_at_epoch<=? AND sent_at_epoch>? ORDER BY sent_at_epoch DESC LIMIT ?",
            (self.tenant, self.group, message["customer_id"], message["sent_at_epoch"], message["sent_at_epoch"] - 90, self.recent_message_limit),
        ).fetchall()
        return [{"sender": row["sender"] or "", "content": row["content"], "sent_at": row["sent_at"]} for row in rows]

    def _customer_facts(self, message) -> list[dict[str, Any]]:
        # Only facts that were already human-confirmed, and only those effective
        # at the message time: the model must not see the future.
        rows = self.orders.db.execute(
            "SELECT kind,value FROM memory_facts WHERE merchant_id=? AND customer_id=? AND confirmed=1"
            " AND effective_from<=? AND (effective_to IS NULL OR effective_to>?) ORDER BY effective_from DESC LIMIT ?",
            (self.tenant, message["customer_id"], message["sent_at"] or message["observed_at"], message["sent_at"] or message["observed_at"], self.history_fact_limit),
        ).fetchall()
        return [{"kind": row["kind"], "value": row["value"]} for row in rows]

    def _recent_orders(self, message) -> list[dict[str, Any]]:
        if message["sent_at_epoch"] is None:
            return []
        rows = self.orders.db.execute(
            "SELECT id,status,delivery_date FROM orders WHERE merchant_id=? AND customer_id=? AND status='confirmed'"
            " AND business_at_epoch<=? ORDER BY business_at_epoch DESC LIMIT ?",
            (self.tenant, message["customer_id"], message["sent_at_epoch"], self.order_limit),
        ).fetchall()
        orders = []
        for row in rows:
            detail = self.orders.order_detail(self.tenant, row["id"])
            orders.append(
                {
                    "order_id": row["id"],
                    "status": row["status"],
                    "delivery_date": row["delivery_date"] or "",
                    "items": [
                        {"sku": item["sku"], "quantity": item["quantity"], "unit": item["unit"]}
                        for item in detail["items"]
                    ],
                }
            )
        return orders

    def _order_state(self, message) -> dict[str, Any]:
        open_rows = self.orders.db.execute(
            "SELECT id,status FROM orders WHERE merchant_id=? AND customer_id=? AND status IN ('draft','review') ORDER BY created_at DESC LIMIT ?",
            (self.tenant, message["customer_id"], self.order_limit),
        ).fetchall()
        return {"status": message["group_id"], "open_orders": [{"order_id": row["id"], "status": row["status"]} for row in open_rows]}

    # ------------------------------------------------------------ submission

    def submit(self, message_id: str, on_progress: Callable[[ParseProgress], None] | None = None) -> ParseProgress:
        """Queue one message for cloud parsing, or report why it could not be sent."""
        emit = on_progress or (lambda _progress: None)
        try:
            request = self.build_request(message_id)
        except ValueError as exc:
            emit(ParseProgress(LOCAL_BLOCKED, str(exc), message_id=message_id))
            return ParseProgress(LOCAL_BLOCKED, str(exc), message_id=message_id)
        with self._lock:
            key = self.queue.enqueue(request, idempotency_key=f"msg-{message_id}")
        progress = ParseProgress(LOCAL_PENDING, "已加入待解析队列", message_id=message_id)
        emit(progress)
        return self.pump(on_progress=emit, single_key=key)

    def pump(self, on_progress: Callable[[ParseProgress], None] | None = None, single_key: str = "", attempts: int = 0) -> ParseProgress:
        """Send whatever is queued, then apply any finished results.

        Safe to call repeatedly; this is what the UI timer and the background
        thread both drive.
        """
        emit = on_progress or (lambda _progress: None)
        entries = self.queue.ready(limit=5)
        if single_key:
            entries = [entry for entry in entries if entry["idempotency_key"] == single_key] or entries[:1]
        for entry in entries:
            progress = self._send(entry, emit)
            if progress is not None and single_key and entry["idempotency_key"] == single_key:
                return progress
        return self.settle(emit)

    def _send(self, entry: Mapping[str, Any], emit) -> ParseProgress | None:
        emit(ParseProgress(LOCAL_QUEUED, "解析排队中", message_id=self._message_id(entry)))
        # 重试前刷新 ``pricing_version``。
        #
        # 队列里存的是**入队那一刻**的请求快照。商户点完「确认这次费用」之后，
        # ``self.pricing_version`` 才拿到新版本号 —— 但那份快照里还是空的，
        # 于是重试**再��次**被网关以 ``pricing_version_required`` 拒掉。
        #
        # 后果是商户确认了价格却永远走不出去：确认 → 重试 → 又要确认，
        # 循环往复，而界面上只会显示「提交前需要先确认这次分析的费用」。
        #
        # 别的字段（消息内容、商品、历史）保持快照原样 —— 那些在排队期间
        # 不该变，尤其是消息原文不能被悄悄改掉。
        request = dict(entry["request"])
        if self.pricing_version:
            request["pricing_version"] = self.pricing_version
        try:
            body = self.client.submit_parse(request, entry["idempotency_key"])
        except GatewayError as exc:
            if exc.code in PRICING_CONFIRM_ERRORS:
                # 缺版本 / 版本过期：把新事实记下来交界面展示，**不自动重发**。
                # 自动重发等于替商户同意了新价格。
                self._pending_pricing = dict(exc.billing or {})
                self.queue.mark_retry(entry["id"], exc.code, delay=5.0)
                label = exc.merchant_message or "分析价格需要您重新确认"
                emit(ParseProgress(LOCAL_BLOCKED, label, message_id=self._message_id(entry)))
                return ParseProgress(LOCAL_BLOCKED, label, message_id=self._message_id(entry))
            if exc.code in {"network_unavailable", "backend_unavailable"}:
                self.queue.mark_retry(entry["id"], "network_unavailable", delay=30.0)
                emit(ParseProgress(LOCAL_NEEDS_RETRY, STATUS_NETWORK_DOWN, message_id=self._message_id(entry)))
                return ParseProgress(LOCAL_NEEDS_RETRY, STATUS_NETWORK_DOWN, message_id=self._message_id(entry))
            if exc.code in HELD_ERRORS:
                # 额度用完 / 模型暂不可用 / 平台暂停接收：都**不是**这条消息的
                # 错。留在本地队列里等，商户本地数据一个字节都不动，也绝不
                # 偷偷换成收费模型重发。
                delay = 600.0 if exc.code in {
                    "quota_exhausted", "free_quota_exhausted", "subscription_inactive",
                } else 120.0
                self.queue.mark_retry(entry["id"], exc.code, delay=delay)
                label = status_label("held", exc.code)
                label = label if label != STATUS_RETRY else (exc.merchant_message or label)
                emit(ParseProgress(LOCAL_BLOCKED, label, message_id=self._message_id(entry)))
                return ParseProgress(LOCAL_BLOCKED, label, message_id=self._message_id(entry))
            if exc.code == "unauthenticated":
                self.queue.mark_retry(entry["id"], "unauthenticated", delay=60.0)
                emit(ParseProgress(LOCAL_NEEDS_RETRY, "登录已过期，请重新登录软件", message_id=self._message_id(entry)))
                return ParseProgress(LOCAL_NEEDS_RETRY, "登录已过期，请重新登录软件", message_id=self._message_id(entry))
            if should_retry(exc.code, entry["attempts"]):
                self.queue.mark_retry(entry["id"], exc.code, delay=60.0)
                emit(ParseProgress(LOCAL_NEEDS_RETRY, status_label("queued", exc.code), message_id=self._message_id(entry)))
                return ParseProgress(LOCAL_NEEDS_RETRY, status_label("queued", exc.code), message_id=self._message_id(entry))
            self.queue.mark_done(entry["id"], "rejected")
            emit(ParseProgress(LOCAL_BLOCKED, exc.merchant_message, message_id=self._message_id(entry)))
            return ParseProgress(LOCAL_BLOCKED, exc.merchant_message, message_id=self._message_id(entry))
        job_id = body["job"]["job_id"]
        self._record_billing(job_id, body.get("billing"))
        self.queue.mark_submitted(entry["id"], job_id)
        emit(ParseProgress(LOCAL_RUNNING, "解析处理中", message_id=self._message_id(entry), job_id=job_id))
        return ParseProgress(LOCAL_RUNNING, "解析处理中", message_id=self._message_id(entry), job_id=job_id)

    # ------------------------------------------------------- billing (服务端)

    def _record_billing(self, job_id: str, facts: Any) -> None:
        """记下**服务端**给的计费事实。

        这里一个字都不算。客户端过去按"每个任务扣 1 次"自己加总，既把免费
        任务算成收费，也和运营配的每任务扣费数对不上。
        """
        if not job_id or not isinstance(facts, Mapping):
            return
        with self._lock:
            self._billing[job_id] = dict(facts)
        self._remember_policy_version(facts)

    def _remember_policy_version(self, facts: Mapping[str, Any]) -> None:
        version = str(facts.get("pricing_version") or "")
        if version:
            self.pricing_version = version

    def billing_fact(self, job_id: str) -> dict[str, Any]:
        """某个任务的服务端计费事实。取不到就是空，**不回退到本地推算**。"""
        with self._lock:
            return dict(self._billing.get(job_id) or {})

    @staticmethod
    def _message_id(entry: Mapping[str, Any]) -> str:
        request = entry.get("request")
        if isinstance(request, Mapping):
            message = request.get("message")
            if isinstance(message, Mapping):
                return str(message.get("id", ""))
        return ""

    # -------------------------------------------------------------- results

    def settle(self, on_progress: Callable[[ParseProgress], None] | None = None) -> ParseProgress:
        """Poll outstanding jobs and apply any finished suggestion locally."""
        emit = on_progress or (lambda _progress: None)
        last = ParseProgress(LOCAL_PENDING, "没有待处理的解析")
        for entry in self.queue.outstanding_and_submitted():
            job_id = entry["job_id"]
            try:
                fetched = self.client.job(job_id)
                job = fetched["job"]
                self._record_billing(job_id, fetched.get("billing"))
            except GatewayError as exc:
                emit(ParseProgress(LOCAL_NEEDS_RETRY, STATUS_NETWORK_DOWN, message_id=entry["message_id"], job_id=job_id))
                return ParseProgress(LOCAL_NEEDS_RETRY, status_label("queued", exc.code), message_id=entry["message_id"], job_id=job_id)
            state = job["state"]
            if state not in _TERMINAL_REMOTE:
                progress = ParseProgress(LOCAL_RUNNING, status_label(state), message_id=entry["message_id"], job_id=job_id)
                emit(progress)
                last = progress
                continue
            last = self._apply(job, entry, emit)
        return last

    def _apply(self, job: Mapping[str, Any], entry: Mapping[str, Any], emit) -> ParseProgress:
        message_id = entry["message_id"]
        if job["state"] != "succeeded":
            self.queue.mark_done(entry["id"], "done")
            reason = (job.get("error") or {}).get("code", "")
            label = status_label(job["state"], reason)
            needs_retry = job["state"] == "outcome_unknown"
            progress = ParseProgress(LOCAL_NEEDS_RETRY if needs_retry else LOCAL_DONE, label, message_id=message_id, job_id=job["job_id"], needs_review=needs_retry)
            emit(progress)
            return progress
        try:
            validate_order_parse_result(job["result"])
        except OrderParseResultInvalid:
            # The cloud should never return this; treat it as a system problem
            # and route it to a human rather than writing anything.
            self.queue.mark_done(entry["id"], "done")
            progress = ParseProgress(
                LOCAL_NEEDS_RETRY, "解析结果异常，已转人工核对", message_id=message_id, job_id=job["job_id"], needs_review=True
            )
            emit(progress)
            return progress
        applied = self.orders.apply_cloud_suggestion(self.tenant, self.group, message_id, job["result"], job_id=job["job_id"])
        self.queue.mark_done(entry["id"], "done")
        # 独立澄清任务算"需要商户看一眼"，但它**不是**订单：order_id 为空。
        needs_review = applied["outcome"] in {"review", "rejected", "clarify", "payment_claim"}
        labels = {
            "ignored": "无需处理",
            "duplicate": "已处理过，未重复建单",
            "clarify": "需要向客户问一句，已记录",
            "payment_claim": "已记录客户声称付款，请核对收款",
        }
        if applied["outcome"] in labels:
            label = labels[applied["outcome"]]
        elif needs_review:
            label = "需要您核对"
        else:
            label = "已生成待确认订单"
        progress = ParseProgress(
            LOCAL_DONE,
            label,
            message_id=message_id,
            job_id=job["job_id"],
            order_id=applied.get("order_id") or "",
            clarification_id=applied.get("clarification_id") or "",
            needs_review=needs_review,
        )
        emit(progress)
        return progress

    # ---------------------------------------------------------------- extras

    def cancel(self, job_id: str) -> bool:
        try:
            self.client.cancel(job_id)
            return True
        except GatewayError:
            return False

    def dashboard(self) -> dict[str, Any]:
        """Everything the shop-owner interface needs in one call."""
        outstanding = self.queue.outstanding()
        return {
            "pending_count": len(outstanding),
            "pending": outstanding,
            "local_orders": self.orders.dashboard(self.tenant),
        }


def describe_subscription(subscription: Mapping[str, Any]) -> dict[str, str]:
    """Plain-Chinese subscription line for the interface."""
    if not subscription:
        return {"headline": "未登录", "detail": ""}
    if not subscription.get("active"):
        return {"headline": STATUS_SUBSCRIPTION_EXPIRED, "detail": "请联系商家续期。本地订单查看和导出仍可正常使用。"}
    return {
        "headline": f"剩余额度：{int(subscription.get('remaining_units', 0))}",
        "detail": f"套餐：{subscription.get('plan_code', '')}　有效期至：{str(subscription.get('period_end', ''))[:10]}",
    }


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
