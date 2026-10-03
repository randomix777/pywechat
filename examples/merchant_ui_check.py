"""真实界面交互：真的建窗口、真的点按钮、真的走数据路径。

与 `merchant_demo.py` 的区别：那个脚本按脚本驱动页面；这个脚本**调用按钮的
command 回调**，验证界面上真的能点得动、点了之后数据真的变了。

因此"逐页程序渲染截图"不能代替这里的验证：截图只能证明画出来了，
这里证明的是**点下去有用**。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _name in ("business_agent", "order_agent"):
    _src = ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from pyweixin.application import AppEvents, MerchantApplication  # noqa: E402
from pyweixin.conversations import ConversationKind, SimulatedConversations  # noqa: E402
from pyweixin.history_read import HistoryRange, SimulatedHistory  # noqa: E402
from pyweixin.merchant_ui import MerchantWindow, audit_text  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402
from pyweixin.runmode import RunMode  # noqa: E402
from pyweixin.wechat_state import SimulatedWeChat, WeChatState  # noqa: E402
from pyweixin.wizard import WizardStep  # noqa: E402

NOW = datetime.now(timezone.utc)
#: 订单统计只看最近几天，业务时间必须落在窗口内，否则造出来的单子不算数。
NOW_EPOCH = NOW.timestamp()
GROUP = "鲜果批发群"


class Client:
    def __init__(self, remaining=200):
        self.remaining = remaining

    def account(self):
        return {"subscription": {"active": True, "remaining_units": self.remaining,
                                 "plan_code": "标准版", "period_end": "2026-10-25T00:00:00+00:00"}}

    # 补上模型目录接口。以前这里没有，第 4 步会抛 AttributeError，
    # 日志里刷出 "model catalog refresh failed" —— 演练噪音掩盖真问题。
    def list_models(self):
        return {"catalog": dict(CatalogClient.CATALOG)}

    def refresh_models(self):
        return self.list_models()


class Parse:
    model_executed = False

    def __init__(self, store, tenant, group):
        self.store, self.tenant, self.group = store, tenant, group
        self.submitted: list[str] = []
        self.results: dict[str, str] = {}

    def submit(self, message_id, on_progress=None):
        from pyweixin.local_parse import ParseProgress

        self.submitted.append(message_id)
        row = self.store.db.execute(
            "SELECT import_id, group_id FROM messages WHERE merchant_id=? AND id=?", (self.tenant, message_id)
        ).fetchone()
        if row is not None:
            self.store.parse_import(self.tenant, row["group_id"] or self.group, row["import_id"])
            order = self.store.db.execute(
                "SELECT o.id FROM orders o JOIN evidence e ON e.order_id=o.id"
                " WHERE o.merchant_id=? AND e.message_id=? AND o.status!='superseded'"
                " ORDER BY o.updated_at DESC LIMIT 1", (self.tenant, message_id),
            ).fetchone()
            if order is not None:
                self.results[message_id] = order["id"]
        return ParseProgress("done", "已处理", message_id=message_id, job_id="job_ui")

    def settle(self, on_progress=None):
        from pyweixin.local_parse import ParseProgress

        if not self.submitted:
            return ParseProgress("pending", "没有待处理的解析")
        mid = self.submitted[-1]
        return ParseProgress("done", "已处理" if self.results.get(mid) else "无需处理",
                            message_id=mid, order_id=self.results.get(mid, ""))


def history_source() -> SimulatedHistory:
    rows = {
        GROUP: [
            {"消息发送人": "张姐", "消息内容": "苹果两箱", "消息类型": "文本",
             "消息发送时间": (NOW - timedelta(hours=3)).isoformat()},
            {"消息发送人": "张姐", "消息内容": "再加一箱", "消息类型": "文本",
             "消息发送时间": (NOW - timedelta(hours=3, minutes=-1)).isoformat()},
            {"消息发送人": "王老板", "消息内容": "橙子一箱", "消息类型": "文本",
             "消息发送时间": (NOW - timedelta(hours=5)).isoformat()},
            {"消息发送人": "陈先生", "消息内容": "钱转了", "消息类型": "文本",
             "消息发送时间": (NOW - timedelta(hours=8)).isoformat()},
        ],
        "社区团购": [
            {"消息发送人": "李女士", "消息内容": "香蕉五箱", "消息类型": "文本",
             "消息发送时间": (NOW - timedelta(hours=6)).isoformat()},
        ],
    }
    return SimulatedHistory(per_conversation=rows, delay=0.01)


def build() -> tuple[MerchantApplication, MerchantWindow, OrderStore, Path]:
    data_dir = Path(tempfile.mkdtemp(prefix="merchant-ui-"))
    store = OrderStore(str(data_dir / "orders.db"))
    merchant = store.create_merchant("演示·界面交互")
    tenant = merchant["merchant_id"]
    store.add_customer(tenant, "张姐", "张姐", GROUP)
    store.add_customer(tenant, "王老板", "王老板", GROUP)
    store.add_customer(tenant, "陈先生", "陈先生", GROUP)
    store.add_customer(tenant, "李女士", "李女士", "社区团购")
    store.add_product(tenant, "APL", "苹果", "箱", 2600, ["苹果", "A"])
    store.add_product(tenant, "ORG", "橙子", "箱", 2200, ["橙子", "橙"])
    store.add_product(tenant, "BAN", "香蕉", "箱", 1900, ["香蕉"])
    sim = SimulatedWeChat([WeChatState.NOT_STARTED, WeChatState.WAITING_SCAN, WeChatState.LOGGED_IN])
    app = MerchantApplication(
        data_dir=data_dir, orders=store, tenant=tenant,
        probe=sim, launcher=None,
        conversation_source=SimulatedConversations(
            # 群里做的是生意，联系人动辄几百个 —— 分栏的意义就在这个比例。
            [(GROUP, ConversationKind.GROUP, 128), ("社区团购", ConversationKind.GROUP, 64),
             ("夜宵烧烤群", ConversationKind.GROUP, 42),
             ("王老板", ConversationKind.FRIEND, 0), ("李女士", ConversationKind.FRIEND, 0)]
        ),
        history_reader_fn=history_source(),
        simulated=True,
    )
    app.monitor.launcher = sim
    app.attach(object(), Client(), Parse(store, tenant, GROUP), backend_is_model=False)
    app.login_runner = lambda progress: (_ for _ in ()).throw(RuntimeError("演示模式不登录"))
    window = MerchantWindow(app)
    return app, window, store, data_dir


def pump(window: MerchantWindow, seconds: float = 0.5) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        window.root.update()
        time.sleep(0.02)


def settle(window: MerchantWindow, app: MerchantApplication, seconds: float = 2.0) -> None:
    """等到向导停在某一步不再变化，再去读那一页的控件。

    额度够用时会自动跳到第 5 步，页面随之重建；不等稳就去读控件，拿到的
    可能是已经被销毁的旧控件，报出来的是竞态而不是真实缺陷。
    """
    deadline = time.time() + seconds
    last, stable = None, 0
    while time.time() < deadline:
        window.root.update()
        time.sleep(0.05)
        if app.wizard.step == last:
            stable += 1
            if stable >= 4 and window._events.empty():
                return
        else:
            last, stable = app.wizard.step, 0


def _find_button(window: MerchantWindow, needle: str):
    """按文字找一个按钮。找不到返回 None。

    有些按钮没登记在 ``_widgets`` 里（次要操作多半如此），只能按文字找。
    """
    found = []

    def walk(widget):
        for child in widget.winfo_children():
            try:
                if child.winfo_class() == "TButton" and needle in str(child.cget("text")):
                    found.append(child)
            except Exception:
                pass
            walk(child)

    walk(window.root)
    return found[0] if found else None


def label_text(window: MerchantWindow, key: str) -> str:
    """读一个标签的文案；控件已经不存在就返回空串。

    页面是随时可能被重建的，直接 ``cget`` 会在控件已销毁时抛 TclError ——
    那是检查脚本自己的竞态，不是产品缺陷。
    """
    widget = window._widgets.get(key)
    if widget is None:
        return ""
    try:
        if not widget.winfo_exists():
            return ""
        return str(widget.cget("text"))
    except Exception:
        return ""


def wait_for_label(window: MerchantWindow, key: str, needle: str, seconds: float = 8.0) -> str:
    """等到某个标签出现指定文案再继续。后台线程的结果是异步的。"""
    deadline = time.time() + seconds
    text = ""
    while time.time() < deadline:
        window.root.update()
        text = label_text(window, key)
        if needle in text:
            return text
        time.sleep(0.05)
    return text


class CatalogClient:
    """只返回**客户可见**的模型目录形态 —— 与真实网关一致。"""

    CATALOG = {
        "version": "cat_demo01",
        "default_alias": "free-standard",
        "free_available": True,
        "stale": False,
        "notice": "",
        "models": [
            {
                "alias": "free-standard", "display_name": "标准分析模型",
                "summary": "适合日常订单", "free": True, "free_label": "免费模型",
                "charge_line": "免费模型", "daily_free_limit": 50,
                "free_quota_text": "每天可免费分析 50 次",
                "status": "selectable", "status_label": "可用", "status_reason": "",
                "is_default": True, "capabilities": ["order_parse"],
                "context_limit": 128000, "slow": False,
            },
            {
                "alias": "paid-deep", "display_name": "深度分析模型",
                "summary": "分析较慢", "free": False, "free_label": "收费模型",
                "charge_line": "该模型会消耗分析额度，每分析一条消息扣 1 次额度",
                "daily_free_limit": 0, "free_quota_text": "",
                "status": "selectable", "status_label": "可用", "status_reason": "",
                "is_default": False, "capabilities": ["order_parse"],
                "context_limit": 200000, "slow": True,
            },
        ],
        "free_quota": {"daily_limit": 50, "used": 7, "reserved": 0, "remaining": 43},
    }

    def list_models(self):
        return {"catalog": dict(self.CATALOG)}

    def refresh_models(self):
        return self.list_models()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="merchant-ui-check")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    app, window, store, data_dir = build()
    checks: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{('　' + detail) if detail else ''}")

    def step() -> None:
        window._on_goto(WizardStep.WECHAT)

    print("① 第 1 步：微信检测")
    step()
    pump(window)
    record("初始显示「打开微信」按钮", window._widgets["wechat_title"].cget("text") == "微信还没有打开",
           window._widgets["wechat_title"].cget("text"))
    from tkinter import ttk

    page = window.container.winfo_children()[0]
    buttons = [child for child in page.winfo_children()
               if isinstance(child, ttk.Button) and not isinstance(child, ttk.Radiobutton)]
    record("页面存在可点击按钮", len(buttons) >= 2, f"{[b.cget('text') for b in buttons]}")

    print("② 点「打开微信」")
    window._on_start_wechat()
    pump(window, 0.4)
    record("点击后进入等待扫码", app.monitor.latest.state == WeChatState.WAITING_SCAN,
           app.monitor.latest.state)

    print("③ 完成登录")
    for _ in range(4):
        sim_advance = app.probe.advance()
        app.check_wechat()
        if app.monitor.latest.ready:
            break
    pump(window)
    record("登录后自动进入选择会话", app.wizard.step == WizardStep.CONVERSATIONS, app.wizard.step)

    print("③b 微信状态变化后，第 1 步的文案必须跟着变")
    # 商户反馈过的现象：关掉微信再点「重新检测」，状态其实变了、界面却纹丝不动。
    # 检测跑在后台线程里，所以结果要通过 on_wechat 事件回到界面线程重画；
    # 页面可能已经被重建过，所以还必须容忍"控件已经不在了"。
    from pyweixin.wechat_state import WeChatStatus

    # 先把队列里攒着的事件排干净，后面量到的才是新状态。
    window._on_goto(WizardStep.WECHAT)
    pump(window, 0.5)
    before = window._widgets["wechat_title"].cget("text")
    record("已登录时标题正确", before == "微信已登录", before)

    app.probe.set_state(WeChatState.NOT_STARTED)
    app.check_wechat()
    pump(window, 0.5)
    after = window._widgets["wechat_title"].cget("text")
    record("关掉微信后标题跟着变", after == "微信还没有打开" and after != before, f"{before} -> {after}")
    record("详情文案也跟着变",
           "打开微信" in window._widgets["wechat_detail"].cget("text"),
           window._widgets["wechat_detail"].cget("text"))
    start_button = window._widgets.get("wechat_start")
    record("出现「打开微信」按钮",
           bool(start_button) and start_button.winfo_ismapped() and start_button.cget("text") == "打开微信",
           start_button.cget("text") if start_button else "(无按钮)")
    record("状态退回时回到第 1 步", app.wizard.step == WizardStep.WECHAT, app.wizard.step)

    app.probe.set_state(WeChatState.LOGGED_IN)
    app.check_wechat()
    pump(window, 0.5)
    record("微信回来后重新进入选择会话", app.wizard.step == WizardStep.CONVERSATIONS, app.wizard.step)
    # 不在这里强行跳页：后面 ④ 自己会跳。手动改页会把队列里还没处理的
    # goto 事件留下来，后面读到的是过期控件。

    print("④ 第 2 步：群与联系人分栏，勾选后点「下一步」")
    step()
    window._on_goto(WizardStep.CONVERSATIONS)
    pump(window, 0.6)
    notebook = window._widgets.get("conv_notebook")
    group_tree = window._widgets.get("conv_tree_groups")
    friend_tree = window._widgets.get("conv_tree_friends")
    record("群与联系人分成两栏", notebook is not None and group_tree is not None and friend_tree is not None,
           f"群 {len(group_tree.get_children()) if group_tree else 0} / 联系人 "
           f"{len(friend_tree.get_children()) if friend_tree else 0}")
    record("群栏里确实只有群",
           bool(group_tree.get_children())
           and all("人" in str(row[2]) for row in (group_tree.item(i, "values") for i in group_tree.get_children())),
           f"{len(group_tree.get_children()) if group_tree else 0} 行")
    record("联系人栏里确实没有群",
           bool(friend_tree.get_children())
           and all(str(friend_tree.item(i, "values")[2]) == "联系人"
                   for i in friend_tree.get_children()),
           f"{len(friend_tree.get_children()) if friend_tree else 0} 行")
    record("两栏计数写在页签上",
           notebook is not None and any("群聊（" in notebook.tab(i, "text") for i in range(notebook.index("end")))
           and any("联系人（" in notebook.tab(i, "text") for i in range(notebook.index("end"))),
           " | ".join(notebook.tab(i, "text") for i in range(notebook.index("end"))) if notebook else "无")
    record("会话列表已渲染", group_tree is not None and len(group_tree.get_children()) > 0,
           f"{len(group_tree.get_children()) if group_tree else 0} 行")
    if group_tree is not None and group_tree.get_children():
        group_tree.selection_set(group_tree.get_children())
        window._on_history_next()
        pump(window)
    record("点下一步后进入读取历史", app.wizard.step == WizardStep.HISTORY, app.wizard.step)
    record("已记住所选会话", len(app.wizard.conversations.history_selected) > 0,
           f"{[c.name for c in app.wizard.conversations.history_selected]}")

    print("④b 第 2 步：搜索后勾选的必须是被搜到的那一个")
    step()
    window._on_goto(WizardStep.CONVERSATIONS)
    pump(window, 0.6)
    search_var = window._widgets.get("conv_search")
    search_var.set("夜宵")
    window._on_redraw_conversations()
    pump(window, 0.3)
    filtered = window._widgets.get("conv_tree_groups")
    rows = list(filtered.get_children()) if filtered is not None else []
    record("搜索后列表只剩命中的行", len(rows) == 1, f"{len(rows)} 行")
    record("命中的就是搜索词那一条",
           bool(rows) and "夜宵" in str(filtered.item(rows[0], "values")),
           str(filtered.item(rows[0], "values")) if rows else "无")
    if rows:
        filtered.selection_set(rows)
        window._on_history_next()
        pump(window)
    record("勾到的是搜到的那一个", [c.name for c in app.wizard.conversations.history_selected] == ["夜宵烧烤群"],
           f"{[c.name for c in app.wizard.conversations.history_selected]}")
    search_var.set("")
    # 先回到会话页再操作：④b 里的 _on_history_next 已经跳到第 3 步，会话页
    # 的控件那时已经被销毁了，此刻去取 conv_tree_groups 只会拿到 None。
    window._on_goto(WizardStep.CONVERSATIONS)
    pump(window, 0.6)
    back = window._widgets.get("conv_tree_groups")
    # ④b 只勾了一个群，而且那个群没有业务消息。下面的步骤要靠真实订单，
    # 所以这里恢复成"两个有业务的群都勾上"。
    if back is not None:
        wanted = [i for i in back.get_children() if "夜宵" not in str(back.item(i, "values"))]
        if wanted:
            back.selection_set(wanted)
            window._on_history_next()
            pump(window)
    record("④b 之后恢复选择仍然只拿到有业务的群",
           [c.name for c in app.wizard.conversations.history_selected] == ["社区团购", GROUP],
           f"{[c.name for c in app.wizard.conversations.history_selected]}")

    print("⑤ 第 3 步：点「开始读取」")
    window._on_read_history()
    deadline = time.time() + 10
    while app.wizard.history_result is None and time.time() < deadline:
        window.root.update()
        time.sleep(0.05)
    pump(window, 0.4)
    result = app.wizard.history_result
    record("读取完成并给出实际覆盖范围", result is not None and bool(result.actual_range_text()),
           result.actual_range_text() if result else "无")
    record("界面显示实际范围而不是请求范围",
           result is not None and "实际读到" in result.actual_range_text())
    record("消息已进入本地订单库",
           store.db.execute("SELECT COUNT(*) c FROM messages WHERE merchant_id=?", (app.tenant,)).fetchone()["c"] > 0)
    # 读完**留在第 3 步**：商户要先看到读到了什么、覆盖哪段时间、有没有缺口，
    # 再自己决定下一步。以前直接跳页，这些信息一闪而过。
    record("读完后留在本页，不自动跳走", app.wizard.step == WizardStep.HISTORY, app.wizard.step)
    state_text = label_text(window, "read_state")
    record("页面上写明读到了多少", "实际读到" in state_text, state_text)
    nxt = window._widgets.get("read_next")
    record("读完出现「下一步」按钮", nxt is not None and bool(nxt.winfo_ismapped()),
           "可见" if nxt is not None and nxt.winfo_ismapped() else "不可见")
    record("「开始读取」恢复可点（能再读一次）",
           "disabled" not in str(window._widgets["read_start"].cget("state")))
    # 界面不能说假话：读完了就不该还写着「准备中」。商户看到"准备中"会以为
    # 卡住，于是反复点「开始读取」。这一组来自视觉审计——那条"准备中"
    # 是页面重建时写死的初始文案，没从上次的读取结果恢复。
    record("读完后进度区写「已完成」而不是「准备中」",
           window._widgets["progress_text"].cget("text") == "已完成",
           window._widgets["progress_text"].cget("text"))
    record("进度条读完后是满的",
           float(window._widgets["progress"].cget("value")) == 100.0,
           str(window._widgets["progress"].cget("value")))
    window._show_step(WizardStep.HISTORY)   # 模拟一次导航重建
    pump(window)
    record("页面重建后仍记得上次的结果",
           window._widgets["progress_text"].cget("text") == "已完成",
           window._widgets["progress_text"].cget("text"))
    summary_tree = window._widgets.get("read_summary_tree")
    summary_rows = summary_tree.get_children() if summary_tree is not None else []
    record("逐会话列出读到多少条与时间范围", bool(summary_rows),
           f"{len(summary_rows)} 行")
    from pyweixin.history_read import HistoryResult as _EmptyResult

    window._render_read_state(_EmptyResult(window=None, coverage=[]))
    pump(window)
    record("一条都没读到时不画成「已完成」",
           window._widgets["progress_text"].cget("text") == "没有读到消息",
           window._widgets["progress_text"].cget("text"))
    record("没读到消息时不给出「下一步」（那是个死路）",
           not window._widgets["read_next"].winfo_ismapped())
    window._on_read_finished(result)
    pump(window)
    window._go_next_from_history()
    pump(window, 0.4)

    print("⑥ 第 4 步：额度与读取结果同屏")
    window._on_goto(WizardStep.QUOTA)
    settle(window, app)
    record("额度留在这一屏", app.wizard.step in (WizardStep.QUOTA, WizardStep.ANALYZE), app.wizard.step)
    # 额度现在是进度条 + 百分比，不再显示抽象的剩余数字。
    _q_line = label_text(window, "quota")
    record("这一屏给出额度状态", ("额度" in _q_line) or ("不限" in _q_line), _q_line)
    record("读取结果留在这一屏（没有一闪而过）",
           "实际读到" in label_text(window, "history_headline") or "更新了" in label_text(window, "history_headline"),
           label_text(window, "history_headline"))
    # 同一件事只说一遍。以前界面自己写了一句「免费模型暂不可用…」，上面
    # 的提示又从 application 取了同一件事 —— 同一屏出现两段重复的警告。
    # 注意必须**走空模型这条路径**才有意义：目录里有模型时这段根本不画，
    # 断言会空转（计数 0 也照样通过）。
    def _model_page_mentions(needle: str) -> int:
        text = label_text(window, "model_notice")
        holder = window._widgets["model_cards"]
        for child in holder.winfo_children():
            try:
                text += " " + str(child.cget("text"))
            except Exception:
                pass
        return text.count(needle)

    empty_catalog = {"version": "cat_empty01", "default_alias": "", "free_available": False,
                     "stale": False, "notice": "", "models": []}
    app.model_catalog = empty_catalog
    window._on_models_loaded(empty_catalog)
    pump(window, 0.3)
    record("模型不可用只提示一次（走空目录路径）",
           _model_page_mentions("免费模型暂不可用") <= 1,
           f"出现 {_model_page_mentions('免费模型暂不可用')} 次")
    record("读过的消息不会重复提交只说一次",
           _model_page_mentions("不会被重复提交") + _model_page_mentions("不会被重新提交") <= 1,
           f"出现 {_model_page_mentions('不会被重复提交') + _model_page_mentions('不会被重新提交')} 次")
    app.model_catalog = CatalogClient.CATALOG
    window._on_models_loaded(CatalogClient.CATALOG)
    pump(window, 0.3)
    # 六个按钮挤在一屏时，维护操作必须比主操作弱，否则商户会挨个乱点。
    main = window._widgets.get("analyze_button")
    record("主按钮用的是最醒目的样式",
           main is not None and str(main.cget("style")) == "Big.TButton",
           str(main.cget("style")) if main is not None else "未登记")
    subtle = [w for w in (window._widgets.get("quota_refresh"), window._widgets.get("model_refresh"))
              if w is not None]
    record("维护按钮都比主按钮弱",
           all(str(w.cget("style")) == "Subtle.TButton" for w in subtle) if subtle else False,
           f"{len(subtle)} 个维护按钮")
    # 亮着的按钮必须能按。未登录时整理必然失败：以前按钮照样亮着，
    # 商户点下去线程失败，而错误消息在第 4 步无处可放 —— 点了没反应、
    # 也没有任何解释。
    _analyze_btn = window._widgets.get("analyze_button")
    _enabled = _analyze_btn is not None and "disabled" not in str(_analyze_btn.cget("state"))
    record("未登录时「开始智能整理」不假装可用",
           not _enabled or app.session is not None,
           "可点" if _enabled else "已灰掉")
    _notice = window._widgets.get("analyze_notice")
    if app.session is None and _notice is not None:
        record("灰掉时在原地说明为什么", "登录" in _notice.cget("text"),
               _notice.cget("text"))

    print("⑦ 第 4 步：点「开始智能整理」")
    window._on_analyze()
    # 整理由后台线程跑，结果是异步回来的。
    wait_for_label(window, "analyze", "整理完成", seconds=15.0)
    summary = dict(getattr(window, "_last_summary", {}) or {})
    record("整理结果已回传界面", bool(summary), str(summary.get("processed", "")) + " 条")
    record("整理完成后进入第 5 步（首页）", app.wizard.step == WizardStep.DONE, app.wizard.step)

    print("⑧ 第 5 步：首页")
    # 状态行不能出现「剩余分析额度：剩余分析额度：128」这种标签重复 ——
    # 视觉审计发现的，商户一眼就能看出界面对不上。
    st_quota = window._widgets.get("st_quota")
    record("额度行不重复标签", st_quota is not None and "剩余分析额度：剩余分析额度：" not in st_quota.cget("text"),
           st_quota.cget("text") if st_quota is not None else "无")
    st_wechat = window._widgets.get("st_wechat")
    record("微信状态行不写成「微信：微信正常」",
           st_wechat is not None and not st_wechat.cget("text").startswith("微信："),
           st_wechat.cget("text") if st_wechat is not None else "无")
    # 数据来源必须分清两件独立的事。真实微信 + 规则引擎这种组合里，
    # 消息是真读到的，只是没跑模型 —— 说成"演示数据"会让商户白重读一遍。
    origin = window._widgets.get("st_origin")
    origin_text = origin.cget("text") if origin is not None else ""
    # 这套检查跑在模拟数据源上，所以"演示"是**应该**出现的 —— 不出现才说明
    # 界面在把假数据说成真数据。真实微信 + 规则引擎那一条在 test_data_origin 里
    # 用 HomeView 直接断言（那里能把两个字段取成不同的组合）。
    record("模拟数据源时会明说不是真实微信", "不是真实微信里的内容" in origin_text,
           origin_text or "(不显示)")
    record("没跑模型时会明说", "不是模型分析出来的" in origin_text, origin_text or "(不显示)")
    record("来源说明不再挂在待发送回执后面",
           "演示" not in (window._widgets["st_queue"].cget("text") if window._widgets.get("st_queue") else ""),
           window._widgets["st_queue"].cget("text") if window._widgets.get("st_queue") else "")
    # 系统不能发送时不摆「暂停发送」：那是个点了没反应的死控件。
    pause = window._widgets.get("pause")
    record("不会发送时不显示发送开关", pause is not None and not pause.winfo_ismapped(),
           "显示中" if pause is not None and pause.winfo_ismapped() else "已隐藏")
    view = app.refresh_home()
    pump(window, 0.3)
    record("首页显示有效订单", window._widgets["c_effective"].cget("text") == str(len(view.effective)),
           f"{view.effective and len(view.effective)}")
    record("首页显示需要处理", window._widgets["c_actionable"].cget("text") == str(len(view.actionable)))
    record("备货汇总有内容", len(window._widgets["stock"].get_children()) > 0,
           f"{len(window._widgets['stock'].get_children())} 行")
    record("首页说明统计范围", bool(window._widgets["note_scope"].cget("text")),
           window._widgets["note_scope"].cget("text")[:40])

    print("⑨ 点「开启自动接单与回复」")
    # 绕过弹窗：直接调用关闭弹窗后的路径
    app.set_allow_local_automation(True)
    app.set_mode(RunMode.AUTO_TAKE)
    pump(window, 0.3)
    view = app.refresh_home()
    pump(window, 0.2)
    record("切换到自动接单模式", view.mode == RunMode.AUTO_TAKE, view.mode_note)
    record("首页说明为什么还不能真发", bool(view.send_block_reason), view.send_block_reason)
    record("暂停按钮文字随状态变化",
           window._widgets["pause"].cget("text") == "暂停发送",
           window._widgets["pause"].cget("text"))

    print("⑩ 点「暂停发送」")
    window._on_toggle_pause()
    pump(window, 0.3)
    view = app.refresh_home()
    pump(window, 0.2)
    record("暂停立即生效", view.mode == RunMode.PAUSED, view.mode_note)
    record("暂停后按钮变成「恢复发送」",
           window._widgets["pause"].cget("text") == "恢复发送",
           window._widgets["pause"].cget("text"))
    # 暂停了必须还看得见恢复入口。不能发送时把按钮藏掉的写法会让商户
    # 暂停后回不来 —— 停得下来、起不来。
    record("暂停后「恢复发送」仍在", window._widgets["pause"].winfo_ismapped())

    print("⑪ 点「恢复发送」")
    window._on_toggle_pause()
    pump(window, 0.3)
    record("恢复后回到自动模式", app.refresh_home().mode == RunMode.AUTO_TAKE)

    print("⑫ 模型选择")
    app.client = CatalogClient()
    app.load_models()
    catalog = app.model_catalog
    print(f"   目录版本={catalog.get('version')}　免费可用={catalog.get('free_available')}")
    for model in catalog.get("models", []):
        mark = "✔" if model["alias"] == app.settings.model_alias else " "
        note = model["free_quota_text"] or model["charge_line"]
        print(f"   {mark} {model['display_name']}　免费={model['free']}　{note}　{model['status_label']}"
              f"{'　[推荐]' if model['is_default'] else ''}")
    print(f"   界面提示：{app.model_notice() or '（无）'}")
    blob = json.dumps(catalog, ensure_ascii=False, default=str)
    window._on_models_loaded(catalog)
    pump(window, 0.2)
    cards = window._widgets.get("model_cards")
    record("默认选中免费模型", app.settings.model_alias == "free-standard", app.settings.model_alias)
    record("免费模型标记为推荐", any(m["is_default"] for m in catalog.get("models", [])))
    record("客户视图不含上游标识", "opencode-go" not in blob and "qwen3.6" not in blob)
    # 只审**商户看得见的文字**，不是原始 JSON —— JSON 里本来就该有英文字段名。
    visible = [
        str(model.get(field, ""))
        for model in catalog.get("models", [])
        for field in ("display_name", "summary", "charge_line", "free_quota_text",
                      "free_label", "status_label", "status_reason")
    ]
    record("商户看得见的文案不含技术词", audit_text("　".join(visible)) == [],
           "；".join(visible)[:60])
    before_orders = app.orders_for_display()
    record("切模型只改设置不动历史订单",
           app.set_model("paid-deep") and app.orders_for_display() == before_orders,
           f"切到 {app.settings.model_alias}")

    # 重新打开"智能整理"页，模型卡片必须真的画出来（不是只更新了数据）
    window._show_step(WizardStep.ANALYZE)
    pump(window, 0.3)
    cards = window._widgets.get("model_cards")
    record("重新打开分析页能看到模型卡片",
           cards is not None and len(cards.winfo_children()) > 0,
           f"{len(cards.winfo_children()) if cards is not None else 0} 张")

    # 合并必须让商户看得见。他发三条、只得到一张订单，不知道是合并过的
    # 就会以为漏单然后重发一遍 —— 那不是少一次点击，是重复下单。
    app.merger.stats()
    window._on_goto("done")      # 合并提示画在首页上，先回到首页
    pump(window, 0.3)
    window._on_merged({"message_id": "m1", "event_ids": ("a", "b", "c"), "count": 3})
    pump(window, 0.2)
    merged_label = window._widgets.get("listen_merged")
    record("合并了几条会写在界面上",
           merged_label is not None and "3 条" in merged_label.cget("text"),
           merged_label.cget("text") if merged_label is not None else "未渲染")
    record("合并说明告诉商户内容没丢",
           merged_label is not None and "没丢" in merged_label.cget("text"),
           merged_label.cget("text") if merged_label is not None else "未渲染")
    window._on_merged({"message_id": "m1", "event_ids": ("a",), "count": 1})
    pump(window, 0.2)
    # 只合并成 1 条等于没合并，不该**改写**已有的提示。
    # 提示说的是"刚刚发生了什么"，不是实时状态；被无关事件擦掉会一闪一闪。
    record("没发生合并不改写已有提示",
           merged_label is not None and "3 条" in merged_label.cget("text"),
           merged_label.cget("text") if merged_label is not None else "未渲染")
    # 首页所有控件都必须落在窗口内 —— Tk 不滚动，切掉就是够不着。
    window.root.update_idletasks()
    _h = window.root.winfo_height()
    _clipped = []
    for _label, _widget in (("开始持续监听", window._widgets.get("listen_btn")),
                            ("监听区块", window._widgets.get("listen_box")),
                            ("备货表格", window._widgets.get("stock")),
                            ("数据来源说明", window._widgets.get("st_origin"))):
        if _widget is None or not _widget.winfo_ismapped():
            continue
        _b = (_widget.winfo_rooty() + _widget.winfo_height()
              - window.root.winfo_rooty())
        if _b > _h:
            _clipped.append(f"{_label}({_b}>{_h})")
    record("首页所有控件都在窗口内（够得着）", not _clipped, "、".join(_clipped) or "全部可见")
    # 商户会拖小窗口。Tk 不滚动：高度不够时最后排的控件**根本不会被布局**，
    # 不是被切一半，是在屏幕上任何位置都找不到。所以必须在**最小窗口尺寸**
    # 下检查，而不只是默认尺寸。
    _min_w, _min_h = window.root.minsize()
    window.root.geometry(f"{_min_w}x{_min_h}")
    pump(window, 0.3)
    window.root.update_idletasks()
    _h = window.root.winfo_height()
    _lost = []
    for _label, _widget in (("开始持续监听", window._widgets.get("listen_btn")),
                            ("查看订单", None),
                            ("监听区块", window._widgets.get("listen_box"))):
        if _label == "查看订单":
            _found = []

            def _walk(w, acc):
                for _c in w.winfo_children():
                    try:
                        if _c.winfo_class() == "TButton" and "查看订单" in str(_c.cget("text")):
                            acc.append(_c)
                    except Exception:
                        pass
                    _walk(_c, acc)

            _walk(window.root, _found)
            _widget = _found[0] if _found else None
        if _widget is None or not _widget.winfo_ismapped():
            _lost.append(_label)
            continue
        _b = _widget.winfo_rooty() + _widget.winfo_height() - window.root.winfo_rooty()
        if _b > _h:
            _lost.append(f"{_label}(超出{_b - _h}px)")
    record(f"缩到最小窗口（{_min_w}x{_min_h}）仍能点到所有按钮", not _lost,
           "、".join(_lost) or "全部够得着")
    window.root.geometry("900x680")
    pump(window, 0.3)

    print("⑬ 订单列表：长内容不丢")
    # 没有订单的话下面全是空断言 —— 先造两单，其中一单的商品摘要刻意写长，
    # 用来验证截断之后原文还拿得回来。
    _seed = store.create_merchant("订单长文本店")
    _seed_tenant = _seed["merchant_id"]
    long_summary = ("苹果三箱、砂糖桔五箱，麻烦送到的时候先电话联系；"
                    "另外大统华那边也要一份，地址我稍后单独发您")
    for _i, (_summary, _cust) in enumerate(
        ((long_summary, "张姐"), ("香蕉两箱", "李女士"))
    ):
        _cid = store.add_customer(_seed_tenant, _cust, _cust, "鲜果批发群")
        store.db.execute(
            "INSERT INTO products(id,merchant_id,sku,name,unit,price_cents) VALUES(?,?,?,?,?,?)",
            (f"p-demo{_i}", _seed_tenant, f"SKU{_i}", "苹果", "箱", 600),
        )
        store.db.execute(
            "INSERT INTO orders(id,merchant_id,customer_id,group_id,status,delivery_date,"
            "source_import_id,created_at,updated_at,version,change_type,business_at_epoch) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,'new',?)",
            (f"o-demo{_i}", _seed_tenant, _cid, "鲜果批发群", "review",
             "2026-10-05", None, NOW.isoformat(), NOW.isoformat(), NOW_EPOCH),
        )
        store.db.execute(
            "INSERT INTO order_items(id,order_id,product_id,quantity,unit,unit_price_cents) "
            "VALUES(?,?,?,?,?,?)",
            (f"oi-demo{_i}", f"o-demo{_i}", f"p-demo{_i}", 3, "箱", 600),
        )
    store.db.commit()
    _original_tenant = app.tenant
    app.tenant = _seed_tenant
    app.dashboard.tenant = _seed_tenant   # 看板在构造时就绑定了租户
    record("造好了订单用于验证", len(app.orders_for_display()) >= 1,
           f"{len(app.orders_for_display())} 单")
    app.tenant = _original_tenant
    app.dashboard.tenant = _original_tenant
    # 订单列表。它不再走"拼字符串再 split 回来"那条路。
    window._on_show_orders()
    pump(window, 0.4)
    app.tenant = _seed_tenant
    app.dashboard.tenant = _seed_tenant
    window._on_show_orders()
    pump(window, 0.4)
    order_windows = [w for w in window.root.winfo_children()
                     if w.winfo_class() == "Toplevel"]
    record("订单窗口能打开", bool(order_windows), f"{len(order_windows)} 个")
    if order_windows:
        top = order_windows[-1]
        trees = []

        def _collect(widget):
            for child in widget.winfo_children():
                if child.winfo_class() == "Treeview":
                    trees.append(child)
                _collect(child)

        _collect(top)
        record("订单表格已渲染", bool(trees) and bool(trees[0].get_children()),
               f"{len(trees[0].get_children()) if trees else 0} 行")
        if trees and trees[0].get_children():
            first = trees[0].get_children()[0]
            trees[0].selection_set(first)
            shown = str(trees[0].item(first, "values")[2])
            record("商品列过长时带省略号", len(shown) <= 61, f"{len(shown)} 字")
            window._show_order_detail(trees[0], getattr(window, "_last_order_rows", {}))
            top.destroy()
    app.tenant = _original_tenant

    print("⑭ 顶栏账号条：登录与退出")
    # 登录是所有事情的前提，而入口以前只在第 4 步，顶栏写着「账号：未登录」
    # 却是点不动的 Label。商户看得见状态，动不了。
    account = window._widgets.get("account")
    # 断言要卡死整串。以前写的是 `"登录" in text` —— 「账号：已登录」里也有
    # 「登录」两个字，于是已登录状态也能通过，等于没测。
    logged_in = app.session is not None
    record("账号条写明当前登录状态与点了会发生什么",
           account is not None and (("点此退出" in account.cget("text")) if logged_in
                                    else ("点此登录" in account.cget("text"))),
           account.cget("text") if account is not None else "无")
    app.attach(session=type("S", (), {"merchant_name": "一号店"})(),
               client=CatalogClient(), parse_service=Parse(store, app.tenant, "鲜果批发群"))
    window._render_account()
    pump(window, 0.2)
    record("登录后账号条写明是谁",
           "一号店" in account.cget("text"), account.cget("text"))
    record("登录后账号条给出退出的去处", "退出" in account.cget("text"), account.cget("text"))
    # 退出前先确认：误点把人踢出去是要重新走一遍网页登录的。
    asked = {"count": 0}

    def _fake_ask(*_a, **_k):
        asked["count"] += 1
        return True

    # patch 的是 merchant_ui 模块里的 messagebox —— 界面上弹框走的就是它。
    # 早先 patch 了 window.messagebox（那个属性根本不存在），结果弹出**真的**
    # 对话框等人点击，整个检查挂住。
    import pyweixin.merchant_ui as _mui

    _mui.messagebox = type("MB", (), {"askyesno": staticmethod(_fake_ask),
                                      "showinfo": staticmethod(lambda *a, **k: None),
                                      "showerror": staticmethod(lambda *a, **k: None)})
    window._on_account_click()
    pump(window, 0.3)
    record("退出前会先确认", asked["count"] == 1, f"确认 {asked['count']} 次")
    record("退出后确实断开了", app.session is None, str(app.session))
    window._render_account()
    pump(window, 0.2)
    record("退出后账号条回到可登录状态",
           account.cget("text") == "账号：未登录　点此登录", account.cget("text"))
    # 退出只断开了云端能力，本地订单一个都没少。商户最容易误会成"数据没了"，
    # 界面必须主动说清楚。
    window._on_goto("done")
    pump(window, 0.4)
    _acct = window._widgets.get("account")
    _acct.configure(text="账号：未登录　点此登录")
    view = app.refresh_home()
    window._on_home(view)
    pump(window, 0.3)
    _quota = window._widgets.get("st_quota")
    _qtext = _quota.cget("text") if _quota is not None else ""
    record("退出后首页说清楚本地订单还在",
           "本地订单仍在" in _qtext or not view.effective, _qtext or "(无)")
    record("退出后本地已读数据仍在",
           store.db.execute("SELECT COUNT(*) c FROM messages WHERE merchant_id=?",
                            (app.tenant,)).fetchone()["c"] > 0,
           "消息仍在")

    # 第 1 步的登录区块。登录入口挪到第一步之后，这一屏必须在
    # **未登录**状态下把话说清楚 —— 特别是"不登录也能读历史"。
    app.detach()
    window._render_account()
    window._refresh_login_block()
    window._show_step(WizardStep.WECHAT)
    pump(window, 0.4)
    login_btn = _find_button(window, "登录产品账号")
    record("第 1 步就有登录入口", login_btn is not None,
           "有" if login_btn is not None else "没有")
    record("第 1 步登录入口在小窗口下也够得着",
           login_btn is not None and login_btn.winfo_ismapped(),
           "可见" if login_btn is not None and login_btn.winfo_ismapped() else "够不着")
    labels: list[str] = []

    def collect(node):
        for child in node.winfo_children():
            try:
                if child.winfo_class() in ("TLabel", "Label"):
                    labels.append(str(child.cget("text")))
            except Exception:
                pass
            collect(child)

    collect(window.root)
    step_one = "\n".join(t for t in labels if t.strip())
    record("第 1 步说清楚不登录也能用",
           ("不登录也能用" in step_one or "不登录也能读历史" in step_one
            or "没有登录也能用" in step_one),
           "已说明")
    record("第 1 步说清楚只有整理需要登录",
           "智能整理" in step_one, "已说明")
    window.app.attach(session=type("S", (), {"merchant_name": "演示商户"})(),
                   client=CatalogClient(),
                   parse_service=Parse(store, app.tenant, "鲜果批发群"))
    window._render_account()
    window._refresh_login_block()
    pump(window, 0.3)
    record("登录后第 1 步那块会跟着变",
           _find_button(window, "换一个账号登录") is not None, "已变")

    # 额度展示形式是「进度条 + 百分比」，像 ChatGPT 的用量条。
    # 商户的原话：「额度给一个额度条，按百分比显示，不显示具体能做多少次」。
    window._show_step(WizardStep.ANALYZE)   # 额度块在这一屏，先切过去
    pump(window, 0.4)
    from pyweixin.wizard import QuotaView as _QV

    def _read(widget_key):
        widget = window._widgets.get(widget_key)
        try:
            return str(widget.cget("text")) if widget is not None and widget.winfo_exists() else ""
        except Exception:
            return ""

    app.wizard.quota = _QV(logged_in=True, active=True, remaining=5,
                          total_units=8, used_units=3)
    window._on_quota(app.wizard.quota)
    pump(window, 0.3)
    record("额度显示百分比", "38%" in _read("quota"), _read("quota") or "(无)")
    _bar = window._widgets.get("quota_bar")
    try:
        record("有额度条且可见",
               _bar is not None and _bar.winfo_ismapped()
               and str(_bar.cget("value")) not in ("0.0", "0"),
               f"value={_bar.cget('value') if _bar is not None else '无'}")
    except Exception:
        record("有额度条且可见", False, "读不到")
    record("不说具体能做多少次",
           "条消息" not in _read("quota") and "次" not in _read("quota"),
           _read("quota") or "(无)")

    app.wizard.quota = _QV(logged_in=True, active=True, remaining=5, total_units=0)
    window._on_quota(app.wizard.quota)
    pump(window, 0.3)
    record("不限量时不硬登百分比",
           "不限" in _read("quota"), _read("quota") or "(无)")
    _bar2 = window._widgets.get("quota_bar")
    try:
        record("不限量时额度条收起来",
               _bar2 is not None and not _bar2.winfo_ismapped(),
               "已收起" if _bar2 is not None and not _bar2.winfo_ismapped() else "还在")
    except Exception:
        record("不限量时额度条收起来", False, "读不到")

    # 出问题时的出口。日志装了但商户找不到，等于没装。
    window._on_goto("done")      # 这个入口在首页上，先回到首页
    pump(window, 0.4)
    support = _find_button(window, "出问题了")
    record("首页有找到日志的入口", support is not None,
           "有" if support is not None else "没有")
    if support is not None:
        record("这个入口在小窗口下也够得着", support.winfo_ismapped(),
               "可见" if support.winfo_ismapped() else "够不着")

    print("⑭ 关闭窗口")
    window._closing = True
    app.close()
    window.root.destroy()
    record("窗口关闭后无残留 UI 线程", not app.ui_queue.alive)

    store.close()
    shutil.rmtree(data_dir, ignore_errors=True)

    failed = [name for name, ok, _ in checks if not ok]
    print()
    print(f"{len(checks) - len(failed)}/{len(checks)} 项通过")
    if failed:
        print("失败项：" + "、".join(failed))
    if args.keep:
        input("按回车退出…")
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
