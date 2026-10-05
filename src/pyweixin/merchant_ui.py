"""商家本地界面（Tkinter）。

界面只做两件事：把 :class:`~pyweixin.application.MerchantApplication` 的状态
画出来，把用户的点击转成对它的调用。**界面里没有任何业务判断** —— 这样
"界面上能走通"和"自动化测试能验证"是同一件事。

设计原则：

* 全部使用简单中文。出现 API Key、token、模型、服务器等技术词即视为回归
  （:func:`audit_text` 会检查）。
* 首页回答商户最关心的问题，而不是把「待确认列表」当唯一主界面。
* 长时间操作（读历史、查询额度、智能整理）全部在后台线程，界面不卡。
* 关闭窗口时停止所有后台线程，不留孤儿。
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .application import LISTEN_FAIR_NOTE, AppEvents, MerchantApplication
from .conversations import Conversation, ConversationKind, search
from .dashboard import METRIC_DEFINITIONS, STATUS_LABELS, HomeView
from .history_read import HistoryProgress, HistoryRange, ReadMode
from .live_listen import (
    LISTEN_DEGRADED,
    LISTEN_RUNNING,
    LISTEN_STARTING,
    LISTEN_STOPPED,
)
from .runmode import RunMode
from . import summary_task
from .wechat_state import STATE_COPY, WeChatState
from .wizard import WizardStep

LOGGER = logging.getLogger("pyweixin.ui")

POLL_INTERVAL_MS = 3000

#: 商家界面上允许出现的全部技术词，出现即视为回归。
FORBIDDEN_TERMS = ("token", "apikey", "api key", "provider", "session", "bearer", "opencode", "model", "sqlite")

#: 摘要最多送多少条消息给模型。
#: 模型上下文和钱都有限，不能把整个群的历史全塞进去。
#: 超出时取**最近**的，并在界面上说明。
SUMMARY_MESSAGE_LIMIT = summary_task.SUMMARY_MESSAGE_LIMIT

#: 表格一列最多显示多少字。超出的部分用省略号收尾 —— 不加省略号的话，
#: 商户看到的是一句**被拦腰切断**的话，看不出订单到底写了什么。
#:
#: 这个数字不是随手定的。用真实群里的消息量过：客户常把问题写在句子后半段，
#: 28 字会把「修改后对之前的订单数据有影响吗？」切成「…有影…」—— 问题本身
#: 正好被吃掉。40 字能盖住大多数一句完整的诉求。
CELL_LIMIT = 40


def _cell(text: Any, limit: int = CELL_LIMIT) -> str:
    """表格里的一格。

    真实群里的订单摘要常常很长（地址、备注、多行内容）。表格宽度固定，
    tk 会直接切掉后面的字，而且**不留任何痕迹** —— 商户看到半句话，
    会以为订单就这么长。这里主动截断并加省略号，让"还有内容"是看得见的。

    被截掉的原文**不会丢**：双击那一行可以看到全文。
    """
    value = str(text if text is not None else "").replace("\n", " ").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "…"

#: 界面上不承诺的事，必须如实告诉商户。
DISCOVERY_NOTE = (
    "这里只列出微信当前能看到的最近会话，不等于您的全部联系人和全部历史。"
    "想读更早的消息，请在微信里先往上翻一翻再读取。"
)

#: 会话列表的显示行数。跟着内容走，不给 2 行数据也占 12 行高度。
CONV_ROWS_MIN = 4
CONV_ROWS_MAX = 12


def conv_rows_height(count: int) -> int:
    """会话列表该显示几行。

    只有 2 个群却占着 12 行的高度，页面下半截全是空白，看着像坏了。
    """
    return max(CONV_ROWS_MIN, min(CONV_ROWS_MAX, int(count)))


class MerchantWindow:
    """主窗口。向导与首页共用一个窗口。"""

    def __init__(self, app: MerchantApplication):
        self.app = app
        self._events: queue.Queue = queue.Queue()
        self._closing = False
        self._conversations: list[Conversation] = []
        #: 会话页两栏各自"当前可见"的会话。Treeview 的 iid 就是它在所属那一份里的
        #: 下标，所以取回会话时必须查同一份，不能查全量列表。
        self._conv_visible: dict[str, list[Conversation]] = {}
        self._history_rows: dict[str, bool] = {}
        self._live_choice: Conversation | None = None
        self._widgets: dict[str, Any] = {}
        #: 最近一次整理结果。页面被导航重建后还能补画，不会白跑一次分析。
        self._last_summary: dict[str, Any] = {}
        #: 最近一次首页数据。同上，重建后补画。
        self._last_home: HomeView | None = None
        self._home_frame: Any = None
        #: 最近一次监听的视图。
        self._last_listen: dict[str, Any] | None = None
        #: 最近一次读取结果。跨页面重建存活 —— 否则商户从别处返回第 3 步，
        #: 会看到一句写死的「准备中」，明明早就读完了。界面不能说假话。
        self._last_read_result: Any = None
        #: 汇总卡。已经在整理中：重复点会花两次钱。
        self._summary_running = False
        self._summary_bar: Any = None
        self._summary_elapsed: Any = None
        self._summary_started: float = 0.0
        self._summary_elapsed_label: Any = None
        self._summary_text: Any = None
        self._summary_status: Any = None

        app.events = AppEvents(
            on_home=lambda view: self._post("home", view=view),
            on_step=lambda step: self._post("goto", step=step),
            on_log=lambda text: self._post("log", text=text),
            on_history_progress=lambda progress: self._post("progress", progress=progress),
            on_history_done=lambda result: self._post("history_done", result=result),
            on_merged=lambda info: self._post("merged", info=info),
            on_conversations=lambda items: self._post("conversations", conversations=items),
            on_wechat=lambda status: self._post("wechat", status=status),
            on_listen=lambda view: self._post("listen", view=view),
            on_error=lambda text: self._post("log", text=text),
        )
        # 向导在构造时拷贝过一遍回调，那时界面的 events 还是空的。绑定完必须
        # 回填一次，否则状态变化永远传不回界面。
        app.wizard.bind_callbacks(
            on_step=app.events.on_step,
            on_log=app.events.on_log,
            on_wechat=app.events.on_wechat,
            on_history_done=app.events.on_history_done,
        )

        self.root = tk.Tk()
        # 变量必须在 Tk 根窗口之后创建，否则 tk 会报"no default root window"。
        self._model_var = tk.StringVar()
        self.root.title("微信群接单助手")
        self.root.geometry("900x680")
        # 最小高度必须容得下首页**最下面那排控件**。Tk 不会滚动：窗口给不够
        # 高度时，最后排的「开始持续监听」「查看订单」根本不会被布局出来 ——
        # 不是被切掉半截，是**在屏幕上任何位置都找不到**。实测：
        #
        #   760x560 → 开始持续监听、监听区块、查看订单 全部消失
        #   800x620 → 开始持续监听 消失
        #
        # 商户把窗口拖小一点就永久失去这两个功能。所以最小高度不低于
        # 默认高度：要缩小可以，要缩到功能消失就不行。
        self.root.minsize(760, 680)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain)
        self.app.start_background()
        self._show_step(self.app.wizard.step)
        self.root.after(400, self._tick)

    # ------------------------------------------------------------------ ui

    def _build(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:  # pragma: no cover - platform dependent
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei", 17, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei", 11, "bold"))
        # 主按钮必须一眼能认出来。``vista`` 主题不吃 background/foreground，
        # 所以靠**加粗 + 更宽的留白 + 一圈描边**做层级，而不是靠颜色。
        style.configure(
            "Big.TButton",
            padding=(14, 10),
            font=("Microsoft YaHei", 10, "bold"),
            borderwidth=2,
            relief="solid",
        )
        style.configure("Pause.TButton", font=("Microsoft YaHei", 11, "bold"), padding=(14, 10))
        # 次要操作（刷新、设置）。第 4 屏有六个按钮，主操作只有一个 ——
        # 维护按钮和主按钮长得一样，商户分析不出来时会挨个乱点。用细边框、
        # 窄留白把它们压下去，主按钮才是这一屏唯一显眼的东西。
        style.configure(
            "Subtle.TButton",
            padding=(8, 5),
            borderwidth=1,
            relief="solid",
            font=("Microsoft YaHei", 9),
        )

        top = ttk.Frame(self.root, padding=(18, 14))
        top.pack(fill="x")
        ttk.Label(top, text="微信群接单助手", style="Title.TLabel").pack(side="left")
        # 账号条做成**按钮**：登录是所有事情的前提，而入口以前只在第 4 步，
        # 顶栏写着「账号：未登录」却点不动 —— 商户看得到状态，动不了。
        # 登录后这里也要能退出：以前能登录但**永远登不出去**，收银电脑换人
        # 就登不进，本地数据还跟着上一位走。
        self._widgets["account"] = ttk.Button(
            top, text="账号：未登录　点此登录", style="Subtle.TButton",
            command=self._on_account_click,
        )
        self._widgets["account"].pack(side="right")

        self.container = ttk.Frame(self.root, padding=(0, 0, 0, 6))
        self.container.pack(fill="both", expand=True)

        self._widgets["log"] = ttk.Label(self.root, text="", foreground="#7b8794", padding=(18, 2))
        self._widgets["log"].pack(fill="x")

    # ------------------------------------------------------------- 事件泵

    def _post(self, kind: str, **payload: Any) -> None:
        if not self._closing:
            self._events.put((kind, payload))

    def _drain(self) -> None:
        if self._closing:
            return
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            handler = getattr(self, f"_on_{kind}", None)
            if not handler:
                continue
            try:
                handler(**payload)
            except Exception:
                # 一个处理器出错不能连累后面排队的事件。页面上有过期的控件引用
                # 时会抛 TclError，不拦住的话后面所有刷新都跟着丢。
                LOGGER.exception("ui handler failed: %s", kind)
        self.root.after(100, self._drain)

    def _on_home(self, view: HomeView) -> None:
        # 先存住：页面随时可能被导航重建，重建后还能补画。
        self._last_home = view
        if self.app.wizard.step == WizardStep.DONE and hasattr(self, "_home_frame"):
            try:
                self._render_home(view)
            except Exception:
                # 首页正在重建：数据已存进 _last_home，_page_home 会补画。
                LOGGER.debug("home render skipped: page is being rebuilt", exc_info=True)

    def _on_conversations(self, conversations: list[Conversation]) -> None:
        self._conversations = conversations
        self._history_rows = {}
        self._on_redraw_conversations()

    def _on_wechat(self, status: Any) -> None:
        """把新的微信状态画到界面上。

        「重新检测」跑完之后靠这里刷新。以前第 1 页的文案只在**建页**时写一次，
        检测在后台线程里跑完了也没人重画 —— 关掉微信再点按钮，界面纹丝不动。

        事件是**排队**处理的，页面可能已经被重建过：拿旧控件去 ``configure``
        会抛 ``TclError``，还会把整条 drain 打断，后面的事件全丢。

        控件已经不在了就**什么都不做**：重建它的那次 ``goto`` 本来就是按当时的
        权威状态画的，这里再抢着重画一次反而会把商户从别的步骤拽回第 1 步。
        """
        title, detail, button = status.copy()
        title_label = self._widgets.get("wechat_title")
        if title_label is None or not title_label.winfo_exists():
            return
        title_label.configure(text=title)
        detail_label = self._widgets.get("wechat_detail")
        if detail_label is not None and detail_label.winfo_exists():
            detail_label.configure(text=detail)
        # 状态决定"打开微信"这个按钮该不该出现，按钮本身不重建，只改显隐。
        start = self._widgets.get("wechat_start")
        if start is not None and start.winfo_exists():
            if button:
                start.configure(text=button)
                if not start.winfo_ismapped():
                    start.pack(anchor="w")
            elif start.winfo_ismapped():
                start.pack_forget()

    def _on_history_done(self, result: Any) -> None:
        self._on_read_finished(result)

    def _on_merged(self, info: Any) -> None:
        """短消息合并了。商户必须看得见 —— 他发了三条只得到一张订单时，
        不知道是系统合的就会以为漏单，然后重发一遍。"""
        if not isinstance(info, dict) or int(info.get("count", 0)) <= 1:
            return
        self._last_merged_count = int(info["count"])
        self._render_listen()

    def _on_error(self, text: str) -> None:
        """后台任务失败。必须留在原地并说清楚，不能让商户一脸茫然。

        消息要**在当前这一屏**显示出来。不同屏有不同的落点：
        读取页有 ``read_state``，整理页有 ``analyze``。以前只写 ``read_state``，
        于是商户在第 4 步点「开始智能整理」而失败时，两条路都走不通 ——
        按钮亮着、点了没反应、也没有任何解释。这是最糟的一种摩擦。
        """
        self._set_reading(False)
        shown = False
        for key in ("read_state", "analyze_notice"):
            widget = self._widgets.get(key)
            if widget is not None and self._alive(widget):
                widget.configure(text=str(text), foreground="#a8590b")
                shown = True
                break
        if not shown:
            # 当前这屏没有专门的提示位。**仍然要让商户看见** ——
            # 弹窗比静默好，哪怕打断流程。
            messagebox.showwarning("没能完成", str(text))
        try:
            self._on_log(text)
        except Exception:
            # log 只在向导页有。第 4 步没有它，不该因此把错误也一起丢掉。
            LOGGER.debug("no log widget on this page", exc_info=True)

    def _on_log(self, text: str) -> None:
        widget = self._widgets.get("log")
        if widget is not None and self._alive(widget):
            widget.configure(text=text)

    @staticmethod
    def _data_origin_note(view: Any) -> str:
        """一句话说清屏幕上的东西**从哪来**。

        两件独立的事实，各说各的：

        * 消息是不是真的读到的（演示数据源 vs 真实微信）
        * 订单是不是模型分析出来的（真模型 vs 固定规则）

        都不成立时**什么都不显示** —— 正常使用时不必一直挂着一句警告，
        那会让真正的警告失去分量。
        """
        notes = []
        if getattr(view, "simulated", False):
            notes.append("读到的消息来自演示，不是真实微信里的内容")
        if not getattr(view, "model_executed", False):
            notes.append("订单不是模型分析出来的，是按固定规则整理的")
        if not notes:
            return ""
        return "数据来源：" + "；".join(notes)

    def _on_goto(self, step: str) -> None:
        self._show_step(step)

    def _on_progress(self, progress: HistoryProgress) -> None:
        bar = self._widgets.get("progress")
        if bar is None:
            return
        if progress.bounded:
            if str(bar.cget("mode")) == "indeterminate":
                bar.stop()
            bar.configure(mode="determinate", value=progress.fraction * 100)
        else:
            # 读全部时微信不告诉总条数，画一个会走完的进度条就是骗人。
            bar.configure(mode="indeterminate")
            bar.start(15)
        label = self._widgets.get("progress_text")
        if label is not None:
            label.configure(text=progress.summary())

    # ------------------------------------------------------------- 页面切换

    def _show_step(self, step: str) -> None:
        for child in self.container.winfo_children():
            child.destroy()
        for key, _title, _kind in self.CONV_TABS:
            self._widgets.pop(f"conv_tree_{key}", None)
        self._widgets.pop("conv_notebook", None)
        self._conv_visible.clear()
        self._widgets.pop("progress", None)
        self._widgets.pop("progress_text", None)
        # 首页离开时控件就没了。别拿旧控件 configure —— 会抛 TclError 并打断整个
        # 事件泵，后面排队的事件全丢。
        self._home_frame = None
        for key in ("listen_btn", "listen_state", "listen_stats", "listen_gaps", "listen_note"):
            self._widgets.pop(key, None)
        for key in ("wechat_title", "wechat_detail", "wechat_start", "wechat_recheck",
                    "login_start", "login_switch"):
            self._widgets.pop(key, None)
        for key in ("clar_box", "fail_box", "home_after_tables", "clar_list", "fail_list",
                    "st_origin", "listen_merged"):
            self._widgets.pop(key, None)
        for key in ("read_start", "read_cancel", "read_next", "read_state",
                    "read_empty_exit",
                    "read_mode", "ranged_box", "all_note",
                    "read_summary", "read_summary_tree",
                    "analyze_button", "quota_refresh", "model_refresh",
                    "quota_bar", "quota_percent", "quota_login",
                    "analyze_notice"):
            self._widgets.pop(key, None)
        builder = {
            WizardStep.WECHAT: self._page_wechat,
            WizardStep.CONVERSATIONS: self._page_conversations,
            WizardStep.HISTORY: self._page_history,
            WizardStep.QUOTA: self._page_analyze,
            WizardStep.ANALYZE: self._page_analyze,
            WizardStep.DONE: self._page_home,
        }.get(step, self._page_wechat)
        builder()

    # --------------------------------------------------------------- ① 微信

    def _page_wechat(self) -> None:
        frame = ttk.Frame(self.container, padding=(18, 6))
        frame.pack(fill="both", expand=True)
        status = self.app.monitor.latest
        title, detail, button = status.copy()
        if status.state == WeChatState.UNREADABLE and status.detail == "尚未检测":
            self._spawn("wechat-check", lambda: self.app.check_wechat())
            title, detail, button = STATE_COPY[WeChatState.UNREADABLE]
        ttk.Label(frame, text="第 1 步　登录并检查微信", style="Section.TLabel").pack(
            anchor="w", pady=(4, 12))

        # ---- 登录放在这一步的最上面 ----
        #
        # 以前登录入口在第 4 步，商户要一路走到"智能整理"才发现登不上，
        # 而那一屏偏偏又显示成"免费模型暂不可用"——归错了因。
        #
        # 登录是"能不能整理"的前提，不是最后一步的事。放最上面，
        # 一眼就知道还差什么。**不挡路**：检查微信、选会话、读历史都不需要登录，
        # 读到的数据留在本地、不扣额度，所以登录失败不该把人卡在原地。
        self._build_login_block(frame)

        ttk.Label(frame, text="", font=("Microsoft YaHei", 10, "bold")).pack(
            anchor="w", pady=(18, 8))
        self._widgets["wechat_title"] = ttk.Label(frame, text=title, font=("Microsoft YaHei", 13, "bold"))
        self._widgets["wechat_title"].pack(anchor="w")
        self._widgets["wechat_detail"] = ttk.Label(frame, text=detail, wraplength=620, foreground="#5a6572")
        self._widgets["wechat_detail"].pack(anchor="w", pady=(6, 14))
        # 两个按钮都挂在页面上、保持稳定：状态变化时只改文案和「打开微信」的
        # 显隐，不销毁重建。销毁重建会闪烁，而且别处按子控件找按钮时会找不到。
        self._widgets["wechat_start"] = ttk.Button(
            frame, text=button or "", style="Big.TButton", command=self._on_start_wechat
        )
        if button:
            self._widgets["wechat_start"].pack(anchor="w")
        self._widgets["wechat_recheck"] = ttk.Button(
            frame, text="重新检测", command=lambda: self._spawn("wechat-check", lambda: self.app.check_wechat())
        )
        self._widgets["wechat_recheck"].pack(anchor="w", pady=(10, 0))
        note = ttk.Label(
            frame,
            text="提示：软件只是被动查看微信状态，不会自己把微信窗口弹到最前面，也不会动您正在输入的内容。",
            foreground="#7b8794", wraplength=620,
        )
        note.pack(anchor="w", pady=(24, 0))

    def _build_login_block(self, parent: Any) -> None:
        """第 1 步顶部的登录区块。"""
        box = ttk.LabelFrame(parent, text="产品账号", padding=10)
        box.pack(fill="x")
        self._login_box = box
        self._render_login(box)

    def _on_start_wechat(self) -> None:
        if self.app.start_wechat():
            self.app.watch_wechat()
            self._widgets["wechat_title"].configure(text="已打开微信，正在等待您登录…")
        else:
            messagebox.showerror("打不开微信", "没有找到电脑版微信，请先安装后再试。")

    # --------------------------------------------------------------- ② 会话

    #: 会话分成两栏：商户做的是群里的生意，群聊必须一眼看全；联系人动辄几百个，
    #: 混在一起会把群挤到看不见的地方。真机上实测是 10 个群对 246 个联系人。
    CONV_TABS = (
        ("groups", "群聊", ConversationKind.GROUP),
        ("friends", "联系人", ConversationKind.FRIEND),
    )

    def _page_conversations(self) -> None:
        frame = ttk.Frame(self.container, padding=(18, 6))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="第 2 步　选择会话", style="Section.TLabel").pack(anchor="w", pady=(4, 6))
        ttk.Label(frame, text=DISCOVERY_NOTE, foreground="#7b8794", wraplength=700).pack(anchor="w", pady=(0, 8))

        search_row = ttk.Frame(frame)
        search_row.pack(fill="x", pady=(0, 8))
        self._widgets["conv_search"] = tk.StringVar()
        entry = ttk.Entry(search_row, textvariable=self._widgets["conv_search"], width=34)
        entry.pack(side="left")
        entry.bind("<KeyRelease>", lambda _e: self._on_redraw_conversations())
        ttk.Button(
            search_row, text="搜索", command=lambda: self._on_redraw_conversations()
        ).pack(side="left", padx=6)
        ttk.Button(
            search_row, text="刷新列表",
            command=lambda: self._spawn("refresh", self.app.refresh_conversations),
        ).pack(side="left")

        notebook = ttk.Notebook(frame)
        notebook.pack(fill="both", expand=True)
        self._widgets["conv_notebook"] = notebook
        for key, title, _kind in self.CONV_TABS:
            page = ttk.Frame(notebook, padding=(4, 6))
            notebook.add(page, text=title)
            columns = ("pick", "name", "members")
            tree = ttk.Treeview(page, columns=columns, show="headings", height=8, selectmode="extended")
            # 列名要说实话：这一列放的是人数，不是别的。以前叫「说明」，
            # 里面却写着「64 人」，商户得自己想那是什么。
            for column, heading, width, anchor in (
                ("pick", "已读", 56, "center"),
                ("name", "会话", 420, "w"),
                ("members", "人数", 90, "e"),
            ):
                tree.heading(column, text=heading)
                tree.column(column, width=width, anchor=anchor)
            tree.tag_configure("dup", foreground="#a8590b")
            tree.pack(fill="both", expand=True)
            self._conv_visible[key] = []
            self._widgets[f"conv_tree_{key}"] = tree

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="下一步：读取历史", style="Big.TButton", command=self._on_history_next).pack(side="left")

        self._spawn("refresh", self.app.refresh_conversations)

    def _split_visible(self) -> dict[str, list[Conversation]]:
        """按当前搜索词把会话分成两栏。"""
        # 注意别写成 self._widgets.get("conv_search", tk.StringVar()) ——
        # dict.get 的第二个参数是**急切求值**的，key 存在也照样先造一个
        # StringVar，白白要求一个 Tk root。
        search_var = self._widgets.get("conv_search")
        found = search(self._conversations, search_var.get() if search_var is not None else "")
        buckets: dict[str, list[Conversation]] = {key: [] for key, _title, _kind in self.CONV_TABS}
        # 认不出来的会话（既不是群也不是好友）归到联系人栏，宁可放在一起也不要
        # 悄悄不见 —— 商户选中的东西不能凭空少一条。
        for item in found:
            bucket = next(
                (key for key, _title, kind in self.CONV_TABS if kind == item.kind), "friends"
            )
            buckets[bucket].append(item)
        return buckets

    def _on_redraw_conversations(self) -> None:
        trees = {
            key: self._widgets.get(f"conv_tree_{key}")
            for key, _title, _kind in self.CONV_TABS
        }
        if not any(trees.values()):
            return
        duplicates = self.app.wizard.conversations.has_ambiguous_names()
        buckets = self._split_visible()
        for key, tree in trees.items():
            if tree is None:
                continue
            rows = buckets[key]
            # iid 就是这一栏可见列表的下标，取的时候按同一份列表还原。
            self._conv_visible[key] = rows
            selected = set(tree.selection())
            tree.delete(*tree.get_children())
            # 表格高度跟着内容走。
            tree.configure(height=conv_rows_height(len(rows)))
            for index, item in enumerate(rows):
                iid = tree.insert(
                    "", "end", iid=str(index),
                    values=(
                        "✓" if self._history_rows.get(item.identity) else "",
                        item.name,
                        f"{item.member_count} 人" if item.member_count else "联系人",
                    ),
                    tags=("dup",) if item.name in duplicates else (),
                )
                if iid in selected:
                    tree.selection_add(iid)

        notebook = self._widgets.get("conv_notebook")
        if notebook is not None:
            for position, (key, title, _kind) in enumerate(self.CONV_TABS):
                notebook.tab(position, text=f"{title}（{len(buckets[key])}）")

    def _on_history_next(self) -> None:
        picked: list[Conversation] = []
        for key, _title, _kind in self.CONV_TABS:
            tree = self._widgets.get(f"conv_tree_{key}")
            if tree is None:
                continue
            # Treeview selection() 返回的是字符串 id。这里按**同一栏的可见列表**
            # 还原成会话对象 —— 早前 iid 是可见列表的下标、取的时候却按全量列表
            # 取，商户一搜索就选到别人头上。
            rows = self._conv_visible.get(key, [])
            for item_id in tree.selection():
                try:
                    index = int(item_id)
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(rows):
                    picked.append(rows[index])
        if not picked:
            messagebox.showinfo("还没选", "请先勾选要读取历史的会话。勾不上可以改用上面的搜索。")
            return
        self.app.choose_history_conversations(picked)
        self.app.choose_live_conversation(picked[0])
        self._show_step(WizardStep.HISTORY)

    # --------------------------------------------------------------- ③ 历史

    def _page_history(self) -> None:
        frame = ttk.Frame(self.container, padding=(18, 6))
        frame.pack(fill="both", expand=True)
        selected = self.app.wizard.conversations.history_selected
        ttk.Label(frame, text="第 3 步　读取历史消息", style="Section.TLabel").pack(anchor="w", pady=(4, 6))
        ttk.Label(
            frame,
            text="将读取：" + ("、".join(item.disambiguation for item in selected) or "未选择"),
            wraplength=700,
        ).pack(anchor="w", pady=(0, 10))

        self._widgets["read_mode"] = tk.StringVar(value=self._default_read_mode(selected))
        ttk.Label(frame, text="要读哪些消息：").pack(anchor="w")
        picker = ttk.Frame(frame)
        picker.pack(anchor="w", pady=(4, 10))
        for value, label in (
            (ReadMode.UPDATE, "更新历史消息"),
            (ReadMode.RECENT, "按时间范围读取"),
            (ReadMode.ALL, "读取全部历史"),
        ):
            ttk.Radiobutton(
                picker, text=label, value=value, variable=self._widgets["read_mode"],
                command=self._on_read_mode_change,
            ).pack(side="left", padx=(0, 14))

        # 只有"按时间范围"才需要时间范围和条数上限，另两种不需要。
        ranged = ttk.Frame(frame)
        ranged.pack(anchor="w", pady=(0, 10))
        self._widgets["ranged_box"] = ranged
        for value, label in (
            (HistoryRange.LAST_DAY, "最近一天"),
            (HistoryRange.LAST_WEEK, "最近七天"),
            (HistoryRange.CUSTOM, "自定义范围"),
        ):
            ttk.Radiobutton(ranged, text=label, value=value, variable=self._range_var()).pack(side="left", padx=(0, 14))
        custom = ttk.Frame(ranged)
        custom.pack(anchor="w", pady=(6, 0))
        ttk.Label(custom, text="自定义：").pack(side="left")
        self._widgets["days"] = tk.StringVar(value="30")
        ttk.Entry(custom, textvariable=self._widgets["days"], width=6).pack(side="left")
        ttk.Label(custom, text="天前　最多读取条数：").pack(side="left", padx=(8, 0))
        self._widgets["limit"] = tk.StringVar(value="200")
        ttk.Entry(custom, textvariable=self._widgets["limit"], width=7).pack(side="left")
        ttk.Label(custom, text="条").pack(side="left")

        # 微信不告诉调用方一共有多少条消息，所以"全部"没法画一个会走完的进度条。
        # 这里说清楚它会读到哪停、以及为什么可能要很久。
        self._widgets["all_note"] = ttk.Label(
            frame,
            text="会一直往回翻，翻到微信翻不出新的为止，可能需要很长时间。\n"
            "随时可以点「取消」，已读到的部分会保留。微信本身能提供多早的内容，"
            "它不会告诉你更多。",
            foreground="#a8590b", wraplength=700,
        )

        bar = ttk.Progressbar(frame, maximum=100)
        bar.pack(fill="x", pady=(6, 2))
        self._widgets["progress"] = bar
        self._widgets["progress_text"] = ttk.Label(frame, text="准备中", foreground="#7b8794")
        self._widgets["progress_text"].pack(anchor="w")
        # 读取过程中的状态。商户要知道"现在在干什么、读了多少、还要不要等"。
        self._widgets["read_state"] = ttk.Label(frame, text="", foreground="#7b8794", wraplength=700)
        self._widgets["read_state"].pack(anchor="w", pady=(2, 0))

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(12, 0))
        self._widgets["read_start"] = ttk.Button(
            actions, text="开始读取", style="Big.TButton", command=self._on_read_history
        )
        self._widgets["read_start"].pack(side="left")
        self._widgets["read_cancel"] = ttk.Button(
            actions, text="取消", command=self.app.cancel_history
        )
        self._widgets["read_cancel"].pack(side="left", padx=8)
        ttk.Button(actions, text="上一步", command=lambda: self._show_step(WizardStep.CONVERSATIONS)).pack(side="left")
        # 读完之后的去处。**显式按钮**，不靠"自动跳到下一页"这种无声行为 ——
        # 商户看不到自己被带到哪，也就没有"下一步"可点。默认收起，读到了才出现。
        self._widgets["read_next"] = ttk.Button(
            actions, text="下一步：智能整理", style="Big.TButton", command=self._go_next_from_history
        )
        # 一条都没读到时的出口。
        #
        # 以前这种情况把「下一步」藏起来，只剩「开始读取」和「上一步」。
        # 商户的反应是反复点读取 —— 而读不到多半不是他的操作问题，
        # 再点一百次还是 0 条。他其实是**卡住了**：没有一条能走的路。
        #
        # 已读到的消息和订单一个都没丢，所以他需要的是"去看已经有的东西"
        # 这条路，而不是再试一次。
        self._widgets["read_empty_exit"] = ttk.Button(
            actions, text="先看看已经读到的内容", style="Big.TButton",
            command=self._go_home_from_history,
        )

        note = ttk.Label(
            frame,
            text="读取历史时软件会短暂操作微信窗口，请先不要在电脑上做别的事。",
            foreground="#7b8794", wraplength=700,
        )
        note.pack(anchor="w", pady=(18, 0))

        # 读到了什么，**逐个会话**摊开。商户在决定要不要花额度去分析之前，
        # 要确认的是"该读的都读到了" —— 一句总数回答不了这个问题。
        # 这一块有内容才出现：没读之前摆一张空表只是占地方。
        summary = ttk.LabelFrame(frame, text="每个会话读到了什么", padding=8)
        self._widgets["read_summary"] = summary
        columns = ("name", "count", "range")
        tree = ttk.Treeview(summary, columns=columns, show="headings", height=5)
        for key, heading, width, anchor in (
            ("name", "会话", 300, "w"),
            ("count", "读到", 70, "e"),
            ("range", "时间范围", 280, "w"),
        ):
            tree.heading(key, text=heading)
            tree.column(key, width=width, anchor=anchor)
        tree.pack(fill="both", expand=True)
        self._widgets["read_summary_tree"] = tree

        self._on_read_mode_change()
        # 放在最后：它要控制 read_next 与 read_summary 的显隐，控件必须先建好。
        # 页面因导航重建时也会走到这里 —— 必须把**上次的结果**画回来，不能留
        # 一句写死的「准备中」。明明早就读完却显示"准备中"，那是界面在说假话。
        self._render_read_state(self._last_read_result or self.app.wizard.history_result)

    def _range_var(self) -> tk.StringVar:
        var = self._widgets.get("range")
        if var is None:
            var = tk.StringVar(value=HistoryRange.LAST_WEEK)
            self._widgets["range"] = var
        return var

    def _default_read_mode(self, selected) -> str:
        """已经读过就默认给"更新"，没读过才给按范围读。"""
        cursor = getattr(self.app.wizard.history, "cursor", None)
        tenant = getattr(self.app.wizard, "tenant", "")
        if cursor is not None and tenant and any(
            cursor.get(item.identity, tenant) is not None for item in selected
        ):
            return ReadMode.UPDATE
        return ReadMode.RECENT

    def _on_read_mode_change(self) -> None:
        """按时间范围才显示时间范围与条数上限。"""
        mode = self._widgets["read_mode"].get()
        ranged = self._widgets.get("ranged_box")
        if ranged is not None:
            ranged.pack_forget() if mode != ReadMode.RECENT else ranged.pack(
                anchor="w", pady=(0, 10), before=self._widgets["progress"]
            )
        all_note = self._widgets.get("all_note")
        if all_note is not None:
            if mode == ReadMode.ALL:
                all_note.pack(anchor="w", pady=(0, 10), before=self._widgets["progress"])
            else:
                all_note.pack_forget()

    def _on_read_history(self) -> None:
        self._set_reading(True)
        mode = self._widgets["read_mode"].get()
        kind = ""
        custom = None
        if mode == ReadMode.RECENT:
            kind = self._range_var().get()
            if kind == HistoryRange.CUSTOM:
                try:
                    days = int(self._widgets["days"].get() or 30)
                except ValueError:
                    messagebox.showerror("天数不对", "请填写一个大于 0 的天数。")
                    return
                custom = _custom_window(days)
        else:
            # 更新与全部都用"从最近一条往回翻"，时间范围和条数上限不适用。
            kind = HistoryRange.LAST_WEEK
        try:
            limit = int(self._widgets["limit"].get() or 200)
        except ValueError:
            limit = 200
        self._last_read_result = None
        self.app.read_history(range_kind=kind, custom=custom, max_messages=limit, mode=mode)

    def _set_reading(self, reading: bool) -> None:
        """读取中禁用会打架的控件，并写明当前在干什么。"""
        start = self._widgets.get("read_start")
        cancel = self._widgets.get("read_cancel")
        state = self._widgets.get("read_state")
        for widget, on in ((start, not reading), (cancel, reading)):
            if widget is not None and self._alive(widget):
                widget.configure(state="normal" if on else "disabled")
        if state is not None and self._alive(state):
            state.configure(text="正在读取，请不要在这台电脑上使用微信……" if reading else "")

    @staticmethod
    def _alive(widget: Any) -> bool:
        try:
            return bool(widget.winfo_exists())
        except Exception:
            return False

    def _on_read_finished(self, result: Any) -> None:
        """读取结束。**留在这一屏**，把结果和下一步说清楚。

        以前直接跳到下一页，商户既看不到读到了什么，也不知道下一步在哪 ——
        尤其读取失败时，界面会莫名其妙退回上一步且一言不发。
        """
        self._last_read_result = result
        self._set_reading(False)
        self._render_read_state(result)

    def _accumulated_coverage(self, result: Any) -> dict[str, tuple[int, str, str]]:
        """转发到共用逻辑。判据只有一份，两个界面都从这里取。"""
        return summary_task.accumulated_coverage(self, result)

    def _render_read_state(self, result: Any) -> None:
        """把读取结果画出来。页面重建后也会被调用，所以它必须是**幂等**的。"""
        state = self._widgets.get("read_state")
        text_widget = self._widgets.get("progress_text")
        bar = self._widgets.get("progress")
        nxt = self._widgets.get("read_next")

        got_messages = result is not None and result.total_messages > 0
        if bar is not None and self._alive(bar):
            if str(bar.cget("mode")) == "indeterminate":
                bar.stop()
            # 一条都没读到时**不**画成满格进度条。空跑一遍不是"完成"。
            bar.configure(mode="determinate", value=100 if got_messages else 0)
        if text_widget is not None and self._alive(text_widget):
            if result is None:
                text_widget.configure(text="准备中", foreground="#7b8794")
            elif got_messages:
                text_widget.configure(text="已完成", foreground="#5a6572")
            else:
                text_widget.configure(text="没有读到消息", foreground="#a8590b")
        if state is not None and self._alive(state):
            if result is None:
                state.configure(text="", foreground="#7b8794")
                # 读取还没出结果、或中途失败。这里**也**要有出路 ——
                # 失败之后停在原地、什么都没得点的商户，只会以为程序死了。
                self._show_empty_exit(False)
                return
            if result.total_messages == 0:
                # 「读到 0 条」和「读到 N 条但一条都没入库」是两回事。
                # 后者意味着**读到了却丢在门口**——商户会以为是自己的操作
                # 问题反复重来。两种情况界面必须说得不一样。
                #
                # 说明：截至目前真机上只观察到"确实读到 0 条"（同一个群
                # 出现过 7/35/0/0 的波动，原因尚未查明），还没抓到"读到 N 条
                # 却入库 0 条"的实例。这条分支是按**可能出现**来写的，
                # 不是按"已经发生过"来写的。
                read_anywhere = sum(
                    int(getattr(item, "message_count", 0) or 0)
                    for item in getattr(result, "coverage", ())
                )
                # 库里已经有多少。更新时「没有新消息」是**正常结果**，不是
                # 「没读到」。不分辨的话，商户刚读完一个有 500 条的群，再点
                # 一次更新就会看到「这次没有读到任何消息」，而且「下一步」
                # 还被藏了 —— 他会以为软件坏了。
                already_have = sum(
                    n for n, _, _ in self._accumulated_coverage(result).values()
                )
                if not read_anywhere and already_have and \
                        getattr(result, "mode", "") == ReadMode.UPDATE:
                    state.configure(
                        text=f"没有新消息。这些群里已经读到的 {already_have} 条"
                             "一条都没少，可以直接进行下一步，也可以继续等新消息。",
                        foreground="#5a6572",
                    )
                    if nxt is not None and self._alive(nxt):
                        nxt.pack(side="left", padx=(8, 0))
                    self._show_empty_exit(False)
                    # 明细表也要画出来：顶部说「没有新消息」而底下一片空白，
                    # 商户看不出这个群里其实有多少条。
                    self._render_coverage_rows(result)
                    return
                elif read_anywhere > 0:
                    state.configure(
                        text=f"刚才读到了 {read_anywhere} 条，但没有一条能入库，"
                             "本地订单里还是空的。请再点一次「开始读取」；"
                             "反复这样说明微信那边暂时读不稳，"
                             "可以改用「读取全部历史」再试。",
                        foreground="#a8590b",
                    )
                else:
                    state.configure(
                        text="这次没有读到任何消息。可以换个读法再试一次，"
                             "也可以先去看看已经读到的内容 —— 之前读到的一个都没丢。",
                        foreground="#a8590b",
                    )
                # 0 条时不给「下一步：智能整理」（那确实是死路），
                # 但**必须**给一条走得通的路，否则商户只能反复点「开始读取」。
                if nxt is not None and self._alive(nxt):
                    nxt.pack_forget()
                self._show_empty_exit(True)
                return
            parts = [result.headline()]
            # 更新模式的 headline 只有增量。补一行**累积总量**，让两种模式
            # 在这个位置回答的是同一个问题：这个群我现在有多少。
            if getattr(result, "mode", "") == ReadMode.UPDATE:
                accumulated = self._accumulated_coverage(result)
                totals = [n for n, _, _ in accumulated.values()]
                if totals:
                    lo = min((x for _, x, _ in accumulated.values() if x), default="")
                    hi = max((x for _, _, x in accumulated.values() if x), default="")
                    span = f"，时间范围 {lo[:10]} 至 {hi[:10]}" if lo and hi else ""
                    parts.append(f"这些群现在共 {sum(totals)} 条{span}")
            for gap in result.gaps:
                parts.append(f"注意：{gap}")
            if result.cancelled:
                parts.append("已读到的部分保留着，可以再点「开始读取」继续。")
            state.configure(text="\n".join(parts), foreground="#5a6572")
        if nxt is not None and self._alive(nxt):
            # 只有真的读到东西才给下一步；没读到还给按钮等于让商户点进死路。
            if got_messages:
                nxt.pack(side="left", padx=(8, 0))
            else:
                nxt.pack_forget()
            # 读到东西了就去看新内容，那个出口该让位。
            self._show_empty_exit(False)

        # 逐会话明细。没有结果时不摆这张空表 —— 空表头只是占地方。
        summary = self._widgets.get("read_summary")
        self._render_coverage_rows(result)
        if summary is not None and self._alive(summary):
            if result is not None and getattr(result, "coverage", None):
                summary.pack(fill="both", expand=True, pady=(14, 0))
            else:
                summary.pack_forget()

    def _render_coverage_rows(self, result: Any) -> None:
        """逐会话明细表。

        抽成方法是为了让「更新后没有新消息」那条分支也能画 —— 那条分支
        原来直接 ``return`` 了，表格空着，于是顶部说「没有新消息」而底下
        一片空白，商户看不出这个群里其实有多少条。
        """
        tree = self._widgets.get("read_summary_tree")
        if tree is None or not self._alive(tree):
            return
        tree.delete(*tree.get_children())
        # 列始终是**总量**，增量单独写在括号里。
        # 否则同一列在「读取全部」下是总量、在「更新」下是增量，
        # 商户没法判断这个群到底有多少条。
        accumulated = self._accumulated_coverage(result)
        for item in getattr(result, "coverage", ()) if result is not None else ():
            name = item.conversation.name
            total = accumulated.get(str(name))
            if total is not None:
                count_text = f"共 {total[0]} 条"
                if item.message_count:
                    count_text += f"（本次 +{item.message_count}）"
                earliest = total[1][:10] if total[1] else "—"
                latest = total[2][:10] if total[2] else "—"
            else:
                count_text = f"{item.message_count} 条"
                earliest = item.earliest.date().isoformat() if item.earliest else "—"
                latest = item.latest.date().isoformat() if item.latest else "—"
            note_text = "　（读失败）" if item.error else ("　（上次已读完）" if item.skipped else "")
            tree.insert(
                "", "end",
                values=(name, count_text, f"{earliest} 至 {latest}{note_text}"),
            )

    def _show_empty_exit(self, show: bool) -> None:
        """「先看看已经读到的内容」这个出口的显隐。

        没读到东西时，它是商户**唯一**走得通的路。把它藏起来，等于让人卡在
        这一屏只能反复点「开始读取」。
        """
        button = self._widgets.get("read_empty_exit")
        if button is None or not self._alive(button):
            return
        if show:
            button.pack(side="left", padx=(8, 0))
        else:
            button.pack_forget()


    def _has_messages_for(self, result: Any) -> bool:
        """放行的唯一判据（共用逻辑）。
        本早有两处各自判断——一处放行、一处拦回去，
        界面就自相矛盾了。"""
        return summary_task.has_messages_for(self, result)

    def _go_next_from_history(self) -> None:
        """「下一步：智能整理」按钮。真的没有消息时才拦 —— 那是死路。"""
        result = self._last_read_result or self.app.wizard.history_result
        if not self._has_messages_for(result):
            messagebox.showinfo("还没有读到消息", "请先点「开始读取」。读到消息之后才能进入下一步。")
            return
        # 走**向导**而不是直接 _show_step：两者必须一致。以前界面翻页了而
        # wizard.step 还停在 history，之后任何依据 step 的判断（恢复、刷新）
        # 都会读到过期状态。
        self.app.wizard._go(WizardStep.ANALYZE)

    def _go_home_from_history(self) -> None:
        """一条都没读到时的去处：回首页看已经有的东西。

        已读到的消息和订单**一个字节都没少**。商户卡在这里的真正原因不是
        "没读到"，而是"没有任何一条能走的路"—— 所以给他一条走得通的。
        """
        # 同样走 wizard：页面和向导状态必须一致，理由同 :meth:`_go_next_from_history`。
        self.app.wizard._go(WizardStep.DONE)

    # ------------------------------------------------- ④ 智能整理（额度同屏）

    def _page_analyze(self) -> None:
        """额度查询与智能整理合成一屏。

        商户的顺序是：读到东西了吗 → 够不够用 → 用哪个模型 → 开始。原来这四件事
        分在两屏、而且第 4 屏会自动跳走，导致"实际读到多少条、覆盖哪段时间、
        有没有缺口"这些如实上报一闪而过。合成一屏后它们一直留在眼前。
        """
        frame = ttk.Frame(self.container, padding=(18, 6))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="第 4 步　智能整理", style="Section.TLabel").pack(anchor="w", pady=(4, 8))

        self._build_summary_card(frame)
        self._build_history_summary(frame)
        self._build_quota_block(frame)

        ttk.Label(frame, text="选择分析模型：", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", pady=(10, 2))
        self._build_model_picker(frame)
        ttk.Label(
            frame,
            text="换模型只影响之后提交的分析。已经整理过的消息不会被重复提交，"
                 "不会生成第二张订单，也不会改动已生效的订单。",
            wraplength=700, foreground="#7b8794",
        ).pack(anchor="w", pady=(6, 0))
        # 换模型重新分析：入口在差异流程端到端验过之前保持关闭。
        reanalysis_note = ttk.Label(
            frame, text=self.app.reanalysis_notice(), wraplength=700, foreground="#a8590b",
        )
        reanalysis_note.pack(anchor="w", pady=(4, 0))
        self._widgets["reanalysis"] = reanalysis_note
        self._widgets["analyze"] = ttk.Label(frame, text="", font=("Microsoft YaHei", 11, "bold"), wraplength=700)
        self._widgets["analyze"].pack(anchor="w", pady=(14, 0))
        # 失败提示的落点。以前第 4 步没有它，商户点「开始智能整理」失败时
        # 错误消息无处可去 —— 按钮亮着，点了没反应，也没有任何解释。
        self._widgets["analyze_notice"] = ttk.Label(
            frame, text="", foreground="#a8590b", wraplength=700,
        )
        self._widgets["analyze_notice"].pack(anchor="w", pady=(6, 0))

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(10, 0))
        self._widgets["analyze_button"] = ttk.Button(
            actions, text="开始智能整理", style="Big.TButton", command=self._on_analyze
        )
        self._widgets["analyze_button"].pack(side="left")

        self._render_analyzed()
        self._refresh_analyze_button()
        self._spawn("quota", self._refresh_quota)
        self._spawn("models", self.app.load_models)

    def _build_summary_card(self, parent: Any) -> None:
        """「把这些消息整理成一段话」—— 只转述，不判断。

        ## 为什么后面还要「智能整理」

        两者是不同的东西，不互相覆盖：

        * **智能整理**（上面那个）建订单、算价钱、匹配商品 —— 需要目录里有商品。
          客户不报商品时它会猜，猜错便羞会发错货。
        * **这里**只把原话整理成一段人话。客户说「那个黄的」就写
          「那个黄的」，不试成目录里的编码。店主本人就在群里，他知道那是什么。
        """
        box = ttk.LabelFrame(parent, text="汇总一段话（不建单、不报价、不猜商品）",
                             padding=10)
        box.pack(fill="x", pady=(0, 10))
        self._widgets["summary_box"] = box
        self._summary_text = tk.Text(box, height=14, wrap="word", relief="flat")
        self._summary_text.pack(fill="both", expand=True)
        self._summary_text.configure(state="disabled")
        self._summary_text.insert("1.0", "读完历史后点「整理成一段话」。")
        bar = ttk.Frame(box)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="整理成一段话", style="Big.TButton",
                   command=self._on_summarize).pack(side="left")
        # 不确定进度（indeterminate），**不是百分比**。
        #
        # 汇总是**一次 HTTP 请求**，不流式、也没有分步回报—— 服务端在让模型写完之前
        # 不会送出任何东西。若画一条会自己往前走的百分比，那就是在擅自€。
        # 常见的错误做法是加一条不动的进度条，日志里却写着模型在思考—— 无法比对。
        #
        # 所以这里用不确定模式 + **已等待几秒**。时间是真的，能让商户知道这个程序没卡死。
        self._summary_bar = ttk.Progressbar(bar, length=180, mode="indeterminate")
        self._summary_bar.pack(side="left", padx=(12, 0))
        self._summary_elapsed = tk.StringVar(value="")
        self._summary_elapsed_label = ttk.Label(bar, textvariable=self._summary_elapsed, foreground="#7b8794")
        self._summary_elapsed_label.pack(side="left", padx=(8, 0))
        self._summary_status = ttk.Label(bar, text="", foreground="#7b8794")

    def _messages_for_summary(self, result: Any) -> list[dict[str, Any]]:
        """取哪些消息给模型（共用逻辑）。详见 ``summary_task``。"""
        return summary_task.messages_for_summary(self, result)

    def _run_summary(self, result: Any) -> dict[str, Any]:
        """真正去调模型（共用逻辑）。在后台线程里跑，不能碰 Tk。"""
        return summary_task.run_summary(self, result)

    def _summary_tick(self) -> None:
        """每半秒刷一次已等待时长。给的是**真实秒数**。"""
        if not self._summary_running or not self._alive(self._summary_elapsed_label):
            return
        waited = int(time.monotonic() - self._summary_started)
        self._summary_elapsed.set(f"已等待 {waited} 秒")
        self.root.after(500, self._summary_tick)

    def _set_summary(self, text: str) -> None:
        if not self._alive(self._summary_text):
            return
        self._summary_text.configure(state="normal")
        self._summary_text.delete("1.0", "end")
        self._summary_text.insert("1.0", text)
        self._summary_text.configure(state="disabled")

    def _on_summarize(self) -> None:
        """汇总已读到的消息。

        模型调用不能在主线程做：请求可能卡几十秒，主线程被占住
        就是界面整个冻住，商户会认为程序卡死。
        """
        if self._summary_running:
            return
        result = self._last_read_result or self.app.wizard.history_result
        if not self._has_messages_for(result):
            messagebox.showinfo("还没读到消息",
                                "请先回上一步读取历史消息。")
            return
        self._summary_running = True
        self._summary_status.configure(text="正在整理……")
        self._summary_started = time.monotonic()
        self._summary_bar.start(12)
        self._summary_tick()
        def work():
            outcome = self._run_summary(result)
            # **必须走 _post，不能在后台线程里碰 Tk。**
            #
            # 原来写的是 self.root.after(0, ...)，结果：
            #   RuntimeError: main thread is not in main loop
            # 它抛在**工作线程**里，不是 Tk 回调里，所以
            # report_callback_exception 抓不到、stderr 一片安静。
            # 于是 _summary_finished 永远不执行、_summary_running 永远是 True，
            # 之后每次点击都被 if self._summary_running 挡掉 —— 点了没反应。
            self._post("summary", outcome=outcome)
        threading.Thread(target=work, daemon=True).start()

    def _on_summary(self, outcome: dict) -> None:
        """主线程入口：_drain 把后台线程的结果送到这里。"""
        self._summary_finished(outcome)

    def _summary_finished(self, outcome: dict) -> None:
        self._summary_running = False
        if self._alive(self._summary_bar):
            self._summary_bar.stop()
        self._summary_elapsed.set("")
        if not self._alive(self._summary_status):
            return
        if outcome.get("error"):
            self._summary_status.configure(text="整理失败：" + outcome["error"])
            return
        text = outcome.get("summary") or ""
        if not text.strip():
            self._summary_status.configure(text="没汇总出内容。")
            return
        self._set_summary(text)
        count = outcome.get("message_count", 0)
        cost = outcome.get("cost")
        tail = f"已整理 {count} 条"
        if cost is not None:
            tail += f"，本次费用 ${cost:.6f}"
        self._summary_status.configure(text=tail)

    def _build_history_summary(self, parent: Any) -> None:
        """读取结果与缺口。**放在最上面** —— 商户最先要确认的是"读到了什么"。"""
        box = ttk.LabelFrame(parent, text="刚才读到的消息", padding=10)
        box.pack(fill="x", pady=(0, 10))
        self._widgets["history_headline"] = ttk.Label(
            box, text="", font=("Microsoft YaHei", 11, "bold"), wraplength=700,
        )
        self._widgets["history_headline"].pack(anchor="w")
        self._widgets["history_gaps"] = ttk.Label(box, text="", foreground="#a8590b", wraplength=700)
        self._widgets["history_gaps"].pack(anchor="w", pady=(4, 0))
        self._render_history_summary()

    def _render_history_summary(self) -> None:
        headline = self._widgets.get("history_headline")
        gaps = self._widgets.get("history_gaps")
        result = self.app.wizard.history_result
        if headline is not None:
            if result is None:
                headline.configure(text="还没有读取记录")
            else:
                headline.configure(text=result.headline())
        if gaps is not None:
            lines = [f"注意：{gap}" for gap in (result.gaps if result is not None else ())]
            gaps.configure(text="\n".join(lines))

    def _build_quota_block(self, parent: Any) -> None:
        box = ttk.LabelFrame(parent, text="分析额度", padding=10)
        box.pack(fill="x", pady=(0, 8))
        self._widgets["quota"] = ttk.Label(box, text="正在查询…", font=("Microsoft YaHei", 11, "bold"))
        self._widgets["quota"].pack(anchor="w")
        # 额度条。商户的参照是 ChatGPT 那样的用量条 —— 一眼看出用了多少。
        #
        # 刻意**不**换算成"还能分析多少条"：一条消息常常不是一笔订单
        # （"苹果两箱、砂糖桔三箱" 是一条消息两笔单），换算出来的条数
        # 会让他照着错误的口径决定充多少。百分比是认得的。
        bar_row = ttk.Frame(box)
        bar_row.pack(fill="x", pady=(8, 0))
        self._widgets["quota_bar"] = ttk.Progressbar(bar_row, length=280)
        self._widgets["quota_bar"].pack(side="left", fill="x", expand=True)
        self._widgets["quota_percent"] = ttk.Label(
            bar_row, text="", foreground="#5a6572",
        )
        self._widgets["quota_percent"].pack(side="right", padx=(10, 0))
        self._widgets["quota_detail"] = ttk.Label(box, text="", foreground="#5a6572", wraplength=700)
        self._widgets["quota_detail"].pack(anchor="w", pady=(6, 6))
        actions = ttk.Frame(box)
        actions.pack(fill="x")
        self._widgets["quota_login"] = ttk.Button(actions, text="登录产品账号", command=self._on_login)
        self._widgets["quota_login"].pack(side="left")
        self._widgets["quota_refresh"] = ttk.Button(
            actions, text="刷新额度", style="Subtle.TButton",
            command=lambda: self._spawn("quota", self._refresh_quota),
        )
        self._widgets["quota_refresh"].pack(side="left", padx=8)
        ttk.Label(
            box,
            text="额度不足时不会丢数据：已经读到的消息会保留，开通后点「开始智能整理」即可继续，"
                 "不会重复读取或重复扣费。",
            foreground="#7b8794", wraplength=700,
        ).pack(anchor="w", pady=(8, 0))

    def _render_quota_bar(self, view: Any) -> None:
        """画额度条。**只在真的有分母时画** —— 无限量套餐没有百分比，
        硬画一条空条等于告诉他"用了很多"，那是假的。"""
        bar = self._widgets.get("quota_bar")
        label = self._widgets.get("quota_percent")
        holder = bar.master if bar is not None else None
        unlimited = bool(getattr(view, "unlimited", False))
        usable = (not unlimited
                  and int(getattr(view, "total_micro_usd", 0) or 0) > 0
                  and bool(getattr(view, "active", False)))
        if bar is not None and self._alive(bar):
            if usable:
                bar.pack(side="left", fill="x", expand=True)
                bar.configure(mode="determinate",
                              maximum=100,
                              value=int(getattr(view, "used_percent", 0)))
            elif not unlimited:
                # 还没查出来 / 没登录 —— 收起来，不占地方也不误导。
                bar.pack_forget()
            else:
                bar.pack_forget()
        if label is not None and self._alive(label):
            label.configure(text="" if not usable else f"已用 {getattr(view, 'used_percent', 0)}%")
        del holder

    def _refresh_quota(self) -> None:
        view = self.app.check_quota()
        self._post("quota", view=view)

    def _on_quota(self, view: Any) -> None:
        # 登录一成功就会查额度，所以这里也是账号条刷新的时机 ——
        # 顶栏那条「账号：未登录」不能等到商户重开程序才变。
        self._render_account()
        # 第 1 步那块也要跟着变。商户多半就停在第 1 步，一块写着
        # 「还没有登录」的框留在那里，会让他以为刚才那次登录没成功。
        self._refresh_login_block()
        # 登录成功后模型相关的按钮立刻恢复。灰着的按钮会让人以为程序坏了。
        self._refresh_analyze_button()
        self._refresh_model_controls()
        # 页面可能已被导航重建；控件不在就跳过，数据留在 wizard.quota 里，
        # 重建页面时会重新查一次。
        label = self._widgets.get("quota")
        if label is not None and label.winfo_exists():
            label.configure(text=view.headline())
        self._render_quota_bar(view)
        detail = self._widgets.get("quota_detail")
        if detail is None or not detail.winfo_exists():
            return
        text = view.message or ""
        if view.logged_in and view.period_end:
            text += f"　有效期至 {view.period_end[:10]}"
        if not view.sufficient and view.purchase_hint:
            text += f"\n{view.purchase_hint}（本软件暂不支持在线付款，不会自动扣费）"
        detail.configure(text=text.strip())
        login = self._widgets.get("quota_login")
        if login is not None and login.winfo_exists():
            login.configure(state="disabled" if view.logged_in else "normal")

    def _refresh_login_block(self) -> None:
        """登录状态变了，第 1 步那块也要跟着变。

        登录完成后商户多半还停在第 1 步 —— 一块写着"还没有登录"的框留在那里，
        会让他以为刚才那次登录没成功。
        """
        box = getattr(self, "_login_box", None)
        if box is None or not self._alive(box):
            return
        self._render_login(box)

    def _render_login(self, box: Any) -> None:
        """把登录区块画出来。

        三件事必须说清楚：

        * 现在是什么状态（谁 / 未登录）
        * **不登录也能做什么** —— 检查微信、选会话、读历史都能用，读到的数据
          留在本地、不扣额度。不说这句，商户会以为登录是所有功能的前提，
          登录失败就干脆不往下走了。
        * 缺登录就做不了什么 —— 只有"智能整理"这一件事需要它。
        """
        for child in box.winfo_children():
            child.destroy()
        self._widgets.pop("login_start", None)
        self._widgets.pop("login_switch", None)
        session = self.app.session
        if session is not None:
            name = str(getattr(session, "merchant_name", "") or "").strip()
            ttk.Label(
                box, text=f"已登录：{name}" if name else "已登录",
                font=("Microsoft YaHei", 10, "bold"),
            ).pack(anchor="w")
            ttk.Label(
                box,
                text="可以智能整理消息、生成订单。不登录也能读历史，读到的数据一样留在本地。",
                foreground="#5a6572", wraplength=620,
            ).pack(anchor="w", pady=(4, 0))
            self._widgets["login_switch"] = ttk.Button(
                box, text="换一个账号登录", command=self._on_login
            )
            self._widgets["login_switch"].pack(anchor="w", pady=(8, 0))
            return

        ttk.Label(box, text="还没有登录产品账号", font=("Microsoft YaHei", 10, "bold"),
                  foreground="#a8590b").pack(anchor="w")
        ttk.Label(
            box,
            text="没有登录也能用：检查微信、选会话、读历史都照常，读到的消息只存在这台电脑上。\n"
                 "只有「智能整理」（把消息变成订单）需要登录后才会开始分析。",
            foreground="#5a6572", wraplength=620,
        ).pack(anchor="w", pady=(4, 0))
        self._widgets["login_start"] = ttk.Button(
            box, text="登录产品账号", style="Big.TButton", command=self._on_login
        )
        self._widgets["login_start"].pack(anchor="w", pady=(8, 0))

    def _on_login(self) -> None:
        self.app.login_requested()

    def _on_account_click(self) -> None:
        """点顶栏的账号条。

        未登录 → 发起登录；已登录 → 确认后退出。退出是不可逆的（要重新走一遍
        网页登录），所以必须先问一句，别让误点把人踢出去。
        """
        session = self.app.session
        if session is None:
            self.app.login_requested()
            return
        name = str(getattr(session, "merchant_name", "") or "当前账号")
        if not messagebox.askyesno(
            "退出登录",
            f"确定退出「{name}」吗？\n\n"
            "已经读到的消息和订单会留在这台电脑上，退出后可以重新登录查看。",
        ):
            return
        self.app.detach()
        self._render_account()
        self._refresh_login_block()
        self._refresh_analyze_button()
        self._refresh_model_controls()
        # 留在**他有数据的那一屏**。退出登录只断开了云端能力，本地读到的
        # 消息和订单一个都没少 —— 把他扔回第 1 步，等于逼他从头再走一遍。
        self._show_step(WizardStep.ANALYZE if self.app.wizard.history_result else WizardStep.WECHAT)
        if self.app.wizard.history_result:
            messagebox.showinfo(
                "已退出登录",
                "已经读到的消息和订单都留在这台电脑上，没有删除。\n\n"
                "重新登录之后点「开始智能整理」就能接着用，"
                "不需要重新读取，也不会重复计费。",
            )

    def _render_account(self) -> None:
        """账号条上如实写清楚当前是谁，以及点了会发生什么。"""
        widget = self._widgets.get("account")
        if widget is None or not self._alive(widget):
            return
        session = self.app.session
        if session is None:
            widget.configure(text="账号：未登录　点此登录")
            return
        name = str(getattr(session, "merchant_name", "") or "").strip()
        widget.configure(text=f"账号：{name}　点此退出" if name else "账号：已登录　点此退出")

    def _build_model_picker(self, parent) -> None:
        """模型卡片区。商户只需要点一张卡片，不需要填任何 ID 或地址。"""
        box = ttk.Frame(parent)
        box.pack(fill="x")
        self._widgets["model_notice"] = ttk.Label(box, text="", foreground="#a8590b", wraplength=700)
        self._widgets["model_notice"].pack(anchor="w", pady=(0, 4))
        self._widgets["model_cards"] = ttk.Frame(box)
        self._widgets["model_cards"].pack(fill="x")
        actions = ttk.Frame(box)
        actions.pack(fill="x", pady=(6, 0))
        self._widgets["model_refresh"] = ttk.Button(
            actions, text="刷新模型列表", style="Subtle.TButton",
            command=lambda: self._spawn("models", lambda: self.app.load_models(force=True)),
        )
        self._widgets["model_refresh"].pack(side="left")
        ttk.Button(actions, text="模型设置", style="Subtle.TButton", command=self._on_model_settings).pack(side="left", padx=8)
        self._on_models_loaded(self.app.model_catalog)

    def _refresh_model_controls(self) -> None:
        """没登录时，模型相关的两个按钮都会白点。

        未登录压根没去取过模型列表，「刷新模型列表」按下去还是空的 ——
        商户会以为网络有问题，反复重试。**按钮亮着就得能按。**
        """
        refresh = self._widgets.get("model_refresh")
        if refresh is not None and self._alive(refresh):
            refresh.configure(state="disabled" if self.app.client is None else "normal")

    def _on_models_loaded(self, catalog: dict[str, Any]) -> None:
        """渲染模型卡片。拉取失败时显示缓存，并写明不是最新。"""
        self._refresh_model_controls()
        holder = (self._widgets.get("model_cards")
                  or self._widgets.get("settings_model_cards"))
        if holder is None or not holder.winfo_exists():
            # 页面已经切走了（模型是后台拉回来的）。这一次的刷新就跳过，
            # 下次打开页面会用最新的目录重画。
            return
        for child in holder.winfo_children():
            child.destroy()
        notice = self._widgets.get("model_notice")
        if notice is not None:
            notice.configure(text=self.app.model_notice())
        self._model_var.set(self.app.settings.model_alias)
        models = catalog.get("models") or []
        if not models:
            # 上面 model_notice 已经按目录事实说明了原因，这里**不要再画一遍**。
            # 两种错法都踩过：以前这里自己写一句（措辞还不一样），改成回退到
            # notice 之后变成同一句话连续出现两次 —— 同一屏重复同一个事实，
            # 商户只会以为出了两个问题。
            if not self.app.model_notice():
                # 目录说没有模型、但又说不出原因：得给一句实话，不能留空白。
                ttk.Label(
                    holder,
                    text="现在没有可用的分析模型。已读取的消息和待处理任务都保留着，可以稍后重试。",
                    foreground="#a8590b", wraplength=660,
                ).pack(anchor="w")
            return
        for item in models:
            self._add_model_card(holder, item)

    def _add_model_card(self, parent, item: dict[str, Any]) -> None:
        alias = str(item.get("alias") or "")
        # 产品里**没有免费模型**。以前默认卡片标题写「免费模型（推荐）」、
        # 标签写「免费」—— 商户会以为不花钱，提交后却被扣额度。
        # 这里只说「推荐」，不承诺任何价格。
        title = ("推荐模型" if item.get("is_default")
                 else (item.get("display_name") or "分析模型"))
        card = ttk.LabelFrame(parent, text=title, padding=8)
        card.pack(fill="x", pady=2)
        row = ttk.Frame(card)
        row.pack(fill="x")
        ttk.Radiobutton(
            row, text="使用", value=alias, variable=self._model_var, command=lambda: self._on_pick_model(alias)
        ).pack(side="left")
        tags = ["分析消息会使用您的分析额度"]
        if item.get("status") == "rate_limited":
            tags.append("当前繁忙，请稍后重试")
        ttk.Label(row, text="　".join(tags), foreground="#7b8794").pack(side="left", padx=(8, 0))
        detail = item.get("summary") or ""
        if detail:
            ttk.Label(card, text=detail, foreground="#5a6572", wraplength=660).pack(anchor="w")
        reason = item.get("status_reason") or ""
        if reason:
            ttk.Label(card, text=reason, foreground="#a8590b", wraplength=660).pack(anchor="w")

    def _on_pick_model(self, alias: str) -> None:
        self.app.set_model(alias)

    def _on_model_settings(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("模型设置")
        window.geometry("640x500")
        box = ttk.Frame(window, padding=12)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="选择分析模型", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        holder = ttk.Frame(box)
        holder.pack(fill="both", expand=True)
        self._widgets["settings_model_cards"] = holder
        self._on_models_loaded(self.app.model_catalog)
        ttk.Label(
            box,
            text="更换模型只影响之后开始的分析；已经提交的分析仍用它开始时选定的模型。\n"
            "切换模型不会影响已有订单和客户习惯记录。",
            foreground="#7b8794", wraplength=600,
        ).pack(anchor="w", pady=(8, 0))

    def _on_analyze(self) -> None:
        self._show_step(WizardStep.ANALYZE)
        summary = self.app.analyze()
        if summary is not None:
            self._post("analyzed", summary=summary)

    def _on_analyzed(self, summary: dict) -> None:
        # 结果先存下来：页面随时可能被一次导航重建，事后再渲染也拿得到。
        self._last_summary = dict(summary)
        self._render_analyzed()

    def _render_analyzed(self) -> None:
        summary = self._last_summary
        label = self._widgets.get("analyze")
        if not summary or label is None or not label.winfo_exists():
            return
        text = (
            f"整理完成：处理 {summary['processed']} 条消息\n"
            f"生成订单 {summary['with_result']} 单\n"
            f"需要您核对 {summary['needs_review']} 单"
        )
        # 扣费口径来自云端账号，本地不推算。
        if summary.get("billing"):
            # 不再拆出「其中不消耗额度」那一栏：免费档位已被焊死，
            # 它恒为 0。写出来只会让商户以为还有不花钱的。
            text += (
                f"\n提交任务 {summary.get('submitted_jobs', 0)} 个，"
                f"实际扣费 {summary.get('charged_micro_usd', 0)} 次"
            )
        skipped = summary.get("already_analyzed") or 0
        if skipped:
            text += f"\n已整理过的 {skipped} 条本次没有重复提交"
        label.configure(text=text)

    def _refresh_analyze_button(self) -> None:
        """按钮亮着就得能按。未登录时整理必然失败，不该给一个假的可用状态。

        以前未登录时「开始智能整理」照样亮着 —— 商户点下去，线程失败，
        而错误消息在第 4 步又无处可放（见 :meth:`_on_error`）。结果就是：
        按钮亮着、点了没反应、没有任何解释。

        灰掉并在原地说明原因，比让商户白点一次要好。
        """
        button = self._widgets.get("analyze_button")
        if button is None or not self._alive(button):
            return
        notice = self._widgets.get("analyze_notice")
        if self.app.session is None:
            button.configure(state="disabled")
            if notice is not None and self._alive(notice):
                notice.configure(
                    text="还没有登录产品账号，不能整理。"
                         "请点右上角「账号：未登录　点此登录」登录后再回来。",
                    foreground="#a8590b",
                )
            return
        button.configure(state="normal")
        if notice is not None and self._alive(notice) and "登录" in str(notice.cget("text")):
            notice.configure(text="")

    # --------------------------------------------------------------- ⑥ 首页

    def _page_home(self) -> None:
        frame = ttk.Frame(self.container, padding=(18, 6))
        frame.pack(fill="both", expand=True)
        self._home_frame = frame

        banner = ttk.Frame(frame)
        banner.pack(fill="x", pady=(0, 10))
        ttk.Label(banner, text="接单助手", style="Section.TLabel").pack(side="left")
        self._widgets["mode"] = ttk.Label(banner, text="", font=("Microsoft YaHei", 10, "bold"))
        self._widgets["mode"].pack(side="right", padx=(0, 14))
        self._widgets["pause"] = ttk.Button(
            banner, text="暂停发送", style="Pause.TButton", command=self._on_toggle_pause
        )
        self._widgets["pause"].pack(side="right")

        status = ttk.LabelFrame(frame, text="当前状态", padding=10)
        status.pack(fill="x", pady=(0, 10))
        self._widgets["st_wechat"] = ttk.Label(status, text="")
        self._widgets["st_wechat"].pack(anchor="w")
        self._widgets["st_conv"] = ttk.Label(status, text="")
        self._widgets["st_conv"].pack(anchor="w", pady=(3, 0))
        self._widgets["st_quota"] = ttk.Label(status, text="")
        self._widgets["st_quota"].pack(anchor="w", pady=(3, 0))
        self._widgets["st_queue"] = ttk.Label(status, text="")
        self._widgets["st_queue"].pack(anchor="w", pady=(3, 0))
        # 数据来源说明。正常时为空 —— 一直挂着警告会让真警告失去分量。
        self._widgets["st_origin"] = ttk.Label(status, text="", foreground="#a8590b", wraplength=700)
        self._widgets["st_origin"].pack(anchor="w", pady=(3, 0))

        counters = ttk.Frame(frame)
        counters.pack(fill="x", pady=(0, 6))
        for key in ("effective", "actionable", "send_errors", "payment_claims", "clarifications"):
            box = ttk.LabelFrame(counters, text=LABEL_FOR_COUNTER[key], padding=8)
            box.pack(side="left", padx=(0, 8))
            self._widgets[f"c_{key}"] = ttk.Label(box, text="0", font=("Microsoft YaHei", 16, "bold"))
            self._widgets[f"c_{key}"].pack()

        # 「自动记单」和「客户未确认」是观察值，不是待办 —— 摆在小字里说明口径。
        notes = ttk.Frame(frame)
        notes.pack(fill="x", pady=(0, 8))
        self._widgets["note_counts"] = ttk.Label(notes, text="", foreground="#7b8794")
        self._widgets["note_counts"].pack(anchor="w")
        self._widgets["note_scope"] = ttk.Label(notes, text="", foreground="#7b8794", wraplength=860)
        self._widgets["note_scope"].pack(anchor="w")

        stock = ttk.LabelFrame(frame, text="今日备货汇总", padding=8)
        stock.pack(fill="both", expand=True)
        columns = ("name", "quantity", "orders", "customers")
        # 行数跟着实际内容走。固定 6 行在只有 1-2 个品时会白占一大块，
        # 而这块高度是从下面那些按钮的预算里扣的 —— 占多了，
        # 「开始持续监听」就会被挤出窗口，商户再也点不到。
        tree = ttk.Treeview(stock, columns=columns, show="headings", height=3)
        for key, title, width in (
            ("name", "商品", 200), ("quantity", "数量", 110),
            ("orders", "单数", 60), ("customers", "客户", 320),
        ):
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor="w")
        tree.pack(fill="both", expand=True)
        self._widgets["stock"] = tree

        # 待澄清单独一栏。它**不是订单**：不进有效订单数、不进备货汇总。
        clar_box = ttk.LabelFrame(
            frame, text="待澄清（问客户一句；不是订单，不计入备货）", padding=8
        )
        clar_box.pack(fill="both", expand=True, pady=(8, 0))
        clar = ttk.Treeview(clar_box, columns=("customer", "question", "group"), show="headings", height=4)
        for key, title, width in (
            ("customer", "客户", 120), ("question", "建议这样问", 520), ("group", "会话", 160),
        ):
            clar.heading(key, text=title)
            clar.column(key, width=width, anchor="w")
        clar.pack(fill="both", expand=True)
        self._widgets["clar_list"] = clar
        self._widgets["clar_box"] = clar_box

        # 系统解析失败：**不是**要问客户的问题，是系统没跑成。单独一栏。
        fail_box = ttk.LabelFrame(
            frame, text="系统解析失败（不是订单，不去问客户；可重试或转人工）", padding=8
        )
        fail_box.pack(fill="both", expand=True, pady=(8, 0))
        fails = ttk.Treeview(fail_box, columns=("reason", "detail", "retry"), show="headings", height=4)
        for key, title, width in (
            ("reason", "原因", 220), ("detail", "说明", 380), ("retry", "可重试", 90),
        ):
            fails.heading(key, text=title)
            fails.column(key, width=width, anchor="w")
        fails.pack(fill="both", expand=True)
        self._widgets["fail_list"] = fails
        self._widgets["fail_box"] = fail_box

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(10, 0))
        # 空表格收起后要能插回这个位置之前。
        self._widgets["home_after_tables"] = actions
        ttk.Button(actions, text="后台整理（不发送）", command=lambda: self._on_mode(RunMode.BACKGROUND)).pack(side="left")
        ttk.Button(actions, text="开启自动接单与回复", command=self._on_enable_auto).pack(side="left", padx=8)
        ttk.Button(actions, text="查看订单", command=self._on_show_orders).pack(side="left")
        ttk.Button(actions, text="云端会发送哪些信息", command=self._on_privacy).pack(side="right")
        # 出问题时商户需要一个能发给客服的东西。没有入口，日志就等于不存在 ——
        # 他不会想到去数据目录里翻文件。
        ttk.Button(actions, text="出问题了？这里有日志", style="Subtle.TButton",
                   command=self._on_support).pack(side="right", padx=(0, 10))
        # 参考信息放在维护那一排（不是主操作那一排）：它是**偶尔做一次**的事，
        # 不是每单都要点的。放进主操作会让「后台整理」这些日常按钮失去层级。
        ttk.Button(actions, text="商品与客户名单", style="Subtle.TButton",
                   command=self._on_reference_data).pack(side="right", padx=(0, 10))

        self._build_listen_box(frame)

        view = self.app.refresh_home()
        self._last_home = view
        try:
            self._render_home(view)
        except Exception:
            LOGGER.debug("home render skipped during rebuild", exc_info=True)
        self._render_listen()

    # --------------------------------------------------------- 持续监听开关

    def _build_listen_box(self, parent: Any) -> None:
        box = ttk.LabelFrame(parent, text="持续监听（实时接收新消息）", padding=10)
        box.pack(fill="x", pady=(12, 0))
        self._widgets["listen_box"] = box

        row = ttk.Frame(box)
        row.pack(fill="x")
        self._widgets["listen_state"] = ttk.Label(row, text="")
        self._widgets["listen_state"].pack(side="left")
        self._widgets["listen_btn"] = ttk.Button(
            row, text="开始持续监听", command=self._on_toggle_listen
        )
        self._widgets["listen_btn"].pack(side="right")

        self._widgets["listen_stats"] = ttk.Label(box, text="", foreground="#7b8794")
        self._widgets["listen_stats"].pack(anchor="w", pady=(4, 0))
        self._widgets["listen_gaps"] = ttk.Label(box, text="", foreground="#a8590b", wraplength=860)
        self._widgets["listen_gaps"].pack(anchor="w", pady=(2, 0))
        # 做不到的事常驻写在界面上，不靠一次弹窗说完。
        self._widgets["listen_note"] = ttk.Label(
            box, text=LISTEN_FAIR_NOTE, foreground="#a8590b", wraplength=860
        )
        self._widgets["listen_note"].pack(anchor="w", pady=(2, 0))
        # 最近一次合并成了一条的内容。以前合并只在控制台打印，界面上完全
        # 看不见 —— 商户发了三条、看见一条订单，不会知道那三条被合到一起了。
        self._widgets["listen_merged"] = ttk.Label(
            box, text="", foreground="#5a6572", wraplength=860
        )
        self._widgets["listen_merged"].pack(anchor="w", pady=(4, 0))
        self._last_merged_text: str = ""

    def _on_toggle_listen(self) -> None:
        view = self.app.listen_view()
        if view["running"]:
            self.app.stop_listening()
            return
        target = self._listen_target()
        if target is None:
            messagebox.showinfo(
                "还没选会话", "请先回到第 2 步选择要监听的会话，再回来开启持续监听。"
            )
            return
        if not messagebox.askyesno("开启持续监听", f"{target.disambiguation}\n\n{LISTEN_FAIR_NOTE}\n\n确定要开始吗？"):
            return
        self.app.start_listening(target)

    def _listen_target(self) -> Conversation | None:
        live = getattr(self.app, "_live_choice", None)
        if live is not None:
            return live
        selected = self.app.wizard.conversations.history_selected
        return selected[0] if selected else None

    def _on_listen(self, view: dict[str, Any]) -> None:
        self._last_listen = view
        try:
            self._render_listen()
        except Exception:
            # 页面可能正在重建：数据已经存进 _last_listen，_page_home 会补画。
            LOGGER.debug("listen render skipped during rebuild", exc_info=True)

    def _render_listen(self) -> None:
        view = self._last_listen or self.app.listen_view()
        button = self._widgets.get("listen_btn")
        state = self._widgets.get("listen_state")
        stats = self._widgets.get("listen_stats")
        gaps = self._widgets.get("listen_gaps")
        if button is None:
            return
        running = bool(view.get("running"))
        if button.winfo_exists():
            button.configure(text="停止持续监听" if running else "开始持续监听")
        labels = {
            LISTEN_STOPPED: "已停止",
            LISTEN_STARTING: "正在启动",
            LISTEN_RUNNING: "监听中",
            LISTEN_DEGRADED: "监听不稳定",
        }
        text = labels.get(str(view.get("state")), "状态未知")
        if view.get("conversation"):
            text = f"{text}　{view['conversation']}"
        if state is not None:
            state.configure(text=text)
        merged = self._widgets.get("listen_merged")
        if merged is not None and merged.winfo_exists():
            count = getattr(self, "_last_merged_count", 0)
            if count > 1:
                merged.configure(
                    text=f"刚刚把 {count} 条连着发的短消息合成一条整理，"
                         "内容没丢，只是先攒一会儿再一起处理。"
                )
            else:
                merged.configure(text="")
        if stats is not None and view.get("summary"):
            stats.configure(text=view["summary"])
        elif stats is not None:
            stats.configure(text="")
        if gaps is not None:
            count = int(view.get("gap_count") or 0)
            detail = str(view.get("detail") or "")
            if count:
                gaps.configure(text=f"有 {count} 段时间没有采集，那期间的消息需要单独补读。{detail}")
            elif detail:
                gaps.configure(text=detail)
            else:
                gaps.configure(text="")

    def _toggle_box(self, key: str, show: bool) -> None:
        """空表格就整块收起来。

        「待澄清」和「系统解析失败」平时都是空的，却一直占着首页一半版面 ——
        商户一眼看过去全是没用的表头，还以为漏了东西。有内容才出现。
        """
        box = self._widgets.get(key)
        if box is None:
            return
        try:
            if not box.winfo_exists():
                return
            if show:
                box.pack(fill="both", expand=True, pady=(8, 0), before=self._widgets.get("home_after_tables"))
            else:
                box.pack_forget()
        except tk.TclError:
            # 页面正在被导航重建：控件已经没了。这里不画，_page_home 会补。
            LOGGER.debug("box toggle skipped during rebuild", exc_info=True)

    def _render_home(self, view: HomeView) -> None:
        # 页面刚重建时控件都是新的，但事件可能排在导航之前到达。控件不在就
        # 跳过这一次渲染：数据已经存进 _last_home，_page_home 会补画。
        def alive(key: str) -> Any:
            widget = self._widgets.get(key)
            try:
                return widget if widget is not None and widget.winfo_exists() else None
            except Exception:
                return None

        set_text = alive("st_wechat")
        if set_text is not None:
            # 不再加"微信："前缀 —— status_line() 本来就自成一��，
            # 加上会显示成「微信：微信正常」。
            set_text.configure(text=view.status_line())
        conv = alive("st_conv")
        if conv is not None:
            note = f"（{view.conversation_note}）" if view.conversation_note else ""
            block = f"　未发送：{view.send_block_reason}" if view.send_block_reason else ""
            conv.configure(text=f"当前会话：{view.conversation}{note}　模式：{view.mode_note}{block}")
        quota = alive("st_quota")
        if quota is not None:
            # 同上：quota_headline 自带「剩余分析额度：」等前缀，
            # 这里再拼一次就成了「剩余分析额度：剩余分析额度：128」。
            text = view.quota_headline
            if self.app.session is None and view.effective:
                # 未登录 + 本地有订单。只写「未登录产品账号」，商户会以为
                # 订单没了。实际上订单好好地在这台电脑上，只是不能继续
                # 调模型分析 —— 这两件事必须分开说。
                text += "　本地订单仍在，重新登录后可继续整理"
            quota.configure(text=text)
        queue = alive("st_queue")
        if queue is not None:
            parts = [f"待发送回执：{view.pending_receipts}"]
            if view.unknown_receipts:
                parts.append(f"结果待确认：{view.unknown_receipts}")
            if view.failed_receipts:
                parts.append(f"发送失败：{view.failed_receipts}")
            queue.configure(text="　".join(parts))
        # 「数据是真的」和「分析是不是模型做的」分开说。
        #
        # 以前只有一句「当前为演示数据，非真实分析结果」，挂在待发送回执后面。
        # 那个说法把两件不同的事糊成一件：真实微信 + 规则引擎（没登录、额度用完）
        # 这种组合里，消息是真读到的、订单也是真的，只是**不是模型理解出来的** ——
        # 却被说成"演示数据"，商户会以为读到的东西都是假的。
        origin = alive("st_origin")
        if origin is not None:
            origin.configure(text=self._data_origin_note(view))
        for key, value in (("c_effective", len(view.effective)),
                          ("c_actionable", len(view.actionable)),
                          ("c_send_errors", view.send_errors),
                          ("c_payment_claims", len(view.payment_claims)),
                          ("c_clarifications", len(view.clarifications))):
            widget = alive(key)
            if widget is not None:
                widget.configure(text=str(value))
        clar = self._widgets.get("clar_list")
        if clar is not None:
            clar.delete(*clar.get_children())
            for task in view.clarifications:
                clar.insert(
                    "", "end",
                    values=(
                        _cell(task.get("customer") or "（未绑定客户）", 16),
                        # 要问客户的话往往很长，得给够宽度再截断，
                        # 不然商户看到的问句是断的。
                        _cell(task.get("suggested_question", ""), 46),
                        _cell(task.get("group_id", ""), 16),
                    ),
                )
            self._toggle_box("clar_box", bool(view.clarifications))
        fails = self._widgets.get("fail_list")
        if fails is not None:
            fails.delete(*fails.get_children())
            for failure in view.system_failures:
                fails.insert(
                    "", "end",
                    values=(
                        _cell(failure.get("reason", ""), 30),
                        _cell(failure.get("error", "") or "系统没有跑出可用结果", 40),
                        "是" if failure.get("retryable", True) else "否",
                    ),
                )
            self._toggle_box("fail_box", bool(view.system_failures))
        counts = self._widgets.get("note_counts")
        if counts is not None and counts.winfo_exists():
            # 「合并中的短消息组 N 组」以前写在一个 ``hasattr`` 分支里，而
            # ``HomeView`` **从来没有这个字段** —— 分支永远走不到，商户看不到。
            # 用 hasattr 藏起来不会让问题消失，只会让它永远看不见。
            text = f"已自动记单 {view.auto_recorded} 单（不需要逐单确认）"
            if view.open_merge_groups:
                text += (
                    f"　合并中的短消息组 {view.open_merge_groups} 组"
                    "（同一个人连着发的几条会先攒一会儿再一起整理）"
                )
            counts.configure(text=text)
        scope = self._widgets.get("note_scope")
        if scope is not None:
            scope.configure(text=f"统计范围：{view.scope_text}　｜　" + METRIC_DEFINITIONS["actionable"])
        mode = self._widgets.get("mode")
        if mode is not None:
            mode.configure(text="已暂停" if view.mode == RunMode.PAUSED else ("可发送" if view.can_send else "不发送"))
        pause = self._widgets.get("pause")
        if pause is not None:
            paused = view.mode == RunMode.PAUSED
            if paused:
                # 暂停了**必须**留着「恢复发送」。刚才那版按"不会发送就隐藏"
                # 处理，结果暂停后按钮既没显示、也没改成恢复文案 —— 商户
                # 暂停了就回不去了。三个状态要分开看：
                #   暂停中 → 显示「恢复发送」（唯一的退路）
                #   运行且会发送 → 显示「暂停发送」
                #   运行但不会发送 → 隐藏（点了没反应的死控件）
                pause.configure(text="恢复发送")
                if not pause.winfo_ismapped():
                    pause.pack(side="right")
            elif view.can_send:
                pause.configure(text="暂停发送")
                if not pause.winfo_ismapped():
                    pause.pack(side="right")
            else:
                pause.pack_forget()
        tree = self._widgets.get("stock")
        if tree is not None:
            tree.delete(*tree.get_children())
            for row in view.stock:
                tree.insert("", "end", values=(
                    row.name, f"{row.quantity:g}{row.unit}", row.order_count,
                    _cell("、".join(row.customers), 30),
                ))
            # 行数跟着内容走：留 1 行余量，下面那排按钮才不会被挤出去。
            tree.configure(height=max(2, min(6, len(view.stock) + 1)))
        if self._widgets.get("account") is not None:
            self._render_account()

    def _on_toggle_pause(self) -> None:
        if self.app.auto.mode == RunMode.PAUSED:
            self.app.resume()
        else:
            self.app.pause()

    def _on_mode(self, mode: str) -> None:
        self.app.set_mode(mode)

    def _on_enable_auto(self) -> None:
        """开启自动接单的两道确认。

        自动回复要操作微信窗口，可能影响正在用电脑的商户，所以这一步
        必须让商户自己再点一次确认，不能静默打开。
        """
        if not self.app.auto.allow_local_automation:
            if not messagebox.askyesno(
                "允许本机自动接单",
                "自动回复需要操作微信窗口，可能会打断您正在输入的内容。\n\n"
                "确定允许这台电脑被自动接单占用吗？\n\n"
                "随时可以点右上角「暂停发送」停下来。",
            ):
                return
            self.app.set_allow_local_automation(True)
        self.app.set_mode(RunMode.AUTO_TAKE)

    def _on_show_orders(self) -> None:
        """订单列表。

        这里以前是：把每行拼成 ``[状态] 客户　商品　金额`` 的字符串，再用
        ``line.split("　")`` 把它切回四列填表格。绕一圈字符串纯属多余，而且
        **客户名或商品摘要里只要出现全角空格，列就会串位** —— 真实群里的
        摘要经常带空格。改成直接按字段填列。

        另外以前是 ``orders[:60]``：第 61 单往后直接不显示，界面还装作
        全部都在。超量必须写出来。
        """
        orders = self.app.orders_for_display()
        window = tk.Toplevel(self.root)
        window.title("订单")
        window.geometry("820x520")
        box = ttk.Frame(window, padding=12)
        box.pack(fill="both", expand=True)

        head = ttk.Label(box, text="")
        head.pack(anchor="w", pady=(0, 8))
        tree = ttk.Treeview(box, columns=("status", "customer", "items", "amount", "delivery"),
                            show="headings")
        for key, title, width, anchor in (
            ("status", "状态", 110, "w"),
            ("customer", "客户", 130, "w"),
            ("items", "商品", 360, "w"),
            ("amount", "金额", 90, "e"),
            ("delivery", "送到", 110, "w"),
        ):
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor=anchor)
        # iid → 原始行。双击时用得上：表格里截断过，原文要能拿回来。
        raw_by_iid: dict[str, Any] = {}
        for row in orders:
            iid = tree.insert("", "end", values=(
                STATUS_LABELS.get(row.status, row.status),
                _cell(row.customer, 18),
                _cell(row.summary, 60),
                f"{row.amount:.2f} 元",
                row.delivery_date or "",
            ))
            raw_by_iid[str(iid)] = row
        tree.pack(fill="both", expand=True)
        ttk.Label(
            box, text="双击任意一行可以看完整内容。", foreground="#7b8794"
        ).pack(anchor="w", pady=(6, 0))
        tree.bind("<Double-1>", lambda _e: self._show_order_detail(tree, raw_by_iid))
        if not orders:
            head.configure(text="现在没有需要处理的订单。")
        else:
            head.configure(text=f"共 {len(orders)} 单。")

    def _show_order_detail(self, tree: Any, rows: dict[str, Any]) -> None:
        """双击一行：看被表格截掉的完整内容。

        截断只是为了让表格能看，**不能变成丢信息**。真实群里的客户常把
        数量、地址、要求写在句子后半段，表格那一格放不下。
        """
        selection = tree.selection()
        if not selection:
            return
        row = rows.get(str(selection[0]))
        if row is None:
            return
        window = tk.Toplevel(self.root)
        window.title("订单详情")
        window.geometry("620x420")
        box = ttk.Frame(window, padding=12)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=f"客户：{row.customer}", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w")
        ttk.Label(box, text=f"状态：{STATUS_LABELS.get(row.status, row.status)}"
                            f"　金额：{row.amount:.2f} 元"
                            f"　送到：{row.delivery_date or '未填'}").pack(anchor="w", pady=(4, 8))
        # 换行必须保留：客户常分行列地址，挤成一行反而看不清。
        body = tk.Text(box, wrap="word", height=14)
        body.insert("1.0", row.summary)
        body.configure(state="disabled")
        body.pack(fill="both", expand=True)

    def _on_reference_data(self) -> None:
        """「参考信息」—— 商户上传自己的商品数据和客户数据。

        ## 为什么单独做一个入口

        这两份名单决定了软件认不认得出「客户说的东西」和「说话的是谁」。
        没有入口的时候它们只能靠命令行导入，等于商户根本用不上 ——
        而「认不出来」的后果全部落在商户身上：东西认错了发错货，
        人认错了把单挂到别人账上。

        ## 为什么用文件而不是让他在界面上一条条录

        商户的数据本来就在 Excel 里。逐条录入没人会做，做了也录不全。
        """
        window = tk.Toplevel(self.root)
        window.title("参考信息")
        window.transient(self.root)
        window.geometry("760x560")
        frame = ttk.Frame(window, padding=(18, 14))
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="上传你店里的商品和客户名单",
                  font=("Microsoft YaHei", 12, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="软件靠这两份名单认人、认商品。名单越全，需要你手动确认的就越少。\n"
                 "用 Excel 另存为 CSV 就行 —— 表格第一行是列名。\n\n"
                 "商品表列名：编码、名称、单位、价格、别名\n"
                 "客户表列名：名字、群、地址、偏好、别名\n\n"
                 "「别名」可以填多个，用逗号隔开。客户平时怎么叫这个商品、"
                 "群里怎么称呼这个人，都填进去 —— 填了以后就自动认得出来。\n"
                 "重复上传没关系：同一个编码/同一个名字会更新，不会变成两条。",
            wraplength=700, foreground="#5a6572", justify="left",
        ).pack(anchor="w", pady=(8, 12))

        for label, key, hint in (
            ("上传商品数据", "products", "商品表"),
            ("上传客户数据", "customers", "客户表"),
        ):
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=(0, 8))
            ttk.Button(row, text=label, style="Big.TButton",
                       command=lambda k=key: self._pick_reference_file(k)).pack(side="left")
            ttk.Label(row, text=hint, foreground="#7b8794").pack(side="left", padx=(10, 0))

        ttk.Label(frame, text="上传结果：").pack(anchor="w", pady=(8, 2))
        self._reference_result = tk.Text(frame, height=12, wrap="word", relief="flat")
        self._reference_result.pack(fill="both", expand=True)
        self._reference_result.configure(state="disabled")
        self._reference_result.insert("1.0", "还没有上传。")
        ttk.Button(frame, text="关闭", command=window.destroy).pack(anchor="e", pady=(10, 0))

    def _pick_reference_file(self, kind: str) -> None:
        """选一个 CSV 文件并导入。导入在后台线程里跑。"""
        path = filedialog.askopenfilename(
            title="选择商品数据" if kind == "products" else "选择客户数据",
            filetypes=[("CSV 表格", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self._set_reference_result(f"正在读取 {os.path.basename(path)} ……")

        def work() -> None:
            try:
                with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                    text = handle.read()
                store = getattr(self.app, "orders", None)
                tenant = getattr(self.app, "tenant", "")
                if store is None:
                    outcome = {"error": "本地数据还没准备好"}
                elif kind == "products":
                    outcome = store.import_products_csv(tenant, text)
                else:
                    outcome = store.import_customers_csv(tenant, text)
                outcome = dict(outcome)
                outcome["kind"] = kind
            except UnicodeDecodeError:
                outcome = {"error": "这个文件不是 UTF-8 编码。用 Excel 另存为 "
                                    "「CSV UTF-8」再试一次。"}
            except Exception as exc:  # 读文件/写库都可能出问题，不能让界面崩
                outcome = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            self._post("reference", outcome=outcome)

        self._spawn("reference", work)

    def _on_reference(self, outcome: dict[str, Any]) -> None:
        """主线程入口：把导入结果显示出来。"""
        if outcome.get("error"):
            self._set_reference_result("上传失败：" + str(outcome["error"]))
            return
        what = "商品" if outcome.get("kind") == "products" else "客户"
        lines = [f"{what}数据已上传：",
                 f"  新增 {outcome.get('created', 0)} 条",
                 f"  更新 {outcome.get('updated', 0)} 条"]
        skipped = outcome.get("skipped", 0)
        if skipped:
            lines.append(f"  跳过 {skipped} 条")
        problems = outcome.get("problems") or []
        if problems:
            lines.append("")
            lines.append("下面这些行没能读进去，改完可以再传一次：")
            lines.extend(f"  {p}" for p in problems[:30])
            if len(problems) > 30:
                lines.append(f"  …还有 {len(problems) - 30} 条")
        if not problems and not skipped:
            lines.append("")
            lines.append("全部读进去了。以后客户怎么叫都认得出来。")
        self._set_reference_result("\n".join(lines))

    def _set_reference_result(self, text: str) -> None:
        widget = getattr(self, "_reference_result", None)
        if not self._alive(widget):
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _on_privacy(self) -> None:
        messagebox.showinfo("云端会发送哪些信息", PRIVACY_TEXT)

    def _on_support(self) -> None:
        """出问题时给商户一个能发给客服的东西。

        没有这个入口，日志就等于不存在 —— 商户不会想到去数据目录里翻文件。
        这里做两件事：告诉他日志在哪，以及怎么打开那个文件夹。
        """
        from . import diagnostics

        path = diagnostics.path_of(self.app.data_dir)
        exists = path.exists()
        try:
            size = path.stat().st_size if exists else 0
        except OSError:
            size = 0
        if exists:
            body = (f"日志文件在：\n{path}\n\n"
                    f"当前大小 {size // 1024} KB。"
                    "出问题时把这个文件发给客服即可。\n\n"
                    "日志里不记录聊天内容，也不记录任何密码或密钥。")
        else:
            body = (f"日志会记录在这里：\n{path}\n\n"
                    "现在还没有内容 —— 说明程序还没遇到需要记录的情况。\n\n"
                    "日志里不记录聊天内容，也不记录任何密码或密钥。")
        if messagebox.askyesno("排查问题", f"{body}\n\n现在打开这个文件夹吗？"):
            try:
                os.startfile(str(path.parent))  # noqa: S606 - Windows 打开资源管理器
            except Exception:
                messagebox.showinfo("日志位置", str(path.parent))

    # ------------------------------------------------------------- 生命周期

    #: 保留这个属性名只为兼容旧代码。会话的真实来源是 ``app.session``——
    #: 这个字段**从来没有被赋过值**，所以以前那句 ``if self.session is not None``
    #: 永远为假，顶栏登录后依然显示「账号：未登录」，商户不知道登的是谁。
    session: Any = None

    def _tick(self) -> None:
        if self._closing:
            return
        self._tick_index = getattr(self, "_tick_index", 0) + 1
        if self._tick_index % 3 == 0 and self.app.wizard.step == WizardStep.DONE:
            self._spawn("home", self.app.refresh_home)
        self.root.after(POLL_INTERVAL_MS, self._tick)

    def _spawn(self, name: str, function: Callable[[], Any]) -> None:
        threading.Thread(target=function, name=f"ui-{name}", daemon=True).start()

    def _on_close(self) -> None:
        self._closing = True
        try:
            self.app.close()
        finally:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


LABEL_FOR_COUNTER = {
    "effective": "有效订单",
    "actionable": "需要处理",
    "send_errors": "发送异常",
    "payment_claims": "待核对付款",
    "clarifications": "待澄清（不算订单）",
}


def _custom_window(days: int):
    from datetime import timedelta

    from .history_read import TimeWindow

    return TimeWindow(start_delta=timedelta(days=max(1, days)), label="custom")


PRIVACY_TEXT = (
    "登录之后，只有在您主动要求分析某条群消息时，软件才会把本次需要分析的内容发送到云端：\n\n"
    "· 这一条待分析的消息内容和发送时间\n"
    "· 已绑定的客户身份（姓名、群昵称）\n"
    "· 商品资料和单位规则\n"
    "· 该客户在该消息时间之前的近期订单和已确认的习惯\n"
    "· 该订单当前的状态\n\n"
    "不会发送：您的微信聊天全库、微信登录状态、其他客户的数据。\n\n"
    "数据保留：\n"
    "· 完整消息、订单和核对记录保存在本机，长期保留，断网也能查看。\n"
    "· 云端在分析完成后立即删除提交的消息正文，只保留分析结论。\n"
    "· 云端使用独立临时会话，任务结束即清理，不作为您的长期记忆。\n\n"
    "如果套餐到期或网络不通，本地订单查看、人工修改和导出仍然可以正常使用。"
)


def audit_text(text: str) -> list[str]:
    """Return any technical term that leaked into merchant-facing text."""
    lowered = text.lower()
    return [term for term in FORBIDDEN_TERMS if term in lowered]


def merchant_labels(job_state: str, error_code: str = "") -> str:
    """Merchant wording for a cloud job state.

    Kept as the single source shared by the interface and the local parse
    service, so a state can never be rendered two different ways.
    """
    from .client_login import status_label

    return status_label(job_state, error_code)


__all__ = [
    "DISCOVERY_NOTE",
    "FORBIDDEN_TERMS",
    "MerchantWindow",
    "PRIVACY_TEXT",
    "audit_text",
    "merchant_labels",
]


def _local_api_key() -> str:
    """共用逻辑：本地配置的上游密钥。详见 ``summary_task``。"""
    return summary_task.local_api_key()
