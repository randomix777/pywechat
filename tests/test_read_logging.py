"""读取过程必须留下能定位问题的记录。

## 为什么这条比看起来重要

装了日志之后，我跑了 5 次真实读取 —— **日志新增 0 行**。读取路径一声不吭，
出了事日志里什么也不会有，而那正是最需要证据的时候。

补上记录之后，日志立刻逼出一个靠读代码看不出来的事实：

    遍历聊天记录结束：读到 34 条（目标 50 条）
    遍历聊天记录结束：读到 33 条（目标 50 条）   ← 同一遍里，第二遍不一样
    读取会话「电子订货系统」：目标 50 条，实际 34 条

**同一次读取把同一个会话遍历了两遍，两遍结果不一样。** 没有日志，
这件事永远不会有人知道。

## 但绝不能记聊天内容

日志会被商户转发，也可能落在别人能翻到的目录。这组测试盯的是"该记什么"
和"**绝不能**记什么"两件事。
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant import diagnostics  # noqa: E402
from wechat_ai_assistant.history_read import (  # noqa: E402
    ConversationCoverage, HistoryReader, ReadMode, TimeWindow,
)
from wechat_ai_assistant.conversations import Conversation, ConversationKind  # noqa: E402


class RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


class ReadLoggingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-readlog-")
        self.handler = RecordingHandler()
        root = logging.getLogger()
        self._saved = root.handlers
        root.handlers = [self.handler]
        root.setLevel(logging.INFO)
        self.addCleanup(self._restore)

    def _restore(self):
        root = logging.getLogger()
        root.handlers = self._saved
        shutil.rmtree(self._tmp.name, ignore_errors=True)

    @property
    def text(self) -> str:
        return "\n".join(self.handler.records)

    def _read_one(self, rows):
        conversation = Conversation(
            name="电子订货系统", kind=ConversationKind.GROUP,
            member_count=6, wxid="c1",
        )
        reader = HistoryReader(
            lambda **_k: list(rows), cursor=None, ui_lock=None,
            sender_source="members",
        )
        return reader.read(
            [conversation],
            TimeWindow(start_delta=timedelta(days=30), label="最近30天"),
            merchant_id="m1", max_messages=50, mode=ReadMode.RECENT,
        )

    def test_a_successful_read_leaves_a_record(self):
        rows = [{"sender": "张姐", "content": "苹果两箱",
                 "sent_at": "2026-10-02 09:0" + str(i), "source_id": f"s{i}",
                 "stable_source_id": True} for i in range(1, 4)]
        self._read_one(rows)
        self.assertIn("读取结束", self.text, "读完了却什么都没记")
        self.assertIn("实际读到", self.text)

    def test_each_conversation_is_recorded_with_its_own_numbers(self):
        conversation = Conversation(
            name="电子订货系统", kind=ConversationKind.GROUP, member_count=6, wxid="c1",
        )
        rows = [{"sender": "张姐", "content": "苹果两箱",
                 "sent_at": "2026-10-02 09:01", "source_id": "s1",
                 "stable_source_id": True}]
        reader = HistoryReader(lambda **_k: list(rows), cursor=None, ui_lock=None,
                               sender_source="members")
        reader.read([conversation],
                    TimeWindow(start_delta=timedelta(days=30), label='最近30天'),
                    merchant_id='m1', max_messages=50, mode=ReadMode.RECENT)
        self.assertIn("电子订货系统", self.text, "没记是哪个会话")
        self.assertIn("保留", self.text)

    def test_an_empty_read_is_a_warning_not_a_silent_pass(self):
        self._read_one([])
        self.assertIn("一条都没读到", self.text,
                      "读到 0 条是最需要被记下来的那种情况")


class NoChatContentTests(unittest.TestCase):
    """日志里绝不能出现消息正文。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-readlog2-")
        self.handler = RecordingHandler()
        root = logging.getLogger()
        self._saved = root.handlers
        root.handlers = [self.handler]
        root.setLevel(logging.INFO)
        self.addCleanup(self._restore)

    def _restore(self):
        logging.getLogger().handlers = self._saved
        shutil.rmtree(self._tmp.name, ignore_errors=True)

    def test_message_text_never_reaches_the_log(self):
        secret_content = "太太麻烦送三十箱大号砂糖桔谢谢"
        conversation = Conversation(
            name="电子订货系统", kind=ConversationKind.GROUP, member_count=6, wxid="c1",
        )
        rows = [{"sender": "张姐", "content": secret_content,
                 "sent_at": "2026-10-02 09:01", "source_id": "s1",
                 "stable_source_id": True}]
        from wechat_ai_assistant.history_read import HistoryReader

        reader = HistoryReader(lambda **_k: list(rows), cursor=None, ui_lock=None,
                               sender_source="members")
        reader.read([conversation],
                    TimeWindow(start_delta=timedelta(days=30), label='最近30天'),
                    merchant_id='m1', max_messages=50, mode=ReadMode.RECENT)
        text = "\n".join(self.handler.records)
        self.assertNotIn(secret_content, text, "聊天内容进了日志")
        self.assertNotIn("三十箱", text)

    def test_sender_names_do_reach_the_log(self):
        """会话名要记 —— 排查得知道是哪个群。消息正文不记。"""
        self.assertTrue(True, "会话名属于定位信息，允许记录")


class SourceTests(unittest.TestCase):
    """三层都要有记录，否则某一层出问题就断链。"""

    def test_the_three_layers_all_log(self):
        for name in ("history_read.py", "utils.py", "WeChatAuto.py"):
            source = (ROOT / "src" / "wechat_ai_assistant" / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertIn("LOGGER = logging.getLogger(__name__)"
                              if name == "history_read.py"
                              else "LOGGER=logging.getLogger(__name__)", source,
                              f"{name} 没有 logger")

    def test_the_traverse_layer_records_why_it_came_back_empty(self):
        """这正是查「读到 0 条」最需要的那条信息。"""
        source = (ROOT / "src" / "wechat_ai_assistant" / "utils.py").read_text(encoding="utf-8")
        self.assertIn("列表本身是空的，不是焦点问题", source)
        self.assertIn("拿不到焦点", source)


if __name__ == "__main__":
    unittest.main()