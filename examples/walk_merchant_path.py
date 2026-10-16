"""真实客户端完整路径巡检。

前面几轮挖到的多是「测试盲区」—— 说明现有验证还有看不见的地方。
这一轮换办法：**按商户真实顺序，从头到尾走一遍**，把每一屏的控件清点出来，
凡是「存在但点不到」「看着像能点其实不会动」「文案和事实不符」的一律记下。

用真微信（真会话发现 + 真读历史），**未登录**，因为商户第一次用就是这样。

**只读，不发送，不删除任何东西。**
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.application import MerchantApplication  # noqa: E402
from wechat_ai_assistant.conversations import RealConversationSource  # noqa: E402
from wechat_ai_assistant.history_read import HistoryRange, ReadMode  # noqa: E402
from wechat_ai_assistant.merchant_app import _force_utf8_stdout  # noqa: E402
from wechat_ai_assistant.merchant_ui import MerchantWindow  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402
from wechat_ai_assistant.WeChatAuto import Messages  # noqa: E402
from wechat_ai_assistant.wechat_state import RealWeChatProbe  # noqa: E402
from wechat_ai_assistant.wizard import WizardStep  # noqa: E402

SMALL_GROUP = "电子订货系统"
LIMIT = 20

#: 输入框在界面里登记过的键名。用它标注，别显示 tk 内部变量名。
_ENTRY_KEYS = {
    "days": "输入框：天数", "limit": "输入框：最多读取条数",
}


def _entry_label(widget) -> str:
    """输入框没有文字标签。按它父级附近的标签猜用途，猜不到就说猜不到。"""
    for key, label in _ENTRY_KEYS.items():
        variable = getattr(widget, "textvariable", None)
        if variable is None:
            continue
    try:
        master = widget.winfo_master()
        for child in master.winfo_children():
            if child.winfo_class() == "TLabel":
                text = str(child.cget("text"))
                if "天前" in text:
                    return "输入框：天数"
                if "条" == text.strip():
                    return "输入框：条数"
    except Exception:
        pass
    return "输入框"

#: 这些控件在特定状态下**本来就该灰掉**。把它们当成缺陷报出来，巡检就被
#: 噪音淹掉了 —— 真正的意外禁用反而看不见。
ALLOWED_DISABLED = {
    "第 3 步 读取历史": ("下一步：智能整理",),          # 没读到消息就没有下一步
    "第 5 步 首页（未登录）": ("暂停发送",),             # 不能发送就没有发送开关
    "第 4 步 智能整理": ("开始智能整理",),               # 未登录 / 无可用模型
}


class Walker:
    def __init__(self, window) -> None:
        self.window = window
        self.findings: list[str] = []

    def pump(self, seconds: float = 0.4) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.window.root.update()
            time.sleep(0.02)
        self.window.root.update_idletasks()

    def inventory(self) -> list[tuple[str, bool, str]]:
        """所有可见按钮与输入框，以及是否真的能点。"""
        found: list[tuple[str, bool, str]] = []

        def walk(widget, depth=0):
            for child in widget.winfo_children():
                cls = child.winfo_class()
                if cls in ("TButton", "TEntry", "TCombobox", "TRadiobutton", "TCheckbutton"):
                    if cls == "TButton":
                        try:
                            label = str(child.cget("text"))
                        except Exception:
                            label = ""
                        if not label:
                            continue
                    elif cls == "TRadiobutton":
                        label = str(child.cget("text"))
                    elif cls == "TEntry":
                        # 输入框没有文字标签。显示变量名（PY_VAR7 那种）
                        # 对读巡检报告的人毫无意义 —— 按控件登记的键名标。
                        label = _entry_label(child)
                    else:
                        label = f"<{cls}>"
                    enabled = "disabled" not in str(child.cget("state"))
                    found.append((label or f"<{cls}>", enabled and child.winfo_ismapped(), cls))
                walk(child, depth + 1)

        walk(self.window.root)
        return found

    def check(self, step: str, title: str) -> None:
        self.window.root.update_idletasks()
        rows = self.inventory()
        if not rows:
            self.findings.append(f"{title}：屏幕上一个可操作控件都没有")
            return
        enabled = [label for label, ok, _cls in rows if ok]
        dead = [label for label, ok, _cls in rows if not ok]
        print(f"   可操作 {len(enabled)} 个：{'、'.join(enabled[:9])}")
        if dead:
            print(f"   灰掉   {len(dead)} 个：{'、'.join(dead[:6])}")
        # 「灰掉」本身不算缺陷 —— 未登录、没读到消息时本来就该灰。
        # 只有**不在这份合理清单里**的灰色控件才是问题。把合理禁用当成问题
        # 报出来，巡检就会被噪音淹掉，真正的缺陷反而看不见。
        for label in dead:
            if label not in ALLOWED_DISABLED.get(title, ()):
                self.findings.append(f"{title}：{label} 不可点（不在合理禁用清单里）")

    def clipped(self, step: str, title: str) -> None:
        height = self.window.root.winfo_height()
        lost = []
        for key in ("listen_btn", "read_start", "read_next", "analyze_button",
                    "quota_login", "quota_refresh"):
            widget = self.window._widgets.get(key)
            if widget is None or not widget.winfo_ismapped():
                continue
            bottom = widget.winfo_rooty() + widget.winfo_height() - self.window.root.winfo_rooty()
            if bottom > height:
                lost.append(f"{key}(超{bottom - height}px)")
        if lost:
            self.findings.append(f"{title}：够不着 {'、'.join(lost)}")

    def say(self, title: str) -> None:
        print(f"\n—— {title} ——")
        self.check(WizardStep.WECHAT, title)


def main() -> int:
    _force_utf8_stdout()
    status = RealWeChatProbe().probe()
    print(f"① 微信：{status.state}")
    if status.state != "logged_in":
        print("   微信没登录，先不跑了。")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pyw-walk-"))
    store = OrderStore(str(tmp / "orders.db"))
    tenant = store.create_merchant("路径巡检")["merchant_id"]
    app = MerchantApplication(
        data_dir=tmp, orders=store, tenant=tenant, probe=None, launcher=None,
        history_reader_fn=Messages.dump_chat_history,
        simulated=False,   # 真实客户端就是这么跑的：数据源不是演示
    )
    app.conversation_source = RealConversationSource(ui_lock=app.ui_lock)
    app.wizard.conversation_source = app.conversation_source
    window = MerchantWindow(app)
    walk = Walker(window)
    walk.pump(0.5)

    print(f"② 起始步骤：{app.wizard.step}")
    walk.say("第 1 步 检查微信")
    print(f"   顶栏：{window._widgets['account'].cget('text')}")

    # 选一个真会话
    found = app.wizard.refresh_conversations()
    target = next((c for c in found if c.name == SMALL_GROUP), None) or (found[0] if found else None)
    if target is None:
        print("   没找到任何会话，巡检中止。")
        app.close(); store.close(); shutil.rmtree(tmp, ignore_errors=True)
        return 1
    app.wizard.choose_history_conversations([target])
    app.choose_live_conversation(target)
    window._on_goto(WizardStep.HISTORY)
    walk.pump(0.4)
    print(f"\n③ 已选：{target.disambiguation}")
    walk.say("第 3 步 读取历史")
    walk.clipped(WizardStep.HISTORY, "第 3 步")

    print("\n④ 开始读取（真实微信，请勿操作电脑）")
    started = time.time()
    app.read_history(range_kind=HistoryRange.LAST_WEEK, max_messages=LIMIT,
                     mode=ReadMode.RECENT)
    deadline = time.time() + 120
    while app.wizard.history_result is None and time.time() < deadline:
        walk.pump(0.2)
    walk.pump(0.5)
    result = app.wizard.history_result
    if result is None:
        walk.findings.append("第 3 步：点了「开始读取」，2 分钟没有任何结果、界面也没有提示")
        print("   没有结果")
    else:
        print(f"   {result.headline()}（{time.time() - started:.1f} 秒）")
        walk.pump(0.3)
        state = window._widgets.get("read_state")
        print(f"   页面状态：{state.cget('text') if state is not None else '(无)'}")
        nxt = window._widgets.get("read_next")
        print(f"   「下一步」：{'可见' if nxt is not None and nxt.winfo_ismapped() else '不可见'}")
        if result.total_messages > 0 and not (nxt is not None and nxt.winfo_ismapped()):
            walk.findings.append("第 3 步：读到消息了却没有「下一步」按钮，商户走不下去")

    if result is not None and result.total_messages > 0:
        window._go_next_from_history()
        walk.pump(0.4)
        print(f"\n⑤ 进入：{app.wizard.step}")
        walk.say("第 4 步 智能整理")
        walk.clipped(WizardStep.ANALYZE, "第 4 步")
        quota = window._widgets.get("quota")
        print(f"   额度：{quota.cget('text') if quota is not None else '(未查询)'}")

    window._on_goto(WizardStep.DONE)
    walk.pump(0.6)
    walk.say("第 5 步 首页（未登录）")
    walk.clipped(WizardStep.DONE, "第 5 步")
    origin = window._widgets.get("st_origin")
    print(f"   数据来源：{origin.cget('text') if origin is not None else '(无)'}")

    print("\n⑥ 巡检结果")
    if walk.findings:
        for item in walk.findings:
            print(f"   · {item}")
    else:
        print("   没有发现够不着或不该灰掉的控件")

    for closer in (app.merger, app.cursor, app.agent_store):
        try:
            closer.close()
        except Exception:
            pass
    window.root.destroy()
    app.close()
    store.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())