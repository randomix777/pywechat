"""首次使用向导：可恢复、可测试、无需命令行。

    ① 微信    →  ② 选会话  →  ③ 读历史  →  ④ 查额度  →  ⑤ 智能整理  →  ⑥ 完成

每一步的结果都写进本地设置，正常情况下下次启动直接进首页，**不重走向导**。
只有两种情况会回到向导：

* 第一次启动；
* 微信账号切换 —— 旧账号的会话绑定和客户记忆不能混用，必须重新绑定。

## 状态机而不是一堆 if

向导的每一步都有明确的进入条件和离开条件，因此可以在没有界面的情况下
完整跑一遍（测试就是这么做的），界面只负责把状态画出来。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .conversations import Conversation, ConversationRegistry
from .history_read import (
    DEFAULT_MAX_MESSAGES,
    HistoryProgress,
    HistoryReader,
    HistoryResult,
    HistoryRange,
    ReadMode,
    TimeWindow,
)
from .runmode import AutoOrderSettings, MerchantSettings, RunMode, SettingsStore
from .wechat_state import WeChatMonitor, WeChatState, WeChatStatus


class WizardStep:
    WECHAT = "wechat"
    CONVERSATIONS = "conversations"
    HISTORY = "history"
    QUOTA = "quota"
    ANALYZE = "analyze"
    DONE = "done"

    #: 商户看到的步骤只有 4 步。额度查询与智能整理是同一件事的两半，
    #: 拆成两屏只是让商户多点一次、多闪一次。
    ORDER = (WECHAT, CONVERSATIONS, HISTORY, ANALYZE, DONE)

    TITLES = {
        WECHAT: "检查微信",
        CONVERSATIONS: "选择会话",
        HISTORY: "读取历史消息",
        QUOTA: "智能整理",
        ANALYZE: "智能整理",
        DONE: "开始使用",
    }


@dataclass
@dataclass
class QuotaView:
    """额度。商户只看得到这些中文，不看单位名。

    展示形式是**进度条 + 百分比**（像 ChatGPT 的用量条），
    **不换算成“还够多少次”。商户的口头：

        额度给一个额度条，按百分比显示，不显示具体能做多少次

    额度不是 token，是产品自己的计量单位。换算成消息条数会让商户
    根据假设去决定充多少（一条消息并不等于一笔订单），
    而百分比是认真的。
    """

    logged_in: bool = False
    active: bool = False
    remaining: int = 0
    plan: str = ""
    period_end: str = ""
    #: 额度总额、0 = 不限量（那时**不显示百分比**）。
    total_units: int = 0
    used_units: int = 0
    #: 提预中的（已接单尚未结算）。展示上算已用量。
    reserved_units: int = 0
    purchase_method: str = "manual"
    purchase_hint: str = ""
    message: str = ""

    @property
    def sufficient(self) -> bool:
        return self.logged_in and self.active and self.remaining > 0

    @property
    def unlimited(self) -> bool:
        return self.total_units <= 0

    @property
    def used_percent(self) -> int:
        """已用百分比 0-100。不限量时返回 0。"""
        if self.unlimited or self.total_units <= 0:
            return 0
        return max(0, min(100, round((self.used_units + self.reserved_units)
                                     * 100 / self.total_units)))

    def headline(self) -> str:
        if not self.logged_in:
            return "未登录产品账号"
        if not self.active:
            return "套餐已到期"
        if self.remaining <= 0:
            return "分析额度已用完，重新开通或续期后即可继续整理"
        if self.unlimited:
            return "分析额度：不限"
        return f"分析额度已用 {self.used_percent}%"

    def to_dict(self) -> dict[str, Any]:
        return {
            "logged_in": self.logged_in,
            "active": self.active,
            "remaining": self.remaining,
            "plan": self.plan,
            "period_end": self.period_end,
            "purchase_method": self.purchase_method,
            "purchase_hint": self.purchase_hint,
            "message": self.message,
            "headline": self.headline(),
            "total_units": self.total_units,
            "used_units": self.used_units,
            "reserved_units": self.reserved_units,
            "used_percent": self.used_percent,
            "unlimited": self.unlimited,
        }


class QuotaBlocked(Exception):
    """额度不足。读取结果保留，等恢复后继续。"""

    def __init__(self, view: QuotaView):
        super().__init__(view.headline())
        self.view = view


class Wizard:
    """向导状态机。

    所有外部依赖都注入，因此可以完整地无界面跑通。
    """

    def __init__(
        self,
        settings_store: SettingsStore,
        monitor: WeChatMonitor,
        conversations: ConversationRegistry,
        history_reader: HistoryReader,
        *,
        tenant: str,
        orders: Any = None,
        client: Any = None,
        pipeline: Any = None,
        on_step: Callable[[str], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        on_wechat: Callable[[WeChatStatus], None] | None = None,
        on_history_done: Callable[[HistoryResult], None] | None = None,
    ):
        self.store = settings_store
        self.settings = settings_store.load()
        self.monitor = monitor
        self.conversations = conversations
        self.history = history_reader
        self.tenant = tenant
        #: 商户本地的订单库。**读历史、存消息、看订单、导出**都用它，
        #: 与有没有登录云端账号无关。
        self._orders = orders
        self.client = client
        self.pipeline = pipeline
        self.on_step = on_step
        self.on_log = on_log
        #: 每次检测都发，**不论结果**。界面靠它刷新。
        self.on_wechat = on_wechat
        #: 读取结束。界面据此显示结果并给出下一步，**不再自动跳页**。
        self.on_history_done: Callable[[HistoryResult], None] | None = on_history_done
        self.step = WizardStep.WECHAT
        self.history_result: HistoryResult | None = None
        self.quota = QuotaView()
        self._lock = threading.RLock()
        self._analyzed_ids: list[str] = []

    def bind_callbacks(
        self,
        on_step: Callable[[str], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        on_wechat: Callable[[WeChatStatus], None] | None = None,
        on_history_done: Callable[[HistoryResult], None] | None = None,
    ) -> None:
        """界面建好之后重新绑定回调。

        构造向导时界面还不存在，那时 ``app.events`` 上的回调都是空的。把它们
        **拷贝**进构造参数，等界面把 ``app.events`` 换成真正的实现之后，向导
        里存的仍然是那批 ``None`` —— 于是「重新检测」跑完了，界面收不到任何
        通知，标题纹丝不动。这里让界面绑定完再回填一次。
        """
        if on_step is not None:
            self.on_step = on_step
        if on_log is not None:
            self.on_log = on_log
        if on_wechat is not None:
            self.on_wechat = on_wechat
        if on_history_done is not None:
            self.on_history_done = on_history_done

    # ------------------------------------------------------------------ log

    def _log(self, text: str) -> None:
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:
                pass

    def _go(self, step: str) -> None:
        with self._lock:
            self.step = step
        self._log(f"进入步骤：{WizardStep.TITLES.get(step, step)}")
        if self.on_step:
            try:
                self.on_step(step)
            except Exception:
                pass

    def save(self) -> None:
        self.store.save(self.settings)

    # --------------------------------------------------------------- ① 微信

    def check_wechat(self) -> WeChatStatus:
        """检测一次微信，并把**新状态**通知出去。

        无论结果如何都要通知：以前只在"已就绪"时才通知，于是关掉微信再点
        「重新检测」，状态变了界面却一动不动。

        已就绪才往下走；**从就绪掉回未就绪**（微信被关掉）则退回第 1 步 ——
        读不到群消息时继续停在后面几步，只会让商户对着一个永远不动的界面
        反复点。
        """
        status = self.monitor.check_once()
        if self.on_wechat:
            try:
                self.on_wechat(status)
            except Exception:
                pass
        if status.ready:
            if self.settings.account_id and self.settings.account_id != status.account_id and status.account_id:
                # 账号切换：旧绑定一律作废，重新走选择。
                self._log("检测到微信账号已切换，重新绑定会话")
                self.settings.reset_for_new_account()
                self.conversations = ConversationRegistry()
            self.settings.account_id = status.account_id
            self.save()
            # 只有**还停在第 1 步**时才推进。监视器每几秒就检测一次；以前不
            # 判断当前在哪一步，于是商户正在读历史（或者已经走到首页了）时，
            # 一次后台检测就能把他弹回选择页 —— 表现为"读完没有下一步"、
            # 进度条不动。状态好着的时候没有理由打断正在进行的流程。
            if self.step == WizardStep.WECHAT:
                self._go(WizardStep.CONVERSATIONS)
        elif self.step != WizardStep.WECHAT:
            self._log(f"微信状态变为「{status.state}」，回到第 1 步")
            self._go(WizardStep.WECHAT)
        return status

    def start_wechat(self) -> bool:
        """用户点了「打开微信」。这是唯一允许主动启动微信的入口。"""
        ok = self.monitor.launch()
        if ok:
            self._log("已请求启动微信，等待登录")
        return ok

    def start_monitoring(self) -> None:
        """后台等商户扫码；登录后自动进入下一步。不叠加线程。"""
        self.monitor.start()

    def stop_monitoring(self) -> None:
        self.monitor.stop()

    # ------------------------------------------------------------- ② 会话

    def refresh_conversations(self, kind: str = "") -> list[Conversation]:
        source = getattr(self, "conversation_source", None)
        if source is None:
            return list(self.conversations.available)
        self.conversations.refresh(source.list_conversations(kind))
        return list(self.conversations.available)

    def choose_history_conversations(self, selected: Sequence[Conversation]) -> None:
        self.conversations.set_history(selected)
        self.settings.history_conversations = [item.identity for item in selected]
        self.save()

    def choose_live_conversation(self, conversation: Conversation) -> None:
        """第一版实时接单只允许一个会话。结构上限制，不靠提示。"""
        self.conversations.set_live(conversation)
        self.settings.live_conversation = conversation.identity
        self.save()
        self._go(WizardStep.HISTORY)

    def go_to_history(self) -> None:
        self._go(WizardStep.HISTORY)

    # ------------------------------------------------------------- ③ 历史

    def read_history(
        self,
        *,
        range_kind: str = "",
        custom: TimeWindow | None = None,
        max_messages: int = 0,
        on_progress: Callable[[HistoryProgress], None] | None = None,
        resume: bool = True,
        mode: str = ReadMode.RECENT,
    ) -> HistoryResult:
        if mode not in ReadMode.ALL_KINDS:
            raise ValueError(f"unknown read mode: {mode}")
        kind = range_kind or self.settings.history_range
        window = HistoryRange.window(kind, custom)
        limit = int(max_messages or self.settings.history_max_messages or DEFAULT_MAX_MESSAGES)
        self.settings.history_range = kind
        self.settings.history_max_messages = limit
        self.save()
        self._go(WizardStep.HISTORY)

        importer = self._make_importer()
        result = self.history.read(
            self.conversations.history_selected,
            window,
            merchant_id=self.tenant,
            max_messages=limit,
            on_progress=on_progress,
            resume=resume,
            importer=importer,
            mode=mode,
        )
        self.history_result = result
        self._log(f"历史读取完成：{result.headline()}")
        for gap in result.gaps:
            self._log(f"注意：{gap}")
        # **不自动跳页**。商户需要先看到"读到了什么、覆盖哪段时间、有没有缺口"，
        # 再自己点下一步。以前这里 `_go(QUOTA)` 直接跳走，这些信息一闪而过；
        # 读取失败时界面还会莫名其妙退回上一步且一言不发。
        if self.on_history_done is not None:
            try:
                self.on_history_done(result)
            except Exception:
                pass
        return result

    def cancel_history(self) -> None:
        self.history.cancel()
        self._log("已请求取消读取")

    def _make_importer(self) -> Callable[[Conversation, Sequence[Mapping[str, Any]], TimeWindow], dict[str, Any]]:
        """把读到的消息写进现有订单库，并绑定已知客户别名。"""

        def import_messages(conversation: Conversation, messages: Sequence[Mapping[str, Any]], window: TimeWindow) -> dict[str, Any]:
            source = f"history:{conversation.identity}"
            return self.orders().add_import(
                self.tenant, conversation.name, list(messages), source, "wechat_history", ""
            )

        return import_messages

    def orders(self) -> Any:
        """商户本地订单库。

        以前这里只认 ``pipeline.orders``，而 ``pipeline`` 只有在**登录云端账号
        之后**才会被建起来。结果：商户第一次用，还没登录，去点「开始读取」，
        必然抛 ``wizard needs an order store to import history`` —— 读历史
        100% 失败，消息一条都存不进来。

        本地读历史、存消息、看订单、导出都**不该**要求云端登录，也不该扣额度。
        所以订单库由宿主直接交给向导，pipeline 只是备选。
        """
        if self._orders is not None:
            return self._orders
        if self.pipeline is not None and getattr(self.pipeline, "orders", None) is not None:
            return self.pipeline.orders
        raise RuntimeError("wizard has no local order store")

    # ------------------------------------------------------------- ④ 额度

    def check_quota(self) -> QuotaView:
        """查询额度。未登录时引导网页登录，不显示任何 Key。"""
        if self.client is None:
            self.quota = QuotaView(logged_in=False, message="尚未登录产品账号")
            return self.quota
        try:
            payload = self.client.account()
        except Exception as exc:
            self.quota = QuotaView(logged_in=False, message=_network_or_login(exc))
            return self.quota
        subscription = (payload or {}).get("subscription") or {}
        self.quota = QuotaView(
            logged_in=True,
            active=bool(subscription.get("active")),
            remaining=int(subscription.get("remaining_units", 0) or 0),
            plan=str(subscription.get("plan_code", "")),
            period_end=str(subscription.get("period_end", "")),
            # 进度条要的是这三个。缺任何一个都画不出百分比 ——
            # 那是「已用 / 共多少」，不是「还剩多少次」。
            total_units=int(subscription.get("quota_units", 0) or 0),
            used_units=int(subscription.get("used_units", 0) or 0),
            reserved_units=int(subscription.get("reserved_units", 0) or 0),
            purchase_method="manual",
            purchase_hint="请联系客服开通或续期。",
            message="",
        )
        if self.quota.sufficient:
            # 以前这里直接 `_go(ANALYZE)`，于是第 4 步一闪就没了 —— 而"实际读到
            # 多少条、覆盖哪段时间、有没有缺口"只画在第 4 步上。商户根本没机会看，
            # 如实上报等于没做。现在不跳：额度与读取结果都留在同一屏里。
            pass
        return self.quota

    def require_quota(self) -> None:
        """额度不足时抛出，但**保留已读取的数据**。"""
        view = self.quota.sufficient and self.quota or self.check_quota()
        if not view.sufficient:
            raise QuotaBlocked(view)

    # ------------------------------------------------------------ ⑤ 整理

    def analyze(self, message_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """对读取到的消息跑一遍智能整理。

        额度不足时抛 :class:`QuotaBlocked`，但**不重复读取、不重复扣费**：
        消息还在本地库，恢复额度后重新调用即可。

        **不盲目地推算扣费。** 之前这里按"每个任务算 1 次"累加，既把免费任务
        算成了收费，也和运营配的每任务扣费数对不上。计费只有网关说了算；
        这里只统计"提交了多少个任务"，金额一律读服务端返回的值。
        """
        self.require_quota()
        self._go(WizardStep.ANALYZE)
        ids = list(message_ids) if message_ids is not None else self._pending_message_ids()
        outcomes: list[dict[str, Any]] = []
        submitted = 0
        billing: list[dict[str, Any]] = []
        for message_id in ids:
            outcome = self.pipeline.process(message_id, source="history", on_progress=None)
            outcomes.append(outcome.to_dict())
            if outcome.trace.job_id:
                submitted += 1
                fact = self.pipeline.billing_fact(outcome.trace.job_id)
                if fact:
                    billing.append(fact)
        self._analyzed_ids = ids
        # 汇总口径来自服务端：网关说扣了多少就是多少。
        charged_units = sum(int(fact.get("charged_units", 0)) for fact in billing)
        summary = {
            "processed": len(ids),
            "with_result": sum(1 for item in outcomes if item.get("order_id")),
            "needs_review": sum(1 for item in outcomes if item.get("needs_review")),
            "ignored": sum(1 for item in outcomes if item.get("outcome") == "ignored"),
            "already_analyzed": self.already_analyzed_count(),
            # submitted 是任务数，charged_units 是服务端判定的扣费量 —— 两者
            # 不一定相等，不能互相替代。
            "submitted_jobs": submitted,
            "charged_units": charged_units,
            "billing": billing,
            "billing_note": "扣费金额由云端账号按服务端策略计算，此处只做汇总。",
            "traces": [item["trace"] for item in outcomes],
        }
        self._log(
            f"整理完成：处理 {summary['processed']} 条，生成订单 {summary['with_result']} 条，"
            f"需要核对 {summary['needs_review']} 条，本次实际扣费 {charged_units} 次"
        )
        self.finish()
        return summary

    def _pending_message_ids(self) -> list[str]:
        """待处理的消息，已按合并关系过滤，并**排除已分析过的**。

        短消息合并开启时，被并入其它消息的原始消息**不会**出现在这里 ——
        否则它们会被再解析一次、再计费一次、再记一次单。

        已分析过的消息同样排除：再次提交会真的产生一次模型调用和一次扣费，
        而结果只会因为"已处理过"被判成 duplicate。商户点第二次"开始智能
        整理"，等于花钱买一个"已处理过"。换模型重新分析要走独立的差异流程，
        在它做好之前这里一律不再重复提交。
        """
        rows = self.orders().db.execute(
            "SELECT id FROM messages WHERE merchant_id=? ORDER BY COALESCE(sent_at_epoch, 0), id", (self.tenant,)
        ).fetchall()
        already = self.orders().analyzed_message_ids(self.tenant)
        merger = self._merger()
        result: list[str] = []
        for row in rows:
            message_id = row["id"]
            if message_id in already:
                continue
            if merger is None or not merger.is_merged(message_id) or merger.representative_of(message_id) == message_id:
                result.append(message_id)
        return result

    def already_analyzed_count(self) -> int:
        """已经整理过、本轮不会重复提交的消息数。界面要如实告诉商户。"""
        try:
            return len(self.orders().analyzed_message_ids(self.tenant))
        except Exception:
            return 0

    def _merger(self):
        merger = getattr(self, "merger", None)
        if merger is None and self.pipeline is not None:
            merger = getattr(self.pipeline, "merger", None)
        return merger

    def merge_pending(self, source: str = "live") -> list[Any]:
        """把待结算的短消息组合并，返回结算结果。

        历史导入默认**不**合并：历史消息本来就带着原始时间，
        逐条处理才能还原当时的因果。合并只用于实时接单。
        """
        merger = self._merger()
        if merger is None:
            return []
        return merger.flush()

    # ------------------------------------------------------------- ⑥ 完成

    def finish(self) -> None:
        self.settings.wizard_complete = True
        self.save()
        self._go(WizardStep.DONE)

    def needs_wizard(self, account_id: str = "") -> bool:
        """是否需要重新走向导。

        正常情况下不重走。只有首次启动、或微信账号换了才重走。
        """
        if not self.settings.wizard_complete:
            return True
        if account_id and self.settings.account_id and account_id != self.settings.account_id:
            return True
        return False

    def restore(self) -> bool:
        """从设置恢复。返回是否成功进入首页。"""
        if self.settings.live_conversation:
            restored = ConversationRegistry.from_dict(
                {
                    "history": self.settings.history_conversations,
                    "live": self.settings.live_conversation,
                },
                self.conversations.available,
            )
            if restored.live_selected is not None:
                self.conversations = restored
                self.step = WizardStep.DONE
                return True
        return False


def _network_or_login(exc: Exception) -> str:
    """区分网络断开与登录过期。

    把所有异常都显示成"网络暂不可用"会让商户一直等一个不会自己好的网络。
    """
    code = str(getattr(exc, "code", "") or "")
    if code in ("network_unavailable", "backend_unavailable"):
        return "网络暂不可用，读取的消息已保留"
    if code == "unauthenticated":
        return "登录已过期，请重新登录"
    if code == "quota_exhausted":
        return "分析额度已用完"
    if code == "subscription_inactive":
        return "套餐已到期"
    return f"查询额度失败：{exc}"


__all__ = [
    "QuotaBlocked",
    "QuotaView",
    "Wizard",
    "WizardStep",
]
