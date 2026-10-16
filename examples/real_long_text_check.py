"""真实微信数据里的长文本与特殊字符。

模拟数据永远测不出这一类问题：真实群里装着几百字的消息、@全体、
表情、链接、多行内容。这里把真读到的原文按"商户会看到的样子"打印出来，
人工判断截断和显示是否说得过去。

**只读，不发送，不删任何东西。**
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.application import MerchantApplication  # noqa: E402
from wechat_ai_assistant.conversations import RealConversationSource  # noqa: E402
from wechat_ai_assistant.merchant_app import _force_utf8_stdout  # noqa: E402
from wechat_ai_assistant.merchant_ui import CELL_LIMIT, _cell  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402
from wechat_ai_assistant.WeChatAuto import Messages  # noqa: E402
from wechat_ai_assistant.wechat_state import RealWeChatProbe  # noqa: E402

TARGETS = ("亲友团", "电子订货系统")
COUNT = 30


def main() -> int:
    _force_utf8_stdout()
    status = RealWeChatProbe().probe()
    print(f"① 微信状态：{status.state}")
    if status.state != "logged_in":
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pyw-long-"))
    store = OrderStore(str(tmp / "orders.db"))
    tenant = store.create_merchant("长文本验证")["merchant_id"]
    app = MerchantApplication(
        data_dir=tmp, orders=store, tenant=tenant, probe=None, launcher=None,
        history_reader_fn=Messages.dump_chat_history, simulated=True,
    )
    app.conversation_source = RealConversationSource(ui_lock=app.ui_lock)
    app.wizard.conversation_source = app.conversation_source

    found = app.wizard.refresh_conversations()
    picked = [c for c in found if c.name in TARGETS] or found[:1]
    print(f"② 读取会话：{[c.name for c in picked]}")
    app.wizard.choose_history_conversations(picked)
    result = app.wizard.read_history(range_kind="last_week", max_messages=COUNT)
    print(f"　{result.headline()}")

    rows = store.db.execute(
        "SELECT sender, content, kind, sent_at FROM messages WHERE merchant_id=? "
        "ORDER BY sent_at_epoch DESC LIMIT 200", (tenant,)
    ).fetchall()
    print(f"③ 取到 {len(rows)} 条原文\n")

    print(f"④ 按表格一格的宽度（上限 {CELL_LIMIT} 字）显示：\n")
    for row in rows[:12]:
        raw = str(row["content"] or "")
        shown = _cell(raw)
        flags = []
        if len(raw) > CELL_LIMIT:
            flags.append(f"超长 {len(raw)} 字")
        if "\n" in raw:
            flags.append("含换行")
        if "@" in raw:
            flags.append("含@")
        if any(ord(ch) > 0x2000 for ch in raw):
            flags.append("含特殊符号")
        print(f"  发送人：{row['sender']}")
        print(f"  原文  （{len(raw):>4} 字）：{raw[:160]}{'…' if len(raw) > 160 else ''}")
        print(f"  显示  （{len(shown):>4} 字）：{shown}")
        if flags:
            print(f"  标记：{'、'.join(flags)}")
        print()

    longest = max(rows, key=lambda r: len(str(r["content"] or "")), default=None)
    if longest is not None:
        raw = str(longest["content"] or "")
        print(f"⑤ 最长的一条：{len(raw)} 字")
        print(f"　显示为：{_cell(raw)}")
        print(f"　判断：{'会被截断，但带省略号，看得出还有内容' if len(raw) > CELL_LIMIT else '完整显示'}")

    app.close()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())