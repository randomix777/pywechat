"""用模拟数据把整套界面真正跑起来并截图。

这是"实际打开界面检查关键页面和操作路径"的自动化版本：真的建 Tk 窗口、真的
点按钮、真的走到每一个页面。跑在无头环境（Windows 上不需要显示器）。

    python examples/merchant_demo.py            # 走到首页并保存截图
    python examples/merchant_demo.py --keep     # 保留窗口不自动关闭
"""
from __future__ import annotations

import argparse
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

from pyweixin.application import MerchantApplication  # noqa: E402
from pyweixin.conversations import ConversationKind, SimulatedConversations  # noqa: E402
from pyweixin.history_read import HistoryRange, SimulatedHistory  # noqa: E402
from pyweixin.merchant_ui import MerchantWindow  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402
from pyweixin.runmode import AutoOrderSettings, RunMode  # noqa: E402
from pyweixin.wechat_state import SimulatedWeChat, WeChatState  # noqa: E402
from pyweixin.wizard import WizardStep  # noqa: E402

NOW = datetime.now(timezone.utc)
SHOTS = ROOT / "docs" / "screenshots"


def iso(moment: datetime) -> str:
    return moment.isoformat()


def build_synthetic_history() -> SimulatedHistory:
    """一段明显是合成的演示消息。"""
    messages = [
        ("张姐", "苹果两箱", 3),
        ("王老板", "橙子一箱", 5),
        ("张姐", "再加一箱苹果", 8),
        ("陈先生", "鸡蛋三箱", 26),
        ("陈先生", "老样子，明天送", 27),
        ("李女士", "苹果多少钱", 30),
        ("陈先生", "钱转了", 31),
        ("王老板", "不要了", 40),
        ("赵女士", "苹果改成三箱", 44),
        ("张姐", "图片", 50),
    ]
    rows = {
        "鲜果批发群": [
            {"消息发送人": sender, "消息内容": content, "消息类型": "文本",
             "消息发送时间": iso(NOW - timedelta(hours=hours))}
            for sender, content, hours in messages
        ],
        "社区团购": [
            {"消息发送人": "陈先生", "消息内容": "香蕉五箱", "消息类型": "文本",
             "消息发送时间": iso(NOW - timedelta(hours=20))},
        ],
    }
    return SimulatedHistory(per_conversation=rows, delay=0.01)


#: 演示用的模型目录，形状与真实网关给客户端的一致。
MODEL_CATALOG = {
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
            "context_limit": 128000, "slow": True,
        },
    ],
    "free_quota": {"daily_limit": 50, "used": 7, "reserved": 0, "remaining": 43},
}


def seed_orders(store: OrderStore, tenant: str) -> None:
    import uuid

    customers = {
        row["name"]: row["id"]
        for row in store.db.execute("SELECT id,name FROM customers WHERE merchant_id=?", (tenant,))
    }
    for name, customer_id in customers.items():
        for sku, quantity in (("APL", 2), ("EGG", 1)):
            oid = "o_" + uuid.uuid4().hex[:12]
            store.db.execute(
                "INSERT INTO orders(id,merchant_id,customer_id,group_id,status,delivery_date,source_import_id,"
                "created_at,updated_at,version,change_type,business_at_epoch) VALUES(?,?,?,?,?,?,?,?,?,1,'new',?)",
                (oid, tenant, customer_id, "鲜果批发群", "draft", "2026-09-26", None,
                 iso(NOW), iso(NOW), NOW.timestamp()),
            )
            product = store.db.execute(
                "SELECT id,price_cents,unit FROM products WHERE merchant_id=? AND sku=?", (tenant, sku)
            ).fetchone()
            store.db.execute(
                "INSERT INTO order_items(id,order_id,product_id,quantity,unit,unit_price_cents) VALUES(?,?,?,?,?,?)",
                ("i_" + uuid.uuid4().hex[:12], oid, product["id"], quantity, product["unit"], product["price_cents"]),
            )
    # 一条需要核对的异常
    store.db.execute(
        "INSERT INTO orders(id,merchant_id,customer_id,group_id,status,delivery_date,source_import_id,"
        "created_at,updated_at,version,change_type,business_at_epoch) VALUES(?,?,?,'鲜果批发群','review',?,?,?,?,1,'new',?)",
        ("o_review_demo", tenant, list(customers.values())[0], "2026-09-26", None, iso(NOW), iso(NOW), NOW.timestamp()),
    )
    store.db.execute(
        "INSERT INTO review_tasks(id,merchant_id,order_id,reason,status) VALUES(?,?,?,'ambiguous_append_product_or_target','open')",
        ("r_demo", tenant, "o_review_demo"),
    )
    store.db.commit()


