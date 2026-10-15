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

import csv
import json
import logging
import os
import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Mapping

from .application import AppEvents, MerchantApplication
from .conversations import Conversation, ConversationKind, search
from .dashboard import METRIC_DEFINITIONS, STATUS_LABELS, HomeView
from .history_read import HistoryProgress, HistoryRange, ReadMode, TimeWindow, DEFAULT_MAX_MESSAGES
from .live_listen import (
    LISTEN_DEGRADED,
    LISTEN_FAILED,
    LISTEN_RUNNING,
    LISTEN_STARTING,
    LISTEN_STOPPED,
)
from .runmode import RunMode
from . import summary_task
from .wechat_state import WeChatState, uia_automation_available
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

#: 内容为空时按类型显示占位。图片/语音等本来就没有文字可显示，
#: 空着商户会以为读坏了。只影响显示，不改库里原文，导出仍是空。
EMPTY_CONTENT_LABELS = {
    "image": "［图片］",
    "voice": "［语音］",
    "video": "［视频］",
    "file": "［文件］",
    "link": "［链接］",
    "quote": "［引用］",
    "card": "［名片］",
    "location": "［位置］",
    "transfer": "［转账］",
    "red_packet": "［红包］",
    "history": "［聊天记录］",
    "other": "［非文字消息］",
}


def display_content(kind: Any, content: Any) -> str:
    """核对页内容格的显示文本。有字显示字，没字按类型给占位。"""
    text = str(content or "").strip()
    if text:
        return text
    return EMPTY_CONTENT_LABELS.get(str(kind or "").strip().lower(), "［非文字消息］")


#: 会话列表的显示行数。最多 6 行，给底部「下一步」留出空间；更多项目在表格内滚动。
CONV_ROWS_MIN = 4
CONV_ROWS_MAX = 6


def conv_rows_height(count: int) -> int:
    """会话列表该显示几行。

    只有 2 个群却占着 12 行的高度，页面下半截全是空白，看着像坏了。
    """
    return max(CONV_ROWS_MIN, min(CONV_ROWS_MAX, int(count)))


def build_review_docx(rows: list) -> bytes:
    """核对明细拼成 docx 文件字节。同样不依赖 python-docx：docx 也是
    zip 包 xml，一张表走天下，Word/WPS 直接打开。
    """
    import io
    import zipfile
    from xml.sax.saxutils import escape

    def _clean(text: Any) -> str:
        # Word 见到非法 XML 字符直接报损坏：\t\n 留下，其余控制字符去掉。
        return "".join(
            ch for ch in str(text or "")
            if ch in ("\n", "\t") or ch >= " "
        )

    def _cell(text: str, bold: bool = False, width: str = "") -> str:
        run = f"<w:r><w:rPr>{'<w:b/>' if bold else ''}</w:rPr><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r>"
        paragraph = f"<w:p><w:pPr><w:spacing w:after=\"0\"/></w:pPr>{run}</w:p>"
        tc_w = f"<w:tcW w:w=\"{width}\" w:type=\"dxa\"/>" if width else ""
        return f"<w:tc><w:tcPr>{tc_w}</w:tcPr>{paragraph}</w:tc>"

    headers = ["会话", "时间", "发送人", "类型", "内容"]
    widths = ["1800", "2000", "2000", "1200", "5600"]
    table_rows = ["<w:tr>" + "".join(
        _cell(header, bold=True, width=width)
        for header, width in zip(headers, widths)
    ) + "</w:tr>"]
    for row in rows or []:
        values = [row.get("group", ""), row.get("sent_at", ""), row.get("sender", ""),
                  row.get("kind", ""), row.get("content", "")]
        table_rows.append("<w:tr>" + "".join(
            _cell(_clean(value), width=width)
            for value, width in zip(values, widths)
        ) + "</w:tr>")
    document = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        "<w:body><w:tbl>"
        "<w:tblPr><w:tblW w:w=\"0\" w:type=\"auto\"/>"
        "<w:tblBorders><w:top w:val=\"single\" w:sz=\"4\"/>"
        "<w:left w:val=\"single\" w:sz=\"4\"/>"
        "<w:bottom w:val=\"single\" w:sz=\"4\"/>"
        "<w:right w:val=\"single\" w:sz=\"4\"/>"
        "<w:insideH w:val=\"single\" w:sz=\"4\"/>"
        "<w:insideV w:val=\"single\" w:sz=\"4\"/></w:tblBorders></w:tblPr>"
        f"<w:tblGrid>{''.join(f'<w:gridCol w:w=\"{w}\"/>' for w in widths)}</w:tblGrid>"
        f"{''.join(table_rows)}</w:tbl></w:body></w:document>"
    )
    content_types = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        "<Override PartName=\"/word/document.xml\" "
        "ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>"
        "</Types>"
    )
    rels = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" "
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" "
        "Target=\"word/document.xml\"/></Relationships>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def build_review_xlsx(rows: list) -> bytes:
    """核对明细拼成 xlsx 文件字节。不依赖 openpyxl：xlsx 就是 zip 包 xml，
    商户机器可能没网装不了库，标准库手写最稳，Excel/WPS 直接打开。

    单元格全用 inline 字符串，不建 sharedStrings 表 —— 实现短一半，
    几千行以内体积差别可忽略。
    """
    import io
    import zipfile
    from xml.sax.saxutils import escape

    def _clean(text: Any) -> str:
        # Excel 见到控制字符直接报"文件损坏"：\n\t 留下，其余控制字符去掉。
        return "".join(
            ch for ch in str(text or "")
            if ch == "\n" or ch == "\t" or ch >= " "
        )

    def _col(index: int) -> str:
        letters = ""
        index += 1
        while index:
            index, rest = divmod(index - 1, 26)
            letters = chr(65 + rest) + letters
        return letters

    headers = ["会话", "时间", "发送人", "类型", "内容"]
    widths = [18, 20, 18, 12, 80]
    sheet_rows = ["<row r=\"1\">" + "".join(
        f"<c r=\"{_col(i)}1\" t=\"inlineStr\"><is><t>{escape(h)}</t></is></c>"
        for i, h in enumerate(headers)
    ) + "</row>"]
    for number, row in enumerate(rows or [], start=2):
        values = [row.get("group", ""), row.get("sent_at", ""), row.get("sender", ""),
                  row.get("kind", ""), row.get("content", "")]
        sheet_rows.append("<row r=\"%d\">" % number + "".join(
            f"<c r=\"{_col(i)}{number}\" t=\"inlineStr\"><is><t>{escape(_clean(v))}</t></is></c>"
            for i, v in enumerate(values)
        ) + "</row>")
    cols = "".join(
        f"<col min=\"{i + 1}\" max=\"{i + 1}\" width=\"{w}\" customWidth=\"1\"/>"
        for i, w in enumerate(widths)
    )
    sheet = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">"
        f"<cols>{cols}</cols><sheetData>{''.join(sheet_rows)}</sheetData></worksheet>"
    )
    content_types = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        "<Override PartName=\"/xl/workbook.xml\" "
        "ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/>"
        "<Override PartName=\"/xl/worksheets/sheet1.xml\" "
        "ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>"
        "</Types>"
    )
    rels = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" "
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" "
        "Target=\"xl/workbook.xml\"/></Relationships>"
    )
    workbook = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">"
        "<sheets><sheet name=\"读取明细\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>"
    )
    workbook_rels = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" "
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" "
        "Target=\"worksheets/sheet1.xml\"/></Relationships>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


