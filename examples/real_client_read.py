"""真实微信 + 真实客户端装配：读历史能读到、也能真的存进本地库。

这轮刻意**按 merchant_app 的方式装配**（不手工挂 pipeline），因为真实缺陷正是
漏在那里的：向导拿不到本地订单库，商户第一次点「开始读取」必然失败。

用小群、少占用微信窗口。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyweixin.application import MerchantApplication  # noqa: E402
from pyweixin.conversations import RealConversationSource  # noqa: E402
from pyweixin.history_read import HistoryReader, HistoryCursor, ReadMode, TimeWindow  # noqa: E402
from pyweixin.merchant_app import _force_utf8_stdout  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402
from pyweixin.wechat_state import RealWeChatProbe  # noqa: E402
from pyweixin.WeChatAuto import Messages  # noqa: E402

TARGET = "电子订货系统"
MEMBERS = 6
COUNT = 20


def main() -> int:
    _force_utf8_stdout()
    status = RealWeChatProbe().probe()
    print(f"① 微信状态：{status.state}")
    if status.state != "logged_in":
        print(f"　详情：{status.to_dict()}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pyw-realwiring-"))
    store = OrderStore(str(tmp / "orders.db"))
    tenant = store.create_merchant("真机接线验证")["merchant_id"]

    # 按真实客户端装配：不手工挂 pipeline。
    app = MerchantApplication(
        data_dir=tmp, orders=store, tenant=tenant,
        probe=None, launcher=None,
        history_reader_fn=Messages.dump_chat_history,
        simulated=True,
    )
    # 会话发现用真实实现，和客户端一致。
    app.conversation_source = RealConversationSource(ui_lock=app.ui_lock)
    app.wizard.conversation_source = app.conversation_source
    print(f"② 向导拿到本地订单库了吗：{app.wizard.orders() is store}")
    print(f"　云端 pipeline：{app.wizard.pipeline}   ← 没登录，这是正常的")

    found = app.wizard.refresh_conversations()
    picked = [c for c in found if c.name == TARGET]
    if not picked:
        print(f"　没找到会话「{TARGET}」，现有：{[c.name for c in found][:10]}")
        app.close()
        store.close()
        return 1
    app.wizard.choose_history_conversations(picked)
    print(f"③ 已选：{[c.disambiguation for c in picked]}")

    print("④ 开始读取（真实微信窗口，会短暂占用前台）")
    result = app.wizard.read_history(range_kind="last_week", max_messages=COUNT)
    print(f"　{result.headline()}")
    print(f"　缺口：{list(result.gaps) or '无'}")

    total = store.db.execute(
        "SELECT COUNT(*) c FROM messages WHERE merchant_id=?", (tenant,)
    ).fetchone()["c"]
    print(f"⑤ 本地订单库里的消息数：{total}   ← 修复前这里必然是 0（读历史直接抛错）")
    rows = store.db.execute(
        "SELECT sender, kind, sent_at, substr(content,1,24) c FROM messages "
        "WHERE merchant_id=? ORDER BY sent_at_epoch LIMIT 5", (tenant,)
    ).fetchall()
    for row in rows:
        print(f"　　[{row['sent_at']}] {row['sender']}：{row['c']}　({row['kind']})")

    app.close()
    store.close()
    if total == 0:
        print("\n结论：仍然失败 —— 消息一条都没存进来")
        return 1
    print("\n结论：读取并入库都成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())