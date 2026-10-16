"""「读取历史偶尔返回 0 条」—— 商户被永久卡在 0 条。

## 现象

商户第一次读取历史，界面显示 0 条。之后无论来多少新消息、再点多少次
「读取」，**永远是 0 条**。

## 根因

游标 ``exhausted`` 的含义是「已经翻到底了」。一旦为真，下次读取会
**整个跳过这个会话**（``history_read.py:517``）。

问题是：一次**读到 0 条**的读取，也会被标成 ``exhausted``：

    exhausted = not truncated and not hit_ceiling and mode != UPDATE

读到 0 条时 ``truncated`` 和 ``hit_ceiling`` 都是 False，于是
``exhausted=True``、``message_count=0`` 被写进游标。

**「一条都没读到」和「翻到底了」是两回事。** 前者是"这次没拿到"，
后者是"确认没有了"。把前者记成后者，商户就再也读不出东西了 ——
而且没有任何办法恢复，除了清库。

## 为什么难被发现

第一次返回 0 条可能是偶发的（微信没聚焦、时间窗不匹配），
商户重试一次就好了，**看起来像是重试解决的**。但如果那次 0 条被记成了
exhausted，之后每一次重试都会被跳过 —— 于是"偶尔 0 条"其实是
"偶尔永久 0 条"，只是大多数情况下第一次没读到 0 条而已。

这个 bug 已经存在很久，之前一直**没有根因**，只记录了现象。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant.conversations import Conversation, ConversationKind  # noqa: E402
from wechat_ai_assistant.history_read import (  # noqa: E402
    DEFAULT_MAX_MESSAGES,
    HistoryCursor,
    HistoryReader,
    ReadMode,
    TimeWindow,
)

GROUP = Conversation(name="订货群", kind=ConversationKind.GROUP,
                      member_count=42, wxid="g1")


def _messages(count: int, start_minutes_ago: int = 30) -> list[dict]:
    """造 ``count`` 条最近的消息。"""
    now = datetime.now(timezone.utc)
    return [
        {
            "source_id": f"s{i}",
            "sent_at": (now - timedelta(minutes=start_minutes_ago - i)).isoformat(),
            "sender": "张姐",
            "content": f"要 {i} 箱",
            "kind": "text",
        }
        for i in range(count)
    ]


class SequenceReader:
    """按调用次序返回预设结果。第一次空、第二次有。"""

    def __init__(self, *rounds: list[dict]):
        self.rounds = list(rounds)
        self.calls = 0

    def __call__(self, **kwargs):
        result = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        report = kwargs.get("on_progress")
        if report is not None:
            for index in range(len(result)):
                report(index + 1)
        return [dict(m) for m in result]


class ZeroDoesNotStuckTests(unittest.TestCase):
    def setUp(self):
        self.cursor = HistoryCursor(":memory:")
        self.reader = HistoryReader(reader=None, cursor=self.cursor, sender_source="members")
        self.window = TimeWindow(start_delta=timedelta(days=1), label="recent")

    def tearDown(self):
        self.cursor.close()

    def _run(self, reader, mode=ReadMode.RECENT):
        self.reader._reader = reader
        return self.reader.read(
            [GROUP], self.window, merchant_id="m1",
            max_messages=DEFAULT_MAX_MESSAGES, resume=True, mode=mode,
        )

    def test_a_zero_read_is_not_recorded_as_exhausted(self):
        """读到 0 条 ≠ 翻到底了。"""
        self._run(SequenceReader([]))
        saved = self.cursor.get(GROUP.identity, "m1")
        self.assertFalse(saved["exhausted"],
                         "读到 0 条被记成了「翻到底」，商户就再也读不出来了")

    def test_the_second_read_still_tries_and_finds_the_new_messages(self):
        """核心复现：第一次空，第二次有 —— 商户必须读得到。"""
        reader = SequenceReader([], _messages(5))
        first = self._run(reader)
        self.assertEqual(first.total_messages, 0)

        second = self._run(reader)
        self.assertGreater(
            second.total_messages, 0,
            "第二次读取被游标跳过了，商户永远卡在 0 条 —— 这就是那个 bug",
        )

    def test_a_real_read_is_still_marked_exhausted(self):
        """真的读完了还是要记 exhausted，否则每次都要重翻一遍，白白拖慢微信。"""
        self._run(SequenceReader(_messages(5)))
        saved = self.cursor.get(GROUP.identity, "m1")
        self.assertTrue(saved["exhausted"], "确实读到内容且没被截断，应记为翻到底")

    def test_a_truncated_read_is_not_exhausted(self):
        """被截断说明还有更多，本来就不该记 exhausted。"""
        self.reader._reader = SequenceReader(_messages(3))
        self.reader.read(
            [GROUP], self.window, merchant_id="m1", max_messages=1,
            resume=True, mode=ReadMode.RECENT,
        )
        saved = self.cursor.get(GROUP.identity, "m1")
        self.assertFalse(saved["exhausted"])

    def test_update_mode_is_never_exhausted_by_definition(self):
        """增量读按定义就没读完，0 条也不例外。"""
        self._run(SequenceReader([], _messages(3)), mode=ReadMode.UPDATE)
        saved = self.cursor.get(GROUP.identity, "m1")
        self.assertFalse(saved["exhausted"])


if __name__ == "__main__":
    unittest.main()