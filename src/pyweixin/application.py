"""商户应用的编排层：界面只调用这里。

把「应用」和「界面」分开，是因为 Tkinter 窗口在自动化测试里既跑不起来也不该
跑。:class:`MerchantApplication` 承载全部业务流转，:mod:`pyweixin.merchant_ui`
只负责把状态画出来。

**这保证了一件事**：界面上点「开始智能整理」，走的就是测试里验证过的那条
真实路径，不是另一条只在测试里存在的捷径。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .agent import AgentStore
from .conversations import (
    Conversation,
    ConversationKind,
    ConversationRegistry,
    ConversationSource,
    RealConversationSource,
)
from .dashboard import ACTIONABLE_STATUSES, DashboardBuilder, HomeView, OrderRow
from .history_read import (
    DEFAULT_MAX_MESSAGES,
    HistoryCursor,
    HistoryProgress,
    HistoryReader,
    HistoryRange,
    HistoryResult,
    ReadMode,
    TimeWindow,
)
from .live_listen import (
    LISTEN_DEGRADED,
    LISTEN_STARTING,
    LISTEN_STOPPED,
    LiveListener,
)
from .merging import MergeSettings, MessageMerger
from .receipts import ReceiptOutbox
from .reliability import UiOperationQueue
from .runmode import AutoOrderSettings, MerchantSettings, RunMode, SettingsStore
from .service import ApplyOutcome, OrderPipeline
from .wechat_send import (
    SafeReceiptSender,
    SendCheck,
    SendHeld,
    SendVerdict,
    TargetInspector,
    real_sending_available,
)
from .wechat_state import (
    RealWeChatLauncher,
    RealWeChatProbe,
    WeChatMonitor,
    WeChatProbe,
    WeChatState,
    WeChatStatus,
)
from .wizard import QuotaView, Wizard, WizardStep

LOGGER = logging.getLogger(__name__)

#: 后台轮询间隔。停止后不会残留线程。
POLL_INTERVAL_SECONDS = 3.0

#: 监听能力在界面上必须常驻的一句话。
#:
#: 打开独立聊天窗口本身就会切前台，这条能力**从未在真实机器上验证过抢不抢
#: 焦点**，因此不能承诺"不打断您使用微信"。把做不到的事写在界面上，比让人
#: 以为可以一边接单一边正常用微信要好。
LISTEN_FAIR_NOTE = (
    "持续监听会打开一个独立的聊天窗口并占用本机前台。"
    "这段时间请不要在这台电脑上使用微信，锁屏或断开远程桌面后监听会中断。"
)

#: 模型列表刷新失败时给商户看的话。**不带异常原文**。
#:
#: 失败原因分三类，商户能据此决定做什么：网络、登录过期、服务端暂时不可用。
#: 具体异常只写日志（``load_models`` 里的 warning），界面上一律不放类名、
#: 方法名或堆栈 —— 商户看不懂，只会觉得软件坏了。
MODEL_STALE_NOTICE = (
    "暂时连不上服务，下面显示的是最近一次获取的模型列表，可能不是最新。"
    "已读取的消息都保留着，可以稍后点「刷新模型列表」重试。"
)
MODEL_UNAVAILABLE_NOTICE = (
    "暂时无法获取分析模型列表，当前显示已保存的模型选择。"
    "请稍后点「刷新」重试；这次失败不会清除原有选择。"
)


@dataclass
class AppEvents:
    """界面需要刷新时收到通知。全部是可调用项，便于测试替换。"""

    on_home: Callable[[HomeView], None] | None = None
    on_step: Callable[[str], None] | None = None
    on_log: Callable[[str], None] | None = None
    on_history_progress: Callable[[HistoryProgress], None] | None = None
    #: 读取结束。界面据此显示结果并给出「下一步」按钮 —— 不再自动跳页。
    on_history_done: Callable[[Any], None] | None = None
    #: 短消息被合并成一条时触发。商户需要看见这个合并。
    on_merged: Callable[[dict[str, Any]], None] | None = None
    on_conversations: Callable[[list[Conversation]], None] | None = None
    on_error: Callable[[str], None] | None = None
    #: 微信状态**每检测一次**就发一次，不论是否满足条件。
    #:
    #: 以前只在"已就绪"时才发事件，于是关掉微信再点「重新检测」时，
    #: 状态其实变了，界面却一动不动 —— 商户看到的还是"已登录"。
    on_wechat: Callable[[Any], None] | None = None
    #: 监听状态或统计发生变化。界面据此刷新开关与数字。
    on_listen: Callable[[dict[str, Any]], None] | None = None
    #: 产品账号登录刚成功。界面靠它刷新登录框与门禁 —— 以前没人通知，
    #: 登录框一直写着"未登录"，门禁也不放行，商户以为登录没成功。
    #: 注意名字不能叫 on_login：界面上 _on_login 是"发起登录"按钮，
    #: 事件泵按 _on_<kind> 找处理器，重名会点成死循环。
    on_logged_in: Callable[[], None] | None = None


class MerchantApplication:
    """把向导、闭环服务、回执和设置串起来的编排层。"""

    def __init__(
        self,
        *,
        data_dir: Any,
        orders: Any,
        tenant: str,
        probe: WeChatProbe | None = None,
        launcher: Any = None,
        conversation_source: ConversationSource | None = None,
        history_reader_fn: Callable[..., Any] | None = None,
        group_members_fn: Callable[[Any], Sequence[str]] | None = None,
        sender_source: str = "members",
        gateway_url: str = "",
        client_factory: Callable[[Any], Any] | None = None,
        sender: Callable[[str, str], Any] | None = None,
        events: AppEvents | None = None,
        simulated: bool = False,
        merge_settings: Mapping[str, Any] | None = None,
        target_inspector: TargetInspector | None = None,
    ):
        self.data_dir = data_dir
        self.orders = orders
        self.tenant = tenant
        self.events = events or AppEvents()
        self.simulated = simulated
        self.gateway_url = gateway_url

        # 所有涉及键鼠的操作共用一条串行队列：历史读取、监听、发送互不打断。
        self.ui_queue = UiOperationQueue(thread_name="merchant-ui")
        self.ui_lock = self.ui_queue

        self.settings_store = SettingsStore(data_dir / "settings.json")
        self.settings: MerchantSettings = self.settings_store.load()
        self.auto: AutoOrderSettings = self.settings.auto

        self.agent_store = AgentStore(str(data_dir / "agent.db"))
        self.outbox = ReceiptOutbox(self.agent_store, sender=sender)
        self.cursor = HistoryCursor(str(data_dir / "history.db"))
        self.merger = MessageMerger(
            str(data_dir / "merge.db"), MergeSettings(**merge_settings) if merge_settings else None
        )
        #: 发送前的草稿守卫。真实发送默认关闭，见 :mod:`pyweixin.wechat_send`。
        self.target_inspector: TargetInspector | None = None
        self.send_guard: SafeReceiptSender | None = None

        self.probe = probe or RealWeChatProbe()
        self.launcher = launcher or RealWeChatLauncher()
        self.monitor = WeChatMonitor(
            self.probe, self.launcher, on_change=self._on_wechat_change,
            # 探测也要动微信窗口。以前监视器和读取各干各的，两个线程可能同时
            # 驱动同一个窗口。共用这把锁去掉的是一个本来就存在的竞争。
            #
            # 注意：这不是"读到 0 条"的修复。对照实验（real_race_repro.py）
            # 没能复现空读 —— 原因仍未查明。
            ui_lock=self.ui_lock,
        )

        self.conversations = ConversationRegistry()
        #: wxid 显示名备注（内存解不出的群名/人名，手备注一次一直有效）。
        self.wxid_aliases: dict = self.load_aliases()
        self.conversation_source = conversation_source or RealConversationSource(ui_lock=self.ui_lock)
        # 解密库优先：有钥匙就走真名真人数，没有就当没这条路（内存兜底）。
        if hasattr(self.conversation_source, "wcdb_fn"):
            try:
                self.conversation_source.wcdb_fn = self.list_wcdb_conversations
            except Exception:
                LOGGER.debug("wcdb hookup failed", exc_info=True)
        #: 没钥匙时点"读取"自动提一次。每个进程只试一次，重试走界面按钮。
        self._wcdb_ensure_attempted = False
        #: 持续监听。默认**不启动** —— 它会打开独立聊天窗口并占用本机前台，
        #: 必须由商户自己在首页上点开，见 :meth:`start_listening`。
        self.listener: LiveListener | None = None
        self._listen_state: tuple[str, str] = (LISTEN_STOPPED, "")

        self.history = HistoryReader(
            history_reader_fn or self._default_history_reader,
            ui_lock=self.ui_lock,
            cursor=self.cursor,
            # ? history_reader_fn ?????:??????????(????
            # 2 ???),????????????,????????
            group_members=group_members_fn or self._default_group_members,
            # 群发送人来源。默认 members（上游原设计）：全量优先；
            # avatar 实验链需要时显式开；有钥匙时 members 自动升级 wcdb。
            sender_source=sender_source,
            # 读历史时顺手记群名（面板标题里的）：解不出名的群也能显示真名。
            alias_recorder=self._record_group_alias,
            # 解密库直读：有钥匙才有，没有则 members 照旧（4.1 界面已死会失败，
            # 但那是老行为，不在这里改门禁）。
            # 只给真实来源接：模拟/测试来源用自己的替身，不碰真微信。
            wcdb_reader_fn=(self._wcdb_reader
                            if isinstance(self.conversation_source, RealConversationSource)
                            else None),
        )

        self.client = None
        self.session = None
        self.parse_service = None
        self.pipeline: OrderPipeline | None = None
        self.dashboard = DashboardBuilder(orders, tenant)

        self.wizard = Wizard(
            self.settings_store,
            self.monitor,
            self.conversations,
            self.history,
            tenant=tenant,
            orders=orders,
            is_product_logged_in=self.product_logged_in,
            on_step=self.events.on_step,
            on_log=self.events.on_log,
            on_wechat=self.events.on_wechat,
        )
        self.wizard.conversation_source = self.conversation_source
        # wizard 建好之后才能恢复：恢复要拿当前绑定的账号做比对。
        self._restore_conversation_cache()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None
        self._background: list[threading.Thread] = []
        self._last_home: HomeView | None = None
        #: 客户可见的模型目录。首次使用时拉取，失败保留上一次。
        self.model_catalog: dict[str, Any] = _empty_model_view()

    # ------------------------------------------------------------- 生命周期

    def attach(self, session: Any, client: Any, parse_service: Any, backend_is_model: bool = False) -> None:
        """登录成功后接上云端能力。

        ``backend_is_model`` 决定结果是否被标注为"真实模型"。默认 False——
        未接真实后端时，界面上的订单必须被明确标为演示，不能无提示地变成
        正式经营订单。
        """
        self.session = session
        self.client = client
        self.parse_service = parse_service
        if parse_service is not None and hasattr(parse_service, "model_alias"):
            parse_service.model_alias = str(self.settings.model_alias or "")
        self.backend_is_model = bool(backend_is_model)
        self.wizard.client = client
        self.wizard.pipeline = self._pipeline()
        self.pipeline = self.wizard.pipeline
        # 结果可能来自真实模型，也可能来自演示后端。标记必须准确。
        try:
            self.parse_service.model_executed = self.backend_is_model
        except AttributeError:
            # 不可变的桩对象：不强设，OrderPipeline 会按 False 处理。
            pass
        if self.events.on_logged_in:
            try:
                self.events.on_logged_in()
            except Exception:
                LOGGER.debug("logged_in handler skipped", exc_info=True)

    def _on_merged(self, message_id: str, event_ids: tuple) -> None:
        """短消息被合并了。告诉界面，让商户能核对。"""
        self._last_merged = {"message_id": message_id, "event_ids": tuple(event_ids),
                             "count": len(event_ids)}
        if self.events.on_merged:
            try:
                self.events.on_merged(self._last_merged)
            except Exception:
                LOGGER.debug("merged handler skipped", exc_info=True)

    def detach(self) -> None:
        """退出登录。

        **必须真的能退出来。** 之前 ``client_login.logout()`` 存在却没人调用，
        商户登录一次之后就再也换不了账号 —— 收银电脑上换个人就登不进去，
        本地已读到的数据和订单也不该跟着别人走。

        这里做四件事：通知服务端作废、删掉本机凭证、断开云端能力、刷新界面。
        本地数据**不删** —— 已读到的消息是商户自己经营出来的。

        本机凭证必须删：只清内存不清文件，下次启动 ``restore_session`` 会
        把这次登出又"恢复"回来 —— 商户明明点了退出，重开发现还登着。
        """
        client = self.client
        if client is not None:
            try:
                client.logout()
            except Exception:
                LOGGER.warning("server logout failed; continuing with local sign-out",
                               exc_info=True)
        # 服务端成没成功，本地凭证都删：断网时退出也必须彻底，
        # 否则下次启动照样自动登回去。
        store = getattr(self, "credentials", None)
        if store is not None:
            try:
                from .client_login import clear_session
                clear_session(store)
            except Exception:
                LOGGER.warning("clearing local credentials failed", exc_info=True)
        self.session = None
        #: 最近一次短消息合并的事实（哪几条合成了哪一条）。
        self._last_merged: dict[str, Any] | None = None
        self.client = None
        self.parse_service = None
        self.backend_is_model = False
        self.wizard.client = None
        self.wizard.pipeline = None
        self.pipeline = None
        self.refresh_home()

    def _pipeline(self) -> OrderPipeline:
        service = self.parse_service
        if service is not None and hasattr(service, "on_merged"):
            service.on_merged = self._on_merged
        return OrderPipeline(
            orders=self.orders,
            tenant=self.tenant,
            group=self.live_conversation_name(),
            parse_service=self.parse_service,
            outbox=self.outbox,
            settings=self.auto,
            merger=self.merger,
            account_id=self.settings.account_id or "default",
        )

    def start_background(self) -> None:
        """启动后台轮询。**只启动一个**，停止后不留孤儿线程。"""
        with self._lock:
            if self._poller is not None and self._poller.is_alive():
                return
            self._stop.clear()
            self._poller = threading.Thread(target=self._poll_loop, name="merchant-poll", daemon=True)
            self._poller.start()

    def stop(self) -> None:
        """停止所有后台线程。"""
        self._stop.set()
        self.monitor.stop()
        self.history.stop()
        if self.listener is not None:
            listener, self.listener = self.listener, None
            listener.stop()
        with self._lock:
            poller = self._poller
            self._poller = None
        if poller is not None and poller.is_alive() and poller is not threading.current_thread():
            poller.join(timeout=3.0)
        for thread in self._background:
            if thread.is_alive():
                thread.join(timeout=1.0)
        self._background.clear()
        self.ui_queue.close(timeout=3.0)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_home()
                if self.parse_service is not None and self.auto.sending_allowed:
                    self.dispatch_receipts()
            except Exception as exc:  # pragma: no cover - defensive
                self._error(str(exc))
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def _spawn(self, name: str, function: Callable[[], None]) -> threading.Thread:
        def guarded() -> None:
            try:
                function()
            except Exception as exc:  # 后台线程的异常不会自己浮到界面上
                # 以前这里没有 try/except：读历史、查额度、智能整理、加载模型
                # 任何一处抛异常都直接消失，商户只看到界面莫名其妙退回上一步，
                # 既不知道发生了什么，也不知道该不该重试。必须变成看得见的中文。
                LOGGER.exception("background task failed: %s", name)
                self._report_failure(name, exc)

        thread = threading.Thread(target=guarded, name=name, daemon=True)
        with self._lock:
            self._background = [item for item in self._background if item.is_alive()]
            self._background.append(thread)
        thread.start()
        return thread

    #: 后台任务失败时给商户看的话。不含异常原文 —— 类名、模块路径对商户
    #: 没有意义，只会让界面看起来像是坏了。具体原因只进日志。
    FAILURE_TEXT = {
        "history-read": "读取消息失败，已读到的部分保留着。可以点「开始读取」重试，或换一种读法。",
        "quota": "查询额度失败，请检查网络后点「刷新额度」重试。",
        "models": "获取模型列表失败，已保留上一次的列表，可稍后点「刷新模型列表」重试。",
        "analyze": "智能整理失败，已读到的消息保留着，没有产生订单。",
        "login": "登录失败，请检查网络后重试。",
    }
    DEFAULT_FAILURE_TEXT = "这一步没能完成，可以重试。已保存的内容不会丢失。"

    def _report_failure(self, name: str, exc: Exception) -> None:
        text = self.FAILURE_TEXT.get(name, self.DEFAULT_FAILURE_TEXT)
        if self.events.on_error:
            try:
                self.events.on_error(text)
            except Exception:  # pragma: no cover - 界面正在重建
                LOGGER.debug("error handler skipped", exc_info=True)

    # -------------------------------------------------------------- ① 微信

    def product_logged_in(self) -> bool:
        """产品账号是否已登录。第 1 步的放行判据只认它。"""
        return self.session is not None

    #: wxid 显示名备注的文件名。内存解不出的群名/人名，商户手备注一次，
    #: 以后都显示备注。只影响显示，不改库里原文。
    WXID_ALIAS_NAME = "wxid_aliases.json"

    def _aliases_path(self):
        return Path(self.data_dir) / self.WXID_ALIAS_NAME

    def load_aliases(self) -> dict:
        """读备注映射。文件坏了当没有，不挡启动。"""
        try:
            raw = json.loads(self._aliases_path().read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): str(value) for key, value in raw.items() if value}

    def display_name_for(self, wxid_or_name: str) -> str:
        """显示用名：有备注用备注，没有用原文。从不返回空。"""
        text = str(wxid_or_name or "")
        if not text:
            return ""
        return self.wxid_aliases.get(text, text)

    def display_name_for_conversation(self, item: Any) -> str:
        """会话行的显示名：手动备注 > 内存辨认出的真名 > 原始编号。

        之前重画时只查备注表，内存辨认出的真名（``Conversation.name``）
        被 wxid 原文盖掉 —— 好不容易认出来的名字根本没画出来，
        第 2 步看起来"一个群名都读不出"。只有备注和真名都没有时
        才显示原始编号。
        """
        raw = str(getattr(item, "wxid", "") or getattr(item, "name", "") or "")
        if not raw:
            return ""
        shown = self.display_name_for(raw)
        if shown != raw:
            return shown
        name = str(getattr(item, "name", "") or "")
        if name and name != raw:
            return name
        return raw

    def set_alias(self, wxid: str, name: str) -> bool:
        """手备注一个 wxid。空名等于删掉这条。"""
        wxid, name = str(wxid or "").strip(), str(name or "").strip()
        if not wxid:
            return False
        if name:
            self.wxid_aliases[wxid] = name
        else:
            self.wxid_aliases.pop(wxid, None)
        try:
            path = self._aliases_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self.wxid_aliases, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(path)
        except Exception:
            LOGGER.debug("save wxid aliases failed", exc_info=True)
            return False
        return True

    def check_wechat(self) -> WeChatStatus:
        before = self.wizard.settings.account_id
        status = self.wizard.check_wechat()
        after = self.wizard.settings.account_id
        # 切号了：旧号的会话名单、缓存统统作废。不清掉，第 2 步会一直
        # 显示上个号的群 —— 点进去读的也是错的。
        if before and after and before != after:
            self._clear_conversation_cache()
        return status

    def _clear_conversation_cache(self) -> None:
        try:
            self._conversations_cache_path().unlink(missing_ok=True)
        except Exception:
            pass

    def start_wechat(self) -> bool:
        return self.wizard.start_wechat()

    def watch_wechat(self) -> None:
        """后台等登录完成，自动进入下一步。"""
        self._spawn("watch-wechat", self.monitor.start)

    def _on_wechat_change(self, status: WeChatStatus) -> None:
        if status.ready:
            self.wizard.check_wechat()

    # ------------------------------------------------------------ ② 会话

    CONVERSATIONS_CACHE_NAME = "conversations_cache.json"

    def _conversations_cache_path(self) -> Path:
        return Path(self.data_dir) / self.CONVERSATIONS_CACHE_NAME

    def _restore_conversation_cache(self) -> int:
        """启动时恢复上次的会话列表。读不到、坏了、账号对不上都当没有，不挡启动。"""
        try:
            raw = json.loads(self._conversations_cache_path().read_text(encoding="utf-8"))
        except Exception:
            return 0
        if not isinstance(raw, dict):
            return 0
        current = str(getattr(self.wizard.settings, "account_id", "") or "")
        saved_account = str(raw.get("account_id", "") or "")
        # 缓存是按号存的：换号之后旧名单不能再摆出来。
        # settings 为空（全新）时也不恢复 —— 等检测出当前号再说。
        if not current or not saved_account or current != saved_account:
            return 0
        rows = raw.get("available")
        try:
            return self.conversations.restore_available(rows)
        except Exception:
            return 0

    def _save_conversation_cache(self, found: Sequence[Conversation]) -> None:
        """拉到名单就落盘，连同账号一起存。空结果不覆盖 —— 读失败不等于没有会话。"""
        if not found:
            return
        payload = {
            "account_id": str(getattr(self.wizard.settings, "account_id", "") or ""),
            "saved_at": datetime.now().astimezone().strftime("%m-%d %H:%M"),
            "available": [item.to_dict() for item in found],
        }
        path = self._conversations_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            LOGGER.debug("save conversation cache failed", exc_info=True)

    def conversation_cache_info(self) -> str:
        """给界面看的缓存状态：上次更新时间，或还没有拉取过。"""
        try:
            raw = json.loads(self._conversations_cache_path().read_text(encoding="utf-8"))
            saved_at = raw.get("saved_at") if isinstance(raw, dict) else ""
        except Exception:
            return "还没有拉取过"
        return f"上次更新：{saved_at}" if saved_at else "还没有拉取过"

    def refresh_conversations(self) -> list[Conversation]:
        # 没钥匙先自动提一次（提完直接读，商户不用管中间步骤）。
        # 只对真实来源自动提：模拟/测试来源不需要钥匙，别去碰真微信。
        # 微信会新建分片/库（手机导入建 message_1.db），钥匙是旧的就
        # 覆盖不全 —— 磁盘上有没钥匙的库时也重提，但两次自动提取至少
        # 间隔 5 分钟，失败了反复扫内存只会越扫越慢，重试走界面按钮。
        if isinstance(self.conversation_source, RealConversationSource):
            self._auto_ensure_wcdb_keys()
        found = self.wizard.refresh_conversations()
        if found:
            self._save_conversation_cache(found)
        if self.events.on_conversations:
            self.events.on_conversations(found)
        return found

    def _auto_ensure_wcdb_keys(self) -> bool:
        """该补钥匙就补（没钥匙，或磁盘上有没钥匙的新库），返回钥匙齐否。

        5 分钟节流：失败了反复扫内存只会越扫越慢，重试走界面按钮。
        只对真实来源调用 —— 模拟/测试来源不需要钥匙，别去碰真微信。
        """
        if not self._wcdb_auto_ensure_due():
            return self._wcdb_keys_complete()
        self._wcdb_ensure_attempted = True
        try:
            import time as _time
            self._wcdb_ensure_at = _time.time()
        except Exception:
            pass
        try:
            ok, _ = self.wcdb_status()
            unkeyed: list = []
            if ok:
                unkeyed = self._wcdb_unkeyed()
        except Exception:
            ok, unkeyed = False, []
        if not ok or unkeyed:
            if unkeyed:
                self._log(f"微信新建了 {len(unkeyed)} 个库还没钥匙，自动补提一次…")
            else:
                self._log("本机数据库还没钥匙，自动提取一次（几十秒）…")
            try:
                done, message = self.ensure_wcdb_keys()
            except Exception as exc:
                done, message = False, f"提取失败：{exc}"
            self._log("密钥已就绪" if done else
                      f"自动提取没成（{message}），可点按钮重试")
            return done
        return True

    def _wcdb_keys_complete(self) -> bool:
        """钥匙齐否：状态 ok 且磁盘上没有没钥匙的库。"""
        try:
            ok, _ = self.wcdb_status()
            if not ok:
                return False
            return not self._wcdb_unkeyed()
        except Exception:
            return False

    def _wcdb_unkeyed(self) -> list:
        """磁盘上有、钥匙里没有的库。拿不到目录当没有（别误触发提取）。"""
        try:
            from .wcdb import WcdbReader
            storage = self._wcdb_db_storage()
            if storage is None:
                return []
            return WcdbReader(storage, self.wcdb_keys_path()).unkeyed_databases()
        except Exception:
            return []

    def _wcdb_auto_ensure_due(self) -> bool:
        """这次要不要自动提钥匙：没试过，或上次已超 5 分钟且仍有缺钥匙的库。"""
        if not getattr(self, "_wcdb_ensure_attempted", False):
            return True
        try:
            import time as _time
            if _time.time() - float(getattr(self, "_wcdb_ensure_at", 0) or 0) < 300:
                return False
        except Exception:
            return True
        try:
            storage = self._wcdb_db_storage()
            if storage is None:
                return False
            from .wcdb import WcdbReader
            return bool(WcdbReader(storage, self.wcdb_keys_path()).unkeyed_databases())
        except Exception:
            return False

    # ------------------------------------------------- 解密库（真名真人数）

    def wcdb_keys_path(self) -> Path:
        """密钥文件位置（应用数据目录）。没有=还没提取过。"""
        return Path(self.data_dir) / "wcdb_keys.json"

    def _wcdb_db_storage(self) -> Path | None:
        """当前登录号的 db_storage 目录。拿不到（没登录）返回 None。"""
        try:
            from .WeChatTools import Tools
            folder = Tools.where_wxid_folder()
        except Exception:
            return None
        if not folder:
            return None
        path = Path(folder) / "db_storage"
        return path if path.is_dir() else None

    def wcdb_status(self) -> tuple[bool, str]:
        """解密库能不能用。(ok, 说明)。界面据此显示入口或引导。"""
        from .wcdb import WcdbReader

        storage = self._wcdb_db_storage()
        if storage is None:
            return False, "微信还没登录，看不到本机数据库"
        reader = WcdbReader(storage, self.wcdb_keys_path())
        if reader.available:
            return True, "本机数据库可读，列表带真名和人数"
        return False, reader.error or "还没有提取密钥，提一次以后一直能用"

    def _wcdb_reader(self) -> Any | None:
        """给 HistoryReader 的解密库来源：有钥匙才有，没有返回 None。

        每次调用都新建（钥匙文件可能刚提出来），prepare 失败由上游吞掉回落。
        """
        from .wcdb import WcdbReader

        storage = self._wcdb_db_storage()
        if storage is None:
            return None
        reader = WcdbReader(storage, self.wcdb_keys_path())
        return reader if reader.available else None

    def list_wcdb_conversations(self, kind: str = "") -> list[Any] | None:
        """解密库会话（注入给 RealConversationSource 的优先路）。

        返回 None 或抛异常都等于"这条路不通"，上游回落内存路。
        """
        from .wcdb import WcdbReader, WcdbUnavailable

        storage = self._wcdb_db_storage()
        if storage is None:
            return None
        reader = WcdbReader(storage, self.wcdb_keys_path())
        try:
            return reader.list_conversations(kind)
        except WcdbUnavailable as exc:
            LOGGER.debug("wcdb conversations unavailable: %s", exc)
            return None

    def ensure_wcdb_keys(self, timeout: float = 300.0) -> tuple[bool, str]:
        """跑上游工具提取一次密钥（后台线程调，会扫几十秒内存）。

        钥匙跟微信大版本走，大版本不变一直能用。返回 (ok, 说明)。
        """
        import subprocess
        import sys

        storage = self._wcdb_db_storage()
        if storage is None:
            return False, "微信还没登录，先登录再提取"
        script = (Path(__file__).resolve().parent.parent.parent
                  / "third_party" / "wcdb-key-tool" / "wcdb_key_tool_windows.py")
        if not script.is_file():
            return False, "提取工具不在（third_party 缺失），按文档手动跑一次"
        keys_path = self.wcdb_keys_path()
        try:
            import os as _os

            env = dict(_os.environ)
            # 子进程强制 UTF-8：它要打印中文，直接继承的系统编码（这台机器是
            # cp1252）连 print 都会崩。父进程这边按 UTF-8 解（见下）。
            env["PYTHONUTF8"] = "1"
            proc = subprocess.run(
                [sys.executable, str(script), "extract",
                 "--db-dir", str(storage), "--output", str(keys_path)],
                capture_output=True, timeout=timeout, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return False, "提取超时（超过 %d 秒），微信太忙时重试" % int(timeout)
        except Exception as exc:
            return False, f"启动提取失败：{exc}"
        # 输出按 UTF-8 解（子进程继承的系统编码可能是 cp1252，直接 text=True
        # 会在中文输出上崩 —— 之前"提取没成功"报的就是这个 charmap错）。
        # Windows C 运行库的中文控制台编码不足以覆盖微信输出中的全部字符，
        # 所以这里不信任 locale，认死 UTF-8（子进程被强制 UTF-8，见 env）。
        stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        if proc.returncode != 0:
            tail = (stderr or stdout)[-300:]
            return False, f"提取没成功：{tail}"
        ok, _ = self.wcdb_status()
        if ok:
            return True, "密钥已就绪，点「刷新列表」刷新即带真名"
        return False, "工具跑完了但钥匙对不上，微信可能刚升级，重提一次试试"

    def choose_history_conversations(self, selected: Sequence[Conversation]) -> None:
        self.wizard.choose_history_conversations(selected)

    def choose_live_conversation(self, conversation: Conversation) -> None:
        """第一版实时接单只允许一个会话。"""
        self.wizard.choose_live_conversation(conversation)
        if self.parse_service is not None:
            self.pipeline = self._pipeline()
            self.wizard.pipeline = self.pipeline
        self.refresh_home()

    def live_conversation_name(self) -> str:
        live = self.wizard.conversations.live_selected
        return live.name if live else ""

    def live_conversation(self) -> Conversation | None:
        return self.wizard.conversations.live_selected

    # ------------------------------------------------------------ ③ 历史

    def read_history(
        self,
        *,
        range_kind: str = "",
        custom: TimeWindow | None = None,
        max_messages: int = 0,
        mode: str = "",
        sender_source: str = "",
    ) -> None:
        def work() -> None:
            # 读之前先保证钥匙齐（手机导入建了新分片时在这里补提，
            # 不用商户回第 2 步）。后台线程里跑，不卡界面。
            if isinstance(self.conversation_source, RealConversationSource):
                try:
                    self._auto_ensure_wcdb_keys()
                except Exception:
                    LOGGER.debug("read-time key ensure failed", exc_info=True)
            result = self.wizard.read_history(
                range_kind=range_kind, custom=custom, max_messages=max_messages,
                on_progress=self.events.on_history_progress, mode=mode or ReadMode.RECENT,
                sender_source=sender_source,
            )
            if self.events.on_history_done:
                try:
                    self.events.on_history_done(result)
                except Exception:
                    LOGGER.debug("history-done handler skipped", exc_info=True)
            self.refresh_home()

        self._spawn("history-read", work)

    def cancel_history(self) -> None:
        self.wizard.cancel_history()

    # ------------------------------------------------------------ 持续监听

    def listen_view(self) -> dict[str, Any]:
        """界面上那个开关要显示的东西。全是商户看得懂的中文。"""
        listener = self.listener
        stats = listener.stats if listener is not None else None
        gaps = listener.gap_report() if listener is not None else []
        return {
            "running": bool(listener is not None and listener.running),
            "conversation": listener.conversation.disambiguation if listener is not None else "",
            "state": self._listen_state[0],
            "detail": self._listen_state[1],
            "summary": stats.describe() if stats is not None else "",
            "started_at": stats.started_at if stats is not None else 0.0,
            "events_seen": stats.events_seen if stats is not None else 0,
            "events_stored": stats.events_stored if stats is not None else 0,
            "orders_applied": stats.orders_applied if stats is not None else 0,
            "reviews_raised": stats.reviews_raised if stats is not None else 0,
            "batches_flushed": stats.batches_flushed if stats is not None else 0,
            "skipped_own": stats.events_skipped_own if stats is not None else 0,
            "skipped_duplicate": stats.events_skipped_duplicate if stats is not None else 0,
            "gaps": gaps,
            "gap_count": len(gaps),
            # 界面常驻说明。这条能力从未在真实机器上验证过抢不抢焦点，
            # 所以不能承诺"不打断您使用微信"。
            "note": LISTEN_FAIR_NOTE,
        }

    def start_listening(self, conversation: Conversation, group_members: Sequence[str] = ()) -> bool:
        """对**一个**会话开持续监听。

        会打开一个独立聊天窗口并占用本机前台 —— 首期只监听一个会话。
        """
        if self.listener is not None and self.listener.running:
            return False
        if self.simulated:
            self._listen_state = (LISTEN_DEGRADED, "演示模式下不真的开监听")
            self._emit_listen()
            return False

        listener = LiveListener(
            conversation=conversation,
            store=self.agent_store,
            merger=self.merger,
            pipeline=self.pipeline,
            account_id=self.settings.account_id or "default",
            group_members=tuple(group_members),
            ui_lock=self.ui_lock,
            sink=self._live_sink(conversation),
            on_state=self._on_listen_state,
        )
        self.listener = listener
        self._listen_state = (LISTEN_STARTING, "正在打开聊天窗口")
        listener.start()
        self._emit_listen()
        return True

    def stop_listening(self) -> None:
        listener = self.listener
        self.listener = None
        if listener is None:
            self._listen_state = (LISTEN_STOPPED, "")
            self._emit_listen()
            return
        listener.stop()
        stats = listener.stats
        lost = len(listener.gaps)
        detail = f"已停止。看到 {stats.events_seen} 条，入库 {stats.events_stored} 条"
        if lost:
            # 不承诺不漏消息：中断期间的缺口必须说出来。
            detail += f"；有 {lost} 段时间没有采集，那期间的消息需要单独补读"
        self._listen_state = (LISTEN_STOPPED, detail)
        self.refresh_home()
        self._emit_listen()

    def _live_sink(self, conversation: Conversation):
        """把一条实时事件写进**商户本地订单库**，返回 (是否新入库, message_id)。

        这里不能用 :class:`AgentStore`。客户端整条链（``OrderPipeline``、
        ``LocalParseService``）都按 ``OrderStore.messages.id`` 回查本地消息，
        拿 ``AgentStore`` 的 ``event_id`` 去查必然落空，消息会被静默丢掉。
        """
        tenant = self.tenant
        orders = self.orders
        # 每个会话一个固定的 source：同一批重复投递会命中
        # UNIQUE(merchant_id,group_id,source_id) 被 INSERT OR IGNORE 挡掉。
        source = f"live:{conversation.identity}"

        def sink(event: Any) -> tuple[bool, str]:
            payload = {
                "sender": event.sender,
                "content": event.content,
                "kind": event.kind.value if hasattr(event.kind, "value") else str(event.kind),
                "sent_at": event.sent_at or "",
                "source_id": event.stable_id or event.event_id,
                "stable_source_id": bool(event.stable_id),
            }
            outcome = orders.add_import(tenant, conversation.name, [payload], source, "live", "")
            ids = list(outcome.get("message_ids") or [])
            return (bool(ids), ids[0] if ids else "")

        return sink

    def _on_listen_state(self, state: str, detail: str) -> None:
        self._listen_state = (state, detail)
        self._emit_listen()

    def _emit_listen(self) -> None:
        if self.events.on_listen:
            try:
                self.events.on_listen(self.listen_view())
            except Exception:
                LOGGER.debug("listen view render skipped", exc_info=True)

    # ------------------------------------------------------------ ④ 额度

    def check_quota(self) -> QuotaView:
        view = self.wizard.check_quota()
        self.refresh_home()
        return view

    def login_requested(self) -> None:
        """界面上点了「登录产品账号」。

        浏览器登录是异步的：点了之后界面继续用，其它步骤照常可用。
        登录成功后走 :meth:`attach`，由启动层提供的 ``attach_session`` 负责
        把本地的解析服务接上。
        """
        runner = getattr(self, "login_runner", None)
        if runner is None:
            self._error("尚未配置产品登录，请用默认网关地址启动本软件。")
            return

        def work() -> None:
            try:
                session = runner(self._login_progress)
            except Exception as exc:
                self._error(f"登录失败：{exc}")
                return
            attach = getattr(self, "attach_session", None)
            if callable(attach):
                attach(session)
            self.refresh_home()

        self._spawn("login", work)

    def _login_progress(self, status: str, detail: str = "") -> None:
        if self.events.on_log:
            try:
                self.events.on_log(detail or status)
            except Exception:
                pass

    # ------------------------------------------------------------ ⑤ 整理

    def analyze(self, role: str = "order") -> dict[str, Any] | None:
        """开始智能整理。额度不足时保留已读取的数据。"""
        result_holder: dict[str, Any] = {}

        def work() -> None:
            from .wizard import QuotaBlocked

            try:
                result_holder["summary"] = self.wizard.analyze(role=role)
            except QuotaBlocked as blocked:
                result_holder["quota"] = blocked.view
                self._error(blocked.view.headline())
            except Exception as exc:
                self._error(f"整理失败：{exc}")
            self.refresh_home()

        thread = self._spawn("analyze", work)
        thread.join()
        return result_holder.get("summary")

    # -------------------------------------------------------- ⑥ 模式/回执

    def set_mode(self, mode: str) -> None:
        self.auto.mode = mode
        self.settings.auto = self.auto
        self.settings_store.save(self.settings)
        if mode == RunMode.PAUSED:
            # Pausing does not discard anything: receipts already queued stay in
            # the outbox and are dispatched when the merchant resumes.
            self.refresh_home()
            return (0, 0)
        # Leaving pause releases held receipts, but **each one is re-validated
        # first**. A receipt held twenty minutes ago may now point at a
        # superseded order version, a conversation that is no longer selected,
        # or a mode that no longer allows sending. Releasing the whole set
        # blindly is how a customer gets told "your order is booked" after the
        # order was already changed.
        released, re_held = self._release_held_validated()
        self.refresh_home()
        return (released, re_held)

    def _release_held_validated(self) -> tuple[int, int]:
        """逐条重新核对 held 回执：通过的放回队列，不通过的继续暂缓。"""
        live = self.live_conversation()
        allowed = (live.name,) if live else ()
        expected = self._latest_versions()
        released = 0
        re_held = 0
        for command_id, target, payload in self.agent_store.held_commands():
            version = int(payload.get("order_version", 0) or 0)
            reason = ""
            if not self.auto.sending_allowed:
                reason = "mode_or_switch_disabled"
            elif target not in allowed:
                reason = "conversation_not_allowed"
            elif expected and version < expected.get(target, version):
                reason = "stale_order_version"
            if reason:
                self.agent_store.mark_hold(command_id, reason)
                re_held += 1
            else:
                self.agent_store.release_held(command_id)
                released += 1
        return released, re_held

    def pause(self) -> None:
        self.set_mode(RunMode.PAUSED)

    def resume(self) -> None:
        self.set_mode(RunMode.AUTO_TAKE if self.auto.allow_local_automation else RunMode.BACKGROUND)

    def set_allow_local_automation(self, allowed: bool) -> None:
        """是否允许本机被自动接单占用。必须显式打开。"""
        self.auto.allow_local_automation = bool(allowed)
        self.settings.auto = self.auto
        self.settings_store.save(self.settings)
        self.refresh_home()

    def dispatch_receipts(self) -> list[dict[str, Any]]:
        """发送回执。暂停、模式不对、版本过期或草稿非空时都不发。"""
        live = self.live_conversation()
        allowed = (live.name,) if live else ()
        expected = self._latest_versions()
        sender = self._guarded_sender()
        return self.outbox.dispatch(
            allowed_targets=allowed,
            paused=self.auto.mode == RunMode.PAUSED,
            expected_versions=expected,
            send=sender,
        )

    def _guarded_sender(self):
        """真实发送走草稿守卫；没有守卫时不允许真的操作微信。"""
        if self.target_inspector is None:
            def refuse(target: str, text: str) -> None:
                # Raise SendHeld, not a generic error: nothing was typed, so
                # the receipt must go back to `held` and be retried later,
                # not be marked as an unknown send.
                raise SendHeld(
                    SendCheck(False, SendVerdict.REAL_SENDING_DISABLED,
                              "没有配置发送前检查器，已阻止真实发送")
                )

            return refuse
        if self.send_guard is None:
            self.send_guard = SafeReceiptSender(
                self.target_inspector,
                inner=self._raw_sender,
                expected_account=self.settings.account_id,
            )
        return self._guard_or_raise

    def _guard_or_raise(self, target: str, text: str) -> None:
        assert self.send_guard is not None
        verdict = self.send_guard.send(target, text)
        if not verdict.ok:
            raise SendHeld(verdict)

    def set_sender(self, sender: Callable[[str, str], Any] | None) -> None:
        """注入真实的发送实现。只有通过草稿守卫后才会被调用。"""
        self._raw_sender = sender

    def enable_target_inspection(self, inspector: TargetInspector, raw_sender: Callable[[str, str], Any] | None = None) -> None:
        """配置发送前检查器。

        真实发送入口在真机验证前保持关闭；:func:`real_sending_available`
        为假时守卫一律暂缓，不会操作微信窗口。
        """
        self.target_inspector = inspector
        self._raw_sender = raw_sender
        self.send_guard = SafeReceiptSender(
            inspector, inner=raw_sender, expected_account=self.settings.account_id
        )

    def _latest_versions(self) -> dict[str, int]:
        """会话 -> 该会话当前最新订单版本。旧版本回执一律不发。"""
        live = self.live_conversation()
        if live is None:
            return {}
        row = self.orders.db.execute(
            "SELECT MAX(version) AS version FROM orders WHERE merchant_id=? AND group_id=?",
            (self.tenant, live.name),
        ).fetchone()
        return {live.name: int(row["version"] or 0)} if row and row["version"] is not None else {}

    # ------------------------------------------------------------- 模型

    def load_models(self, force: bool = False) -> dict[str, Any]:
        """拉取模型目录；网络失败时回退到本机上次成功的目录。"""
        if self.client is None:
            self.model_catalog = _empty_model_view()
            return self.model_catalog
        # 桩对象/单测可能没有 data_dir：没有就不走本机缓存，直接调网关。
        # 所有文件读写本来就有 OSError 兜底，这里只是别在拼路径时先炸。
        base = getattr(self, "data_dir", "") or ""
        cache_path = Path(base) / "model_catalog_cache.json"
        cooldown_path = Path(base) / "model_catalog_retry_after.txt"
        if not (self.model_catalog or {}).get("models"):
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and isinstance(cached.get("models"), list):
                    self.model_catalog = cached
            except (OSError, ValueError, TypeError):
                pass
        try:
            retry_at = float(cooldown_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError, TypeError):
            retry_at = 0.0
        remaining = retry_at - time.time()
        if remaining > 0:
            self.model_catalog = dict(self.model_catalog or _empty_model_view())
            self.model_catalog["stale"] = True
            self.model_catalog["notice"] = (
                f"模型列表请求繁忙，请约 {int(remaining + 0.99)} 秒后刷新。"
                "已保存的模型选择仍保留。"
            )
            return self.model_catalog
        try:
            refresh = getattr(self.client, "refresh_models", None) if force else None
            payload = (refresh() if callable(refresh) else self.client.list_models()) or {}
        except Exception as exc:
            # 不用「空列表」伪装成「没有模型」；保留旧数据并说明原因。
            self.model_catalog = dict(self.model_catalog or _empty_model_view())
            self.model_catalog["stale"] = True
            # 异常原文**不能**进商户界面。这里曾经直接 f-string 拼 {exc}，
            # 结果界面上冒出 "'DemoGateway' object has no attribute 'list_models'"
            # 这种东西 —— 商户看不懂，只会觉得软件坏了。技术细节只进日志。
            LOGGER.warning("model catalog refresh failed: %s", exc, exc_info=True)
            retry_after = getattr(exc, "retry_after", None)
            if retry_after:
                try:
                    cooldown_path.parent.mkdir(parents=True, exist_ok=True)
                    cooldown_path.write_text(
                        str(time.time() + min(max(float(retry_after), 1.0), 120.0)),
                        encoding="ascii",
                    )
                except OSError:
                    LOGGER.debug("Could not persist model catalog cooldown", exc_info=True)
            self.model_catalog["notice"] = (
                MODEL_STALE_NOTICE if self.model_catalog.get("models")
                else MODEL_UNAVAILABLE_NOTICE
            )
            return self.model_catalog
        try:
            cooldown_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.debug("Could not clear model catalog cooldown", exc_info=True)
        self.model_catalog = payload.get("catalog") or _empty_model_view()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.model_catalog, ensure_ascii=False), encoding="utf-8")
            temporary.replace(cache_path)
        except OSError:
            LOGGER.debug("Could not persist model catalog cache", exc_info=True)
        self._reconcile_model_choice()
        self._adopt_pricing_version()
        return self.model_catalog

    def _adopt_pricing_version(self) -> None:
        """把服务端当前的计费策略版本记下来，下次提交时带上。

        记下来的只是"商户看过的**那一版**"，不代表商户已经同意按它扣费；
        提交后如果版本对不上，服务端会要求重新确认。
        """
        version = str((self.model_catalog or {}).get("version") or "")
        service = self._parse_service()
        if version and service is not None:
            service.pricing_version = version

    def _parse_service(self) -> Any:
        """拿到真正负责提交的那个解析服务（界面与向导共用一个）。"""
        for holder in (self.service_factory_result(), getattr(self, "wizard", None)):
            service = getattr(holder, "parse_service", None) or getattr(holder, "service", None)
            if service is not None:
                return service
        pipeline = getattr(self, "pipeline", None)
        return getattr(pipeline, "parse_service", None) if pipeline is not None else None

    def service_factory_result(self) -> Any:
        return getattr(self, "_service", None)

    def confirm_pricing(self) -> dict[str, Any]:
        """商户点了"确认新价格"之后才走这里。"""
        service = self._parse_service()
        return service.confirm_pricing() if service is not None else {}

    def pending_pricing(self) -> dict[str, Any]:
        service = self._parse_service()
        return service.pending_pricing() if service is not None else {}

    def pricing_confirmation_text(self) -> str:
        service = self._parse_service()
        return service.pricing_confirmation_text() if service is not None else ""

    def _reconcile_model_choice(self) -> None:
        """记住客户选择；失效时回落到默认免费模型并说明。"""
        catalog = self.model_catalog or {}
        available = {str(item.get("alias")) for item in catalog.get("models") or []}
        default_alias = str(catalog.get("default_alias") or "")
        # JYI 目录不是产品计费目录，没有 default_alias/free_available；
        # 仍应允许使用者选择上游返回的模型。优先用客户端默认值，否则取首项。
        if not default_alias and self.client is not None and callable(getattr(self.client, "text_model", None)):
            try:
                candidate = str(self.client.text_model() or "")
                default_alias = candidate if candidate in available else ""
            except Exception:
                default_alias = ""
        if not default_alias and available and self.client is not None and callable(getattr(self.client, "text_model", None)):
            default_alias = next(
                (str(item.get("alias")) for item in catalog.get("models") or []
                 if str(item.get("alias") or "") in available),
                "",
            )
        chosen = self.settings.model_alias
        if chosen and chosen in available:
            return
        if default_alias and default_alias in available:
            if chosen:
                self._log(f"上次使用的模型已不可用，已改为「{self._label_of(default_alias)}」")
            self.settings.model_alias = default_alias
            self.settings_store.save(self.settings)
            return
        # 没有合格免费模型：保持未选择，由界面如实显示「暂不可用」。
        self.settings.model_alias = ""
        self.settings_store.save(self.settings)

    def _label_of(self, alias: str) -> str:
        for item in (self.model_catalog or {}).get("models") or []:
            if str(item.get("alias")) == alias:
                return str(item.get("display_name") or alias)
        return alias

    def selected_model(self) -> dict[str, Any]:
        """当前选中的模型卡片。"""
        alias = self.settings.model_alias
        for item in (self.model_catalog or {}).get("models") or []:
            if str(item.get("alias")) == alias:
                return item
        return {}

    def set_model(self, alias: str) -> bool:
        """切换模型。

        **只影响新任务**：已提交的任务在服务端冻结了自己的模型快照，
        这里改设置不会让正在跑的任务换模型。历史订单和客户记忆也不受影响。
        """
        available = {str(item.get("alias")) for item in (self.model_catalog or {}).get("models") or []}
        if alias not in available:
            return False
        self.settings.model_alias = alias
        self.settings_store.save(self.settings)
        service = self._parse_service()
        if service is not None and hasattr(service, "model_alias"):
            service.model_alias = alias
        self.refresh_home()
        return True

    def model_notice(self) -> str:
        """给商户看的提示。技术细节不往这里放。

        **没登录时必须说"没登录"，不能说"模型不可用"。**
        未登录压根没去取过模型列表（:meth:`load_models` 直接返回空视图），
        报成"免费模型暂不可用"是**归错了因**：商户会去反复点「刷新模型列表」，
        刷多少次都不会好——因为问题根本不在模型上。
        """
        if self.client is None:
            return (
                "还没有登录产品账号，所以还没有可选的分析模型。"
                "请先点右上角「账号：未登录　点此登录」。"
            )
        catalog = self.model_catalog or {}
        if catalog.get("stale"):
            return str(catalog.get("notice") or "模型列表不是最新状态")
        if catalog.get("models") and self.client is not None and callable(getattr(self.client, "text_model", None)):
            return str(catalog.get("notice") or "")
        if not catalog.get("free_available"):
            # 注意：产品里**没有免费模型**。以前这里写「免费模型暂不可用」，
            # 商户看到会去反复刷新模型列表 —— 问题根本不在模型上。
            # 说人话：分析还没开通，或暂时没有可用模型。
            return ("暂时没有可用于分析消息的模型。已读取的消息和待处理任务"
                    "都保留着，可稍后重试或联系客服。")
        return ""

    #: 换模型重新分析的入口开关。
    #:
    #: 差异流程（差异比较 → 版本校验 → 应用）已经在
    #: ``OrderStore.start_reanalysis`` / ``apply_reanalysis_suggestion`` 里
    #: 实现并有测试覆盖，但它**还没有端到端跑过一次真实模型**，所以界面上
    #: 保持关闭。开着它等于收一笔费然后告诉商户"已处理过"—— 那比没有这个
    #: 功能更糟。等真实链路验过再把它打开。
    REANALYSIS_ENABLED = False

    def reanalysis_notice(self) -> str:
        """换模型重新分析为什么现在不能点。"""
        if self.REANALYSIS_ENABLED:
            return ""
        return (
            "换模型重新分析暂时不可用：它会真实调用模型并消耗分析次数，"
            "而差异对比流程还没有在真实链路上验过。已有订单查看和核对不受影响。"
        )

    def pending_reanalysis_count(self) -> int:
        """等待商户决定要不要应用的差异建议条数。"""
        try:
            return len(self.orders.pending_reanalysis_suggestions(self.tenant))
        except Exception:
            return 0

    def free_quota_remaining(self) -> int:
        """免费额度剩余。免费档位已焊死，恒为 0。

        保留这个方法是为了让调用方不必改 —— 但它**永远返回 0**，
        以后有人拿它做判断会得到"没有免费额度"的正确答案。
        """
        return 0

    def _log(self, text: str) -> None:
        if self.events.on_log:
            try:
                self.events.on_log(text)
            except Exception:
                pass

    # ------------------------------------------------------------- 首页

    def home(self) -> HomeView:
        view = HomeView()
        status = self.monitor.latest
        if status.state == WeChatState.UNREADABLE and status.detail == "尚未检测":
            status = self.monitor.check_once()
        view.wechat_state = status.state
        view.wechat_detail = status.copy()[1]
        view.wechat_ready = status.ready
        view.simulated = status.simulated or self.simulated
        # 消息是不是真的、和订单是不是模型分析的，是两件事。首页以前只有
        # 一个 ``simulated``，于是"真实微信 + 规则引擎"这种组合会被说成
        # "演示数据"—— 消息明明是真的。
        view.model_executed = bool(getattr(self.parse_service, "model_executed", False))
        try:
            view.open_merge_groups = int(self.merger.stats().get("open_groups", 0))
        except Exception:
            # 合并库不可用时不该拖垮首页：没有这一项只是少说一句话。
            LOGGER.debug("merge stats unavailable", exc_info=True)
            view.open_merge_groups = 0

        live = self.live_conversation()
        view.conversation = live.disambiguation if live else "未选择会话"
        if live and live.name in self.wizard.conversations.has_ambiguous_names():
            view.conversation_note = "存在同名会话，已按人数区分，请确认选择的是正确的群"
        elif live and not live.member_count and live.kind == ConversationKind.GROUP:
            view.conversation_note = "未能读到群人数，执行前请再次确认"

        view.mode = self.auto.mode
        view.mode_note = self.auto.describe()
        view.send_block_reason = self._send_block_reason()
        view.can_send = not view.send_block_reason

        view.effective = self.dashboard.effective_orders()
        view.actionable = self.dashboard.actionable_orders()
        view.auto_recorded = self.dashboard.auto_recorded_count()
        view.stock = self.dashboard.stock_summary(view.effective)
        view.payment_claims = self.dashboard.payment_claims()
        # 独立澄清任务单独一栏：它不是订单，不进订单数、不进备货。
        view.clarifications = self.dashboard.clarifications()
        # 系统解析失败也是单独一栏：不是订单，也不会去问客户。
        view.system_failures = self.dashboard.system_failures()
        view.parse_failures = self.dashboard.parse_failures()
        view.scope_text = self.dashboard.scope_text()

        quota = self.wizard.quota
        view.quota_headline = quota.headline()
        view.quota_ok = quota.sufficient

        view.pending_receipts = self.outbox.pending_count()
        view.unknown_receipts = self.outbox.unknown_count()
        view.failed_receipts = self.outbox.failed_count()
        # 发送异常 = 没发出去的 + 结果无法确认的。两类都需要商户知道。
        view.send_errors = view.unknown_receipts + view.failed_receipts
        if self.history_result is not None:
            view.last_actual_range = self.history_result.actual_range_text()
        return view

    def _send_block_reason(self) -> str:
        """为什么现在发不出去。空字符串表示可以发。

        真实发送在真机验证前是关着的，所以这一栏**永远**要有话说 ——
        否则商户看到「可发送」却发现什么都没发出去。
        """
        if not real_sending_available():
            return "真实发送尚未在真机验证，目前只整理不发送"
        if self.auto.mode == RunMode.PAUSED:
            return "已暂停发送"
        if not self.auto.allow_local_automation:
            return "尚未允许本机自动操作"
        if not self.auto.auto_reply:
            return "已关闭自动回复"
        if self.target_inspector is None:
            return "尚未配置发送前检查器"
        return ""

    @property
    def history_result(self) -> HistoryResult | None:
        return self.wizard.history_result

    def refresh_home(self) -> HomeView:
        view = self.home()
        self._last_home = view
        if self.events.on_home:
            try:
                self.events.on_home(view)
            except Exception:
                pass
        return view

    def orders_for_display(self) -> list[OrderRow]:
        """界面上「查看订单」列出的内容：有效订单 + 需要处理的。"""
        actionable = {item.order_id for item in self.dashboard.actionable_orders()}
        rows = self.dashboard.effective_orders()
        return [item for item in rows if item.status in ACTIONABLE_STATUSES or item.order_id in actionable]

    # ------------------------------------------------------------ internals

    def _error(self, message: str) -> None:
        if self.events.on_error:
            try:
                self.events.on_error(message)
            except Exception:
                pass

    @staticmethod
    def _default_history_reader(**kwargs: Any) -> list[dict[str, Any]]:
        from .WeChatAuto import Messages

        return list(Messages.dump_chat_history(**kwargs) or [])

    @staticmethod
    def _record_group_alias(self, wxid: str, name: str) -> None:
        """读历史时顺手记群名：面板标题里的群名 → 备注映射。

        内存里解不出名的群（资料页没进内存），靠这次翻到的标题补齐。
        已有备注不覆盖；写盘失败只留日志，不挡读取。
        """
        self.set_alias(wxid, name)

    def _default_group_members(conversation: Any) -> list[str]:
        """群成员的昵称列表。读取群聊历史时用来认出发送人。

        ## 为什么必须传

        不传的话读取器靠「两次遍历对比」反推发送人，对不上时会把发送人写成
        「红包或转账(无法获取发送人)」这个固定占位串 —— 商户拿到一堆
        不知道是谁说的话。传了成员就**直接解析**，不依赖那次对比。
        不传的话读取器靠「两次遍历对比」反推发送人，对不上时会把发送人写成
        「红包或转账(无法获取发送人)」这个固定占位串 —— 商户拿到一堆
        不知道是谁说的话。传了成员就**直接解析**，不依赖那次对比。

        ## 失败不抛

        取成员要打开微信界面，可能超时、可能失败。**取不到只是发送人认不出来，
        消息本身还能读** —— 为此中断整次读取是拿小的换大的。
        """
        try:
            from .WeChatAuto import Contacts

            return [str(name) for name in (Contacts.get_groupMembers_info(
                conversation.name, is_maximize=False, close_weixin=False) or []) if name]
        except Exception as exc:
            # **不打堆栈。** 「取不到群成员」是常见情况，不是崩溃：
            # 微信没开、群名对不上、界面正忙 —— 都会走到这里。
            # 打完整堆栈会让真正的故障淹没在噪声里。
            #
            # 但也不能不出声：这次读取的发送人会认不出来，排查时要知道原因。
            LOGGER.warning("取不到群成员，这次读取的发送人可能认不出来：%s（%s: %s）",
                           getattr(conversation, "name", ""), type(exc).__name__, exc)
            return []

    def close(self) -> None:
        self.stop()
        self.outbox.close()
        self.cursor.close()


__all__ = ["AppEvents", "MerchantApplication", "POLL_INTERVAL_SECONDS"]


def _empty_model_view() -> dict[str, Any]:
    """空目录视图。**没有目录就说没有**，不用固定模型顶替。"""
    return {
        "version": "",
        "default_alias": "",
        "free_available": False,
        "stale": False,
        "notice": "",
        "models": [],
        "free_quota": {"daily_limit": 0, "used": 0, "reserved": 0, "remaining": 0},
    }
