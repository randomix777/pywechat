"""首页数据：不依赖 Tkinter，可以直接测试。

界面只负责画；所有"首页上显示什么"的判断都在这里，因此商户看到的每一个
数字都能被测试覆盖。

首页回答商户最关心的七件事：
微信状态、当前会话、当前模式、暂停、今日订单、备货汇总、异常数量、剩余额度。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from .conversations import Conversation
from .runmode import AutoOrderSettings, RunMode
from .wechat_state import WeChatState, WeChatStatus


@dataclass
class OrderRow:
    """首页/列表里的一行订单。"""

    order_id: str
    customer: str
    status: str
    summary: str
    amount: float
    delivery_date: str = ""
    change_type: str = "new"
    version: int = 1
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "customer": self.customer,
            "status": self.status,
            "summary": self.summary,
            "amount": self.amount,
            "delivery_date": self.delivery_date,
            "change_type": self.change_type,
            "version": self.version,
            "updated_at": self.updated_at,
        }


#: 订单状态 -> 商户看得懂的文字。
STATUS_LABELS = {
    "draft": "已自动记单",
    "review": "需要核对",
    "cancel_review": "取消待核对",
    "confirmed": "已确认",
    "cancelled": "已取消",
    "superseded": "已被新版本替代",
    "ignored_duplicate": "重复消息",
}

#: 计入「有效订单」的状态。
#:
#: **只有** 出现在这里的版本才算数。被新版本替代的（``superseded``）、
#: 已取消的、已忽略重复的都不算 —— 否则改一次量备货就会翻倍，
#: 商户按汇总去采购会多买。
EFFECTIVE_STATUSES = ("draft", "review", "cancel_review", "confirmed")

#: 需要商户动手处理的状态。``draft`` 不在其中：自动记单的订单不需要逐单确认，
#: 把它算成「待办」等于把自动化的成果又变回人工清单。
ACTIONABLE_STATUSES = ("review", "cancel_review")

#: 首页每个指标的口径。界面上直接显示这行字，避免"今日订单"含义不明。
METRIC_DEFINITIONS = {
    "effective_orders": "有效订单：当前未被替代、未取消的订单版本；不含改量前的旧版本",
    "actionable": "需要处理：处于待核对或取消待核对状态的订单",
    "auto_recorded": "自动记单：已由系统记下、尚未人工确认的订单（不需要逐单确认）",
    "stock": "备货汇总：只统计有效订单版本，按商品合计",
    "payment_claims": "待核对付款：客户声称已付款，需要人工核对收款；不等于已到账",
    "send_errors": "发送异常：回执没有发出去或结果无法确认的条数",
    "quota": "剩余分析额度：来自云端账号，本地离线时显示上次已知值",
}


@dataclass
class StockRow:
    """备货汇总的一行。"""

    sku: str
    name: str
    quantity: float
    unit: str
    order_count: int
    customers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "name": self.name,
            "quantity": self.quantity,
            "unit": self.unit,
            "order_count": self.order_count,
            "customers": list(self.customers),
        }


class DashboardBuilder:
    """从本地订单库组装首页数据。

    口径写在 :data:`METRIC_DEFINITIONS` 里，界面上直接显示，
    这样"今日订单 12"这个数字到底指什么，商户看得见。
    """

    #: 首页订单的时间范围：最近 N 秒内更新的订单。默认按本地日期的今天。
    scope_hours: int = 24

    def __init__(self, orders: Any, tenant: str, clock: Callable[[datetime] | None] = None,
                 scope_hours: int | None = None):
        self.orders = orders
        self.tenant = tenant
        self._now = clock or (lambda: datetime.now(timezone.utc))
        if scope_hours is not None:
            self.scope_hours = int(scope_hours)

    def _scope_cutoff(self) -> str:
        return (self._now() - timedelta(hours=self.scope_hours)).isoformat()

    def scope_text(self) -> str:
        if self.scope_hours >= 24:
            return f"最近 {self.scope_hours // 24} 天内更新的订单"
        return f"最近 {self.scope_hours} 小时内更新的订单"

    def _superseded_ids(self) -> set[str]:
        """被更新版本顶替、但尚未确认的旧版本。

        追加/改量/取消会生成一个新版本，旧版本要等到**确认**时才被标记
        superseded。在确认之前两者都是有效状态，如果都计入备货，
        「两箱 + 追加一箱」会算成 2 + 3 = 5 箱，商户就会多备两箱。

        只对 ``change_type`` 是 ``modify``/``cancel`` 的订单生效：
        「老样子」生成的**新单**虽然也记了 ``history_order_id``，但它是
        另一笔生意，不该把被参照的那一单从备货里抹掉 —— 抹掉就会少备
        （历史 3 箱 + 新单 3 箱 本该备 6，只显示 3 就少买了 3 箱）。

        这里不改动订单状态（确认新版本时仍然要留痕），只是**不把旧版本
        重复计入**。新版本被否决时旧版本自然重新成为唯一有效版本。
        """
        rows = self.orders.db.execute(
            f"""
            SELECT history_order_id FROM orders
            WHERE merchant_id=? AND history_order_id IS NOT NULL
              AND change_type IN ('modify','cancel')
              AND status IN ({",".join("?" for _ in EFFECTIVE_STATUSES)})
            """,
            (self.tenant, *EFFECTIVE_STATUSES),
        ).fetchall()
        return {str(row["history_order_id"]) for row in rows if row["history_order_id"]}

    def effective_orders(self) -> list[OrderRow]:
        """当前有效的订单版本。

        关键：**只取每个订单链的最新有效版本**。改一次量会产生新版本，
        旧版本被标为 ``superseded``；如果不按状态过滤，备货汇总会把
        「两箱」和「改成三箱」加起来变成五箱。同理，一个尚在待核对的
        新版本已经顶替了旧版本，旧版本不能再计一次。
        """
        replaced = self._superseded_ids()
        placeholders = ",".join("?" for _ in EFFECTIVE_STATUSES)
        rows = self.orders.db.execute(
            f"""
            SELECT o.id, o.status, o.delivery_date, o.change_type, o.version, o.updated_at,
                   c.name AS customer
            FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
            WHERE o.merchant_id=? AND o.status IN ({placeholders}) AND o.updated_at >= ?
            ORDER BY o.updated_at DESC LIMIT 200
            """,
            (self.tenant, *EFFECTIVE_STATUSES, self._scope_cutoff()),
        ).fetchall()
        result: list[OrderRow] = []
        for row in rows:
            if row["id"] in replaced:
                continue
            detail = self.orders.order_detail(self.tenant, row["id"])
            if not detail:
                continue
            items = detail.get("items", [])
            if not items and row["status"] != "cancel_review":
                continue
            summary = "、".join(
                f"{item.get('product_name') or item.get('sku')} {item.get('quantity')}{item.get('unit')}"
                for item in items
            )
            amount = sum(
                float(item.get("quantity") or 0) * int(item.get("unit_price_cents") or 0) for item in items
            ) / 100
            result.append(
                OrderRow(
                    order_id=row["id"],
                    customer=str(row["customer"] or "未绑定客户"),
                    status=row["status"],
                    summary=summary or "（无商品明细）",
                    amount=amount,
                    delivery_date=str(row["delivery_date"] or ""),
                    change_type=str(row["change_type"] or "new"),
                    version=int(row["version"] or 1),
                    updated_at=str(row["updated_at"] or ""),
                )
            )
        return result

    def actionable_orders(self) -> list[OrderRow]:
        """真正需要商户动手的订单。

        自动记单（``draft``）**不算**待办 —— 那正是自动化的目的。
        """
        placeholders = ",".join("?" for _ in ACTIONABLE_STATUSES)
        rows = self.orders.db.execute(
            f"""
            SELECT o.id, o.status, o.updated_at FROM orders o
            WHERE o.merchant_id=? AND o.status IN ({placeholders}) AND o.updated_at >= ?
            ORDER BY o.updated_at DESC LIMIT 200
            """,
            (self.tenant, *ACTIONABLE_STATUSES, self._scope_cutoff()),
        ).fetchall()
        keep = {row["id"] for row in rows}
        return [item for item in self.effective_orders() if item.order_id in keep]

    def auto_recorded_count(self) -> int:
        """已自动记单、尚未人工确认的数量。单独显示，不作为待办。"""
        row = self.orders.db.execute(
            "SELECT COUNT(*) AS count FROM orders WHERE merchant_id=? AND status='draft' AND updated_at >= ?",
            (self.tenant, self._scope_cutoff()),
        ).fetchone()
        return int(row["count"] or 0)

    def stock_summary(self, rows: Sequence[OrderRow] | None = None) -> list[StockRow]:
        """备货汇总。按商品聚合，数量求和，只用有效版本。"""
        rows = rows if rows is not None else self.effective_orders()
        if not rows:
            return []
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            detail = self.orders.order_detail(self.tenant, row.order_id)
            for item in detail.get("items", ()) if detail else ():
                sku = str(item.get("sku") or "")
                if not sku:
                    continue
                bucket = buckets.setdefault(
                    sku,
                    {"name": str(item.get("product_name") or sku), "quantity": 0.0, "unit": str(item.get("unit") or ""), "orders": 0, "customers": []},
                )
                bucket["quantity"] += float(item.get("quantity") or 0)
                bucket["orders"] += 1
                if row.customer not in bucket["customers"]:
                    bucket["customers"].append(row.customer)
        return [
            StockRow(
                sku=sku,
                name=str(bucket["name"]),
                quantity=round(float(bucket["quantity"]), 3),
                unit=str(bucket["unit"]),
                order_count=int(bucket["orders"]),
                customers=tuple(bucket["customers"]),
            )
            for sku, bucket in sorted(buckets.items())
        ]

    def exception_count(self) -> int:
        """真正需要人工处理的条数。

        口径：**订单**里挂着未解决审核任务、且订单处于待核对状态的不同订单数，
        加上未关闭的**独立澄清任务**数。两者都算异常，但澄清任务**不是订单**：
        它不计入订单数、不进备货汇总，界面上也分开列。
        """
        placeholders = ",".join("?" for _ in ACTIONABLE_STATUSES)
        row = self.orders.db.execute(
            f"""
            SELECT COUNT(DISTINCT r.order_id) AS count
            FROM review_tasks r JOIN orders o ON o.id = r.order_id
            WHERE r.merchant_id=? AND r.status='open' AND o.status IN ({placeholders})
            """,
            (self.tenant, *ACTIONABLE_STATUSES),
        ).fetchone()
        return int(row["count"] or 0) + len(self.orders.open_clarifications(self.tenant))

    def clarifications(self) -> list[dict[str, Any]]:
        """未关闭的独立澄清任务。**不是订单**，单独展示。"""
        return self.orders.open_clarifications(self.tenant)

    def unconfirmed_count(self) -> int:
        """已记单但客户还没有明确回复确认的条数。

        客户没回复**不等于**确认，也不等于付款。这个数用于观察自动化的
        覆盖面，**不作为默认待办**。
        """
        row = self.orders.db.execute(
            "SELECT COUNT(*) AS count FROM orders WHERE merchant_id=? AND status IN ('draft','review')",
            (self.tenant,),
        ).fetchone()
        return int(row["count"] or 0)

    def payment_claims(self) -> list[dict[str, Any]]:
        """客户声称已付款的订单。

        永远单独列出，且永远不会标成"已付款"——到账要靠人工核对收款。
        """
        rows = self.orders.db.execute(
            "SELECT id, customer_id, status FROM orders WHERE merchant_id=? AND claim_payment=1", (self.tenant,)
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            customer = self.orders.db.execute(
                "SELECT name FROM customers WHERE id=? AND merchant_id=?", (row["customer_id"], self.tenant)
            ).fetchone()
            result.append(
                {
                    "order_id": row["id"],
                    "customer": str(customer["name"]) if customer else "未绑定客户",
                    "note": "客户声称已付款，需要人工核对收款",
                }
            )
        return result

    def parse_failures(self) -> int:
        row = self.orders.db.execute(
            "SELECT COUNT(*) AS count FROM parse_runs WHERE merchant_id=? AND status='failed'", (self.tenant,)
        ).fetchone()
        return int(row["count"] or 0)

    def system_failures(self) -> list[dict[str, Any]]:
        """系统解析失败（超时 / 输出非法 / 网络错误 / 执行失败）。

        与 :meth:`clarifications` 严格分开：那些是"客户表达缺信息"，
        要**问客户**；这些是"系统没跑成"，要**重试或转人工**，绝不能伪装成
        需要客户回答的问题。
        """
        return self.orders.system_failures(self.tenant)


@dataclass
class HomeView:
    """首页的全部内容。每个数字都有明确口径。"""

    wechat_state: str = WeChatState.UNREADABLE
    wechat_detail: str = ""
    wechat_ready: bool = False
    simulated: bool = False
    #: 订单**是不是模型分析出来的**。
    #:
    #: 和 :attr:`simulated` 是两回事，别混成一句话：
    #:
    #: * ``simulated`` —— 读到的消息本身是不是假的（演示数据源）
    #: * ``model_executed`` —— 订单是真模型分析的，还是按固定规则拼的
    #:
    #: 真实微信 + 规则引擎 是完全可能发生的组合（没登录、没配密钥、额度用完）。
    #: 那时候消息是真的、订单也是真的，但**不是模型理解出来的**。商户必须能
    #: 分清这两件事，否则会把规则结果当成 AI 的判断。
    model_executed: bool = False
    conversation: str = ""
    conversation_note: str = ""
    mode: str = RunMode.BACKGROUND
    mode_note: str = ""
    can_send: bool = False
    send_block_reason: str = ""
    #: 有效订单（不含被替代的旧版本）。
    effective: list[OrderRow] = field(default_factory=list)
    #: 真正需要商户动手的订单。
    actionable: list[OrderRow] = field(default_factory=list)
    auto_recorded: int = 0
    #: 正在等后续消息、还没凑齐的短消息组。商户发了"苹果两箱"就停住时，
    #: 他需要知道系统**还在等**，而不是以为漏掉了。
    open_merge_groups: int = 0
    stock: list[StockRow] = field(default_factory=list)
    payment_claims: list[dict[str, Any]] = field(default_factory=list)
    #: 未关闭的独立澄清任务。**不是订单**：不进订单数、不进备货。
    clarifications: list[dict[str, Any]] = field(default_factory=list)
    #: 系统解析失败。同样不是订单，也不会去问客户。
    system_failures: list[dict[str, Any]] = field(default_factory=list)
    quota_headline: str = "未登录产品账号"
    quota_ok: bool = False
    pending_receipts: int = 0
    unknown_receipts: int = 0
    failed_receipts: int = 0
    #: 发送异常 = 没发出去 + 结果无法确认。
    send_errors: int = 0
    parse_failures: int = 0
    #: 每个指标的口径，界面上直接显示。
    definitions: dict[str, str] = field(default_factory=lambda: dict(METRIC_DEFINITIONS))
    scope_text: str = ""
    last_actual_range: str = ""

    @property
    def exceptions(self) -> int:
        return len(self.actionable) + len(self.clarifications)

    def status_line(self) -> str:
        return "微信正常" if self.wechat_ready else self.wechat_detail or "微信状态未知"

    def summary_line(self) -> str:
        """一句话摘要。"""
        if not self.wechat_ready:
            return self.wechat_detail or "请先完成微信登录"
        clar = f"，待澄清 {len(self.clarifications)} 条" if self.clarifications else ""
        return (
            f"{self.conversation or '未选择会话'}　{self.mode_note}　"
            f"有效订单 {len(self.effective)} 单，需要处理 {self.exceptions} 单{clar}，{self.quota_headline}"
        )


__all__ = [
    "ACTIONABLE_STATUSES",
    "EFFECTIVE_STATUSES",
    "METRIC_DEFINITIONS",
    "STATUS_LABELS",
    "DashboardBuilder",
    "HomeView",
    "OrderRow",
    "StockRow",
]
