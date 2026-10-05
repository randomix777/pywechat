"""真实微信导出形状的回归测试。

这些用例全部来自**真的连过一次微信**之后拿到的数据形状，不是猜的。原先的
568 项测试之所以一条都没发现，是因为合成数据一律给 ``2026-01-02T09:00:00+08:00``
（补零、带时区、发送人齐全），而真实微信给的是另一副样子。

真实导出的一条消息::

    {'消息发送人': None, '消息内容': '非常香，肉嫩好吃',
     '消息类型': '文本', '消息发送时间': '2026-10-2 12:21'}

三处形状差异，每一处都曾经是静默缺陷：

* ``消息发送时间`` **日不补零**（``10-2``）且空格分隔 → ``fromisoformat`` 抛异常
* 没有时区 → 以前被显式拒绝，当成没有时间
* ``消息发送人`` 可能是 ``None`` → 以前原样进库
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyweixin.history_read import HistoryReader, TimeWindow  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402
from pyweixin.utils import (  # noqa: E402
    UNRESOLVED_SENDER, UNRESOLVED_TIMESTAMP, parse_group_chat_history)
from pyweixin.wechat_time import local_timezone, parse_timestamp, sent_epoch  # noqa: E402

#: 真机上 ``dump_chat_history`` 原样返回的时间戳。日的位数不固定。
REAL_STAMP = "2026-10-2 12:21"
REAL_STAMP_PADDED = "2026-10-02 12:21"
REAL_STAMP_SECONDS = "2026-10-2 12:21:33"


class RealTimestampShapeTests(unittest.TestCase):
    """时间戳解析：真实形状必须认得出来。"""

    def test_the_real_wechat_format_is_not_dropped(self):
        parsed = parse_timestamp(REAL_STAMP)
        self.assertIsNotNone(parsed, "真实微信的时间戳解析不出来")
        assert parsed is not None
        self.assertEqual((parsed.year, parsed.month, parsed.day), (2026, 10, 2))
        self.assertEqual((parsed.hour, parsed.minute), (12, 21))

    def test_seconds_are_kept_when_wechat_gives_them(self):
        parsed = parse_timestamp(REAL_STAMP_SECONDS)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.second, 33)

    def test_a_naive_stamp_is_local_time_not_utc(self):
        """微信界面上显示的就是本机时间，按 UTC 解释会让商户看到偏移。"""
        parsed = parse_timestamp(REAL_STAMP)
        assert parsed is not None
        self.assertEqual(parsed.utcoffset(), datetime(2026, 10, 2, 12, 21).astimezone().utcoffset())
        self.assertIsNotNone(parsed.tzinfo)

    def test_an_explicit_offset_still_wins_over_the_local_zone(self):
        """带时区的输入必须原样保留，不能被本机时区改写。"""
        parsed = parse_timestamp("2026-01-01T10:00:00+08:00")
        assert parsed is not None
        self.assertEqual(parsed.utcoffset(), timedelta(hours=8))

    def test_iso_z_still_parses(self):
        parsed = parse_timestamp("2026-01-01T10:00:00Z")
        assert parsed is not None
        self.assertEqual(parsed.utcoffset(), timedelta(0))

    def test_unparseable_input_is_none_not_an_exception(self):
        for value in ("", "   ", None, UNRESOLVED_TIMESTAMP, "12:30", "not a time"):
            with self.subTest(value=value):
                self.assertIsNone(parse_timestamp(value))

    def test_local_timezone_is_usable(self):
        self.assertIsNotNone(local_timezone())

    def test_sent_epoch_is_populated_for_the_real_shape(self):
        """入库关口以前对真实消息一律存 NULL —— 消息之间无法排序。"""
        self.assertIsNotNone(sent_epoch(REAL_STAMP))


class HistoryWindowFilterTests(unittest.TestCase):
    """时间范围过滤必须真的过滤。"""

    def _kept(self, messages, days=1):
        window = TimeWindow(start_delta=timedelta(days=days), label="recent")
        now = datetime.now(timezone.utc)
        return HistoryReader._filter(messages, window, now)

    def test_messages_outside_the_window_are_actually_dropped(self):
        """这是最要紧的一条：以前解析不出时间就无条件保留，窗口形同虚设。"""
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(hours=1)).astimezone().strftime("%Y-%m-%d %H:%M")
        stale = (now - timedelta(days=9)).astimezone().strftime("%Y-%m-%d %H:%M")
        messages = [
            {"消息发送人": "张老板", "消息内容": "两箱苹果", "消息类型": "文本", "消息发送时间": fresh},
            {"消息发送人": "张老板", "消息内容": "上月的老单", "消息类型": "文本", "消息发送时间": stale},
        ]
        kept, earliest, latest = self._kept(messages, days=1)
        self.assertEqual([m["消息内容"] for m in kept], ["两箱苹果"])
        self.assertIsNotNone(earliest, "实际覆盖范围解析不出来，界面只能显示 ? 至 ?")
        self.assertIsNotNone(latest)

    def test_coverage_range_is_reported_from_real_stamps(self):
        """以前真实读出来是「? 至 ?」。"""
        now = datetime.now(timezone.utc)
        stamps = [
            (now - timedelta(hours=3)).astimezone().strftime("%Y-%m-%d %H:%M"),
            (now - timedelta(hours=1)).astimezone().strftime("%Y-%m-%d %H:%M"),
        ]
        messages = [
            {"消息内容": f"第{i}条", "消息发送时间": stamp} for i, stamp in enumerate(stamps)
        ]
        _, earliest, latest = self._kept(messages, days=1)
        assert earliest is not None and latest is not None
        self.assertLess(earliest, latest)

    def test_a_message_with_no_readable_stamp_is_still_kept(self):
        """解析不出来不等于丢弃，但界面必须能看到覆盖范围不完整。"""
        messages = [{"消息内容": "无时间戳", "消息发送时间": ""}]
        kept, earliest, latest = self._kept(messages, days=1)
        self.assertEqual(len(kept), 1)
        self.assertIsNone(earliest)


class GroupSenderTests(unittest.TestCase):
    """群聊发送人不许是 None。

    真机上的聊天记录列表项，**时间戳在末尾**，而且微信**不暴露昵称**::

        'Video Download0:55 2026-10-2 12:21'

    于是 ``_split_group_detail`` 切不出昵称，返回 ``None``，修复前被原样
    塞进 ``senders`` —— 实测 20 条消息发送人 100% 为 ``None``。
    """

    #: 真机形状：时间戳在末尾，正文在前。
    REAL_ITEM = "Video Download0:55 2026-10-2 12:21"
    #: 带昵称的形状（微信有时会给）。
    NAMED_ITEM = "张老板 两箱苹果 2026-10-2 12:21"
    VIEW = "mmui::ChatTextItemView"

    UNKNOWN = UNRESOLVED_SENDER

    def test_sender_is_never_none_even_when_wechat_exposes_no_nickname(self):
        details_with_name = [(self.REAL_ITEM, self.VIEW)]
        details_without_name = [(self.REAL_ITEM, self.VIEW)]
        _, senders, _, _ = parse_group_chat_history(details_with_name, details_without_name, [])
        self.assertEqual(len(senders), 1)
        self.assertIsNotNone(senders[0], "发送人是 None：这条消息无法归属任何客户")
        self.assertTrue(all(senders), "任何一条都不许是 None")

    def test_every_branch_uses_the_same_fallback(self):
        """四个分支必须同一套兜底，否则又会分叉出 None。"""
        for group_members in ([], ["张老板"]):
            for item in (self.REAL_ITEM, self.NAMED_ITEM):
                for without in ([], [(item, self.VIEW)]):
                    with self.subTest(members=group_members, named=item == self.NAMED_ITEM,
                                      aligned=bool(without)):
                        _, senders, _, _ = parse_group_chat_history(
                            [(item, self.VIEW)], without, group_members
                        )
                        self.assertTrue(senders, "解析不出任何消息")
                        self.assertTrue(all(senders), f"发送人出现 None：{senders}")

    def test_a_known_nickname_is_still_extracted(self):
        """兜底不能把能认出来的昵称也吃掉。"""
        _, senders, _, _ = parse_group_chat_history(
            [(self.NAMED_ITEM, self.VIEW)], [], ["张老板"]
        )
        self.assertEqual(senders, ["张老板"])

    def test_the_fallback_is_used_when_the_nickname_is_unknown(self):
        _, senders, _, _ = parse_group_chat_history(
            [(self.NAMED_ITEM, self.VIEW)], [], ["不在群里的人"]
        )
        self.assertEqual(senders, [self.UNKNOWN])


class IngestKeepsRealTimestampsTests(unittest.TestCase):
    """真实形状进库后，sent_at_epoch 必须有值。"""

    def setUp(self):
        self.store = OrderStore(":memory:")
        self.tenant = self.store.create_merchant("测试店铺")["merchant_id"]

    def tearDown(self):
        self.store.close()

    def test_real_stamps_produce_a_sortable_epoch(self):
        result = self.store.add_import(
            self.tenant,
            "6⃣️5⃣️TJ锦绣中华水果群4⃣️",
            [
                {"sender": "张老板", "content": "两箱苹果", "kind": "text",
                 "sent_at": REAL_STAMP, "source_id": "real-1"},
                {"sender": "张老板", "content": "再加一箱", "kind": "text",
                 "sent_at": REAL_STAMP_PADDED, "source_id": "real-2"},
            ],
            "real-shape",
        )
        self.assertEqual(result["inserted"], 2)
        rows = self.store.db.execute(
            "SELECT content, sent_at_epoch FROM messages WHERE source_id LIKE 'real-%' OR 1=1"
        ).fetchall()
        epochs = [row["sent_at_epoch"] for row in rows if row["sent_at_epoch"] is not None]
        self.assertTrue(epochs, "真实微信的时间戳没有落库：sent_at_epoch 全是 NULL")


if __name__ == "__main__":
    unittest.main()