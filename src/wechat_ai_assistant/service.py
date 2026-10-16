"""商户闭环服务：一条消息从读到变成订单，再到回执。

    本地上下文  →  网关任务  →  Agent/订单插件  →  结果
        →  订单规则校验  →  本地状态更新  →  回执入队  →  发送

这个模块是**界面**与**业务**之间唯一的桥。界面不直接调插件，测试也不绕过它
——否则就会出现"测试里走新路径、正式界面还在走旧路径"的情况。

## 可追溯

每次运行都记录 ``request_id`` / ``job_id`` / ``run_id`` / ``order_id`` 的
对应关系。商户报"这一单为什么错了"时，能一路追到模型当时看到了什么。

## 假后端的结果

网关返回 ``is_model = False`` 时，本模块会把订单标记为**演示来源**并在
``order_ambiguities`` 之外单独记录。演示数据不允许在没有提示的情况下
变成正式经营订单。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from ._packages import ensure_packages_on_path

ensure_packages_on_path()

from business_agent import AgentResult, RunStatus  # noqa: E402
from business_agent.contracts.errors import AgentError  # noqa: E402

from .conversations import Conversation  # noqa: E402
from .merging import BufferedMessage, MergeKey  # noqa: E402
from .local_parse import LOCAL_BLOCKED  # noqa: E402
from .receipts import (  # noqa: E402
    NO_RECEIPT_SOURCES,
    ReceiptOutbox,
    ReceiptRequest,
    build_receipt_text,
    receipt_kind_for,
)
from .runmode import AutoOrderSettings, RunMode  # noqa: E402


class JobTrace:
    """一次解析的完整可追溯记录。"""

    def __init__(self, request_id: str, message_id: str):
        self.request_id = request_id
        self.message_id = message_id
        self.job_id = ""
        self.run_id = ""
        self.order_id = ""
        self.status = ""
        self.error_category = ""
        self.model_executed = False
        #: 有几条短消息被合并成了这一条。商户需要看得见这个合并。
        self.on_merged: Callable[[str, tuple], None] | None = None
        self.receipt_key = ""
        self.created_at = time.time()
        #: 本次解析覆盖的全部原始消息（短消息合并时大于 1）。
        self.merged_event_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "message_id": self.message_id,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "order_id": self.order_id,
            "status": self.status,
            "error_category": self.error_category,
            "model_executed": self.model_executed,
            "receipt_key": self.receipt_key,
            "created_at": self.created_at,
            "merged_event_ids": list(self.merged_event_ids),
        }

    def trace_text(self) -> str:
        """界面"查看详情"里的可读追溯行。"""
        parts = [f"消息 {self.message_id}"]
        if len(self.merged_event_ids) > 1:
            parts.append(f"合并自 {len(self.merged_event_ids)} 条短消息")
        parts.append(f"请求 {self.request_id}")
        if self.job_id:
            parts.append(f"云端任务 {self.job_id}")
        if self.run_id:
            parts.append(f"运行 {self.run_id}")
        if self.order_id:
            parts.append(f"订单 {self.order_id}")
        parts.append(f"结果 {self.status}")
        if not self.model_executed:
            parts.append("（演示结果，非真实模型）")
        return "　".join(parts)


@dataclass
class ApplyOutcome:
    """一条消息处理完之后的结论。界面据此刷新。"""

    trace: JobTrace
    outcome: str
    order_id: str = ""
    needs_review: bool = False
    question: str = ""
    ambiguities: tuple[str, ...] = ()
    receipt_queued: bool = False
    simulated: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace": self.trace.to_dict(),
            "outcome": self.outcome,
            "order_id": self.order_id,
            "needs_review": self.needs_review,
            "question": self.question,
            "ambiguities": list(self.ambiguities),
            "receipt_queued": self.receipt_queued,
            "simulated": self.simulated,
            "message": self.message,
        }


class OrderPipeline:
    """把一条本地消息变成订单（可能还带回执）。

    ``local_parse`` 是数据最小化边界的所在地（哪些字段允许离开本机），
    ``orders.OrderStore`` 是唯一的订单真相源。这里只做编排。
    """

    def __init__(
        self,
        orders: Any,
        tenant: str,
        group: str,
        parse_service: Any,
        outbox: ReceiptOutbox | None = None,
        settings: AutoOrderSettings | None = None,
        clock: Callable[[], float] = time.time,
        merger: Any = None,
        account_id: str = "default",
    ):
        self.orders = orders
        self.tenant = tenant
        self.group = group
        self.parse_service = parse_service
        self.outbox = outbox
        self.settings = settings or AutoOrderSettings()
        self.merger = merger
        self.account_id = account_id
        self._clock = clock
        self.traces: list[JobTrace] = []
        self._lock = threading.RLock()
        self._max_traces = 500

    # ------------------------------------------------------------- pipeline

    def process(
        self,
        message_id: str,
        *,
        source: str = "live",
        on_progress: Callable[[Any], None] | None = None,
    ) -> ApplyOutcome:
        """处理一条消息。``source`` 决定是否产生回执。

        ``source`` 为历史类（``history``/``replay``/``backfill``/``import``）时
        **不生成回执** —— 历史整理不该让客户收到一堆"已记单"。
        """
        request_id = f"req-{message_id}"
        trace = JobTrace(request_id=request_id, message_id=message_id)
        self._record(trace)

        # 0) 短消息合并：原始消息一旦已进入某个合并组就不再单独处理。
        #    只有代表消息会走到解析 —— 否则同一笔订单会被拆成三笔、三次计费、
        #    三条回执。
        if self.merger is not None and self.merger.is_merged(message_id):
            if self.merger.representative_of(message_id) != message_id:
                return self._finish(trace, "merged_away", message="已并入同一组短消息，由代表消息统一处理")
            trace.merged_event_ids = tuple(self.merger.evidence_map(message_id)["merged_event_ids"])
            # 通知外面：这几条被合成了这一条。以前这个事实只留在 trace 里，
            # 商户在界面上完全看不到 —— 他发了三条只得到一张订单，
            # 不会知道系统把它们合到一起了。
            if self.on_merged is not None:
                try:
                    self.on_merged(message_id, trace.merged_event_ids)
                except Exception:
                    pass

        # 1) 自己的回执不再触发下单。
        message = self._message(message_id)
        if message is None:
            return self._finish(trace, "rejected", message="本地没有这条消息")
        if self.outbox is not None and self.outbox.is_own_receipt(str(message["content"] or "")):
            return self._finish(trace, "own_receipt", message="这是本软件自己发出的回执，已跳过")

        # 2) 本地上下文 → 网关任务 → 插件 → 结果。
        try:
            progress = self.parse_service.submit(message_id, on_progress=on_progress)
        except Exception as exc:
            trace.status = "failed"
            trace.error_category = "internal_error"
            return self._finish(trace, "failed", message=f"处理失败：{exc}")

        trace.job_id = getattr(progress, "job_id", "") or ""
        trace.status = getattr(progress, "state", "") or ""
        if progress.needs_review:
            return self._finish(trace, "needs_review", needs_review=True, message=progress.message)
        # 提交被本地规则挡住时必须原样报出原因。之前直接往下走 settle()，
        # 结果商户看到的是「没有待处理的解析」——听起来像没活干，
        # 实际上是这条消息根本没能提交。
        if getattr(progress, "state", "") == LOCAL_BLOCKED:
            trace.error_category = "input_error"
            return self._finish(trace, "blocked", needs_review=True, message=progress.message)

        # 3) 拿结果。settle 会把云端建议落进本地订单库。
        settled = self.parse_service.settle(on_progress=on_progress)
        trace.job_id = getattr(settled, "job_id", "") or trace.job_id
        trace.run_id = trace.run_id or f"run-{trace.job_id or request_id}"
        order_id = getattr(settled, "order_id", "") or ""
        trace.order_id = order_id

        if not order_id:
            label = settled.message or getattr(settled, "state", "")
            return self._finish(trace, "ignored", message=label)

        # 4) 结果回执。演示结果明确标记，不冒充真实模型输出。
        # ``model_executed`` 缺失时按 False 处理：没有证据表明跑了真模型时，
        # 宁可标成演示，也不要让商户以为结果来自真实分析。
        simulated = not bool(getattr(self.parse_service, "model_executed", False))
        trace.model_executed = not simulated
        receipt_queued = self._maybe_queue_receipt(order_id, source=source, simulated=simulated)
        message = settled.message or "已处理"
        if simulated:
            message += "（演示结果）"
        return self._finish(
            trace,
            settled.state if hasattr(settled, "state") else "done",
            order_id=order_id,
            needs_review=bool(settled.needs_review),
            receipt_queued=receipt_queued,
            simulated=simulated,
            message=message,
        )

    # -------------------------------------------------------------- receipts

    def _maybe_queue_receipt(self, order_id: str, *, source: str, simulated: bool) -> bool:
        """落库之后才入队。顺序错了就会出现"没保存却说已记单"。"""
        if self.outbox is None:
            return False
        if source in NO_RECEIPT_SOURCES:
            return False
        if not self.settings.sending_allowed:
            return False
        if simulated:
            # 演示数据允许走完整流程以便演示，但会标明来源，不会混进真实经营数据。
            pass
        detail = self.orders.order_detail(self.tenant, order_id)
        if not detail or not detail.get("items"):
            return False
        if detail.get("status") not in ("draft", "review", "cancel_review"):
            # 已确认的订单不再发确认回执。
            return False

        priced, price_pending = self._items_for_receipt(detail)
        if not priced and not price_pending:
            # 缺价且不允许价格待定：转人工，不发一条没有金额的"已记单"。
            return False
        kind = receipt_kind_for(str(detail.get("change_type", "new")))
        try:
            text = build_receipt_text(
                priced, delivery_date=str(detail.get("delivery_date") or ""),
                price_pending=price_pending, kind=kind,
            )
        except ValueError:
            return False
        request = ReceiptRequest(
            order_id=order_id,
            order_version=int(detail.get("version", 1)),
            conversation=self.group,
            kind=kind,
            text=text,
            source=source,
            price_pending=price_pending,
            customer_name=str(detail.get("customer_name") or ""),
        )
        if not self.outbox.enqueue(request):
            return False
        for trace in self.traces:
            if trace.order_id == order_id:
                trace.receipt_key = request.idempotency_key
        return True

    def _items_for_receipt(self, detail: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        """区分"没有价格"和"价格待定"。

        两者都缺价，但只有后者允许发回执 —— 而且必须写明价格待定，不能显示零元。
        """
        priced: list[dict[str, Any]] = []
        pending = False
        for item in detail.get("items", ()):
            price = int(item.get("unit_price_cents", 0) or 0)
            entry = {
                "name": str(item.get("product_name") or item.get("sku") or ""),
                "quantity": item.get("quantity", 0),
                "unit": str(item.get("unit") or ""),
            }
            if price > 0:
                entry["unit_price_cents"] = price
                priced.append(entry)
            else:
                pending = True
        if pending and self.settings.allow_price_pending:
            return priced, True
        return priced, False

    # ------------------------------------------------------------ internals

    def _message(self, message_id: str):
        return self.orders.db.execute(
            "SELECT * FROM messages WHERE merchant_id=? AND id=?", (self.tenant, message_id)
        ).fetchone()

    def _record(self, trace: JobTrace) -> None:
        with self._lock:
            self.traces.append(trace)
            if len(self.traces) > self._max_traces:
                del self.traces[: len(self.traces) - self._max_traces]

    def _finish(self, trace: JobTrace, outcome: str, **kwargs: Any) -> ApplyOutcome:
        kwargs.setdefault("message", "")
        result = ApplyOutcome(trace=trace, outcome=outcome, **kwargs)
        return result

    def offer_to_merger(self, message_ids: Sequence[str], *, quote_of: Mapping[str, str] | None = None) -> list[str]:
        """把新到的消息投进短消息合并缓冲区。

        只在**实时**路径上调用。历史导入逐条处理，因为历史消息带着原始
        时间戳，合并会破坏因果。
        """
        if self.merger is None:
            return []
        accepted: list[str] = []
        for message_id in message_ids:
            row = self.orders.db.execute(
                "SELECT id,sender,content,kind,sent_at_epoch,group_id FROM messages WHERE merchant_id=? AND id=?",
                (self.tenant, message_id),
            ).fetchone()
            if row is None or not str(row["content"] or "").strip():
                continue
            if row["kind"] not in ("text", "text_message"):
                # 图片、语音不参与合并，直接按单条处理。
                continue
            text = str(row["content"])
            if len(text) > self.merger.settings.max_message_chars:
                continue
            quote = (quote_of or {}).get(message_id, "")
            key = MergeKey(
                account_id=self.account_id,
                conversation=str(row["group_id"] or self.group),
                sender=str(row["sender"] or ""),
                topic=quote,
            )
            buffered = BufferedMessage(
                event_id=message_id,
                text=text,
                sent_at=float(row["sent_at_epoch"] or self._clock()),
                kind=str(row["kind"]),
                quoted=quote,
            )
            if self.merger.offer(key, buffered):
                accepted.append(message_id)
        return accepted

    def dispatch_merged(self, on_progress: Callable[[Any], None] | None = None) -> list[ApplyOutcome]:
        """结算到期的合并组并解析代表消息。

        返回每个代表消息的处理结果。原始消息不会重复解析、计费或发送。
        """
        if self.merger is None:
            return []
        outcomes: list[ApplyOutcome] = []
        for batch in self.merger.flush():
            outcomes.append(self.process(batch.representative_event_id, source="live", on_progress=on_progress))
        return outcomes

    def recent_traces(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [trace.to_dict() for trace in self.traces[-limit:]][::-1]

    def billing_fact(self, job_id: str) -> dict[str, Any]:
        """某个云端任务的**服务端**计费事实。

        这里只转发，不计算。扣费、免费次数、策略版本一律由网关判定；本地
        曾经按"每个任务扣 1 次"自己加总，既把免费任务算成收费，也和运营配
        的每任务扣费数对不上。
        """
        getter = getattr(self.parse_service, "billing_fact", None)
        if not callable(getter) or not job_id:
            return {}
        try:
            return dict(getter(job_id) or {})
        except Exception:
            return {}


class OrderFollowUp:
    """最少追问。"""

    def __init__(self, plugin: Any = None):
        if plugin is None:
            from order_agent import OrderPlugin

            plugin = OrderPlugin()
        self.plugin = plugin

    def question_for(self, output: Mapping[str, Any], request: Any) -> str:
        question = self.plugin.follow_up_question(output, request)
        return question.text if question is not None else ""


__all__ = ["ApplyOutcome", "JobTrace", "OrderFollowUp", "OrderPipeline"]
