"""三种读法：按范围 / 读全部 / 更新。

这三种的差别全在**该不该停、停在哪、以及如实怎么说**，所以测试盯的是
``truncated`` / ``exhausted`` / ``gaps`` 这些"有没有把话说圆"的字段，
不是消息条数本身。

背景（决定了实现方式）：微信**不告诉调用方一共有多少条消息** —— 聊天记录窗口
标题里只有会话名。所以"读全部"只能实现成"一直往下翻，翻到没有新消息为止"，
而不是"先查总数再读"。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.conversations import Conversation, ConversationKind  # noqa: E402
from wechat_ai_assistant.history_read import (  # noqa: E402
    READ_ALL_CEILING,
    DEFAULT_MAX_MESSAGES,
    HistoryCursor,
    HistoryReader,
    HistoryResult,
    ReadMode,
    TimeWindow,
)

#: 消息锚点必须跟着**真实时钟**走。
#:
#: 这里原来写死 `datetime(2026, 10, 2, 12, 0)`，而读取窗口是 `now - 1 天` ——
#: 于是测试的成败取决于**它跑在哪一刻**：真实时钟越过 2026-10-02 13:11 之后，
#: 前面 120 条消息就落到一天窗口外面去了，测试开始失败。
#:
#: 表现是"我刚改了点别的东西，怎么这个测试红了" —— 差点把锅甩错地方。
#: **测试不能依赖运行时刻**：要么锚在 now，要么用与时间无关的窗口。
NOW = datetime.now(timezone.utc) - timedelta(minutes=60)
GROUP = Conversation(name="鲜果批发群", kind=ConversationKind.GROUP, member_count=424)


def message(offset_minutes: int, text: str) -> dict:
    """真实微信形状：日不补零、空格分隔、无时区。"""
    moment = NOW - timedelta(minutes=offset_minutes)
    return {
        "消息发送人": "张姐",
        "消息内容": text,
        "消息类型": "文本",
        "消息发送时间": moment.astimezone().strftime("%Y-%m-%d %H:%M"),
    }


class _Recorder:
    """替身读取器：返回给定的消息，并记录收到的参数。"""

    def __init__(self, messages):
        self.messages = list(messages)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        report = kwargs.get("on_progress")
        if report is not None:
            for index in range(len(self.messages)):
                report(index + 1)
        return [dict(m) for m in self.messages]

    @property
    def last(self) -> dict:
        return self.calls[-1]


class ReadModeTests(unittest.TestCase):
    def setUp(self):
        self.cursor = HistoryCursor(":memory:")
        self.reader = HistoryReader(reader=None, cursor=self.cursor, sender_source="members")
        self.window = TimeWindow(start_delta=timedelta(days=1), label="recent")

    def tearDown(self):
        self.cursor.close()

    def _run(self, reader, mode, limit=DEFAULT_MAX_MESSAGES, resume=True):
        self.reader._reader = reader
        return self.reader.read(
            [GROUP], self.window, merchant_id="m1", max_messages=limit,
            resume=resume, mode=mode,
        )

    # ------------------------------------------------------------ 读全部

    def test_all_keeps_reading_past_the_normal_limit(self):
        """读全部不受"最多读取条数"限制 —— 那是按范围读才用的。"""
        recorder = _Recorder([message(i, f"第{i}条") for i in range(DEFAULT_MAX_MESSAGES + 50)])
        result = self._run(recorder, ReadMode.ALL, limit=5)
        self.assertEqual(len(recorder.messages), DEFAULT_MAX_MESSAGES + 50)
        self.assertEqual(recorder.last["number"], READ_ALL_CEILING, "全部模式应按兜底上限读")
        self.assertEqual(result.total_messages, DEFAULT_MAX_MESSAGES + 50)
        self.assertFalse(result.cancelled)

    def test_all_does_not_claim_more_than_wechat_gave(self):
        """读不满预算 = 翻到底了，不该再说"可能还有更早的"。"""
        recorder = _Recorder([message(i, f"第{i}条") for i in range(7)])
        result = self._run(recorder, ReadMode.ALL)
        self.assertEqual(result.coverage[0].truncated, False)
        self.assertEqual(result.gaps, (), "翻到底了却还在报缺口")

    def test_all_reports_honestly_when_it_stops_at_the_ceiling(self):
        recorder = _Recorder([message(i, f"第{i}条") for i in range(READ_ALL_CEILING)])
        result = self._run(recorder, ReadMode.ALL)
        self.assertEqual(result.coverage[0].truncated, True, "撞上限必须说")
        self.assertTrue(any("更早" in gap for gap in result.gaps), result.gaps)

    def test_all_can_be_cancelled(self):
        recorder = _Recorder([message(i, f"第{i}条") for i in range(100)])
        self.reader.cancel()
        result = self._run(recorder, ReadMode.ALL)
        self.assertTrue(result.cancelled)

    # -------------------------------------------------------------- 更新

    def test_update_only_keeps_messages_newer_than_the_cursor(self):
        first = _Recorder([message(i, f"旧的{i}") for i in range(1, 6)])
        self._run(first, ReadMode.RECENT, limit=5)
        saved = self.cursor.get(GROUP.identity, "m1")
        self.assertIsNotNone(saved)

        # 第二次读：2 条比上次更新的（负偏移=更晚）+ 3 条早就读过的
        again = _Recorder(
            [message(-i, f"新{i}") for i in (0, 5)]
            + [message(600 + i, f"旧{i}") for i in range(3)]
        )
        result = self._run(again, ReadMode.UPDATE, limit=5)
        self.assertEqual(result.total_messages, 2, "更新只应留下比上次更新的消息")
        # 偏移 -5 比 -0 更晚，所以覆盖范围的最新一条应当是它。
        self.assertEqual(result.coverage[0].latest, _parse_message(message(-5, "")))

    def test_update_drops_the_ones_already_seen(self):
        self._run(_Recorder([message(i, f"第{i}条") for i in range(1, 6)]), ReadMode.RECENT, limit=5)
        again = _Recorder(
            [message(-i, f"新{i}") for i in (0, 5)] + [message(i, f"第{i}条") for i in range(1, 4)]
        )
        result = self._run(again, ReadMode.UPDATE, limit=5)
        self.assertEqual(result.total_messages, 2)

    def test_update_after_reading_everything_still_reads_again(self):
        """读全部标了 exhausted 之后，「更新」仍然要再读 —— 新消息正是它要抓的。"""
        everything = _Recorder([message(i, f"第{i}条") for i in range(9)])
        self._run(everything, ReadMode.ALL)
        saved = self.cursor.get(GROUP.identity, "m1")
        self.assertEqual(int(saved["exhausted"]), 1)

        again = _Recorder([message(-5, "刚来的")])
        result = self._run(again, ReadMode.UPDATE)
        self.assertEqual(len(again.calls), 1, "exhausted 不该让「更新」跳过读取")
        self.assertEqual(result.total_messages, 1)

    def test_update_without_a_previous_read_falls_back_to_the_window(self):
        """没读过就没有"上次"，这时不敢只取新的，按范围老实读。"""
        recorder = _Recorder([message(i, f"第{i}条") for i in range(4)])
        result = self._run(recorder, ReadMode.UPDATE)
        self.assertEqual(result.total_messages, 4)
        self.assertEqual(result.mode, ReadMode.UPDATE)

    def test_update_never_marks_the_conversation_exhausted(self):
        recorder = _Recorder([message(i, f"第{i}条") for i in range(3)])
        self._run(recorder, ReadMode.UPDATE)
        saved = self.cursor.get(GROUP.identity, "m1")
        self.assertEqual(int(saved["exhausted"]), 0, "按定义就没读完，不能说读完了")

    def test_all_reads_again_after_exhausted(self):
        """点"读取全部"就是要一次全量：上次翻到底也不许跳过。"""
        first = _Recorder([message(i, f"第{i}条") for i in range(5)])
        self._run(first, ReadMode.ALL)
        saved = self.cursor.get(GROUP.identity, "m1")
        self.assertEqual(int(saved["exhausted"]), 1)

        again = _Recorder([message(i, f"第{i}条") for i in range(5)])
        result = self._run(again, ReadMode.ALL)
        self.assertEqual(len(again.calls), 1, "全部模式不许跳过已读完的会话")
        self.assertFalse(result.coverage[0].skipped)

    def test_recent_still_skips_exhausted(self):
        """按范围读保持跳过：同一窗口短期重翻只是浪费时间。"""
        first = _Recorder([message(i, f"第{i}条") for i in range(5)])
        self._run(first, ReadMode.ALL)
        again = _Recorder([message(i, f"第{i}条") for i in range(5)])
        result = self._run(again, ReadMode.RECENT, limit=5)
        self.assertEqual(len(again.calls), 0, "按范围读应跳过已翻到底的会话")
        self.assertTrue(result.coverage[0].skipped)

    # -------------------------------------------------------------- 按范围

    def test_recent_reports_that_more_may_exist(self):
        recorder = _Recorder([message(i, f"第{i}条") for i in range(20)])
        result = self._run(recorder, ReadMode.RECENT, limit=20)
        self.assertEqual(result.coverage[0].truncated, True)
        self.assertTrue(any("读取全部历史" in gap for gap in result.gaps), result.gaps)

    def test_recent_passes_the_configured_limit_through(self):
        recorder = _Recorder([message(i, f"第{i}条") for i in range(3)])
        self._run(recorder, ReadMode.RECENT, limit=7)
        self.assertEqual(recorder.last["number"], 7)

    def test_update_reports_its_own_gap_not_the_earlier_one(self):
        """更新撞满预算，缺的是更新的那部分。文案说反方向会误导商户。"""
        recorder = _Recorder([message(i, f"第{i}条") for i in range(20)])
        result = self._run(recorder, ReadMode.UPDATE, limit=20)
        self.assertTrue(result.gaps)
        joined = " ".join(result.gaps)
        self.assertIn("更新历史消息", joined)
        self.assertNotIn("更早", joined, "更新模式不该催商户去读更早的")
        self.assertNotIn("读取全部历史", joined)

    def test_an_unknown_mode_is_rejected(self):
        recorder = _Recorder([])
        with self.assertRaises(ValueError):
            self._run(recorder, "nonsense")


class ProgressByMessageTests(unittest.TestCase):
    """进度必须按条数走。只按会话数走的话，读一个群几万条，进度条会卡在 0/1。"""

    def setUp(self):
        self.cursor = HistoryCursor(":memory:")
        self.seen: list = []

    def tearDown(self):
        self.cursor.close()

    def _reader(self, messages):
        recorder = _Recorder(messages)
        reader = HistoryReader(reader=recorder, cursor=self.cursor, sender_source="members")
        return reader, recorder

    def test_progress_reports_message_counts_while_reading(self):
        reader, _ = self._reader([message(i, f"第{i}条") for i in range(30)])
        reader.read([GROUP], TimeWindow(start_delta=timedelta(days=1)),
                    merchant_id="m1", max_messages=100,
                    on_progress=self.seen.append, mode=ReadMode.RECENT)
        counts = [p.messages_read for p in self.seen if p.messages_read]
        self.assertTrue(counts, "一条都没报，读全部时界面进度条不会动")
        self.assertEqual(max(counts), 30)

    def test_all_mode_says_the_total_is_unknown(self):
        reader, _ = self._reader([message(i, f"第{i}条") for i in range(5)])
        reader.read([GROUP], TimeWindow(start_delta=timedelta(days=1)),
                    merchant_id="m1", on_progress=self.seen.append, mode=ReadMode.ALL)
        states = [p for p in self.seen if p.messages_read]
        self.assertTrue(states)
        for state in states:
            self.assertFalse(state.bounded, "读全部时总条数未知，不能画会走完的进度条")
            self.assertEqual(state.message_target, 0)

    def test_recent_mode_has_a_known_total_so_the_bar_can_fill(self):
        reader, _ = self._reader([message(i, f"第{i}条") for i in range(5)])
        reader.read([GROUP], TimeWindow(start_delta=timedelta(days=1)),
                    merchant_id="m1", max_messages=10,
                    on_progress=self.seen.append, mode=ReadMode.RECENT)
        self.assertTrue(all(p.bounded for p in self.seen))
        self.assertTrue(all(p.message_target == 10 for p in self.seen))

    def test_summary_names_the_message_count(self):
        reader, _ = self._reader([message(i, f"第{i}条") for i in range(7)])
        reader.read([GROUP], TimeWindow(start_delta=timedelta(days=1)),
                    merchant_id="m1", max_messages=7,
                    on_progress=self.seen.append, mode=ReadMode.RECENT)
        text = " ".join(p.summary() for p in self.seen)
        self.assertIn("条", text)


class ResultWordingTests(unittest.TestCase):
    """措辞要区分三种读法，不能一律"实际读到 N 条"。"""

    def _result(self, mode, count=1):
        return HistoryResult(
            window=TimeWindow(start_delta=timedelta(days=1)),
            mode=mode,
            coverage=[_coverage(count, earliest=NOW - timedelta(days=2), latest=NOW)],
        )

    def test_update_says_it_updated(self):
        self.assertIn("更新了", self._result(ReadMode.UPDATE, 4).headline())

    def test_update_with_nothing_new_says_so_plainly(self):
        result = HistoryResult(window=TimeWindow(), mode=ReadMode.UPDATE, coverage=[])
        self.assertEqual(result.headline(), "没有发现上次之后的新消息")

    def test_update_zero_says_no_new_info_not_read_failure(self):
        """更新无新消息是"没有读到新信息"，不是"没有读到"。

        回归：更新点完看到"这次没有读到任何消息"，商户以为读失败了。
        """
        update = HistoryResult(window=TimeWindow(), mode=ReadMode.UPDATE, coverage=[])
        self.assertEqual(update.actual_range_text(), "没有读到新信息")
        for mode in (ReadMode.ALL, ReadMode.RECENT):
            with self.subTest(mode=mode):
                result = HistoryResult(window=TimeWindow(), mode=mode, coverage=[])
                self.assertEqual(result.actual_range_text(), "这次没有读到任何消息")

    def test_all_and_recent_report_the_actual_range(self):
        for mode in (ReadMode.ALL, ReadMode.RECENT):
            with self.subTest(mode=mode):
                self.assertIn("实际读到", self._result(mode).headline())

    def test_cancelled_is_said_out_loud(self):
        result = self._result(ReadMode.ALL)
        result.cancelled = True
        self.assertIn("已取消", result.headline())

    def test_unknown_mode_is_rejected_by_the_constructor_path(self):
        reader = HistoryReader(reader=None, cursor=HistoryCursor(":memory:"), sender_source="members")
        with self.assertRaises(ValueError):
            reader.read([GROUP], TimeWindow(), merchant_id="m1", mode="whatever")
        reader.cursor.close()


def _coverage(count, earliest=None, latest=None, truncated=False, error="", skipped=False):
    from wechat_ai_assistant.history_read import ConversationCoverage

    return ConversationCoverage(
        conversation=GROUP, requested_count=10, message_count=count,
        earliest=earliest, latest=latest, truncated=truncated, error=error, skipped=skipped,
    )


def _parse(value):
    from wechat_ai_assistant.wechat_time import parse_timestamp

    return parse_timestamp(value)


def _parse_message(raw):
    from wechat_ai_assistant.wechat_time import parse_timestamp

    return parse_timestamp(raw["消息发送时间"])


class UnresolvedSenderCountTests(unittest.TestCase):
    """未认出计数看的必须是"消息发送人"：各路读取器产出的都是这个键，
    "sender"是入库后的字段名。

    回归真机 case：第 4 步说"2 条发送人没认出来"，第 5 步却写着名字 ——
    计数查错了键，条条算没认出来。
    """

    def _read(self, rows):
        reader = HistoryReader(_Recorder(rows), cursor=HistoryCursor(":memory:"),
                               sender_source="members")
        try:
            return reader.read([GROUP], TimeWindow(), merchant_id="m1",
                               max_messages=10, mode=ReadMode.ALL)
        finally:
            reader.cursor.close()

    def test_resolved_names_are_not_counted(self):
        result = self._read([message(1, "苹果两箱"), message(2, "再加一箱")])
        self.assertEqual(result.coverage[0].unresolved_senders, 0)

    def test_placeholders_are_counted(self):
        from wechat_ai_assistant.utils import UNRESOLVED_SENDER

        bad = message(1, "红包")
        bad["消息发送人"] = UNRESOLVED_SENDER
        result = self._read([message(2, "苹果"), bad])
        self.assertEqual(result.coverage[0].unresolved_senders, 1)


if __name__ == "__main__":
    unittest.main()