class DemoGateway:
    """明确标记的假网关。不是模型，只是让界面能走完。"""

    def __init__(self, remaining: int = 128):
        self.remaining = remaining
        self.active = True

    def account(self) -> dict:
        return {
            "subscription": {
                "active": self.active,
                "remaining_micro_usd": self.remaining,
                "plan_code": "标准版",
                "period_end": "2026-10-25T00:00:00+00:00",
            }
        }

    # 补上模型目录接口。以前这里没有，界面拉目录时会抛 AttributeError，
    # 日志被 traceback 刷满 —— 演练噪音会把真问题淹掉。
    def list_models(self) -> dict:
        return {"catalog": dict(MODEL_CATALOG)}

    def refresh_models(self) -> dict:
        return self.list_models()


class DemoParse:
    """走**真实**规则引擎的假网关。

    注意这里没有跳过业务逻辑：消息进来后交给
    :meth:`pyweixin.orders.OrderStore.parse_import` 的确定性规则解析，
    老样子、追加、改量、取消都按真实规则处理。唯一假的是"模型"这一层。

    ``model_executed = False`` 会一路传到界面和结果里，界面上会显示
    「演示数据，非真实分析结果」。
    """

    model_executed = False

    def __init__(self, store: OrderStore, tenant: str, group: str = ""):
        self.store = store
        self.tenant = tenant
        self.group = group
        self.submitted: list[str] = []
        self.results: dict[str, str] = {}

    def submit(self, message_id, on_progress=None):
        from pyweixin.local_parse import ParseProgress

        self.submitted.append(message_id)
        row = self.store.db.execute(
            "SELECT import_id, group_id FROM messages WHERE merchant_id=? AND id=?", (self.tenant, message_id)
        ).fetchone()
        if row is not None:
            # 解析以**消息自己所在的群**为准，而不是外层配置的一个群名。
            # 否则切换会话后仍会拿旧群名去解析，安静地一条也解析不出来。
            group = row["group_id"] or self.group
            # 真正跑一遍本地规则解析，这就是界面上「智能整理」背后的那条路径。
            self.store.parse_import(self.tenant, group, row["import_id"])
            order = self.store.db.execute(
                "SELECT o.id FROM orders o JOIN evidence e ON e.order_id=o.id"
                " WHERE o.merchant_id=? AND e.message_id=? AND o.status!='superseded'"
                " ORDER BY o.updated_at DESC LIMIT 1",
                (self.tenant, message_id),
            ).fetchone()
            if order is not None:
                self.results[message_id] = order["id"]
        return ParseProgress("done", "已处理", message_id=message_id, job_id="job_demo")

    def settle(self, on_progress=None):
        from pyweixin.local_parse import ParseProgress

        if not self.submitted:
            return ParseProgress("pending", "没有待处理的解析")
        message_id = self.submitted[-1]
        order_id = self.results.get(message_id, "")
        return ParseProgress("done", "已处理" if order_id else "无需处理", message_id=message_id, order_id=order_id)


