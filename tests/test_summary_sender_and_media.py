"""摘要必须说清「谁说的」，并且不能假装看得见图片。

## 出过的事（三个，都是静默丢数据）

### 一、``sqlite3.Row`` 被当成对象，全部字段丢光

``_as_message`` 原本只认 ``Mapping`` 和属性访问。而 ``sqlite3.Row``
**不是 Mapping**（它没注册那个 ABC），也不支持属性访问 —— 两条路都落空，
返回**全空字典**。

模型收到的就是「发送人：　内容：　时间：」×5，回一句
「这批消息全是空的」，而商户手里明明有五条。

### 二、不幂等：转两次就清空

调用方先自己把消息转成中文键，``summarize`` 内部再转一次 ——
第二次找不到 ``sender``/``content``（已经变成「发送人」「内容」），
于是再次**静默清空**。

### 三、``kind`` 被丢掉，图片语音当正文

图片消息的 ``content`` 是占位符「图片」，**语音消息的 content 是空的**。
不带 kind，模型看到「王姨：（空）」完全不知道发生了什么。

## 现在的行为

每条都带发送人；非文字消息写「发了一张图片」并单独列出来让店主自己看。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.summary import KIND_LABELS, SUMMARY_SYSTEM_PROMPT, _as_message  # noqa: E402


def _row(**columns):
    """造一个真的 ``sqlite3.Row`` —— 就是生产路径上那种。

    连接必须**活着**：sqlite3.Row 持有对连接的引用，连接被回收之后这个 Row
    就用不了了（``Cannot operate on a closed database``）。所以连接挂在模块上，
    不能是函数里的局部变量。
    """
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    connection = sqlite3.connect(":memory:")
    # **必须设 row_factory**，否则 sqlite3 返回的是普通 tuple ——
    # 那样 _as_message 认不出来（tuple 既不是映射也没有属性）。
    # 生产路径的 OrderStore 设了它，测试也得设，否则测的不是同一条路。
    connection.row_factory = sqlite3.Row
    connection.execute(f"CREATE TABLE t({names})")
    connection.execute(f"INSERT INTO t VALUES({marks})", tuple(columns.values()))
    row = connection.execute("SELECT * FROM t").fetchone()
    _CONNECTIONS.append(connection)      # 别让它被回收
    return row


#: 活着的连接。测试进程结束时自然释放。
_CONNECTIONS: list[sqlite3.Connection] = []


class SqliteRowTests(unittest.TestCase):
    """``sqlite3.Row`` 必须能读出字段。"""

    def test_a_row_is_not_a_mapping(self):
        """事实核查：它就是不走 Mapping 那条路，所以才需要单独处理。"""
        from collections.abc import Mapping
        self.assertNotIsInstance(_row(sender="张姐"), Mapping)

    def test_sender_and_content_survive(self):
        message = _as_message(_row(sender="张姐", content="要两箱苹果",
                                   sent_at="2026-10-14T09:00:00", kind="text"))
        self.assertEqual(message["发送人"], "张姐")
        self.assertEqual(message["内容"], "要两箱苹果")
        self.assertEqual(message["时间"], "2026-10-14T09:00:00")

    def test_an_empty_row_does_not_become_a_silent_blank(self):
        """空值可以，但**字段本身要在** —— 否则界面读不出是缺数据还是没人。"""
        message = _as_message(_row(sender="", content="", sent_at="", kind="text"))
        self.assertIn("发送人", message)
        self.assertIn("内容", message)


class IdempotenceTests(unittest.TestCase):
    """转两次必须和转一次一样。"""

    def test_converting_twice_keeps_the_content(self):
        once = _as_message(_row(sender="张姐", content="要两箱苹果",
                                sent_at="2026-10-14T09:00:00", kind="text"))
        twice = _as_message(once)
        self.assertEqual(twice, once,
                         "\u518d\u8f6c\u4e00\u6b21\u4e0d\u80fd\u628a\u5185\u5bb9\u6e05\u7a7a")

    def test_the_non_text_marker_survives_a_second_pass(self):
        once = _as_message(_row(sender="李叔", content="图片",
                                sent_at="2026-10-14T09:12:00", kind="image"))
        twice = _as_message(once)
        self.assertEqual(twice.get("这条不是文字消息"), "图片")


class NonTextMessageTests(unittest.TestCase):
    """图片、语音、文件必须被标出来。"""

    def test_an_image_is_marked(self):
        message = _as_message(_row(sender="李叔", content="图片",
                                   sent_at="2026-10-14T09:12:00", kind="image"))
        self.assertEqual(message["这条不是文字消息"], "图片")

    def test_a_voice_message_with_no_content_is_still_marked(self):
        """语音的 content 是空的 —— 不标出来模型只会看到一条空消息。"""
        message = _as_message(_row(sender="王姨", content="",
                                   sent_at="2026-10-14T09:14:00", kind="voice"))
        self.assertEqual(message["这条不是文字消息"], "语音")
        self.assertEqual(message["发送人"], "王姨")

    def test_plain_text_is_not_marked(self):
        message = _as_message(_row(sender="张姐", content="要两箱苹果",
                                   sent_at="2026-10-14T09:00:00", kind="text"))
        self.assertNotIn("这条不是文字消息", message)

    def test_every_kind_has_a_label(self):
        for kind in ("image", "voice", "video", "file", "link", "quote"):
            self.assertIn(kind, KIND_LABELS, f"{kind} \u6ca1\u6709\u4e2d\u6587\u8bf4\u6cd5")


class PromptTests(unittest.TestCase):
    """提示词必须把这两件事说清楚。"""

    def test_it_requires_the_sender_on_every_message(self):
        self.assertIn("谁说的", SUMMARY_SYSTEM_PROMPT)
        self.assertIn("每段开头必须是发送人的称呼", SUMMARY_SYSTEM_PROMPT)

    def test_it_forbids_guessing_what_is_in_an_image(self):
        self.assertIn("不要猜测图片", SUMMARY_SYSTEM_PROMPT)

    def test_it_asks_for_a_separate_section_for_non_text(self):
        self.assertIn("需要你自己看一眼的", SUMMARY_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()