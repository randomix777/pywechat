"""真实微信：先读一轮，再验「更新历史」只拿新增的。

用小群，尽量少占用微信窗口。两次读之间不引入新消息，第二次应当明确回答
"没有发现上次之后的新消息" —— 这正是「更新」该有的行为。
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyweixin.conversations import Conversation, ConversationKind  # noqa: E402
from pyweixin.history_read import HistoryCursor, HistoryReader, ReadMode, TimeWindow  # noqa: E402
from pyweixin.merchant_app import _force_utf8_stdout  # noqa: E402
from pyweixin.WeChatAuto import Messages  # noqa: E402
from pyweixin.wechat_state import RealWeChatProbe  # noqa: E402

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

    conversation = Conversation(name=TARGET, kind=ConversationKind.GROUP, member_count=MEMBERS)
    window = TimeWindow(start_delta=timedelta(days=3650), label="wide")
    cursor = HistoryCursor(":memory:")
    reader = HistoryReader(reader=Messages.dump_chat_history, cursor=cursor)

    def report(progress):
        if progress.messages_read:
            print(f"   进度：{progress.summary()}　(总条数已知={progress.bounded})")

    print(f"② 第一轮：按范围读 {COUNT} 条 → {TARGET}（{MEMBERS}人）")
    first = reader.read([conversation], window, merchant_id="real-update",
                        max_messages=COUNT, on_progress=report, mode=ReadMode.RECENT)
    print(f"　{first.headline()}")
    print(f"　缺口：{list(first.gaps) or '无'}")
    row = cursor.get(conversation.identity, "real-update")
    print(f"　游标：已见 {row['message_count']} 条，最新 {row['latest']}，exhausted={row['exhausted']}")

    print("③ 第二轮：选「更新历史」，期间没有新消息")
    second = reader.read([conversation], window, merchant_id="real-update",
                         max_messages=COUNT, on_progress=report, mode=ReadMode.UPDATE)
    print(f"　{second.headline()}")
    print(f"　缺口：{list(second.gaps) or '无'}")
    row = cursor.get(conversation.identity, "real-update")
    print(f"　游标：已见 {row['message_count']} 条，最新 {row['latest']}，exhausted={row['exhausted']}")

    print("④ 第三轮：选「更新历史」，这次只拉一条（模拟预算很小）")
    third = reader.read([conversation], window, merchant_id="real-update",
                        max_messages=1, on_progress=report, mode=ReadMode.UPDATE)
    print(f"　{third.headline()}")
    print(f"　缺口：{list(third.gaps) or '无'}")

    cursor.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())