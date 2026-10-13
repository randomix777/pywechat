"""第 2 步群名修复的回归测试：不碰真实微信，全走合成字节与模拟对象。

覆盖：
* resolve_group_wxid 已是 wxid 时直接认，不扫内存；
* 同名出现在多个群的消息里时按票数选，并列分不清返回 None（不串群）；
* 显示优先级：备注 > 内存真名 > 原始编号；
* 界面排序：真名在前，编号沉底。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from pyweixin import localdb  # noqa: E402
from pyweixin.conversations import (  # noqa: E402
    Conversation,
    ConversationKind,
)


def _contact_chunk(wxid: str, name: str) -> bytes:
    """合成一条"资料 record"：wxid + 中文名，头格式与线上一致。"""
    wb, nb = wxid.encode("ascii"), name.encode("utf-8")
    header = bytes([3, 13 + 2 * len(wb), 13 + 2 * len(nb)])
    assert header[0] == len(header) == 3
    return b"\x00" * 64 + header + wb + nb + b"\x00" * 64


def _patched_chunks(*chunks):
    return (
        unittest.mock.patch.object(localdb, "_weixin_pids", return_value=[123]),
        unittest.mock.patch.object(localdb, "_open_process", return_value=object()),
        unittest.mock.patch.object(
            localdb, "_iter_regions", return_value=[(0, blob) for blob in chunks]),
    )


class ResolveGroupWxidTests(unittest.TestCase):
    def setUp(self):
        localdb.clear_name_caches()

    def tearDown(self):
        localdb.clear_name_caches()

    def test_wxid_input_is_accepted_without_scanning(self):
        """列表里选出来的 wxid 行不用再扫一遍内存证明它存在。"""
        with unittest.mock.patch.object(
                localdb, "_iter_regions",
                side_effect=AssertionError("wxid 直认，不该扫内存")):
            self.assertEqual(
                localdb.resolve_group_wxid("167629349@chatroom"),
                "167629349@chatroom")

    def test_empty_input_is_none(self):
        for text in ("", "   ", None):
            with self.subTest(text=text):
                self.assertIsNone(localdb.resolve_group_wxid(text))

    def test_majority_wxid_wins(self):
        """同名在 A 群出现两次、B 群出现一次 → 选 A，不串到 B。"""
        chunks = (_contact_chunk("111@chatroom", "水果群"),
                  _contact_chunk("111@chatroom", "水果群"),
                  _contact_chunk("222@chatroom", "水果群"))
        p1, p2, p3 = _patched_chunks(*chunks)
        with p1, p2, p3:
            self.assertEqual(localdb.resolve_group_wxid("水果群"), "111@chatroom")

    def test_tie_returns_none_instead_of_guessing(self):
        """两群票数一样：分不清，返回 None 让调用方回落，绝不猜一个。"""
        chunks = (_contact_chunk("111@chatroom", "水果群"),
                  _contact_chunk("222@chatroom", "水果群"))
        p1, p2, p3 = _patched_chunks(*chunks)
        with p1, p2, p3:
            self.assertIsNone(localdb.resolve_group_wxid("水果群"))

    def test_missing_name_returns_none(self):
        p1, p2, p3 = _patched_chunks(b"\x00" * 256)
        with p1, p2, p3:
            self.assertIsNone(localdb.resolve_group_wxid("不存在的群"))

    def test_second_call_hits_cache(self):
        """同一群名只扫一遍内存，第二次走缓存。"""
        chunks = (_contact_chunk("111@chatroom", "水果群"),)
        p1, p2, p3 = _patched_chunks(*chunks)
        with p1, p2, p3:
            self.assertEqual(localdb.resolve_group_wxid("水果群"), "111@chatroom")
        with unittest.mock.patch.object(
                localdb, "_iter_regions",
                side_effect=AssertionError("第二次该走缓存")):
            self.assertEqual(localdb.resolve_group_wxid("水果群"), "111@chatroom")


class DisplayPriorityTests(unittest.TestCase):
    """显示优先级：手动备注 > 内存真名 > 原始编号。"""

    def _app(self):
        from pyweixin.application import MerchantApplication
        from pyweixin.conversations import SimulatedConversations
        from pyweixin.history_read import SimulatedHistory
        from pyweixin.orders import OrderStore
        from pyweixin.wechat_state import SimulatedWeChat, WeChatState

        self.dir = Path(tempfile.mkdtemp(prefix="pyw-find-"))
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.merchant = self.store.create_merchant("测试商家")
        self.tenant = self.merchant["merchant_id"]
        self.app = MerchantApplication(
            data_dir=self.dir, orders=self.store, tenant=self.tenant,
            probe=SimulatedWeChat([WeChatState.LOGGED_IN]),
            conversation_source=SimulatedConversations(
                [("已有群", ConversationKind.GROUP, 10)]),
            history_reader_fn=SimulatedHistory(default_count=0),
            sender=lambda t, x: None,
        )
        return self.app

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            try:
                self.app.close()
            except Exception:
                pass
            self.app = None
        if getattr(self, "store", None) is not None:
            try:
                self.store.close()
            except Exception:
                pass
        if getattr(self, "dir", None) is not None:
            shutil.rmtree(self.dir, ignore_errors=True)
        localdb.clear_name_caches()

    def test_display_prefers_alias_then_memory_name_then_raw(self):
        app = self._app()
        memory_named = Conversation(name="内存真名", kind=ConversationKind.GROUP,
                                    wxid="111@chatroom")
        self.assertEqual(app.display_name_for_conversation(memory_named), "内存真名")
        app.set_alias("111@chatroom", "手动备注")
        self.assertEqual(app.display_name_for_conversation(memory_named), "手动备注")
        unresolved = Conversation(name="222@chatroom", kind=ConversationKind.GROUP,
                                  wxid="222@chatroom")
        self.assertEqual(app.display_name_for_conversation(unresolved), "222@chatroom")


class ConvOrderKeyTests(unittest.TestCase):
    """界面排序：真名在前，编号沉底。不建 Tk 窗口，只调纯逻辑。"""

    class _FakeApp:
        def __init__(self, aliases):
            self._aliases = dict(aliases)

        def display_name_for(self, raw):
            return self._aliases.get(str(raw), str(raw))

        def display_name_for_conversation(self, item):
            raw = str(item.wxid or item.name or "")
            shown = self.display_name_for(raw)
            if shown != raw:
                return shown
            if item.name and item.name != raw:
                return item.name
            return raw

    def _window(self, aliases):
        from pyweixin.merchant_ui import MerchantWindow

        window = MerchantWindow.__new__(MerchantWindow)
        window.app = self._FakeApp(aliases)
        return window

    def test_named_before_unresolved(self):
        named = Conversation(name="真群", kind=ConversationKind.GROUP,
                             wxid="222@chatroom")
        unresolved = Conversation(name="111@chatroom", kind=ConversationKind.GROUP,
                                  wxid="111@chatroom")
        window = self._window({})
        ordered = sorted([unresolved, named], key=window._conv_order_key)
        self.assertEqual(ordered, [named, unresolved])

    def test_named_keep_stable_relative_order(self):
        """真名之间不重排：原来第 2 步的顺序不能被排序打乱。"""
        first = Conversation(name="苹果群", kind=ConversationKind.GROUP)
        second = Conversation(name="香蕉群", kind=ConversationKind.GROUP)
        window = self._window({})
        ordered = sorted([second, first], key=window._conv_order_key)
        self.assertEqual(ordered, [second, first])

    def test_unresolved_without_chatroom_suffix_is_not_sunk(self):
        """好友（没有 @chatroom）不受编号沉底影响。"""
        friend = Conversation(name="王老板", kind=ConversationKind.FRIEND)
        window = self._window({})
        self.assertFalse(window._conv_is_unresolved(friend))


class CleanNameTests(unittest.TestCase):
    """带控制符/换行的"名字"不能上桌面：那是二进制误解析或消息粘贴。"""

    def test_control_chars_are_rejected(self):
        self.assertFalse(localdb._is_clean_name("食品(e\x143\x08wxid_"))
        self.assertFalse(localdb._is_clean_name("明天\n一板油"))
        self.assertFalse(localdb._is_clean_name("a\u0000b"))

    def test_real_names_survive(self):
        for text in ("那达慕餐饮组", "蒙古袍群*.。(๑･∀･๑)",
                     "🍀小林老师（教辅收徒）", "2026马年3月8日 新世纪皇宫 义工群",
                     "epresentative关联CRA", "OK团购群"):
            with self.subTest(text=text):
                self.assertTrue(localdb._is_clean_name(text))

    def test_vote_skips_dirty_candidates(self):
        """全是脏串时认怂回空串，不拿二进制当群名。"""
        self.assertEqual(localdb._vote_name(["abc\x14def", "hi\nbye"]), "")
        # 干净的照样能投出来。
        self.assertEqual(
            localdb._vote_name(["abc\x14def", "水果群", "水果群"]), "水果群")


if __name__ == "__main__":
    unittest.main()