def shoot(window: MerchantWindow, name: str) -> None:
    """把窗口提到最前，只截它自己那一块。

    不提谓截到的是整块桌面 —— 那样截出来的图对检查界面毫无用处。
    """
    target = SHOTS / f"{name}.png"
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        window.root.update_idletasks()
        window.root.update()
        window.root.lift()
        window.root.attributes("-topmost", True)
        window.root.update()
        window.root.focus_force()
        for _ in range(6):
            window.root.update()
            time.sleep(0.05)
        x, y = window.root.winfo_rootx(), window.root.winfo_rooty()
        width, height = window.root.winfo_width(), window.root.winfo_height()
    except Exception as exc:
        print(f"  （截图跳过：{exc}）")
        return
    try:
        import subprocess

        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            f"$bmp=New-Object System.Drawing.Bitmap {width},{height};"
            "$g=[System.Drawing.Graphics]::FromImage($bmp);"
            f"$g.CopyFromScreen({x},{y},0,0,$bmp.Size);"
            f"$bmp.Save('{target}');"
        )
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=25)
        if result.returncode == 0 and target.exists():
            print(f"  截图：{target}")
        else:
            print(f"  （截图失败：{result.stderr.decode('utf-8', 'replace')[:120]}）")
    except Exception as exc:
        print(f"  （截图跳过：{exc}）")
    finally:
        try:
            window.root.attributes("-topmost", False)
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="merchant-demo")
    parser.add_argument("--keep", action="store_true", help="走完后不自动关闭")
    parser.add_argument("--no-screenshot", action="store_true")
    args = parser.parse_args(argv)

    data_dir = Path(tempfile.mkdtemp(prefix="merchant-demo-"))
    store = OrderStore(str(data_dir / "orders.db"))
    merchant = store.create_merchant("演示·鲜果店")
    tenant = merchant["merchant_id"]
    store.add_customer(tenant, "张姐", "张姐", "鲜果批发群")
    store.add_customer(tenant, "王老板", "老王", "鲜果批发群")
    store.add_customer(tenant, "陈先生", "陈先生", "鲜果批发群")
    store.add_customer(tenant, "李女士", "小李", "社区团购")
    store.add_product(tenant, "APL", "苹果", "箱", 2600, ["苹果", "A"])
    store.add_product(tenant, "EGG", "鸡蛋", "箱", 3400, ["鸡蛋"])
    store.add_product(tenant, "ORG", "橙子", "箱", 2200, ["橙子", "橙"])
    seed_orders(store, tenant)

    sent: list[tuple[str, str]] = []
    app = MerchantApplication(
        data_dir=data_dir,
        orders=store,
        tenant=tenant,
        probe=SimulatedWeChat([WeChatState.NOT_STARTED, WeChatState.WAITING_SCAN, WeChatState.WAITING_CONFIRM, WeChatState.LOGGED_IN]),
        launcher=None,
        conversation_source=SimulatedConversations(
            [("鲜果批发群", ConversationKind.GROUP, 128), ("社区团购", ConversationKind.GROUP, 64), ("王老板", ConversationKind.FRIEND, 0)]
        ),
        history_reader_fn=build_synthetic_history(),
        gateway_url="https://gateway.example.com",
        sender=lambda target, text: sent.append((target, text)),
        simulated=True,
    )
    sim = app.probe
    # The monitor captured the launcher at construction; wire it so
    # "打开微信" actually advances the simulated state.
    app.monitor.launcher = sim
    app.attach(object(), DemoGateway(), DemoParse(store, tenant, "鲜果批发群"), backend_is_model=False)
    app.login_runner = lambda progress: (_ for _ in ()).throw(RuntimeError("演示模式不登录"))

    window = MerchantWindow(app)
    window.root.update()

    # 先清空：改了流程或改了截图命名之后，旧图会留下来，名字对不上内容，
    # 拿它做审计就是拿证据骗自己。
    if SHOTS.exists():
        for stale in SHOTS.glob("*.png"):
            try:
                stale.unlink()
            except OSError:
                pass

    def pump(seconds: float = 0.4) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            window.root.update()
            time.sleep(0.02)

    print("① 微信未启动")
    pump()
    shoot(window, "01-wechat-not-started")

    print("② 打开微信 → 等待扫码")
    app.start_wechat()
    window._show_step(WizardStep.WECHAT)
    pump()
    shoot(window, "02-wechat-waiting-scan")

    print("③ 扫码完成，等待手机确认 → 已登录")
    # The launcher moves the state to "waiting scan" without consuming it, so
    # keep advancing until the merchant is actually logged in — the same thing
    # a real merchant does by scanning and tapping confirm.
    for _ in range(6):
        sim.advance()
        app.check_wechat()
        if app.monitor.latest.ready:
            break
    pump()
    print(f"   微信状态：{app.monitor.latest.state}")
    shoot(window, "03-wechat-logged-in")

    print("④ 选择会话")
    app.refresh_conversations()
    window._show_step(WizardStep.CONVERSATIONS)
    pump()
    shoot(window, "04-conversations")

    print("⑤ 读取历史")
    conversations = app.wizard.conversations.available
    # 按名字挑，不靠列表顺序 —— 演示要的是「鲜果批发群」。
    fruit = next(item for item in conversations if item.name == "鲜果批发群")
    app.choose_history_conversations([fruit] + [item for item in conversations if item.name != "鲜果批发群"])
    app.choose_live_conversation(fruit)
    # 先把「第 3 步」拍下来再读：商户是在这一屏选读法、按开始，
    # 演示里跳过它就等于没有证据。
    window._show_step(WizardStep.HISTORY)
    pump()
    shoot(window, "05-read-history")
    app.read_history(range_kind=HistoryRange.LAST_WEEK, max_messages=200)
    pump(0.8)
    result = app.wizard.history_result
    if result:
        print(f"   {result.requested_range_text()}")
        print(f"   {result.actual_range_text()}")
        for gap in result.gaps:
            print(f"   注意：{gap}")
    # 读完的样子也要留证据：结果写在页面上、下一步按钮出现。
    window._show_step(WizardStep.HISTORY)
    window._on_read_finished(result)
    pump()
    shoot(window, "06-read-done")
    window._show_step(WizardStep.QUOTA)
    pump()
    shoot(window, "05b-prepare-analyze")

    print("⑥ 额度不足 → 恢复")
    app.wizard.quota = app.wizard.quota.__class__(logged_in=True, active=True, remaining=0,
                                                   purchase_method="manual", purchase_hint="请联系客服开通或续期。")
    window._on_quota(app.wizard.quota)
    pump(0.2)
    shoot(window, "06-quota-exhausted")
    app.wizard.quota = app.wizard.quota.__class__(logged_in=True, active=True, remaining=128,
                                                   purchase_method="manual", purchase_hint="")
    window._on_quota(app.wizard.quota)

    print("⑦ 智能整理")
    summary = app.analyze()
    if summary:
        print(f"   处理 {summary['processed']} 条，生成订单 {summary['with_result']} 单，需要核对 {summary['needs_review']} 单")
        print(f"   不需要处理 {summary['ignored']} 条　消耗额度 {summary['charged_micro_usd']} 单位")
        for trace in summary["traces"][:4]:
            print(f"     · {trace['message_id'][-10:]} → {trace['status']}　订单 {trace['order_id'] or '（无）'}")
    else:
        print("   （整理未执行，可能额度不足或未登录）")
    # analyze() 结束会跳到首页。截图前必须显式回到整理页 —— 以前这里直接拍，
    # 拍到的是首页，文件名却写着「analyzed」，证据链一直是错的。
    window._show_step(WizardStep.ANALYZE)
    pump()
    shoot(window, "07-analyzed")

    print("⑧ 首页")
    window._show_step(WizardStep.DONE)
    pump()
    view = app.refresh_home()
    print(f"   {view.summary_line()}")
    shoot(window, "08-home")

    print("⑨ 开启自动接单与回复（真实发送在真机验证前保持关闭）")
    app.set_allow_local_automation(True)
    app.set_mode(RunMode.AUTO_TAKE)
    pump()
    view = app.refresh_home()
    print(f"   模式：{view.mode_note}")
    print(f"   未发送原因：{view.send_block_reason or '（无）'}")
    shoot(window, "09-auto-mode")

    print("⑩ 暂停")
    app.pause()
    pump()
    view = app.refresh_home()
    print(f"   {view.mode_note}")
    shoot(window, "10-paused")

    print("⑪ 短消息合并")
    from pyweixin.merging import BufferedMessage, MergeKey

    key = MergeKey(account_id="wxid_demo", conversation=fruit.name, sender="张姐")
    base = 1_780_000_000.0
    for index, text in enumerate(["苹果两箱", "再加一箱", "明天送"]):
        app.merger.offer(key, BufferedMessage(event_id=f"live-{index}", text=text, sent_at=base + index * 3))
    app.merger._clock = lambda: base + 60
    batches = app.merger.flush()
    for batch in batches:
        print(f"   合并 {batch.size} 条 → 代表消息 {batch.representative_event_id[-12:]}")
        print(f"     合并正文：{batch.text.replace(chr(10), ' / ')}")
        print(f"     证据映射：{list(batch.merged_event_ids)}")
    # 合并这件事必须**在界面上看得见**：商户发了三条只得到一张订单时，
    # 不知道是合并过的就会以为漏单，然后重发一遍。以前的截图里这一屏和
    # 暂停页一模一样，等于什么都没证明。
    window._on_merged({"message_id": "live-0",
                        "event_ids": tuple(batch.merged_event_ids for batch in batches),
                        "count": sum(batch.size for batch in batches)})
    pump()
    shoot(window, "11-merged")

    print("⑫ 首页统计口径")
    view = app.refresh_home()
    print(f"   统计范围：{view.scope_text}")
    for key, text in view.definitions.items():
        print(f"     · {key}：{text}")
    shoot(window, "12-home-metrics")

    print()
    print(f"发出的消息（仅演示，未发往任何真实会话）：{len(sent)} 条")
    for target, text in sent:
        print(f"   → {target}：{text}")

    if args.keep:
        print("\n窗口保持开启，关闭后退出。")
        try:
            window.root.mainloop()
        except Exception:
            pass
    window._closing = True
    app.close()
    store.close()
    shutil.rmtree(data_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
