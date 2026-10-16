"""大群的读取性能。

产品对外说过"能读群历史"。这句话在小群（6 人）上验证过，在**424 人的群**
上从没量过。商户真正用的是大群，所以必须知道：

* 读 50 条要多久
* 进度条是不是**真的在动**（而不是长时间停在 0%���商户会以为卡死了）
* 读全部历史能不能撑住

**只读，不发送，不删任何东西。**
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.application import MerchantApplication  # noqa: E402
from wechat_ai_assistant.conversations import RealConversationSource  # noqa: E402
from wechat_ai_assistant.merchant_app import _force_utf8_stdout  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402
from wechat_ai_assistant.WeChatAuto import Messages  # noqa: E402
from wechat_ai_assistant.wechat_state import RealWeChatProbe  # noqa: E402

TARGET = "TJ锦绣中华水果群"
LIMIT = 50


def main() -> int:
    _force_utf8_stdout()
    status = RealWeChatProbe().probe()
    print(f"① 微信状态：{status.state}")
    if status.state != "logged_in":
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pyw-perf-"))
    store = OrderStore(str(tmp / "orders.db"))
    tenant = store.create_merchant("大群性能验证")["merchant_id"]
    app = MerchantApplication(
        data_dir=tmp, orders=store, tenant=tenant, probe=None, launcher=None,
        history_reader_fn=Messages.dump_chat_history, simulated=True,
    )
    app.conversation_source = RealConversationSource(ui_lock=app.ui_lock)
    app.wizard.conversation_source = app.conversation_source

    found = app.wizard.refresh_conversations()
    target = next((c for c in found if TARGET in c.name), None)
    if target is None:
        print(f"② 没找到「{TARGET}」，现有：{[c.name for c in found][:8]}")
        app.close()
        store.close()
        return 1
    print(f"② 目标：{target.disambiguation}")
    app.wizard.choose_history_conversations([target])

    ticks: list[tuple[float, int]] = []
    started = time.time()

    def on_progress(progress) -> None:
        # 记录进度条真的动过几次。商户盯着进度条，停在 0% 比慢更让人慌。
        ticks.append((time.time() - started, int(getattr(progress, "messages_read", 0))))

    print(f"③ 开始读取（上限 {LIMIT} 条）—— 期间请不要操作微信")
    result = app.wizard.read_history(
        range_kind="last_week", max_messages=LIMIT, on_progress=on_progress
    )
    elapsed = time.time() - started
    print(f"　{result.headline()}")
    print(f"　耗时 {elapsed:.1f} 秒")

    # 进度条走到 6，结果却是 0 条 —— 那几条去哪了必须说清楚。
    print("⑥ 逐会话明细：")
    for item in result.coverage:
        print(f"　  会话：{item.conversation.name}")
        print(f"　  读到 {item.message_count} 条，入库 {getattr(item, 'imported', '?')} 条")
        print(f"　  失败：{item.error or '无'}　跳过：{'是' if item.skipped else '否'}")
        print(f"　  可能没读全：{'是' if item.truncated else '否'}")
        print(f"　  时间范围：{item.earliest} ~ {item.latest}")
    print(f"　整体缺口：{list(result.gaps) or '无'}")

    distinct = []
    for _t, count in ticks:
        if not distinct or distinct[-1] != count:
            distinct.append(count)
    print(f"④ 进度回调 {len(ticks)} 次，条数变化 {len(distinct)} 段：{distinct[:12]}")
    print(f"　前 5 秒读了多少条：{[c for t, c in ticks if t <= 5][:10]}")

    total = store.db.execute(
        "SELECT COUNT(*) c FROM messages WHERE merchant_id=?", (tenant,)
    ).fetchone()["c"]
    per_second = total / elapsed if elapsed else 0
    print(f"⑤ 入库 {total} 条，平均 {per_second:.1f} 条/秒")
    if ticks:
        first_move = next((t for t, c in ticks if c > 0), None)
        if first_move is None:
            print("　★ 进度条从头到尾没有动过 —— 商户会以为卡死")
        else:
            print(f"　首次显示进度：{first_move:.1f} 秒")

    app.close()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())