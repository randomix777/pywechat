"""数一数：第一次使用，到看见第一条订单，要点几次。

目标是"少点击"，但这个数从来没被量过。凭感觉说"够简洁"是没有意义的。

走的是**真实的窗口**，只把微信换成模拟的（真微信要占用前台，而且会往
真实群里读）。商户的真实处境是：刚装好、没登录、什么都不懂。

**只读，不发送。**
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "order_agent" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "business_agent" / "src"))

from pyweixin.application import MerchantApplication  # noqa: E402
from pyweixin.conversations import ConversationKind, SimulatedConversations  # noqa: E402
from pyweixin.history_read import SimulatedHistory  # noqa: E402
from pyweixin.merchant_app import _force_utf8_stdout  # noqa: E402
from pyweixin.merchant_ui import MerchantWindow  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402
from pyweixin.wizard import WizardStep  # noqa: E402

NOW = datetime.now(timezone.utc)


def rows():
    out = {}
    for name, count in (("鲜果批发群", 12), ("社区团购", 6)):
        out[name] = [
            {"消息发送人": f"客户{i % 3}", "消息内容": f"苹果{i + 1}箱",
             "消息类型": "文本",
             "消息发送时间": (NOW - timedelta(hours=i)).isoformat()}
            for i in range(count)
        ]
    return out


def main() -> int:
    _force_utf8_stdout()
    tmp = Path(tempfile.mkdtemp(prefix="pyw-clicks-"))
    store = OrderStore(str(tmp / "orders.db"))
    tenant = store.create_merchant("点击数验证")["merchant_id"]
    app = MerchantApplication(
        data_dir=tmp, orders=store, tenant=tenant, probe=None, launcher=None,
        conversation_source=SimulatedConversations(
            [("鲜果批发群", ConversationKind.GROUP, 128),
             ("社区团购", ConversationKind.GROUP, 64),
             ("王老板", ConversationKind.FRIEND, 0)]
        ),
        history_reader_fn=SimulatedHistory(rows(), delay=0.01), simulated=True,
    )
    window = MerchantWindow(app)

    def pump(seconds: float = 0.3) -> None:
        end = time.time() + seconds
        while time.time() < end:
            window.root.update()
            time.sleep(0.01)
        window.root.update_idletasks()

    clicks: list[str] = []

    def click(label: str) -> None:
        clicks.append(label)

    print("① 新商户的起点")
    pump(0.5)
    print(f"   起始步骤：{app.wizard.step}")
    print(f"   顶栏：{window._widgets['account'].cget('text')}")
    print(f"   登录状态：{app.session is not None}\n")

    print("② 按商户会做的顺序走一遍")
    # —— 检查微信（第 1 步）
    app.wizard.check_wechat()
    pump()
    if app.wizard.step != "conversations":
        print(f"   ★ 没自动进入下一步，停在：{app.wizard.step}")

    # —— 选会话（第 2 步）
    window._on_goto(WizardStep.CONVERSATIONS)
    pump(0.4)
    tree = window._widgets.get("conv_tree_groups")
    if tree is not None and tree.get_children():
        tree.selection_set(tree.get_children())
        window._on_history_next()          # ①点「下一步：读取历史」
        click("下一步：读取历史")
        pump(0.4)
    print(f"   选了 {len(app.wizard.conversations.history_selected)} 个会话")

    # —— 读取（第 3 步）
    window._on_read_history()             # ②点「更新消息」
    click("更新消息")
    deadline = time.time() + 30
    while app.wizard.history_result is None and time.time() < deadline:
        pump(0.2)
    pump(0.5)
    result = app.wizard.history_result
    print(f"   {result.headline() if result else '没有结果'}")
    nxt = window._widgets.get("read_next")
    print(f"   「下一步」按钮：{'可见' if nxt is not None and nxt.winfo_ismapped() else '不可见'}")

    if result is not None and result.total_messages > 0:
        window._go_next_from_history()     # ③点「下一步：智能整理」
        click("下一步：智能整理")
        pump(0.5)
        print(f"   进入：{app.wizard.step}")

        # —— 整理（第 4 步）
        quota = window._widgets.get("quota")
        print(f"   额度：{quota.cget('text') if quota is not None else '(未查询)'}")
        btn = window._widgets.get("analyze_button")
        enabled = btn is not None and "disabled" not in str(btn.cget("state"))
        print(f"   「开始智能整理」可点：{enabled}")
        if not enabled and app.session is None:
            print("   ★ 未登录时点不动 —— 商户必须先去顶栏登录")
            print(f"     登录入口：{window._widgets['account'].cget('text')}")
            clicks.append("账号：未登录　点此登录（必须先做这一步）")
        elif enabled:
            window._on_analyze()           # ④点「开始智能整理」
            click("开始智能整理")
            pump(1.2)
            print(f"   整理后步骤：{app.wizard.step}")
            summary = getattr(window, "_last_summary", {}) or {}
            print(f"   整理结果：{summary}")

    print(f"\n③ 到这里一共点了 {len(clicks)} 次")
    for index, label in enumerate(clicks, 1):
        print(f"   {index}. {label}")

    print("\n④ 能不能更少")
    if len(clicks) >= 3:
        print("   「下一步：智能整理」这一跳是第 1 轮加的 —— 当时为了让商户")
        print("   先看清读到了什么。现在读到的结果在第 4 屏还会再显示一次，")
        print("   所以这一跳是否还有必要，值得再想。")
    print("   「读取完成后自动进入下一步」能把 3 次降到 2 次，")
    print("   代价是商户来不及确认读到了什么 —— 这是取舍，不是 bug。")

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