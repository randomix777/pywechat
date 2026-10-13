"""头像发送人链路的回归测试：读历史时到底走成员名单还是逐条点头像。

背景：4.x 消息正文里没有发送人名，成员名单匹配全军覆没（全是占位串）。
``sender_source="avatar"`` 让群聊走 ``Messages.dump_senders_via_avatar``，
逐条点头像读资料卡。

本文件只测路由与适配（纯内存，不建微信窗口、不联网）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyweixin.conversations import Conversation, ConversationKind  # noqa: E402
from pyweixin.history_read import HistoryReader  # noqa: E402


def _group(name="测试群"):
    return Conversation(name=name, kind=ConversationKind.GROUP, member_count=10)


def _friend(name="张老板"):
    return Conversation(name=name, kind=ConversationKind.FRIEND)


def _reader(items):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return items

    fake.calls = calls
    return fake


AVATAR_RECORDS = [
    {"消息发送人": "苹果哥(张老板)", "昵称": "张老板", "备注": "苹果哥",
     "微信号": "wxid_1", "消息内容": "两箱苹果", "消息类型": "文本"},
]


class AvatarRoutingTests(unittest.TestCase):
    def test_default_is_members_upstream_design(self):
        reader = HistoryReader(_reader([]))
        self.assertEqual(reader._sender_source, "members")

    def test_avatar_mode_group_uses_avatar_not_members(self):
        dump = _reader([])
        members_calls = []
        reader = HistoryReader(
            dump, group_members=lambda conv: members_calls.append(conv) or ["张老板"],
            sender_source="avatar",
        )
        with patch("pyweixin.WeChatAuto.Messages.dump_senders_via_avatar",
                   return_value=list(AVATAR_RECORDS)) as avatar:
            got = reader._read_one(_group(), 50)
        self.assertEqual(avatar.call_count, 1)
        self.assertEqual(avatar.call_args.kwargs["friend"], "测试群")
        self.assertEqual(dump.calls, [], "成员名单那条路不该再跑")
        self.assertEqual(members_calls, [], "成员名单都不该去取")
        self.assertEqual(got[0]["消息发送人"], "苹果哥(张老板)")
        self.assertEqual(got[0]["昵称"], "张老板")
        self.assertEqual(got[0]["备注"], "苹果哥")
        self.assertEqual(got[0]["消息内容"], "两箱苹果")

    def test_avatar_mode_private_chat_stays_on_dump(self):
        dump = _reader([{"消息发送人": "张老板", "消息内容": "你好",
                         "消息类型": "文本", "消息发送时间": "2026-10-2 12:21"}])
        reader = HistoryReader(dump, sender_source="avatar")
        with patch("pyweixin.WeChatAuto.Messages.dump_senders_via_avatar") as avatar:
            got = reader._read_one(_friend(), 50)
        self.assertEqual(avatar.call_count, 0)
        self.assertEqual(len(dump.calls), 1)
        self.assertEqual(got[0]["消息发送人"], "张老板")

    def test_members_mode_unchanged(self):
        rows = [{"消息发送人": "张老板", "消息内容": "两箱",
                 "消息类型": "文本", "消息发送时间": "2026-10-2 12:21"}]
        dump = _reader(rows)
        reader = HistoryReader(
            dump, group_members=lambda conv: ["张老板"], sender_source="members",
        )
        with patch("pyweixin.WeChatAuto.Messages.dump_senders_via_avatar") as avatar:
            got = reader._read_one(_group(), 50)
        self.assertEqual(avatar.call_count, 0)
        self.assertEqual(len(dump.calls), 1)
        self.assertEqual(dump.calls[0]["groupMembers"], ["张老板"])
        self.assertEqual(got, rows)

    def test_unknown_sender_source_falls_back_to_members(self):
        dump = _reader([])
        reader = HistoryReader(dump, group_members=lambda conv: [], sender_source="whatever")
        self.assertEqual(reader._sender_source, "members")
        with patch("pyweixin.WeChatAuto.Messages.dump_senders_via_avatar") as avatar:
            reader._read_one(_group(), 5)
        self.assertEqual(avatar.call_count, 0)
        self.assertEqual(len(dump.calls), 1)


class AvatarCardCopyTests(unittest.TestCase):
    """右键复制兜底：卡片文本树为空时，从剪贴板拿。纯假对象，不碰界面。"""

    class FakeCopyItem:
        def __init__(self, exists=True):
            self._exists = exists
            self.clicked = False

        def exists(self, timeout=None):
            return self._exists

        def click_input(self):
            self.clicked = True

    class FakeProfile:
        def __init__(self, exists=True):
            self.copy_item = AvatarCardCopyTests.FakeCopyItem(exists)
            self.right_clicked = False

        def right_click_input(self):
            self.right_clicked = True

        def child_window(self, **kwargs):
            return self.copy_item

    def _clipboard(self, reads):
        import pyweixin.utils as utils_mod

        writes = []
        return (
            patch.object(utils_mod.SystemSettings, "get_copied_text", side_effect=list(reads)),
            patch.object(utils_mod.SystemSettings, "copy_text_to_clipboard",
                         side_effect=lambda text: writes.append(text)),
            writes,
        )

    def test_copy_success_returns_text_and_restores_clipboard(self):
        import pyweixin.utils as utils_mod

        profile = self.FakeProfile()
        get_patch, set_patch, writes = self._clipboard(["旧剪贴板", "张老板\n微信号：wxid_1"])
        with get_patch, set_patch:
            got = utils_mod._copy_avatar_card_text(profile)
        self.assertTrue(profile.right_clicked)
        self.assertTrue(profile.copy_item.clicked)
        self.assertIn("张老板", got)
        self.assertEqual(writes[-1], "旧剪贴板")

    def test_no_copy_menu_raises(self):
        import pyweixin.utils as utils_mod

        profile = self.FakeProfile(exists=False)
        get_patch, set_patch, writes = self._clipboard(["旧剪贴板"])
        with get_patch, set_patch:
            with self.assertRaises(ValueError):
                utils_mod._copy_avatar_card_text(profile)
        self.assertEqual(writes[-1], "旧剪贴板")

    def test_empty_clipboard_after_copy_raises(self):
        import pyweixin.utils as utils_mod

        profile = self.FakeProfile()
        get_patch, set_patch, writes = self._clipboard(["旧剪贴板", "   "])
        with get_patch, set_patch:
            with self.assertRaises(ValueError):
                utils_mod._copy_avatar_card_text(profile)
        self.assertEqual(writes[-1], "旧剪贴板")


if __name__ == "__main__":
    unittest.main()