class MerchantWindow:
    """主窗口。向导与首页共用一个窗口。"""

    def __init__(self, app: MerchantApplication):
        self.app = app
        self._events: queue.Queue = queue.Queue()
        self._closing = False
        self._quota_refresh_lock = threading.Lock()
        self._quota_refresh_in_flight = False
        self._last_quota_refresh = 0.0
        self._tick_index = 0
        self._conversations: list[Conversation] = []
        #: 会话页两栏各自"当前可见"的会话。Treeview 的 iid 就是它在所属那一份里的
        #: 下标，所以取回会话时必须查同一份，不能查全量列表。
        self._conv_visible: dict[str, list[Conversation]] = {}
        #: 打勾选中的会话 identity。按 identity 存，搜索过滤、翻页重画都不丢；
        #: 下一步只认这一份，不认 Treeview 的高亮行。
        self._conv_checked: set[str] = set()
        self._history_rows: dict[str, bool] = {}
        self._live_choice: Conversation | None = None
        self._widgets: dict[str, Any] = {}
        #: 最近一次整理结果。页面被导航重建后还能补画，不会白跑一次分析。
        self._last_summary: dict[str, Any] = {}
        self._last_analysis_details: list[dict[str, Any]] = []
        self._last_paragraph_outcome: dict[str, Any] = {}
        self._analysis_results_mode = False
        self._analysis_call_active = False
        self._analysis_running = False
        self._last_analysis_error = ""
        #: 最近一次角色整理的分类报告。同上，跨页面重建存活。
        self._last_role_report: dict[str, Any] = {}
        #: 最近一次首页数据。同上，重建后补画。
        self._last_home: HomeView | None = None
        self._home_frame: Any = None
        #: 最近一次监听的视图。
        self._last_listen: dict[str, Any] | None = None
        #: 最近一次读取结果。跨页面重建存活 —— 否则商户从别处返回第 3 步，
        #: 会看到一句写死的「准备中」，明明早就读完了。界面不能说假话。
        self._last_read_result: Any = None
        #: 上次确认的读取配置。执行页「上一步」回来改范围，原样恢复，不重选。
        self._last_read_config: dict = {}
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
            on_logged_in=lambda: self._post("logged_in"),
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
        try:
            from .version import __version__ as _app_version
        except Exception:
            _app_version = ""
        self.root.title(f"微信AI智能助理 v{_app_version}" if _app_version else "微信AI智能助理")
        # 900x760 在常见的 768 高笔记本屏幕上会把窗口底部和「下一步」
        # 挤到任务栏后面。Tk 的底部操作栏固定显示，所以按屏幕高度给默认
        # 窗口留出标题栏/任务栏空间；页面内容本身可滚动。
        screen_height = self.root.winfo_screenheight()
        default_height = min(760, max(600, screen_height - 120))
        self.root.geometry(f"900x{default_height}")
        self.root.minsize(760, default_height)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain)
        # 回焦点自动刷：去微信聊完回来，第 2 步的列表自己新。
        # 只在第 2 步、超 60 秒没刷过、且当前没在刷时才动 ——
        # 弹窗关掉、切出去看一眼这种高频焦点事件不能每次都扫一遍库。
        try:
            self.root.bind("<FocusIn>", self._on_focus_back, add="+")
        except Exception:
            pass
        self.app.start_background()
        self._show_step(self.app.wizard.step)
        # 每次启动静默更新可用模型，网络请求不阻塞窗口首屏。
        if self.app.session is not None:
            self._spawn("models", lambda: self._post("models_loaded", catalog=self.app.load_models(force=True)))
        self.root.after(400, self._tick)

    # ------------------------------------------------------------------ ui

    def _build(self) -> None:
        from . import ui_theme

        ui_theme.apply_theme(self.root)

        top = ttk.Frame(self.root, style="TopBar.TFrame", padding=(18, 12))
        top.pack(fill="x")
        # 品牌 + 版本：标题栏也有版本，这里给人看（标题栏经常被截掉）。
        brand_row = ttk.Frame(top, style="TopBar.TFrame")
        brand_row.pack(side="left")
        try:
            from .version import __version__ as _app_version
        except Exception:
            _app_version = ""
        ttk.Label(brand_row, text="微信AI智能助理", style="Brand.TLabel").pack(side="left")
        if _app_version:
            try:
                from .version import SUPPORTED_WECHAT as _supported
            except Exception:
                _supported = ""
            suffix = f"　支持{_supported}" if _supported else ""
            ttk.Label(brand_row, text=f"  v{_app_version}{suffix}",
                      style="Muted.TLabel").pack(side="left")
        # 更新提示位：平时空着，新版下载好才出现一句话。
        # 静默更新不能等于"一声不吭" —— 商户下次打开发现变了版本，
        # 得有个地方能对上发生了什么。
        self._widgets["update_note"] = ttk.Label(brand_row, text="", style="Muted.TLabel")
        self._widgets["update_note"].pack(side="left")
        # 账号条做成**按钮**：登录是所有事情的前提，而入口以前只在第 4 步，
        # 顶栏写着「账号：未登录」却点不动 —— 商户看得到状态，动不了。
        # 登录后这里也要能退出：以前能登录但**永远登不出去**，收银电脑换人
        # 就登不进，本地数据还跟着上一位走。
        self._widgets["account"] = ttk.Button(
            top, text="账号：未登录　点此登录", style="Subtle.TButton",
            command=self._on_account_click,
        )
        self._widgets["account"].pack(side="right")
        # 步骤条：商户随时知道自己在哪、后面还有几步。
        stepper_bar = ttk.Frame(self.root, style="TopBar.TFrame", padding=(18, 0, 18, 10))
        stepper_bar.pack(fill="x")
        self._stepper = ui_theme.Stepper(stepper_bar)

        self.container = ttk.Frame(self.root, style="Page.TFrame", padding=(18, 4, 18, 0))
        self.container.pack(fill="both", expand=True)

        # 吸底操作栏：各页的"上一步/下一步/主按钮"搬到这里，滚成什么样都点得到。
        # 大厂桌面向导导航本来就在底部；页面带滚动时再让按钮跟着内容走，
        # 等于逼商户滚到底才交卷。顶栏步骤条管"在哪"，这栏管"往哪走"。
        self.footer = ttk.Frame(self.root, style="TopBar.TFrame", padding=(18, 8))
        self.footer.pack(fill="x")
        ttk.Separator(self.footer, orient="horizontal").pack(fill="x", pady=(0, 8))
        self.footer_body = ttk.Frame(self.footer, style="TopBar.TFrame")
        self.footer_body.pack(fill="x")

        self._widgets["log"] = ttk.Label(self.root, text="", style="MutedPage.TLabel",
                                         padding=(18, 2))
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

    def _on_refresh_conversations(self) -> None:
        """点"读取"后立刻给忙态：按钮变灰、转圈条出现，回来再恢复。

        以前点完十几秒没动静，商户以为没点上就反复点 —— 后台扫好几遍，
        越扫越慢。忙的时候按钮是灰的，点不动。
        """
        button = self._widgets.get("conv_refresh_btn")
        if button is not None and self._alive(button):
            button.configure(state="disabled", text="正在读取…")
        busy = self._widgets.get("conv_refresh_busy")
        if busy is not None and self._alive(busy):
            busy.pack(side="left", padx=8)
            try:
                busy.start(15)
            except Exception:
                pass
        self._conv_refreshing = True
        self._spawn("refresh", self._guarded_refresh_conversations)

    def _guarded_refresh_conversations(self) -> None:
        """读列表的实际执行：记时间、清忙态标记，抛异常也不把标记卡死。

        标记卡死的话，回焦点自动刷就永远被"正在刷"挡住 ——
        静默失败一次，列表就再也不自动新了。
        """
        self._last_conv_refresh_at = time.time()
        try:
            self.app.refresh_conversations()
        finally:
            self._conv_refreshing = False

    def _set_conv_refresh_idle(self) -> None:
        button = self._widgets.get("conv_refresh_btn")
        if button is not None and self._alive(button):
            button.configure(state="normal", text="刷新列表")
        busy = self._widgets.get("conv_refresh_busy")
        if busy is not None and self._alive(busy):
            try:
                busy.stop()
            except Exception:
                pass
            busy.pack_forget()

    def _on_conversations(self, conversations: list[Conversation]) -> None:
        self._conversations = conversations
        self._history_rows = {}
        self._on_redraw_conversations()
        self._set_conv_refresh_idle()
        self._refresh_wcdb_hint()
        hint = self._widgets.get("conv_cache_hint")
        if hint is not None and self._alive(hint):
            hint.configure(text=f"显示上次记录。{self.app.conversation_cache_info()}，点「刷新列表」更新。")

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
        self._refresh_wechat_gate(status)
        self._refresh_wechat_recheck(status)
        self._refresh_wechat_restart(status)

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
        self._set_conv_refresh_idle()
        if self._analysis_results_mode:
            self._last_analysis_error = str(text)
        shown = False
        for key in ("read_state", "analysis_result_summary", "analyze_notice", "analyze"):
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

    def _on_update_staged(self, info: Any) -> None:
        """新版已下载暂存：在顶栏版本号后面留一句话，下次打开自动更新。"""
        try:
            version = str((info or {}).get("version") or "")
        except Exception:
            version = ""
        if not version:
            return
        widget = self._widgets.get("update_note")
        if widget is not None and self._alive(widget):
            widget.configure(text=f"　新版 v{version} 已就绪，下次打开自动更新")

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
        if step == WizardStep.ANALYZE and self._analysis_call_active:
            if getattr(self, "_visible_step", None) == WizardStep.RESULTS:
                return
            step = WizardStep.RESULTS
        # 向导标记 DONE 后，刚完成整理的这次运行落在第七步详情页；
        # 普通启动/回到首页仍走首页。
        if step == WizardStep.DONE and self._analysis_results_mode:
            if getattr(self, "_visible_step", None) == WizardStep.RESULTS:
                return
            step = WizardStep.RESULTS
        self._visible_step = step
        for child in self.container.winfo_children():
            child.destroy()
        # 吸底栏的按钮是各页的，各页重建时一起清掉重画。
        footer_body = getattr(self, "footer_body", None)
        if footer_body is not None:
            for child in footer_body.winfo_children():
                child.destroy()
        # 滚动条是全局绑定的，翻页时先解掉，新页需要再绑 —— 否则滚轮事件
        # 会发到已销毁的画布上。
        try:
            self.root.unbind_all("<MouseWheel>")
        except Exception:
            pass
        for key, _title, _kind in self.CONV_TABS:
            self._widgets.pop(f"conv_tree_{key}", None)
        self._widgets.pop("conv_notebook", None)
        self._widgets.pop("conv_checked_count", None)
        self._widgets.pop("conv_cache_hint", None)
        self._widgets.pop("conv_refresh_btn", None)
        self._widgets.pop("conv_refresh_busy", None)
        self._conv_visible.clear()
        self._widgets.pop("progress", None)
        self._widgets.pop("progress_text", None)
        # 首页离开时控件就没了。别拿旧控件 configure —— 会抛 TclError 并打断整个
        # 事件泵，后面排队的事件全丢。
        self._home_frame = None
        for key in ("listen_btn", "listen_state", "listen_stats", "listen_gaps", "listen_note"):
            self._widgets.pop(key, None)
        for key in ("wechat_title", "wechat_detail", "wechat_start", "wechat_recheck",
                    "wechat_restart",
                    "wechat_next", "wechat_gate", "login_switch"):
            self._widgets.pop(key, None)
        for key in ("clar_box", "fail_box", "home_after_tables", "clar_list", "fail_list",
                    "st_origin", "listen_merged"):
            self._widgets.pop(key, None)
        for key in ("read_start", "read_cancel", "read_next", "read_state",
                    "read_update_btn", "read_all_btn", "history_actions",
                    "progress", "progress_text",
                    "read_summary", "read_summary_tree",
                    "review_count", "review_tree",
                    "analyze_button",
                    "analyze_notice", "role_picker", "model_picker",
                    "model_picker_widget",
                     "role_report_frame", "role_report_tree",
                      "scroll_canvas"):
            self._widgets.pop(key, None)
        builder = {
            WizardStep.WECHAT: self._page_wechat,
            WizardStep.CONVERSATIONS: self._page_conversations,
            WizardStep.HISTORY: self._page_history,
            WizardStep.READ: self._page_read,
            WizardStep.REVIEW: self._page_review,
            WizardStep.QUOTA: self._page_analyze,
            WizardStep.ANALYZE: self._page_analyze,
            WizardStep.RESULTS: self._page_analysis_results,
            WizardStep.DONE: self._page_home,
        }.get(step, self._page_wechat)
        builder()
        # 步骤条跟着走。页面可能是测试直接搭的（没有步骤条），缺了就跳过。
        try:
            stepper = getattr(self, "_stepper", None)
            if stepper is not None:
                stepper.set_step(step)
        except Exception:
            pass
        # 吸底栏没按钮就不占地方（首页工具按钮留在页里，不进吸底栏）。
        try:
            footer = getattr(self, "footer", None)
            body = getattr(self, "footer_body", None)
            if footer is not None and body is not None:
                if body.winfo_children():
                    # footer 之前 pack_forget 后重新 pack 会落到 log 后面；
                    # expand=True 的页面容器把空间占完，底栏就被挤出窗口。
                    # 明确放回 log 前，保证底部操作始终在可视区域内。
                    footer.pack(fill="x", before=self._widgets.get("log"))
                else:
                    footer.pack_forget()
        except Exception:
            pass
        # 顶栏账号条每次切页都刷一次。以前只靠登录事件那一下，事件和翻页
        # 时序错开一次，顶栏就永远停在"未登录"，而页里明明已经登上了。
        try:
            self._render_account()
        except Exception:
            pass

    # --------------------------------------------------------------- ① 微信

    def _scrollable_page(self, padding=(18, 6)):
        """可滚动的页面容器：内容超过窗口高度时滚轮往下翻。

        只给超长的页用（目前只有智能整理页）。滚轮是全局绑定的，
        翻页时由 :meth:`_show_step` 统一解绑，不会漏到别的页。
        """
        canvas = tk.Canvas(self.container, highlightthickness=0)
        bar = ttk.Scrollbar(self.container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        try:
            from .ui_theme import CARD_BG
            canvas.configure(background=CARD_BG)
        except Exception:
            pass
        body = ttk.Frame(canvas, style="Page.TFrame", padding=(0, 0, 12, 0))
        self._widgets["scroll_canvas"] = canvas
        inner = canvas.create_window((0, 0), window=body, anchor="nw")

        def _fit(_event=None):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.itemconfig(inner, width=canvas.winfo_width())
            except Exception:
                pass

        body.bind("<Configure>", _fit)
        canvas.bind("<Configure>", _fit)

        def _wheel(event):
            try:
                if not canvas.winfo_exists():
                    return
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            except Exception:
                pass

        self.root.bind_all("<MouseWheel>", _wheel)
        return body

    def _scroll_page_to_end(self) -> None:
        """把滚动页滚到底。商户点最下面的按钮前不用自己找滚动条；
        单测也用它把底部的按钮滚进窗口再断言可见性。"""
        canvas = self._widgets.get("scroll_canvas")
        try:
            if canvas is not None and canvas.winfo_exists():
                canvas.update_idletasks()
                canvas.yview_moveto(1.0)
        except Exception:
            pass

    @staticmethod
    def _tree_with_scroll(parent: Any, *, columns: Any, show: str = "headings",
                          height: int = 8, **kwargs: Any):
        """带自己滚条的表格：长名单在框里滚，不把整页撑长。

        页面级滚动只留作小窗口兜底。返回 (外框, 表格)，外框按原来
        表格的 pack 方式摆。
        """
        box = ttk.Frame(parent)
        tree = ttk.Treeview(box, columns=columns, show=show, height=height,
                            **kwargs)
        bar = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return box, tree

    def _page_wechat(self) -> None:
        frame = self._scrollable_page()
        status = self.app.monitor.latest
        title, detail, button = status.copy()
        if status.state == WeChatState.UNREADABLE and status.detail == "尚未检测":
            self._spawn("wechat-check", lambda: self.app.check_wechat())
            # 还没查过就别摆失败脸：检测在后台跑，结果回来自动重画。
            title, detail, button = (
                "正在检测微信状态…",
                "第一次打开会慢几秒，结果出来这一页会自动刷新，什么都不用点。",
                "",
            )
        ttk.Label(frame, text="第 1 步　登录并检查微信", style="SectionPage.TLabel").pack(
            anchor="w", pady=(4, 12))

        # ---- 先微信、后产品登录 ----
        #
        # 以前登录框压在最上面（单测还锁着那个顺序），但真正卡住流程的是微信
        # 状态，商户先看到的却是一个跟当前步骤无关的登录框。
        # 现在主角是微信：状态 + 动作在上，产品登录在下；且未登录产品账号时
        # 不许进入下一步（门禁判据见 Wizard.product_gate_open）。
        status_box = ttk.LabelFrame(frame, text="微信状态", padding=12)
        status_box.pack(fill="x")
        self._widgets["wechat_title"] = ttk.Label(status_box, text=title, font=("Microsoft YaHei", 13, "bold"))
        self._widgets["wechat_title"].pack(anchor="w")
        self._widgets["wechat_detail"] = ttk.Label(status_box, text=detail, wraplength=620, foreground="#5a6572")
        self._widgets["wechat_detail"].pack(anchor="w", pady=(6, 14))
        # 两个按钮都挂在页面上、保持稳定：状态变化时只改文案和「打开微信」的
        # 显隐，不销毁重建。销毁重建会闪烁，而且别处按子控件找按钮时会找不到。
        self._widgets["wechat_start"] = ttk.Button(
            status_box, text=button or "", style="Big.TButton", command=self._on_start_wechat
        )
        if button:
            self._widgets["wechat_start"].pack(anchor="w")
        self._widgets["wechat_recheck"] = ttk.Button(
            status_box, text="重新检测", style="Subtle.TButton",
            command=lambda: self._spawn("wechat-check", lambda: self.app.check_wechat())
        )
        self._widgets["wechat_recheck"].pack(anchor="w", pady=(10, 0))
        # 更新后卡死专用：进程在、界面出不来时才出现。平时藏着，
        # 正常情况多个按钮只会让人不知道点哪个。
        self._widgets["wechat_restart"] = ttk.Button(
            status_box, text="重启微信", style="Subtle.TButton", command=self._on_restart_wechat
        )
        self._refresh_wechat_restart(status)
        # 显式的下一步：自动推进是兜底，真正在界面上说了算的是这个按钮。
        # 条件不满足时点它会说清楚还差什么，而不是没反应。
        # 放吸底栏：滚成什么样都点得到。
        self._widgets["wechat_next"] = ttk.Button(
            self.footer_body, text="下一步：选择会话", style="Big.TButton", command=self._on_wechat_next
        )
        self._widgets["wechat_next"].pack(side="left")
        # 门禁提示位：微信就绪但产品未登录时，告诉商户还差什么。
        self._widgets["wechat_gate"] = ttk.Label(
            status_box, text="", foreground="#a8590b", wraplength=620,
        )
        self._widgets["wechat_gate"].pack(anchor="w", pady=(10, 0))
        self._refresh_wechat_gate(status)
        self._refresh_wechat_recheck(status)
        self._refresh_wechat_restart(status)

        ttk.Label(
            frame,
            text="如无必要，请不要升级微信客户端，新版可能导致暂时无法读取。\n"
                 "杀毒软件如有报警均为误报：本工具只读本机数据，不上传您的聊天内容与个人信息。",
            foreground="#7b8794", wraplength=620,
        ).pack(anchor="w", pady=(24, 0))

    def _refresh_wechat_recheck(self, status: Any = None) -> None:
        """「重新检测」显隐：微信已登录就不摆了，摆着也是多余的一行。

        登录掉线/没登录时才需要它。和「重启微信」一样只改显隐不重建。
        """
        button = self._widgets.get("wechat_recheck")
        if button is None or not self._alive(button):
            return
        show = (self.app.wizard.step == WizardStep.WECHAT
                and getattr(status, "state", "") != WeChatState.LOGGED_IN)
        # 看 pack 状态而不是 ismapped：窗口 withdraw 时 ismapped 恒 0，
        # 按它判断会把已经摆好的按钮反复 pack（闪一下）。
        try:
            managed = "in" in button.pack_info()
        except Exception:
            managed = bool(button.winfo_ismapped())
        if show and not managed:
            button.pack(anchor="w", pady=(10, 0))
        elif not show and managed:
            button.pack_forget()

    def _refresh_wechat_gate(self, status: Any = None) -> None:
        """第 1 步的门禁提示。微信就绪、但产品账号没登录时才出现。"""
        label = self._widgets.get("wechat_gate")
        if label is None or not self._alive(label):
            return
        if self.app.wizard.step != WizardStep.WECHAT:
            label.pack_forget()
            return
        state = getattr(status, "state", "")
        gate_open = self.app.wizard.product_gate_open()
        if state == WeChatState.LOGGED_IN and not gate_open:
            label.configure(text="微信已就绪，还差一步：点右上角「账号」登录产品账号，登录后自动进入下一步。")
            if not label.winfo_ismapped():
                label.pack(anchor="w", pady=(10, 0))
        else:
            label.pack_forget()

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

    def _refresh_wechat_restart(self, status: Any = None) -> None:
        """「重启微信」按钮显隐：只有读不到界面时才出现。"""
        button = self._widgets.get("wechat_restart")
        if button is None or not self._alive(button):
            return
        show = (self.app.wizard.step == WizardStep.WECHAT
                and getattr(status, "state", "") == WeChatState.UNREADABLE)
        try:
            managed = "in" in button.pack_info()
        except Exception:
            managed = bool(button.winfo_ismapped())
        if show and not managed:
            button.pack(anchor="w", pady=(10, 0))
        elif not show and managed:
            button.pack_forget()

    def _on_restart_wechat(self) -> None:
        """点了「重启微信」。关掉重开要丢未发送的内容，先问一句。"""
        if not messagebox.askyesno(
            "重启微信",
            "会先关闭微信再重新打开。微信里没发出去的内容可能会丢失，\n"
            "聊天记录在本地，不会丢。\n\n确定重启吗？",
        ):
            return
        self._widgets["wechat_title"].configure(text="正在重启微信，请稍等…")
        self._spawn("wechat-restart", self._do_restart_wechat)

    def _do_restart_wechat(self) -> None:
        from .WeChatTools import Tools

        if not Tools.restart_weixin():
            self._post("log", text="微信没能重新打开，请手动打开微信后再点「重新检测」。")
            return
        import time as _time
        _time.sleep(8)
        self.app.check_wechat()

    def _on_wechat_next(self) -> None:
        """第 1 步「下一步」。条件不满足就明说，不静默。"""
        status = self.app.monitor.latest
        if not status.ready:
            messagebox.showinfo(
                "微信还没就绪",
                "微信还没登录或窗口没打开。先在微信里登录并把窗口打开，"
                "再点「重新检测」，就绪后点这里继续。",
            )
            self._spawn("wechat-check", lambda: self.app.check_wechat())
            return
        if not self.app.wizard.product_gate_open():
            messagebox.showinfo(
                "还差产品登录",
                "微信已经就绪。登录下面的产品账号后即可进入下一步。",
            )
            return
        self.app.wizard._go(WizardStep.CONVERSATIONS)

    # --------------------------------------------------------------- ② 会话

    #: 会话分成两栏：商户做的是群里的生意，群聊必须一眼看全；联系人动辄几百个，
    #: 混在一起会把群挤到看不见的地方。真机上实测是 10 个群对 246 个联系人。
    CONV_TABS = (
        ("groups", "群聊", ConversationKind.GROUP),
        ("friends", "联系人", ConversationKind.FRIEND),
    )

    def _page_conversations(self) -> None:
        # This step fits the fixed-height window: the conversation tables have
        # their own scrollbars, so the whole page should not scroll vertically.
        frame = ttk.Frame(self.container, style="Page.TFrame", padding=(18, 6))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="第 2 步　选择会话", style="SectionPage.TLabel").pack(anchor="w", pady=(4, 6))

        # Treeview 的勾选框是用字符绘制的，单独提高这一页的字号和行高，
        # 让方框更清楚，也让每行更容易点中；其他表格仍保留原来的密度。
        ttk.Style().configure(
            "Conversation.Treeview",
            font=("Microsoft YaHei", 12),
            rowheight=34,
        )

        search_row = ttk.Frame(frame)
        search_row.pack(fill="x", pady=(0, 8))
        self._widgets["conv_refresh_btn"] = ttk.Button(
            search_row, text="刷新列表",
            command=self._on_refresh_conversations,
        )
        self._widgets["conv_refresh_btn"].pack(side="left")
        self._widgets["conv_refresh_busy"] = ttk.Progressbar(
            search_row, mode="indeterminate", length=120)
        self._widgets["conv_checked_count"] = tk.StringVar(value="已选 0 个")
        ttk.Label(search_row, textvariable=self._widgets["conv_checked_count"],
                  foreground="#1a7f37").pack(side="left", padx=8)
        self._widgets["conv_search"] = tk.StringVar()
        ttk.Label(search_row, text="搜索").pack(side="left", padx=(12, 4))
        search_entry = ttk.Entry(search_row, textvariable=self._widgets["conv_search"], width=28)
        search_entry.pack(side="left", fill="x", expand=True)
        search_entry.bind(
            "<Escape>",
            lambda _event: self._widgets["conv_search"].set(""),
        )
        self._widgets["conv_search"].trace_add(
            "write", lambda *_args: self._on_redraw_conversations())
        wcdb_row = ttk.Frame(frame)
        wcdb_row.pack(fill="x", pady=(0, 8))
        self._widgets["conv_wcdb_btn"] = ttk.Button(
            wcdb_row, text="重新提取密钥",
            command=self._on_ensure_wcdb_keys,
        )
        self._widgets["conv_wcdb_btn"].pack(side="left")
        self._widgets["conv_wcdb_hint"] = ttk.Label(
            wcdb_row, text="", foreground="#7b8794", wraplength=620)
        self._widgets["conv_wcdb_hint"].pack(side="left", padx=8)
        self._refresh_wcdb_hint()
        # 重启后列表还在：启动时从落盘缓存恢复，这里只负责画出来，
        # 真正的新数据由进页自动刷 + 回焦点刷进来（见 _maybe_auto_refresh）。
        self._conversations = list(self.app.wizard.conversations.available)
        # 勾选只认还在名单里的：切号后旧勾选要是留着，下一步会带走别人的群。
        valid = {item.identity for item in self._conversations}
        self._conv_checked = {identity for identity in self._conv_checked if identity in valid}
        self._widgets["conv_cache_hint"] = ttk.Label(
            frame,
            text=f"显示上次记录。{self.app.conversation_cache_info()}，点「刷新列表」更新。",
            foreground="#7b8794", wraplength=700,
        )
        self._widgets["conv_cache_hint"].pack(anchor="w", pady=(0, 4))

        notebook = ttk.Notebook(frame)
        notebook.pack(fill="both", expand=True)
        self._widgets["conv_notebook"] = notebook
        for key, title, _kind in self.CONV_TABS:
            page = ttk.Frame(notebook, padding=(4, 6))
            notebook.add(page, text=title)
            # 全选只管这一栏：放顶栏会被当成"两栏全选"，歧义就是这么来的。
            tab_bar = ttk.Frame(page)
            tab_bar.pack(fill="x", pady=(0, 4))
            ttk.Button(
                tab_bar, text="全选本页",
                command=lambda tab=key: self._conv_check_all(True, tab),
            ).pack(side="left")
            ttk.Button(
                tab_bar, text="全不选",
                command=lambda tab=key: self._conv_check_all(False, tab),
            ).pack(side="left", padx=6)
            # Treeview 没有原生 checkbox：第一列画 ☐/☑，点中该格切换。
            columns = ("check", "pick", "name", "members")
            tree_box, tree = self._tree_with_scroll(
                page, columns=columns, show="headings", height=8,
                selectmode="extended", style="Conversation.Treeview")
            tree_box.pack(fill="both", expand=True)
            # 列名要说实话：这一列放的是人数，不是别的。以前叫「说明」，
            # 里面却写着「64 人」，商户得自己想那是什么。
            for column, heading, width, anchor in (
                ("check", "选", 48, "center"),
                ("pick", "已读", 56, "center"),
                ("name", "会话", 400, "w"),
                ("members", "人数", 90, "e"),
            ):
                tree.heading(column, text=heading)
                if column == "check":
                    tree.column(column, width=width, anchor=anchor, stretch=False)
                else:
                    tree.column(column, width=width, anchor=anchor)
            tree.tag_configure("dup", foreground="#a8590b")
            tree.bind("<Button-1>", lambda event, tab=key: self._on_conv_toggle(event, tab))
            self._conv_visible[key] = []
            self._widgets[f"conv_tree_{key}"] = tree

        actions = ttk.Frame(self.footer_body)
        actions.pack(fill="x")
        ttk.Button(actions, text="下一步：读取历史", style="Big.TButton", command=self._on_history_next).pack(side="left")
        ttk.Button(actions, text="上一步", command=lambda: self._show_step(WizardStep.WECHAT)).pack(side="left", padx=8)

        self._on_redraw_conversations()
        self._maybe_auto_refresh_conversations()

    def _maybe_auto_refresh_conversations(self) -> None:
        """进第 2 步就自动读一次，和微信一致：每次看列表都是新的。

        唯一的 guard 是微信没登录 —— 那时扫内存提钥匙是无用功（几十秒），
        等用户手动点（点了会自动提）。钥匙提取本身有 5 分钟节流，
        进出翻页不会反复扫。
        """
        try:
            logged_in = (self.app.monitor.latest.state == WeChatState.LOGGED_IN)
        except Exception:
            logged_in = False
        if not logged_in:
            return
        self._on_refresh_conversations()

    def _on_focus_back(self, _event: Any = None) -> None:
        """窗口拿回焦点：停在第 2 步且 60 秒没刷过，就刷一次。"""
        try:
            if self.app.wizard.step != WizardStep.CONVERSATIONS:
                return
            if bool(getattr(self, "_conv_refreshing", False)):
                return
            last = float(getattr(self, "_last_conv_refresh_at", 0) or 0)
            if time.time() - last < 60:
                return
        except Exception:
            return
        self._maybe_auto_refresh_conversations()

    def _split_visible(self) -> dict[str, list[Conversation]]:
        """按当前搜索词筛选会话，再拆分到群聊和联系人两栏。"""
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

    def _refresh_wcdb_hint(self) -> None:
        """只有自动提取失败或钥匙不完整时才显示重试入口。

        未登录时 ``wcdb_status`` 也会返回不可用，但这不代表提取失败；
        初次提取由「刷新列表」自动执行，只有尝试过仍不可用才显示重试按钮。
        """
        hint = self._widgets.get("conv_wcdb_hint")
        button = self._widgets.get("conv_wcdb_btn")
        try:
            logged_in = self.app.monitor.latest.state == WeChatState.LOGGED_IN
        except Exception:
            logged_in = False
        if not logged_in:
            if hint is not None and self._alive(hint):
                hint.pack_forget()
            if button is not None and self._alive(button):
                button.pack_forget()
            return
        try:
            ok, message = self.app.wcdb_status()
        except Exception:
            return
        attempted = bool(getattr(self.app, "_wcdb_ensure_attempted", False))
        try:
            keys_complete = bool(self.app._wcdb_keys_complete())
        except Exception:
            keys_complete = ok
        # 页面初始状态可能还没有钥匙；刷新列表会自动尝试提取，只有失败/缺钥匙
        # 的情况才需要把人工重试入口露出来。
        show_retry = attempted and not keys_complete
        if hint is not None and self._alive(hint):
            if not show_retry:
                hint.pack_forget()
            else:
                detail = message if not ok else "检测到尚未提取密钥的新数据库"
                hint.configure(text=f"本机数据库：{detail}。可重新提取密钥后再刷新列表。")
                if not hint.winfo_ismapped():
                    hint.pack(side="left", padx=8)
        if button is not None and self._alive(button):
            if not show_retry:
                button.pack_forget()
            else:
                button.configure(text="重新提取密钥")
                if not button.winfo_ismapped():
                    button.pack(side="left")

    def _on_ensure_wcdb_keys(self) -> None:
        """「重新提取密钥」（重试用）：扫一遍微信内存找钥匙，几十秒，钥匙跟大版本走。

        只读不改微信，提一次一直用。跑完自动刷一遍列表。
        """
        button = self._widgets.get("conv_wcdb_btn")
        if button is not None and self._alive(button):
            button.configure(state="disabled", text="正在提取…")
        self._spawn("wcdb-keys", self._do_ensure_wcdb_keys)

    def _do_ensure_wcdb_keys(self) -> None:
        try:
            ok, message = self.app.ensure_wcdb_keys()
        except Exception as exc:
            ok, message = False, f"提取失败：{exc}"
        self._post("wcdb_keys_done", ok=ok, message=message)

    def _on_wcdb_keys_done(self, ok: bool, message: str = "") -> None:
        button = self._widgets.get("conv_wcdb_btn")
        if button is not None and self._alive(button):
            button.configure(state="normal", text="重新提取密钥")
        self._refresh_wcdb_hint()
        if ok:
            messagebox.showinfo("密钥已就绪", f"{message}")
            self._on_refresh_conversations()
        else:
            messagebox.showinfo("没能提取", message)

    def _on_conv_toggle(self, event, key: str) -> None:
        """点中「选」格切换打勾，其他列不管（走默认高亮）。"""
        tree = self._widgets.get(f"conv_tree_{key}")
        if tree is None:
            return
        if tree.identify_column(event.x) != "#1":
            return
        row = tree.identify_row(event.y)
        if not row:
            return
        rows = self._conv_visible.get(key, [])
        try:
            index = int(row)
        except (TypeError, ValueError):
            return
        if not 0 <= index < len(rows):
            return
        identity = rows[index].identity
        if identity in self._conv_checked:
            self._conv_checked.discard(identity)
        else:
            self._conv_checked.add(identity)
        # 只改这一格，不整表重画——否则滚动条跳回顶部，连勾十几个很烦。
        values = list(tree.item(row, "values"))
        if values:
            values[0] = "☑" if identity in self._conv_checked else "☐"
            tree.item(row, values=values)
        self._update_conv_count()

    def _conv_check_all(self, check: bool, key: str) -> None:
        """全选 / 全不选：只管调用方那一栏。key 由栏内按钮直接传，不猜当前页。"""
        for item in self._conv_visible.get(key, []):
            if check:
                self._conv_checked.add(item.identity)
            else:
                self._conv_checked.discard(item.identity)
        self._on_redraw_conversations()

    def _update_conv_count(self) -> None:
        var = self._widgets.get("conv_checked_count")
        if var is not None:
            try:
                var.set(f"已选 {len(self._conv_checked)} 个")
            except Exception:
                pass

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
            # 真名在前、编号在后：内存里解不出名的群只能显示一串编号，
            # 原来按拼音排，编号全挤在最前面，真名被淹没。三档排序，
            # 档内保持原顺序（sorted 稳定），勾选与行号映射不受影响。
            # 手动找回的置顶：刚找到的群必须一眼看见。
            rows = sorted(buckets[key], key=self._conv_order_key)
            # iid 就是这一栏可见列表的下标，取的时候按同一份列表还原。
            self._conv_visible[key] = rows
            tree.delete(*tree.get_children())
            # 表格高度跟着内容走。
            tree.configure(height=conv_rows_height(len(rows)))
            for index, item in enumerate(rows):
                if item.kind == ConversationKind.GROUP:
                    members_text = f"{item.member_count} 人" if item.member_count else "群"
                else:
                    members_text = "联系人"
                tree.insert(
                    "", "end", iid=str(index),
                    values=(
                        "☑" if item.identity in self._conv_checked else "☐",
                        "✓" if self._history_rows.get(item.identity) else "",
                        self.app.display_name_for_conversation(item),
                        members_text,
                    ),
                    tags=("dup",) if item.name in duplicates else (),
                )

        notebook = self._widgets.get("conv_notebook")
        if notebook is not None:
            for position, (key, title, _kind) in enumerate(self.CONV_TABS):
                notebook.tab(position, text=f"{title}（{len(buckets[key])}）")
        self._update_conv_count()

    def _conv_is_unresolved(self, item: Conversation) -> bool:
        """这行显示的是真名还是一串编号。内存里解不出名的群，
        ``name`` 就是 wxid 本身，界面只能原样摆出来。"""
        raw = str(item.wxid or item.name or "")
        if not raw:
            return False
        try:
            shown = str(self.app.display_name_for_conversation(item) or "")
        except Exception:
            shown = raw
        return shown == raw and "@chatroom" in raw

    def _conv_order_key(self, item: Conversation) -> int:
        """重画排序：真名在前，编号沉底。

        只分两档，档内保持原顺序（sorted 稳定）：真名之间不重排，
        原来第 2 步的顺序不会被排序打乱。
        """
        if self._conv_is_unresolved(item):
            return 1
        return 0

    def _on_history_next(self) -> None:
        # 只认打勾，不认高亮行：勾选态按 identity 跨栏保持。
        by_identity = {item.identity: item for item in self._conversations}
        picked = [by_identity[identity] for identity in self._conv_checked
                  if identity in by_identity]
        # 保持会话列表原有顺序，商户看到的下一步与列表一致。
        order = {item.identity: position for position, item in enumerate(self._conversations)}
        picked.sort(key=lambda item: order.get(item.identity, 0))
        if not picked:
            messagebox.showinfo("还没选", "请先在「选」列打勾要读取历史的会话，或点各栏里的「全选本页」。")
            return
        self.app.choose_history_conversations(picked)
        self.app.choose_live_conversation(picked[0])
        self._show_step(WizardStep.HISTORY)

    # --------------------------------------------------------------- ③ 历史

    def _page_history(self) -> None:
        """第 3 步　读取历史消息：两个按钮 —— 更新消息 / 读取全部。

        时间选择、条数上限、认人方式全砍掉：数据库直读下时间窗没有意义
        （库里有什么读什么），认人只有数据库一种。没钥匙时按钮变灰，
        指回第 2 步先提密钥。
        """
        frame = self._scrollable_page()
        selected = self.app.wizard.conversations.history_selected
        ttk.Label(frame, text="第 3 步　读取历史消息", style="SectionPage.TLabel").pack(anchor="w", pady=(4, 6))
        ttk.Label(
            frame,
            text="将读取：" + ("、".join(item.disambiguation for item in selected) or "未选择"),
            wraplength=700,
        ).pack(anchor="w", pady=(0, 10))

        try:
            wcdb_ok, _wcdb_message = self.app.wcdb_status()
        except Exception:
            wcdb_ok = False
        if not wcdb_ok:
            ttk.Label(
                frame,
                text="本机数据库还读不了：回第 2 步点「刷新列表」，"
                "会自动提取密钥，提完再来读。",
                foreground="#a8590b", wraplength=700,
            ).pack(anchor="w", pady=(0, 10))

        actions = ttk.Frame(self.footer_body)
        actions.pack(fill="x")
        self._widgets["history_actions"] = actions
        update_btn = ttk.Button(
            actions, text="更新消息", style="Big.TButton",
            command=lambda: self._on_read_history(ReadMode.UPDATE),
        )
        update_btn.pack(side="left")
        all_btn = ttk.Button(
            actions, text="读取全部",
            command=lambda: self._on_read_history(ReadMode.ALL),
        )
        all_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="上一步", command=lambda: self._show_step(WizardStep.CONVERSATIONS)).pack(side="left", padx=8)
        self._widgets["read_update_btn"] = update_btn
        self._widgets["read_all_btn"] = all_btn
        # 旧入口名保留给 _set_reading 与例子：主按钮即开始。
        self._widgets["read_start"] = update_btn
        if not wcdb_ok or not selected:
            update_btn.configure(state="disabled")
            all_btn.configure(state="disabled")

    def _page_read(self) -> None:
        """第 4 步　执行与结果：进度、缺口、逐会话明细都在这里。

        进这一页即表示读取已经开始（或刚结束）。返回上一步改范围，
        配置会保留，不用重选。
        """
        frame = self._scrollable_page()
        selected = self.app.wizard.conversations.history_selected
        ttk.Label(frame, text="第 4 步　读取历史消息", style="SectionPage.TLabel").pack(anchor="w", pady=(4, 6))
        ttk.Label(
            frame,
            text="正在读：" + ("、".join(item.disambiguation for item in selected) or "未选择"),
            wraplength=700,
        ).pack(anchor="w", pady=(0, 10))

        bar = ttk.Progressbar(frame, maximum=100)
        bar.pack(fill="x", pady=(6, 2))
        self._widgets["progress"] = bar
        self._widgets["progress_text"] = ttk.Label(frame, text="准备中", foreground="#7b8794")
        self._widgets["progress_text"].pack(anchor="w")
        # 读取过程中的状态。商户要知道"现在在干什么、读了多少、还要不要等"。
        self._widgets["read_state"] = ttk.Label(frame, text="", foreground="#7b8794", wraplength=700)
        self._widgets["read_state"].pack(anchor="w", pady=(2, 0))

        actions = ttk.Frame(self.footer_body)
        actions.pack(fill="x")
        self._widgets["read_cancel"] = ttk.Button(
            actions, text="取消", command=self.app.cancel_history
        )
        self._widgets["read_cancel"].pack(side="left")
        ttk.Button(actions, text="上一步：改范围", command=lambda: self._show_step(WizardStep.HISTORY)).pack(side="left", padx=8)
        # 读完之后的去处。**显式按钮**，不靠"自动跳到下一页"这种无声行为 ——
        # 商户看不到自己被带到哪，也就没有"下一步"可点。默认收起，读到了才出现。
        self._widgets["read_next"] = ttk.Button(
            actions, text="下一步：智能整理", style="Big.TButton", command=self._go_next_from_history
        )

        # 读到了什么，**逐个会话**摊开。商户在决定要不要花额度去分析之前，
        # 要确认的是"该读的都读到了" —— 一句总数回答不了这个问题。
        # 这一块有内容才出现：没读之前摆一张空表只是占地方。
        summary = ttk.LabelFrame(frame, text="每个会话读到了什么", padding=8)
        self._widgets["read_summary"] = summary
        columns = ("name", "count", "range")
        summary_box, tree = self._tree_with_scroll(
            summary, columns=columns, show="headings", height=5)
        summary_box.pack(fill="both", expand=True)
        for key, heading, width, anchor in (
            ("name", "会话", 300, "w"),
            ("count", "读到", 70, "e"),
            ("range", "时间范围", 280, "w"),
        ):
            tree.heading(key, text=heading)
            tree.column(key, width=width, anchor=anchor)
        self._widgets["read_summary_tree"] = tree

        # 放在最后：它要控制 read_next 与 read_summary 的显隐，控件必须先建好。
        # 页面因导航重建时也会走到这里 —— 必须把**上次的结果**画回来，不能留
        # 一句写死的「准备中」。明明早就读完却显示"准备中"，那是界面在说假话。
        result = self._last_read_result or self.app.wizard.history_result
        self._render_read_state(result)
        self._set_reading(result is None)

    def _default_read_mode(self) -> str:
        """无参数入口的默认：读过选更新，没读过选全部。"""
        cursor = getattr(self.app.wizard.history, "cursor", None)
        tenant = getattr(self.app.wizard, "tenant", "")
        selected = getattr(self.app.wizard.conversations, "history_selected", ())
        if cursor is not None and tenant and any(
            cursor.get(item.identity, tenant) is not None for item in selected
        ):
            return ReadMode.UPDATE
        return ReadMode.ALL

    def _on_read_history(self, mode: str = "") -> None:
        """两个按钮的统一入口：更新消息 / 读取全部，全走数据库直读。

        无参数调用时（例子与旧入口）按老规矩默认：读过选更新，没读过选全部。
        时间窗一律全量（库里有什么读什么），增量由 UPDATE 在库内过滤。
        """
        if mode not in (ReadMode.UPDATE, ReadMode.ALL):
            mode = self._default_read_mode()
        if getattr(self.app.wizard.history, "running", False):
            messagebox.showinfo("正在读取", "上一轮还没读完，等它结束再点。")
            return
        custom = TimeWindow(end=None, start=None, label="全部历史")
        self._last_read_config = {"mode": mode, "sender": "wcdb"}
        self._last_read_result = None
        self.app.read_history(range_kind=HistoryRange.CUSTOM, custom=custom,
                              max_messages=DEFAULT_MAX_MESSAGES,
                              mode=mode, sender_source="wcdb")

    def _set_reading(self, reading: bool) -> None:
        """读取中禁用会打架的控件，并写明当前在干什么。"""
        start = self._widgets.get("read_start")
        all_btn = self._widgets.get("read_all_btn")
        cancel = self._widgets.get("read_cancel")
        state = self._widgets.get("read_state")
        for widget, on in ((start, not reading), (all_btn, not reading),
                           (cancel, reading)):
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
            elif getattr(result, "mode", "") == ReadMode.UPDATE:
                text_widget.configure(text="没有新消息", foreground="#5a6572")
            else:
                text_widget.configure(text="没有读到消息", foreground="#a8590b")
        if state is not None and self._alive(state):
            if result is None:
                state.configure(text="", foreground="#7b8794")
                # 读取还没出结果：页上的「取消」和「上一步」一直都在，
                # 不会卡死。
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
                    # 明细表也要画出来：顶部说「没有新消息」而底下一片空白，
                    # 商户看不出这个群里其实有多少条。
                    self._render_coverage_rows(result)
                    return
                elif read_anywhere > 0:
                    state.configure(
                        text=f"刚才读到了 {read_anywhere} 条，但没有一条能入库，"
                             "本地订单里还是空的。点「上一步」改范围后可重新读取；"
                             "反复这样说明微信那边暂时读不稳，"
                             "可以改用「读取全部历史」再试。",
                        foreground="#a8590b",
                    )
                else:
                    # 更新时库里一条都没有：同样是"没有新信息"，不是"没读到"。
                    if getattr(result, "mode", "") == ReadMode.UPDATE:
                        state.configure(
                            text="没有读到新信息。上次之后没有新消息，之前读到的一个都没丢。",
                            foreground="#5a6572",
                        )
                    else:
                        state.configure(
                            text="这次没有读到任何消息。可以点「上一步」换个读法再试一次 —— "
                                 "之前读到的一个都没丢。",
                            foreground="#a8590b",
                        )
                # 0 条时不给「下一步」（那确实是死路）；「上一步」和「取消」
                # 一直都在，不会卡死。
                if nxt is not None and self._alive(nxt):
                    nxt.pack_forget()
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
                parts.append("已读到的部分保留着，点「上一步」改范围后可继续读。")
            state.configure(text="\n".join(parts), foreground="#5a6572")
        if nxt is not None and self._alive(nxt):
            # 只有真的读到东西才给下一步；没读到还给按钮等于让商户点进死路。
            if got_messages:
                nxt.pack(side="left", padx=(8, 0))
            else:
                nxt.pack_forget()

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
            # 发送人没认出来**必须写在这一行**，不能只记在库里。
            # 「读到 107 条」和「读到 107 条但每条发送人都是占位符」在界面上
            # 曾经一模一样，商户于是以为成功了，转头去查「为什么发件人都是那句话」。
            # 那批消息里红包 0 条，全是普通聊天 —— 占位符原来写的是
            # 「红包或转账」，商户会得出一个完全错误的结论。
            # 读失败必须写清原因，不能只写"读失败"三个字 —— 尤其冷群
            # （本机没存它的消息）时，原因里带着"去电脑版微信打开它翻几页"
            # 的指引，不显示等于没说。
            if item.error:
                reason = str(item.error).replace("\n", " ").strip()
                note_text = "　（读失败：" + (reason[:80] + "…" if len(reason) > 80 else reason) + "）"
            else:
                note_text = "　（上次已读完）" if item.skipped else ""
            unresolved = int(getattr(item, "unresolved_senders", 0) or 0)
            if unresolved and not item.error:
                note_text += f"　（{unresolved} 条发送人没认出来）"
            # **一条都没读到时不能只说「完成」。**
            #
            # 两种情况原来在界面上完全一样、都显示「本次 0 条」：
            #   * 微信里确实没有新消息 → 正常
            #   * 读取器根本没取到（窗口没聚焦、群名搜不到、时间窗不匹配）
            # 商户看「完成」只会以为群里没消息，于是永远不会去查「读取是不是没成功」。
            fetched = int(getattr(item, "fetched_count", 0) or 0)
            if not item.error and not item.skipped and not item.message_count:
                note_text += ("　（读取器拿回 0 条，**这不等于群里没消息**）"
                              if fetched == 0 else
                              f"　（读取器拿回 {fetched} 条，但本次一条都没进库）")
            tree.insert(
                "", "end",
                values=(name, count_text, f"{earliest} 至 {latest}{note_text}"),
            )

    def _has_messages_for(self, result: Any) -> bool:
        """放行的唯一判据（共用逻辑）。
        本早有两处各自判断——一处放行、一处拦回去，
        界面就自相矛盾了。"""
        return summary_task.has_messages_for(self, result)

    def _go_next_from_history(self, target: str = WizardStep.REVIEW) -> None:
        """「下一步」按钮（执行页与核对页共用，目标不同）。

        执行页的下一步是核对页（REVIEW），核对页的下一步是智能整理
        （ANALYZE）。以前写死 REVIEW，核对页点它等于原地踏步 ——
        看起来就是"点了没反应"。真的没有消息时才拦，那是死路。
        """
        result = self._last_read_result or self.app.wizard.history_result
        if not self._has_messages_for(result):
            messagebox.showinfo("还没有读到消息", "读取完成后才能进入下一步。如需改范围，点「上一步」。")
            return
        # 走**向导**而不是直接 _show_step：两者必须一致。以前界面翻页了而
        # wizard.step 还停在 history，之后任何依据 step 的判断（恢复、刷新）
        # 都会读到过期状态。
        self.app.wizard._go(target)

    # ------------------------------------------------- ⑤ 核对明细与导出

    def _page_review(self) -> None:
        """第 5 步　核对读取明细：亲眼看到读到了什么，再决定花不花额度整理。

        只读本地库，不碰微信。导出同理 —— 落盘的是 CSV/XLSX/DOCX/JSON 文件。
        """
        frame = self._scrollable_page()
        selected = self.app.wizard.conversations.history_selected
        ttk.Label(frame, text="第 5 步　核对读取明细", style="SectionPage.TLabel").pack(anchor="w", pady=(4, 6))
        names = [item.disambiguation for item in selected]
        ttk.Label(
            frame,
            text="会话：" + ("、".join(names) or "未选择"),
            wraplength=700,
        ).pack(anchor="w", pady=(0, 6))

        rows = self._load_review_rows([item.name for item in selected])
        self._last_review_rows = rows
        self._widgets["review_count"] = ttk.Label(
            frame, text=f"共 {len(rows)} 条（最多显示最新 200 条）",
            foreground="#5a6572",
        )
        self._widgets["review_count"].pack(anchor="w", pady=(0, 6))

        columns = ("group", "sent_at", "sender", "content")
        review_box, tree = self._tree_with_scroll(
            frame, columns=columns, show="headings", height=12)
        review_box.pack(fill="both", expand=True)
        for key, heading, width, anchor in (
            ("group", "会话", 150, "w"),
            ("sent_at", "时间", 130, "w"),
            ("sender", "发送人", 130, "w"),
            ("content", "内容", 400, "w"),
        ):
            tree.heading(key, text=heading)
            tree.column(key, width=width, anchor=anchor)
        for row in rows:
            shown = display_content(row.get("kind", ""), row.get("content", ""))
            content = shown if len(shown) <= 60 else shown[:60] + "…"
            tree.insert("", "end", values=(
                row.get("group", ""), row.get("sent_at", ""),
                self.app.display_name_for(row.get("sender", "")),
                content,
            ))
        self._widgets["review_tree"] = tree

        actions = ttk.Frame(self.footer_body)
        actions.pack(fill="x")
        ttk.Button(actions, text="导出CSV", command=self._export_review_csv).pack(side="left")
        ttk.Button(actions, text="导出XLSX", command=self._export_review_xlsx).pack(side="left", padx=8)
        ttk.Button(actions, text="导出DOCX", command=self._export_review_docx).pack(side="left")
        ttk.Button(actions, text="导出JSON", command=self._export_review_json).pack(side="left")
        ttk.Button(actions, text="上一步", command=lambda: self._show_step(WizardStep.READ)).pack(side="left")
        ttk.Button(actions, text="下一步：智能整理", style="Big.TButton",
                   command=lambda: self._go_next_from_history(WizardStep.ANALYZE)).pack(side="left", padx=8)

    def _load_review_rows(self, groups: list) -> list:
        """读本地库拿所选会话的最新消息。库不可用就给空表，不崩页。"""
        store = getattr(self.app, "orders", None)
        tenant = getattr(self.app, "tenant", "")
        if store is None or not groups:
            return []
        try:
            return list(store.recent_messages(tenant, groups, 200) or [])
        except Exception:
            return []

    def _export_review_rows(self) -> list:
        """导出用的行：内容按核对页的显示口径（图片/语音等给占位），
        且只放过了一遍 AI 的（订单解析完成，或任一角色整理过）。

        没过 AI 的导出去等于绕开整理 —— 与其悄悄放行，不如直接卡住，
        让商户去跑一遍整理。跳过条数记在 ``_last_export_skipped``，
        调用方拿去说话。
        """
        store = getattr(self.app, "orders", None)
        tenant = getattr(self.app, "tenant", "")
        try:
            processed = store.processed_message_ids(tenant) \
                if store is not None else set()
        except Exception:
            processed = set()
        rows = []
        skipped = 0
        for row in (getattr(self, "_last_review_rows", []) or []):
            item = dict(row or {})
            if str(item.get("id", "") or "") not in processed:
                skipped += 1
                continue
            item["content"] = display_content(item.get("kind", ""), item.get("content", ""))
            rows.append(item)
        self._last_export_skipped = skipped
        return rows

    def _ask_saveas(self, title: str, initialfile: str,
                    filetypes: list, defaultextension: str) -> str:
        """Win11 原生保存框，失败回落 tkinter。取消返回空串。"""
        try:
            from .win_dialog import ask_saveas_path
            hwnd = 0
            try:
                hwnd = int(self.root.winfo_id())
            except Exception:
                hwnd = 0
            path = ask_saveas_path(
                parent_hwnd=hwnd, title=title,
                initial_dir=self._export_initial_dir(),
                filename=initialfile, filetypes=filetypes,
                default_ext=defaultextension)
            if path is not None:
                return path
        except Exception:
            pass
        return filedialog.asksaveasfilename(
            parent=self.root, title=title, initialdir=self._export_initial_dir(),
            initialfile=initialfile, defaultextension=defaultextension,
            filetypes=filetypes) or ""

    @staticmethod
    def _safe_filename_part(text: str, limit: int = 30) -> str:
        """文件名片段：去非法字符、限长，空了回空串（调用方再兜底）。"""
        cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "", str(text or "")).strip()
        return cleaned[:limit]

    def _suggest_export_name(self, ext: str, rows: list | None = None) -> str:
        """预生成导出文件名：产品名-内容-群-条数-时间。

        商户以前每次都要手敲文件名 —— 四个格式各敲一次。名字里带上
        群、条数和时间，存完在文件夹里一眼认得出来，不用来回改。
        """
        rows = rows if rows is not None else self._export_review_rows()
        groups = []
        for row in rows:
            group = str((row or {}).get("group", "") or "").strip()
            if group and group not in groups:
                groups.append(group)
        if len(groups) == 1:
            scope = self._safe_filename_part(groups[0], 24) or "会话"
        elif groups:
            scope = f"{self._safe_filename_part(groups[0], 16) or '会话'}等{len(groups)}个会话"
        else:
            scope = "会话"
        stamp = time.strftime("%Y%m%d_%H%M")
        return f"微信AI智能助理-核对明细-{scope}-{len(rows)}条-{stamp}.{ext}"

    @staticmethod
    def _export_initial_dir() -> str:
        """导出对话框的初始目录：文档库，没有就退回家目录。"""
        for candidate in (Path.home() / "Documents", Path.home()):
            try:
                if candidate.is_dir():
                    return str(candidate)
            except Exception:
                continue
        return ""

    def _check_exportable(self) -> list | None:
        """导出门禁：只放过了一遍 AI 的。返回行，或 None（调用方收摊）。

        * 全都没过：指路去智能整理，不放行；
        * 部分没过：问一句，只导已整理的；
        * 全都过：直接放行，一句废话没有。
        """
        rows = self._export_review_rows()
        skipped = int(getattr(self, "_last_export_skipped", 0) or 0)
        if rows and not skipped:
            return rows
        if not rows and not skipped:
            messagebox.showinfo("没有可导出的内容", "先读到消息再导出。")
            return None
        if not rows:
            messagebox.showinfo(
                "先去智能整理",
                "这些消息还没整理过，导出去等于绕开整理。\n"
                "点下面「下一步：智能整理」跑一遍再导出。")
            return None
        if messagebox.askyesno(
                "只导出已整理的部分",
                f"{skipped} 条还没整理过，不在导出里。\n"
                f"只导出已整理的 {len(rows)} 条？"):
            return rows
        return None

    def _export_review_csv(self) -> None:
        rows = self._check_exportable()
        if rows is None:
            return
        path = self._ask_saveas(
            title="导出读取明细",
            initialfile=self._suggest_export_name("csv", rows),
            filetypes=[("CSV 表格", "*.csv"), ("所有文件", "*.*")],
            defaultextension=".csv")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["会话", "时间", "发送人", "类型", "内容"])
                for row in rows:
                    writer.writerow([row.get("group", ""), row.get("sent_at", ""),
                                     row.get("sender", ""), row.get("kind", ""),
                                     row.get("content", "")])
        except Exception as exc:
            messagebox.showerror("导出失败", f"写文件时出错：{exc}"[:200])
            return
        messagebox.showinfo("已导出", f"共导出 {len(rows)} 条：\n{path}")

    def _export_review_json(self) -> None:
        rows = self._check_exportable()
        if rows is None:
            return
        path = self._ask_saveas(
            title="导出读取明细",
            initialfile=self._suggest_export_name("json", rows),
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")],
            defaultextension=".json")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(rows, handle, ensure_ascii=False, indent=2)
        except Exception as exc:
            messagebox.showerror("导出失败", f"写文件时出错：{exc}"[:200])
            return
        messagebox.showinfo("已导出", f"共导出 {len(rows)} 条：\n{path}")

    def _export_review_xlsx(self) -> None:
        rows = self._check_exportable()
        if rows is None:
            return
        path = self._ask_saveas(
            title="导出读取明细",
            initialfile=self._suggest_export_name("xlsx", rows),
            filetypes=[("Excel 表格", "*.xlsx"), ("所有文件", "*.*")],
            defaultextension=".xlsx")
        if not path:
            return
        try:
            with open(path, "wb") as handle:
                handle.write(build_review_xlsx(rows))
        except Exception as exc:
            messagebox.showerror("导出失败", f"写文件时出错：{exc}"[:200])
            return
        messagebox.showinfo("已导出", f"共导出 {len(rows)} 条：\n{path}")

    def _export_review_docx(self) -> None:
        rows = self._check_exportable()
        if rows is None:
            return
        path = self._ask_saveas(
            title="导出读取明细",
            initialfile=self._suggest_export_name("docx", rows),
            filetypes=[("Word 文档", "*.docx"), ("所有文件", "*.*")],
            defaultextension=".docx")
        if not path:
            return
        try:
            with open(path, "wb") as handle:
                handle.write(build_review_docx(rows))
        except Exception as exc:
            messagebox.showerror("导出失败", f"写文件时出错：{exc}"[:200])
            return
        messagebox.showinfo("已导出", f"共导出 {len(rows)} 条：\n{path}")

    # ------------------------------------------------- ⑥ 智能整理（额度同屏）

    def _page_analyze(self) -> None:
        """第 6 步：查看逐会话读取范围，选择角色并发起 LLM 分析。"""
        frame = self._scrollable_page(padding=(18, 6))
        ttk.Label(frame, text="第 6 步　智能整理", style="SectionPage.TLabel").pack(anchor="w", pady=(4, 8))

        ttk.Label(frame, text="整理角色：", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", pady=(10, 2))
        self._build_role_picker(frame)
        self._widgets["analyze_scope"] = ttk.Label(
            frame, text="", foreground="#5a6572", wraplength=720, justify="left",
        )
        self._widgets["analyze_scope"].pack(anchor="w", pady=(4, 0))
        self._render_analyze_scope(self._last_read_result or self.app.wizard.history_result)
        ttk.Label(
            frame,
            text="多会话和长消息会按会话分批调用模型；实际用量会随会话数量和消息量变化。",
            foreground="#7b8794", wraplength=760,
        ).pack(anchor="w", pady=(6, 0))

        self._widgets["analyze"] = ttk.Label(frame, text="", font=("Microsoft YaHei", 11, "bold"), wraplength=700)
        self._widgets["analyze"].pack(anchor="w", pady=(14, 0))
        # 失败提示的落点。以前第 4 步没有它，商户点「开始智能整理」失败时
        # 错误消息无处可去 —— 按钮亮着，点了没反应，也没有任何解释。
        self._widgets["analyze_notice"] = ttk.Label(
            frame, text="", foreground="#a8590b", wraplength=700,
        )
        self._widgets["analyze_notice"].pack(anchor="w", pady=(6, 0))

        # 角色整理报告区：订单角色没有报告，平时收起，读到报告才展开。
        report_frame = ttk.LabelFrame(frame, text="角色整理结果", padding=8)
        self._widgets["role_report_frame"] = report_frame
        report_box, report_tree = self._tree_with_scroll(
            report_frame,
            columns=("bucket", "group", "sender", "sent_at", "content", "facts"),
            show="headings", height=8)
        report_box.pack(fill="both", expand=True)
        for key, heading, width, anchor in (
            ("bucket", "类别", 90, "w"),
            ("group", "会话", 140, "w"),
            ("sender", "发送人", 110, "w"),
            ("sent_at", "时间", 130, "w"),
            ("content", "内容", 300, "w"),
            ("facts", "抽到的事实", 150, "w"),
        ):
            report_tree.heading(key, text=heading)
            report_tree.column(key, width=width, anchor=anchor)
        self._widgets["role_report_tree"] = report_tree

        actions = ttk.Frame(self.footer_body)
        actions.pack(fill="x")
        self._widgets["analyze_button"] = ttk.Button(
            actions, text="开始智能整理", style="Big.TButton", command=self._on_analyze
        )
        self._widgets["analyze_button"].pack(side="left")
        ttk.Button(actions, text="上一步", command=lambda: self._show_step(WizardStep.REVIEW)).pack(side="left", padx=8)

        self._render_analyzed()
        self._render_role_report()
        self._refresh_analyze_button()

    def _page_analysis_results(self) -> None:
        """第 7 步：展示 AI 对本次读取消息逐条给出的处理结果。"""
        frame = self._scrollable_page(padding=(18, 6))
        ttk.Label(
            frame, text="第 7 步　AI分析详情", style="SectionPage.TLabel",
        ).pack(anchor="w", pady=(4, 8))
        self._widgets["analysis_result_summary"] = ttk.Label(
            frame, text="正在载入分析结果……", font=("Microsoft YaHei", 10, "bold"),
            wraplength=760,
        )
        self._widgets["analysis_result_summary"].pack(anchor="w", pady=(0, 8))

        summary_box = ttk.LabelFrame(frame, text="AI角色分析报告", padding=8)
        summary_box.pack(fill="x", pady=(0, 8))
        self._widgets["summary_box"] = summary_box
        self._summary_text = tk.Text(summary_box, height=8, wrap="word", relief="flat")
        summary_scroll = ttk.Scrollbar(summary_box, orient="vertical",
                                       command=self._summary_text.yview)
        self._summary_text.configure(yscrollcommand=summary_scroll.set)
        self._summary_text.pack(side="left", fill="both", expand=True)
        summary_scroll.pack(side="right", fill="y")
        self._summary_text.configure(state="disabled")
        summary_bar = ttk.Frame(summary_box)
        self._widgets["analysis_activity_row"] = summary_bar
        self._summary_bar = ttk.Progressbar(summary_bar, length=150, mode="indeterminate")
        self._summary_bar.pack(side="left")
        self._summary_elapsed = tk.StringVar(value="")
        self._summary_elapsed_label = ttk.Label(
            summary_bar, textvariable=self._summary_elapsed, foreground="#7b8794",
        )
        self._summary_elapsed_label.pack(side="left", padx=(8, 0))
        self._summary_status = ttk.Label(
            summary_bar, text="", foreground="#7b8794", wraplength=600,
        )
        self._summary_status.pack(side="left", padx=(8, 0))
        if self._analysis_running:
            summary_bar.pack(fill="x", pady=(6, 0))
            self._summary_started = time.monotonic()
            self._summary_status.configure(text="正在分析本次读取的消息……")
            self._summary_bar.start(12)
            self._summary_tick()

        attachment_frame = ttk.LabelFrame(frame, text="本次读取的附件（仅展示，不提交给 AI）", padding=8)
        self._widgets["analysis_attachments_box"] = attachment_frame
        attachment_frame.pack(fill="x", pady=(0, 8))
        attachment_table, attachment_tree = self._tree_with_scroll(
            attachment_frame, columns=("group", "type", "name", "path"),
            show="headings", height=4,
        )
        attachment_table.pack(fill="x", expand=False)
        for key, heading, width in (("group", "会话", 140), ("type", "类型", 70),
                                    ("name", "附件", 220), ("path", "本地位置", 360)):
            attachment_tree.heading(key, text=heading)
            attachment_tree.column(key, width=width, anchor="w")
        attachment_tree.bind("<Double-1>", lambda _event: self._open_selected_analysis_attachment())
        self._widgets["analysis_attachments_tree"] = attachment_tree

        detail_box = ttk.LabelFrame(frame, text="本次纳入分析的消息", padding=8)
        detail_box.pack(fill="both", expand=True)
        tree_box, tree = self._tree_with_scroll(
            detail_box, columns=("group", "content", "analysis", "result"),
            show="headings", height=9,
        )
        tree_box.pack(fill="both", expand=True)
        for key, heading, width, anchor in (
            ("group", "会话", 140, "w"),
            ("content", "原消息", 260, "w"),
            ("analysis", "AI分析详情", 360, "w"),
            ("result", "处理结果", 110, "w"),
        ):
            tree.heading(key, text=heading)
            tree.column(key, width=width, anchor=anchor)
        tree.bind("<Double-1>", lambda _event: self._show_analysis_detail(tree))
        self._widgets["analysis_detail_tree"] = tree

        actions = ttk.Frame(self.footer_body)
        actions.pack(fill="x")
        ttk.Button(
            actions, text="返回第六步", command=lambda: self._show_step(WizardStep.ANALYZE),
        ).pack(side="left")
        back_button = actions.winfo_children()[-1]
        ttk.Button(
            actions, text="回到首页", command=self._return_home_from_results,
        ).pack(side="left", padx=8)
        home_button = actions.winfo_children()[-1]
        self._widgets["analysis_back_button"] = back_button
        self._widgets["analysis_home_button"] = home_button
        self._render_analysis_results()

    def _open_selected_analysis_attachment(self) -> None:
        tree = self._widgets.get("analysis_attachments_tree")
        if tree is None or not self._alive(tree):
            return
        selection = tree.selection()
        if selection:
            path = str(tree.set(selection[0], "path") or "")
            if path:
                self._open_media_attachment(path)

    def _return_home_from_results(self) -> None:
        self._analysis_results_mode = False
        self._show_step(WizardStep.DONE)

    def _render_analysis_results(self) -> None:
        summary = self._last_summary
        label = self._widgets.get("analysis_result_summary")
        if label is not None and self._alive(label):
            if summary:
                text = (f"{summary.get('role_name', 'AI')} 已分析 "
                        f"{summary.get('processed', 0)} 条消息。详细分析报告见下方。")
                if summary.get("scope_note"):
                    text += "\n" + str(summary["scope_note"])
                if summary.get("partial_error"):
                    text += "\n" + str(summary["partial_error"])
                label.configure(text=text)
            elif self._analysis_running:
                label.configure(text="正在分析本次读取的消息……")
            elif self._last_analysis_error:
                label.configure(text=self._last_analysis_error)
            else:
                label.configure(text="分析尚未完成。可以返回第六步重新开始。")
        button_state = "disabled" if self._analysis_running else "normal"
        for key in ("analysis_back_button", "analysis_home_button"):
            button = self._widgets.get(key)
            if button is not None and self._alive(button):
                button.configure(state=button_state)

        tree = self._widgets.get("analysis_detail_tree")
        attachment_tree = self._widgets.get("analysis_attachments_tree")
        attachment_box = self._widgets.get("analysis_attachments_box")
        if attachment_tree is not None and self._alive(attachment_tree):
            attachment_tree.delete(*attachment_tree.get_children())
            attachments = list(summary.get("attachments") or []) if summary else []
            for index, item in enumerate(attachments):
                if not isinstance(item, dict):
                    continue
                attachment_tree.insert("", "end", iid=f"attachment-{index}", values=(
                    item.get("group", ""), item.get("type", "附件"),
                    item.get("name", ""), item.get("path", ""),
                ))
            if attachment_box is not None and self._alive(attachment_box):
                if attachments:
                    attachment_box.pack(fill="x", pady=(0, 8))
                else:
                    attachment_box.pack_forget()
        if tree is not None and self._alive(tree):
            tree.delete(*tree.get_children())
            self._analysis_detail_rows: dict[str, dict[str, Any]] = {}
            outcomes = self._last_analysis_details
            for index, outcome in enumerate(outcomes):
                if not isinstance(outcome, dict):
                    continue
                trace = dict(outcome.get("trace") or {})
                message_id = str(trace.get("message_id") or "")
                message = dict(outcome.get("message") or {})
                if message_id:
                    try:
                        row = self.app.orders.db.execute(
                            "SELECT group_id,sender,content,sent_at FROM messages "
                            "WHERE merchant_id=? AND id=?",
                            (self.app.tenant, message_id),
                        ).fetchone()
                        message = dict(row) if row else {}
                    except Exception:
                        message = {}
                order_id = str(outcome.get("order_id") or trace.get("order_id") or "")
                order_summary = ""
                if order_id:
                    try:
                        detail = self.app.orders.order_detail(self.app.tenant, order_id) or {}
                        items = detail.get("items") or []
                        item_text = "、".join(
                            f"{item.get('product_name', '商品')} {item.get('quantity', '')}"
                            f"{item.get('unit', '')}" for item in items
                        )
                        order_summary = str(detail.get("customer_name") or "")
                        if item_text:
                            order_summary += ("：" if order_summary else "") + item_text
                        if not order_summary:
                            order_summary = "已生成订单"
                    except Exception:
                        order_summary = "已生成订单"
                status = str(outcome.get("status") or outcome.get("outcome") or trace.get("status") or "")
                status_label = {
                    "done": "已完成", "needs_review": "待核对",
                    "blocked": "未能分析", "failed": "分析失败",
                    "ignored": "未生成订单", "own_receipt": "已跳过",
                    "merged_away": "已合并",
                }.get(status, status or "已分析")
                analysis = str(
                    outcome.get("message") or outcome.get("question")
                    or outcome.get("outcome") or "已完成分析"
                )
                if order_summary:
                    analysis = f"{analysis}；{order_summary}" if analysis else order_summary
                iid = tree.insert("", "end", iid=str(index), values=(
                    message.get("group_label") or message.get("group_id", ""), _cell(message.get("content", ""), 42),
                    _cell(analysis, 58), status_label,
                ))
                self._analysis_detail_rows[iid] = {
                    "group": str(message.get("group_label") or message.get("group_id", "")),
                    "sender": str(message.get("sender", "")),
                    "sent_at": str(message.get("sent_at", "")),
                    "content": str(message.get("content", "")),
                    "kind": str(message.get("kind", "")),
                    "analysis": analysis,
                    "status": status_label,
                    "trace": trace,
                    "attachments": list(message.get("attachments") or []),
                    "media_status": str(message.get("media_status") or ""),
                }

        self._render_analysis_paragraph()

    def _render_analysis_paragraph(self) -> None:
        outcome = self._last_paragraph_outcome
        if not self._summary_running and not outcome:
            return
        status = self._summary_status
        if self._summary_running and self._alive(self._summary_bar):
            self._summary_bar.start(12)
            if self._alive(status):
                status.configure(text="正在整理……")
        if not outcome:
            return
        text = str(outcome.get("summary") or "")
        if text:
            self._set_summary(text)
            message_count = outcome.get("message_count", 0)
            if self._alive(status):
                status.configure(text=f"已整理 {message_count} 条")
        elif outcome.get("error") and self._alive(status):
            status.configure(text="整理失败：" + str(outcome["error"]))

    def _show_analysis_detail(self, tree: Any) -> None:
        selection = tree.selection()
        if not selection:
            return
        detail = getattr(self, "_analysis_detail_rows", {}).get(str(selection[0]))
        if not detail:
            return
        window = tk.Toplevel(self.root)
        window.title("AI分析详情")
        window.geometry("700x520")
        box = ttk.Frame(window, padding=12)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=f"会话：{detail['group']}　发送人：{detail['sender']}"
                           f"　时间：{detail['sent_at']}").pack(anchor="w", pady=(0, 8))
        body = tk.Text(box, wrap="word")
        attachments = list(detail.get("attachments") or [])
        if attachments:
            ttk.Label(box, text="附件（仅展示，不提交给 AI）：").pack(anchor="w", pady=(8, 2))
            for path in attachments:
                row = ttk.Frame(box)
                row.pack(fill="x", pady=1)
                ttk.Label(row, text=str(path), wraplength=530).pack(side="left", fill="x", expand=True)
                ttk.Button(row, text="打开", command=lambda p=str(path): self._open_media_attachment(p)).pack(side="right")
        elif detail.get("media_status"):
            ttk.Label(box, text=str(detail["media_status"])).pack(anchor="w", pady=(8, 2))
        original_content = str(detail.get("content") or "").strip()
        if not original_content:
            original_content = f"[{detail.get('kind') or '媒体'}消息]"
        body.pack(fill="both", expand=True, pady=(8, 0))
        body.insert("1.0", f"原消息：\n{original_content}\n\n"
                            f"AI分析：\n{detail['analysis']}\n\n"
                            f"处理结果：{detail['status']}\n\n"
                            f"任务追踪：{json.dumps(detail['trace'], ensure_ascii=False, indent=2)}")
        body.configure(state="disabled")

    @staticmethod
    def _open_media_attachment(path: str) -> None:
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror("无法打开附件", f"附件文件无法打开：{exc}")

    def _render_role_report(self) -> None:
        """角色整理报告：按桶列出消息（群｜发送人｜时间｜内容｜抽到的事实）。

        订单角色没有报告（它的产出去订单/核对页），这里直接收起。
        """
        frame = self._widgets.get("role_report_frame")
        tree = self._widgets.get("role_report_tree")
        report = dict(getattr(self, "_last_role_report", None) or {})
        buckets = report.get("buckets") if isinstance(report, dict) else None
        if frame is None or tree is None or not buckets:
            if frame is not None and self._alive(frame):
                frame.pack_forget()
            return
        if self._alive(frame) and not frame.winfo_ismapped():
            frame.pack(fill="both", expand=True, pady=(10, 0))
        try:
            tree.delete(*tree.get_children())
        except Exception:
            return
        for label, items in buckets.items():
            for item in items if isinstance(items, list) else ():
                if not isinstance(item, dict):
                    continue
                facts = item.get("facts") or {}
                fact_text = "；".join(f"{k}：{v}" for k, v in facts.items()) if isinstance(facts, dict) else ""
                content = str(item.get("content", ""))
                if len(content) > 60:
                    content = content[:60] + "…"
                tree.insert("", "end", values=(
                    label, item.get("group", ""), item.get("sender", ""),
                    item.get("sent_at", ""), content, fact_text,
                ))

    def _messages_for_summary(self, result: Any) -> list[dict[str, Any]]:
        """取哪些消息给模型（共用逻辑）。详见 ``summary_task``。"""
        return summary_task.messages_for_summary(self, result)

    def _run_summary(self, result: Any) -> dict[str, Any]:
        """真正去调模型（共用逻辑）。在后台线程里跑，不能碰 Tk。"""
        return summary_task.run_summary(self, result)

    def _summary_tick(self) -> None:
        """每半秒刷一次已等待时长。给的是**真实秒数**。"""
        if not (self._summary_running or self._analysis_running) or not self._alive(self._summary_elapsed_label):
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
        """由「开始智能整理」触发，汇总已读到的消息。

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
        box = self._widgets.get("summary_box")
        if box is not None and self._alive(box) and not box.winfo_ismapped():
            box.pack(fill="x", pady=(10, 0))
        self._summary_running = True
        self._summary_status.configure(text="正在整理……")
        self._summary_started = time.monotonic()
        self._summary_bar.start(12)
        self._summary_tick()
        def work():
            try:
                outcome = self._run_summary(result)
            except Exception as exc:
                # _post 必须执行：注释里记过这个坑 —— 线程里抛出来，
                # _summary_running 永远是 True，之后每次点击都静默返回。
                outcome = {"summary": "", "error": f"整理失败：{exc}",
                           "cost": None, "message_count": 0}
            self._post("summary", outcome=outcome)
        threading.Thread(target=work, daemon=True).start()

    def _on_summary(self, outcome: dict) -> None:
        """主线程入口：_drain 把后台线程的结果送到这里。"""
        self._summary_finished(outcome)

    def _summary_finished(self, outcome: dict) -> None:
        self._last_paragraph_outcome = dict(outcome)
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
                              value=int(getattr(view, "remaining_percent", 0)))
            elif not unlimited:
                # 还没查出来 / 没登录 —— 收起来，不占地方也不误导。
                bar.pack_forget()
            else:
                bar.pack_forget()
        if label is not None and self._alive(label):
            label.configure(text="" if not usable else f"剩余 {getattr(view, 'remaining_percent', 0)}%")
        del holder

    def _refresh_quota(self) -> None:
        with self._quota_refresh_lock:
            if self._quota_refresh_in_flight:
                return
            self._quota_refresh_in_flight = True
        self._last_quota_refresh = time.monotonic()
        try:
            view = self.app.check_quota()
            self._post("quota", view=view)
        finally:
            with self._quota_refresh_lock:
                self._quota_refresh_in_flight = False

    def _on_logged_in(self) -> None:
        """产品账号刚登录成功。刷新所有跟登录有关的显示，并重过第 1 步门禁。

        以前登录成功没人通知界面：第 1 步登录框一直写着"未登录"，门禁也
        不放行 —— 商户刚在浏览器点完确认，回来以为登录没成功。
        """
        self._render_account()
        self._refresh_login_block()
        self._refresh_analyze_button()
        # OAuth succeeds before the async balance request. Fetch right away;
        # the periodic check below retries automatically after network recovery.
        self._last_quota_refresh = 0.0
        self._schedule_quota_refresh(force=True)
        if self.app.wizard.step == WizardStep.WECHAT:
            self._spawn("wechat-check", lambda: self.app.check_wechat())
        self._spawn("models", lambda: self._post("models_loaded", catalog=self.app.load_models(force=True)))

    def _on_models_loaded(self, catalog: dict[str, Any]) -> None:
        self._refresh_model_picker()

    def _schedule_quota_refresh(self, force: bool = False) -> None:
        if self._closing or self.app.session is None:
            return
        quota = self.app.wizard.quota
        interval = 20.0 if not getattr(quota, "balance_known", False) else 90.0
        now = time.monotonic()
        if force or now - self._last_quota_refresh >= interval:
            self._spawn("quota-refresh", self._refresh_quota)

    def _on_quota(self, view: Any) -> None:
        # 登录一成功就会查额度，所以这里也是账号条刷新的时机 ——
        # 顶栏那条「账号：未登录」不能等到商户重开程序才变。
        self._render_account()
        # 第 1 步那块也要跟着变。商户多半就停在第 1 步，一块写着
        # 「还没有登录」的框留在那里，会让他以为刚才那次登录没成功。
        self._refresh_login_block()
        self._refresh_analyze_button()
        # 如果人还停在第 1 步，这一次检测就可能直接放行 —— 登录是门禁的
        # 另一半，只刷登录框不重新过门禁，商户会卡在"明明登上了还不让走"。
        if self.app.wizard.step == WizardStep.WECHAT:
            self._spawn("wechat-check", lambda: self.app.check_wechat())
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
        * **不登录不许往下走** —— 门禁规则（见 Wizard.product_gate_open）：
          必须登录产品账号才能进入下一步选会话。
        * 登录买到什么 —— 只有"智能整理"这一件事需要它；读到的消息只存在
          这台电脑上。
        """
        for child in box.winfo_children():
            child.destroy()
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
                text="可以继续选择会话、读取历史，并智能整理消息、生成订单。",
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
            text="需要先登录产品账号，才能进入下一步选会话。"
                 "点右上角「账号：未登录　点此登录」即可，登录后自动进入下一步。",
            foreground="#5a6572", wraplength=620,
        ).pack(anchor="w", pady=(4, 0))

    def _on_login(self) -> None:
        self.app.login_requested()

    def _on_account_click(self) -> None:
        """点顶栏的账号条。

        未登录 → 发起登录；已登录 → 打开账号中心（余额、套餐、充值、退出）。
        退出是不可逆的（要重新走一遍网页登录），放在账号中心里二次确认，
        别让误点把人踢出去。
        """
        session = self.app.session
        if session is None:
            self.app.login_requested()
            return
        self._show_account_center()

    def _account_center_data(self) -> dict[str, Any]:
        """账号中心要显示的数据。纯逻辑，方便单测；显示层只管摆。

        只读缓存（登录时的 subscription + 后台刷过的 wizard.quota），
        **绝不现场查网** —— 下拉是点开即看的东西，在 UI 线程里等网络
        就是卡死。余额以缓存为准，首页额度行自有后台任务在刷。
        用量只给百分比不给具体数（和 Codex 一样）：具体数会让人去换算
        "还能整理几次"，而一条消息不等于一笔订单，那种换算是错的。
        """
        session = self.app.session
        subscription = {}
        try:
            subscription = dict(getattr(session, "subscription", None) or {})
        except Exception:
            subscription = {}
        name = ""
        try:
            name = str(getattr(session, "merchant_name", "") or "").strip()
        except Exception:
            name = ""
        quota = None
        try:
            quota = getattr(getattr(self.app, "wizard", None), "quota", None)
        except Exception:
            quota = None
        plan = str(subscription.get("plan_code", "") or "").strip()
        # JYI is the provider/account name in the pay-as-you-go flow, not a
        # subscription tier. Older saved sessions may still carry this default.
        if (str(getattr(session, "provider", "") or "").lower() == "jyi"
                and plan.casefold() == "jyi"):
            plan = ""
        return {
            "name": name or "当前账号",
            "plan": plan,
            "period_end": str(subscription.get("period_end", "") or "")[:10],
            "quota": self._quota_bar_data(quota, subscription),
            "purchase_hint": "请联系客服开通或续期。",
            "topup_url": str(subscription.get("topup_url", "") or ""),
        }

    @staticmethod
    def _quota_bar_data(quota: Any, subscription: Mapping[str, Any]) -> dict[str, Any]:
        """用量条数据：{"kind": percent/unlimited/unknown, "percent": int}。

        优先用后台刷过的 quota（最新），没有再拿登录缓存的 subscription
        现算。两边都没数就认"未知"，不编一个百分比。
        """
        try:
            logged_in = bool(getattr(quota, "logged_in", False))
            total = int(getattr(quota, "total_micro_usd", 0) or 0)
        except (TypeError, ValueError):
            logged_in, total = False, 0
        if logged_in and total > 0:
            try:
                return {"kind": "percent",
                        "percent": int(getattr(quota, "remaining_percent", 0) or 0)}
            except (TypeError, ValueError):
                pass
        if logged_in and bool(getattr(quota, "unlimited", False)):
            return {"kind": "unlimited", "percent": 0}
        try:
            total = int((subscription or {}).get("quota_micro_usd", 0) or 0)
            used = int((subscription or {}).get("used_micro_usd", 0) or 0)
            reserved = int((subscription or {}).get("reserved_micro_usd", 0) or 0)
        except (TypeError, ValueError):
            total, used, reserved = 0, 0, 0
        if total > 0:
            percent = 100 - max(0, min(100, round((used + reserved) * 100 / total)))
            return {"kind": "percent", "percent": percent}
        return {"kind": "unknown", "percent": 0}

    def _show_account_center(self) -> None:
        """账号下拉：挂在账号按钮下面，点外面自动收起（大厂右上角标准件）。

        余额、套餐、充值、退出都在这一层，不再另开窗口。
        """
        data = self._account_center_data()
        anchor = self._widgets.get("account")
        menu = tk.Toplevel(self.root)
        menu.overrideredirect(True)
        menu.attributes("-topmost", True)
        body = ttk.Frame(menu, style="Card.TFrame", padding=14)
        body.pack(fill="both", expand=True)
        # 细边框：无边框窗口自己不带框，不画一条会跟底下内容糊在一起。
        try:
            menu.configure(background="#e3e7ec")
            body.pack(fill="both", expand=True, padx=1, pady=1)
        except Exception:
            pass
        ttk.Label(body, text=data["name"],
                  font=("Microsoft YaHei", 11, "bold")).pack(anchor="w")
        if data["plan"]:
            ttk.Label(body, text=f"套餐：{data['plan']}",
                      foreground="#5a6572").pack(anchor="w", pady=(2, 0))
        quota = data["quota"] if isinstance(data.get("quota"), dict) else {}
        kind = quota.get("kind", "unknown")
        if kind == "unlimited":
            ttk.Label(body, text="分析额度：不限",
                      font=("Microsoft YaHei", 13, "bold")).pack(anchor="w", pady=(8, 0))
        elif kind == "percent":
            percent = max(0, min(100, int(quota.get("percent", 0) or 0)))
            bar_row = ttk.Frame(body)
            bar_row.pack(fill="x", pady=(8, 0))
            bar = ttk.Progressbar(bar_row, length=220, mode="determinate",
                                  maximum=100, value=percent)
            bar.pack(side="left")
            ttk.Label(bar_row, text=f"剩余 {percent}%",
                      foreground="#5a6572").pack(side="left", padx=(8, 0))
        else:
            ttk.Label(body, text="额度未知，联网后自动刷新",
                      foreground="#a8590b").pack(anchor="w", pady=(8, 0))
        if data["period_end"]:
            ttk.Label(body, text=f"有效期至 {data['period_end']}",
                      foreground="#5a6572").pack(anchor="w", pady=(2, 0))
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="充值", style="Big.TButton",
                   command=lambda: (menu.destroy(), self._on_recharge(self.root))).pack(side="left")
        ttk.Button(actions, text="退出登录",
                   command=lambda: self._on_logout_from_center(menu)).pack(side="left", padx=8)
        # 点外面收起 + Esc 收起。不能直接绑 FocusOut 就销毁：
        # 点菜单里的按钮时焦点先离开菜单（FocusOut），直接销毁会把按钮
        # 连同点击一起吃掉 —— 点充值没反应就是这么来的。
        # 所以 idle 时再看焦点真走了才收。
        def _maybe_dismiss() -> None:
            try:
                if not menu.winfo_exists():
                    return
                focused = menu.focus_get()
                if focused is not None and str(focused).startswith(str(menu)):
                    return
            except Exception:
                pass
            try:
                menu.destroy()
            except Exception:
                pass

        try:
            menu.bind("<FocusOut>", lambda _event: menu.after_idle(_maybe_dismiss))
            menu.bind("<Escape>", lambda _event: menu.destroy())
        except Exception:
            pass
        try:
            menu.update_idletasks()
            width = menu.winfo_reqwidth()
            if anchor is not None and self._alive(anchor):
                x = anchor.winfo_rootx() + anchor.winfo_width() - width
                y = anchor.winfo_rooty() + anchor.winfo_height() + 4
            else:
                x = self.root.winfo_rootx() + 100
                y = self.root.winfo_rooty() + 100
            menu.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass
        try:
            menu.focus_force()
        except Exception:
            pass

    def _on_recharge(self, window: Any) -> None:
        """充值：跳 JYI 网站收银页，不在客户端里弹 Stripe。

        大厂桌面端标准做法：收银放网页（PCI 不碰卡、3D 验证要浏览器、
        改价不用发版），Stripe 由 JYI 页内接。客户端只负责开浏览器。
        地址可配（PYWEIXIN_BILLING_URL），默认 JYI 控制台账单页。
        打不开浏览器就把地址给出来让人手开，不算失败。
        """
        import os
        import webbrowser

        from .jyi_client import JYI_AUTH_ORIGIN

        data = self._account_center_data()
        url = (os.environ.get("PYWEIXIN_BILLING_URL", "") or "").strip()
        if not url:
            from urllib.parse import urlencode, urlsplit, urlunsplit

            billing_url = data.get("topup_url") or JYI_AUTH_ORIGIN.rstrip("/") + "/console/billing"
            parsed = urlsplit(billing_url)
            trusted = urlsplit(JYI_AUTH_ORIGIN)
            if (parsed.scheme == "https" and parsed.netloc == trusted.netloc
                    and parsed.path == "/console/billing"):
                # Start at sign-in with an explicit same-site return target.
                url = urlunsplit((parsed.scheme, parsed.netloc, "/signin",
                                  urlencode({"next": "/console/billing"}), ""))
            else:
                url = billing_url
        try:
            if webbrowser.open(url):
                return
        except Exception:
            pass
        messagebox.showinfo(
            "充值",
            f"没能自动打开浏览器，请手动打开这个地址充值：\n{url}\n\n{data['purchase_hint']}",
            parent=window,
        )

    def _on_logout_from_center(self, window: Any) -> None:
        # 先关下拉再问：确认框挂在无边框顶层菜单下会被压住看不见，
        # 商户点完毫无反应，还以为按钮坏了。
        try:
            window.destroy()
        except Exception:
            pass
        session = self.app.session
        name = ""
        try:
            name = str(getattr(session, "merchant_name", "") or "当前账号")
        except Exception:
            name = "当前账号"
        if not messagebox.askyesno(
            "退出登录",
            f"确定退出「{name}」吗？\n\n"
            "已经读到的消息和订单会留在这台电脑上，退出后可以重新登录查看。",
            parent=self.root,
        ):
            return
        try:
            self.app.detach()
        except Exception:
            pass
        for refresh in (self._render_account, self._refresh_login_block,
                        self._refresh_analyze_button):
            try:
                refresh()
            except Exception:
                pass
        # 留在**他有数据的那一屏**。退出登录只断开了云端能力，本地读到的
        # 消息和订单一个都没少 —— 把他扔回第 1 步，等于逼他从头再走一遍。
        try:
            self._show_step(WizardStep.ANALYZE if self.app.wizard.history_result else WizardStep.WECHAT)
        except Exception:
            pass
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
        widget.configure(text=f"账号：{name}　▾" if name else "账号：已登录　▾")

    def _build_role_picker(self, parent: Any) -> None:
        """目前只提供订单助手，保留单选控件以兼容原有页面状态。"""
        from .roles import ROLE_ORDER, get_role

        var = tk.StringVar(value=ROLE_ORDER)
        self._widgets["role_picker"] = var
        row = ttk.Frame(parent)
        row.pack(anchor="w", pady=(4, 6))
        role = get_role(ROLE_ORDER)
        ttk.Radiobutton(
            row, text=role.name, value=role.id, variable=var,
        ).pack(side="left")
        ttk.Label(row, text="分析模型：").pack(side="left", padx=(18, 4))
        labels = self._model_options()
        model_var = tk.StringVar(value=(self._selected_model_label(labels)
                                        if labels else self._model_picker_placeholder()))
        self._widgets["model_picker"] = model_var
        picker = ttk.Combobox(row, textvariable=model_var, values=list(labels),
                              state="readonly" if labels else "disabled", width=28)
        picker.pack(side="left")
        picker.bind("<<ComboboxSelected>>", self._on_model_selected)
        self._widgets["model_picker_widget"] = picker
        retry = ttk.Button(row, text=self._model_retry_text(),
                           command=self._refresh_models)
        retry.pack(side="left", padx=(8, 0))
        if self.app.session is None:
            retry.configure(state="disabled")
        self._widgets["model_retry_button"] = retry
        notice = ttk.Label(parent, text=self._model_picker_notice(),
                           foreground="#a8590b", wraplength=560, justify="left")
        notice.pack(anchor="w", padx=(18, 0), pady=(0, 4))
        self._widgets["model_picker_notice"] = notice

    def _model_retry_text(self) -> str:
        catalog = self.app.model_catalog or {}
        return "重试" if catalog.get("stale") or not catalog.get("models") else "刷新列表"

    def _model_picker_notice(self) -> str:
        catalog = self.app.model_catalog or {}
        if catalog.get("stale"):
            return str(catalog.get("notice") or self.app.model_notice())
        if not catalog.get("models"):
            return ("登录后自动加载可用模型列表。" if self.app.session is None
                    else "正在加载可用模型列表…")
        return ""

    def _model_picker_placeholder(self) -> str:
        alias = str(getattr(self.app.settings, "model_alias", "") or "")
        if not alias:
            return "暂无可用模型"
        display = "JYI DEFAULT" if alias.casefold() == "jyi-default" else alias
        return f"{display}（保留上次选择）"

    def _model_options(self) -> dict[str, str]:
        labels = {}
        for item in (self.app.model_catalog or {}).get("models") or []:
            alias = str(item.get("alias") or "")
            if not alias:
                continue
            label = str(item.get("display_name") or alias)
            context = item.get("context_window")
            output = item.get("max_output_tokens")
            details = []
            if context:
                details.append(f"上下文 {int(context):,}")
            if output:
                details.append(f"输出上限 {int(output):,}")
            modalities = item.get("input_modalities") or []
            if "image" in modalities:
                details.append("支持图片")
            if details:
                label += "（" + " · ".join(details) + "）"
            labels[label] = alias
        return labels

    def _selected_model_label(self, labels: Mapping[str, str]) -> str:
        alias = str(getattr(self.app.settings, "model_alias", "") or "")
        return next((label for label, value in labels.items() if value == alias), "")

    def _on_model_selected(self, _event: Any = None) -> None:
        variable = self._widgets.get("model_picker")
        labels = self._model_options()
        label = str(variable.get() or "") if variable is not None else ""
        alias = labels.get(label, "")
        if alias and self.app.set_model(alias):
            self._refresh_analyze_button()

    def _refresh_models(self) -> None:
        retry = self._widgets.get("model_retry_button")
        notice = self._widgets.get("model_picker_notice")
        if retry is not None and self._alive(retry):
            retry.configure(state="disabled", text="正在重试…")
        if notice is not None and self._alive(notice):
            notice.configure(text="正在重新获取可用模型列表…")
        self._spawn("models", lambda: self._post("models_loaded", catalog=self.app.load_models(force=True)))

    def _refresh_model_picker(self) -> None:
        picker = self._widgets.get("model_picker_widget")
        variable = self._widgets.get("model_picker")
        if picker is None or not self._alive(picker):
            return
        labels = self._model_options()
        picker.configure(values=list(labels), state="readonly" if labels else "disabled")
        if variable is not None:
            variable.set(self._selected_model_label(labels)
                         if labels else self._model_picker_placeholder())
        notice = self._widgets.get("model_picker_notice")
        if notice is not None and self._alive(notice):
            notice.configure(text=self._model_picker_notice())
        retry = self._widgets.get("model_retry_button")
        if retry is not None and self._alive(retry):
            retry.configure(
                text=self._model_retry_text(),
                state="normal" if self.app.session is not None else "disabled",
            )

    def _picked_role(self) -> str:
        """当前选中的整理角色 id。控件不在（测试/旧页面）就回订单助手。"""
        from .roles import ROLE_ORDER

        var = self._widgets.get("role_picker")
        try:
            value = str(var.get() or "").strip() if var is not None else ""
        except Exception:
            value = ""
        return value or ROLE_ORDER

    def _render_analyze_scope(self, result: Any) -> None:
        label = self._widgets.get("analyze_scope")
        if label is None or not self._alive(label):
            return
        if result is None:
            label.configure(text="尚无读取结果。请先在第三步读取所选会话。")
            return
        history_counts = dict(summary_task.history_message_counts(self.app, result))
        lines = ["本次读取范围："]
        for item in getattr(result, "coverage", ()) or ():
            conversation = getattr(item, "conversation", None)
            name = str(getattr(conversation, "disambiguation", "") or "未知会话")
            if getattr(item, "error", ""):
                detail = f"读取失败（{item.error}）"
            elif int(getattr(item, "message_count", 0) or 0):
                amount_label = "本次新增" if str(getattr(result, "mode", "")) == ReadMode.UPDATE else "本次读取"
                detail = f"{amount_label} {int(item.message_count)} 条"
            else:
                group_id = str(getattr(conversation, "name", "") or "")
                detail = f"本次无新增，历史 {history_counts.get(group_id, 0)} 条"
            lines.append(f"• {name}：{detail}")
        label.configure(text="\n".join(lines))

    def _on_analyze(self) -> None:
        result = self._last_read_result or self.app.wizard.history_result
        if result is None:
            messagebox.showinfo("还没有读取结果", "请先到第三步读取所选会话的消息。", parent=self.root)
            return
        fresh_count = sum(int(getattr(item, "message_count", 0) or 0)
                          for item in getattr(result, "coverage", ()) or ())
        is_update = str(getattr(result, "mode", "")) == ReadMode.UPDATE
        read_errors = [item for item in (getattr(result, "coverage", ()) or ())
                       if getattr(item, "error", "")]
        scope_note = ""
        if fresh_count <= 0:
            if read_errors:
                failures = "\n".join(
                    f"• {item.conversation.disambiguation}：{item.error}"
                    for item in read_errors
                )
                messagebox.showinfo(
                    "有会话读取失败",
                    "这些会话的读取结果不确定，不能当作‘没有新消息’：\n\n"
                    f"{failures}\n\n请先返回第三步重新读取。",
                    parent=self.root,
                )
                return
            counts = dict(summary_task.history_message_counts(self.app, result))
            total_history = sum(counts.values())
            if total_history <= 0:
                messagebox.showinfo(
                    "没有可分析的消息",
                    "本次没有新增消息，所选会话也没有已保存的历史消息，因此无需分析。",
                    parent=self.root,
                )
                return
            lines = []
            for item in getattr(result, "coverage", ()) or ():
                name = str(getattr(item.conversation, "name", "") or "")
                if not getattr(item, "error", "") and name in counts:
                    lines.append(f"• {item.conversation.disambiguation}：{counts[name]} 条")
            prompt = (
                ("本次更新没有发现新消息。要分析所选会话中已保存的历史消息吗？\n\n"
                 if is_update else
                 "本次读取没有读到消息。要分析所选会话中已保存的历史消息吗？\n\n")
                + "\n".join(lines)
                + f"\n\n共 {total_history} 条；将按会话分批分析，可能产生多次模型调用。"
            )
            if not messagebox.askyesno("分析历史消息？", prompt, parent=self.root):
                return
            analysis_messages = summary_task.messages_for_history(self.app, result)
            scope_note = ("本次无新增，已按确认分析所选会话的历史消息。"
                          if is_update else "已按确认分析所选会话的历史消息。")
        else:
            analysis_messages = summary_task.messages_for_summary(self.app, result)
            if read_errors:
                names = "、".join(item.conversation.disambiguation for item in read_errors)
                scope_note = f"本次按会话分析全部已存消息；以下会话读取失败，未纳入分析：{names}。"
            else:
                scope_note = "本次按会话分析全部已存消息（含本次新增）。"
        if not analysis_messages:
            messagebox.showinfo(
                "没有可分析的消息",
                "没有可提交给 AI 的消息。请返回第三步检查读取结果。",
                parent=self.root,
            )
            return
        role = self._picked_role()
        self._analysis_results_mode = True
        self._analysis_call_active = True
        self._analysis_running = True
        self._summary_started = time.monotonic()
        self._last_analysis_error = ""
        self._last_summary = {}
        self._last_analysis_details = []
        self._last_paragraph_outcome = {}
        self._show_step(WizardStep.RESULTS)
        self._set_summary("")
        def work() -> None:
            try:
                from .roles import get_role

                attachment_kinds = {
                    "image": "图片", "audio": "音频", "voice": "音频",
                    "video": "视频", "file": "文档/文件",
                }
                attachment_rows = []
                for item in analysis_messages:
                    kind = str(item.get("kind", "") or "").lower()
                    paths = [str(path) for path in item.get("attachments") or []]
                    for path in paths:
                        suffix = Path(path).suffix.lower()
                        media_type = attachment_kinds.get(kind, "附件")
                        if kind == "file" and suffix in {".docx", ".pdf"}:
                            media_type = suffix[1:].upper()
                        path = str(path)
                        attachment_rows.append({
                            "group": str(item.get("group_label") or item.get("group_id") or ""),
                            "type": media_type,
                            "name": Path(path).name,
                            "path": path,
                        })
                    if not paths and kind in attachment_kinds:
                        attachment_rows.append({
                            "group": str(item.get("group_label") or item.get("group_id") or ""),
                            "type": attachment_kinds[kind],
                            "name": str(item.get("media_status") or "附件未缓存到本地"),
                            "path": "",
                        })

                text_messages = summary_task._text_analysis_messages(analysis_messages)
                if text_messages:
                    outcome = summary_task.run_summary(
                        self.app, result, role_id=role, messages_override=analysis_messages,
                        on_progress=lambda text: self._post("analysis_progress", text=text),
                        on_stream=lambda text: self._post("analysis_stream", text=text))
                else:
                    outcome = {
                        "summary": "本次读取只有附件，没有可供 AI 分析的文字内容。附件已列在下方。",
                        "message_count": 0, "completed_groups": [], "partial_error": "",
                    }
                if outcome.get("error") and not outcome.get("summary"):
                    raise RuntimeError(str(outcome["error"]))
                completed_groups = set(outcome.get("completed_groups") or ())
                detail_messages = [
                    item for item in text_messages
                    if not completed_groups or str(item.get("group_id") or item.get("group") or "所选会话") in completed_groups
                ]
                summary = {
                    "processed": int(outcome.get("message_count", len(detail_messages)) or 0),
                    "role_id": outcome.get("role_id", role),
                    "role_name": get_role(role).name,
                    "analysis_text": outcome.get("summary", ""),
                    "partial_error": outcome.get("partial_error", ""),
                    "scope_note": scope_note,
                    "attachments": attachment_rows,
                    "details": [
                        {"message": item, "analysis": "已纳入上方角色分析报告",
                         "status": "已纳入分析", "trace": {}}
                        for item in detail_messages
                    ],
                }
            except Exception as exc:
                LOGGER.exception("AI analysis failed")
                self._post("error", text=f"AI分析失败：{exc}")
                summary = None
            if summary is not None:
                self._post("analyzed", summary=summary)
            self._post("analysis_finished", success=summary is not None)

        threading.Thread(target=work, name="merchant-analysis", daemon=True).start()

    def _on_analysis_finished(self, success: bool) -> None:
        self._analysis_running = False
        self._analysis_call_active = False
        if self._alive(self._summary_bar):
            self._summary_bar.stop()
            self._summary_bar.configure(value=0)
        self._summary_elapsed.set("")
        activity_row = self._widgets.get("analysis_activity_row")
        if activity_row is not None and self._alive(activity_row):
            activity_row.pack_forget()
        if not success and not self._last_analysis_error:
            self._last_analysis_error = "AI分析未完成，请返回第六步重试。"
        self._render_analysis_results()

    def _on_analysis_progress(self, text: str) -> None:
        status = self._summary_status
        if status is not None and self._alive(status):
            status.configure(text=str(text or "正在分析……"))

    def _on_analysis_stream(self, text: str) -> None:
        """Render model deltas on Tk's event thread as soon as they arrive."""
        widget = self._summary_text
        if not text or not self._alive(widget):
            return
        widget.configure(state="normal")
        widget.insert("end", str(text))
        widget.see("end")
        widget.configure(state="disabled")
        status = self._summary_status
        if status is not None and self._alive(status):
            status.configure(text="AI 正在生成分析内容……")

    def _on_analyzed(self, summary: dict) -> None:
        # 结果先存下来：页面随时可能被一次导航重建，事后再渲染也拿得到。
        self._last_summary = dict(summary)
        self._last_analysis_details = list(summary.get("details") or [])
        self._last_role_report = dict(summary.get("role_report") or {})
        analysis_text = str(summary.get("analysis_text") or "")
        if analysis_text:
            self._last_paragraph_outcome = {
                "summary": analysis_text,
                "message_count": summary.get("processed", 0),
                "partial_error": summary.get("partial_error", ""),
            }
            self._set_summary(analysis_text)
            if self._alive(self._summary_status):
                status = f"AI 已分析 {summary.get('processed', 0)} 条消息"
                if summary.get("partial_error"):
                    status = "部分完成，可返回第六步重试失败会话"
                self._summary_status.configure(text=status)
        self._render_analyzed()
        self._render_role_report()
        self._render_analysis_results()

    def _render_analyzed(self) -> None:
        summary = self._last_summary
        label = self._widgets.get("analyze")
        if not summary or label is None or not label.winfo_exists():
            return
        text = f"AI 分析完成：处理 {summary['processed']} 条消息"
        # 扣费口径来自云端账号，本地不推算。
        if summary.get("billing"):
            # 不再拆出「其中不消耗额度」那一栏：免费档位已被焊死，
            # 它恒为 0。写出来只会让商户以为还有不花钱的。
            text += (
                f"\n提交任务 {summary.get('submitted_jobs', 0)} 个，"
                f"实际扣费 {summary.get('charged_micro_usd', 0)} 次"
            )
        elif summary.get("provider") == "jyi" and summary.get("processed"):
            # JYI 按账号计量、不下发任务账单：没有 billing 不等于没花钱，
            # 别让"实际扣费 0 次"误导。用量去 JYI 网站看。
            text += (f"\n已分析 {summary['processed']} 条，用量按 JYI 账号计量")
        skipped = summary.get("already_analyzed") or 0
        if skipped:
            text += f"\n已整理过的 {skipped} 条本次没有重复提交"
        if summary.get("partial_error"):
            text += f"\n{summary['partial_error']}"
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
        # 首页内容多（状态、计数、备货、两张表、按钮排），小窗口装不下。
        # 跟整理页一样走滚动容器：窗口再小，滚到底也点得到下面的按钮。
        frame = self._scrollable_page()
        self._home_frame = frame

        banner = ttk.Frame(frame)
        banner.pack(fill="x", pady=(0, 10))
        ttk.Label(banner, text="微信AI智能助理", style="Section.TLabel").pack(side="left")
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
        # 「查看订单」就会被挤出窗口，商户再也点不到。
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
        # 持续监听与自动接单已撤掉入口：4.1.15 起微信界面点不动（UIA 黑盒），
        # 摆着两个点上去就弹窗"点了也没用"的死按钮不如不摆。以后界面能点了
        # 再加回来（后端 LiveListener 与自动流程都在，只是没人调）。
        ttk.Button(actions, text="查看订单", command=self._on_show_orders).pack(side="left")
        ttk.Button(actions, text="上一步", command=lambda: self._show_step(WizardStep.ANALYZE)).pack(side="left", padx=8)
        ttk.Button(actions, text="云端会发送哪些信息", command=self._on_privacy).pack(side="right")
        # 出问题时商户需要一个能发给客服的东西。没有入口，日志就等于不存在 ——
        # 他不会想到去数据目录里翻文件。
        ttk.Button(actions, text="出问题了？这里有日志", style="Subtle.TButton",
                   command=self._on_support).pack(side="right", padx=(0, 10))
        # 参考信息放在维护那一排：它是**偶尔做一次**的事，
        # 不是每单都要点的。
        ttk.Button(actions, text="商品与客户名单", style="Subtle.TButton",
                   command=self._on_reference_data).pack(side="right", padx=(0, 10))

        view = self.app.refresh_home()
        self._last_home = view
        try:
            self._render_home(view)
        except Exception:
            LOGGER.debug("home render skipped during rebuild", exc_info=True)
        self._render_listen()

    # --------------------------------------------------------- 持续监听开关

    def _require_uia(self, what: str) -> bool:
        """要动手（点/发/监听）的功能先过这道门。

        4.1.15 起微信界面点不动（UIA 黑盒），点了只会干等超时 ——
        直接明说，不让用户等。探针自己坏了不挡路（默认放行）。
        """
        try:
            ok = uia_automation_available()
        except Exception:
            return True
        if not ok:
            messagebox.showinfo(
                "当前微信版本点不动",
                f"{what}需要操作微信界面，可当前版本界面读不出来，点了也没用。\n\n"
                "能照常做的：检查微信、会话列表、内存读取、核对导出、智能整理。",
            )
            return False
        return True

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
            LISTEN_FAILED: "启动失败",
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
        order_box, tree = self._tree_with_scroll(
            box, columns=("status", "customer", "items", "amount", "delivery"),
            show="headings")
        order_box.pack(fill="both", expand=True)
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
        title = "选择商品数据" if kind == "products" else "选择客户数据"
        filetypes = [("CSV 表格", "*.csv"), ("所有文件", "*.*")]
        path: str | None = ""
        try:
            from .win_dialog import ask_open_path
            hwnd = 0
            try:
                hwnd = int(self.root.winfo_id())
            except Exception:
                hwnd = 0
            path = ask_open_path(
                parent_hwnd=hwnd, title=title,
                initial_dir=self._export_initial_dir(), filetypes=filetypes)
        except Exception:
            path = None
        if path is None:
            # Win11 框起不来才走 tkinter；用户点取消（""）直接收摊。
            try:
                path = filedialog.askopenfilename(
                    parent=self.root, title=title,
                    initialdir=self._export_initial_dir(), filetypes=filetypes) or ""
            except Exception:
                path = ""
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
        self._schedule_quota_refresh()
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
    "FORBIDDEN_TERMS",
    "MerchantWindow",
    "PRIVACY_TEXT",
    "audit_text",
    "display_content",
    "merchant_labels",
]
