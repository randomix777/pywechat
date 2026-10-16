"""群发送人按昵称+备注双匹配的回归测试。

背景：4.x 微信的 UIA 文本里经常没有发送人名，能匹配的唯一依据就是
群成员名单。以前名单只有一种名字（``["张老板"]``），备注名开头的消息
匹配不上；现在名单支持 ``[{"昵称":..,"备注":..}]``，两个都拿去匹配，
发送人一次输出 ``备注(昵称)`` —— 昵称和备注一次列全。

全部纯逻辑，不连微信。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.utils import (  # noqa: E402
    UNRESOLVED_SENDER,
    avatar_click_point,
    avatar_group_key,
    bubble_side,
    format_sender_display,
    match_group_sender,
    normalize_group_members,
    parse_avatar_profile_texts,
    parse_group_chat_history,
    parse_group_messages,
    resolve_group_senders_via_avatar,
)

VIEW = "mmui::ChatTextItemView"


class FormatSenderDisplayTests(unittest.TestCase):
    def test_both_names_are_listed(self):
        self.assertEqual(format_sender_display("张老板", "苹果哥"), "苹果哥(张老板)")

    def test_nickname_only(self):
        self.assertEqual(format_sender_display("张老板", ""), "张老板")
        self.assertEqual(format_sender_display("张老板"), "张老板")

    def test_same_name_is_not_doubled(self):
        self.assertEqual(format_sender_display("张老板", "张老板"), "张老板")

    def test_wu_remark_is_dropped(self):
        self.assertEqual(format_sender_display("张老板", "无"), "张老板")


class NormalizeGroupMembersTests(unittest.TestCase):
    def test_plain_names_still_work(self):
        members = normalize_group_members(["张老板"])
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["显示名"], "张老板")
        self.assertEqual(members[0]["别名"], ["张老板"])

    def test_dict_carries_both_names(self):
        members = normalize_group_members([{"昵称": "张老板", "备注": "苹果哥"}])
        self.assertEqual(members[0]["别名"], ["张老板", "苹果哥"])
        self.assertEqual(members[0]["显示名"], "苹果哥(张老板)")

    def test_tuple_shape(self):
        members = normalize_group_members([("张老板", "苹果哥")])
        self.assertEqual(members[0]["显示名"], "苹果哥(张老板)")

    def test_empty_entries_are_skipped(self):
        self.assertEqual(normalize_group_members(["", None, "  "]), [])
        self.assertEqual(normalize_group_members(None), [])


class NicknameRemarkMatchTests(unittest.TestCase):
    MEMBERS = [{"昵称": "张老板", "备注": "苹果哥"}, {"昵称": "李叔", "备注": ""}]

    def test_nickname_prefix_matches(self):
        sender, rest = match_group_sender("张老板 两箱苹果", normalize_group_members(self.MEMBERS))
        self.assertEqual(sender, "苹果哥(张老板)")
        self.assertEqual(rest, "两箱苹果")

    def test_remark_prefix_matches(self):
        sender, rest = match_group_sender("苹果哥 两箱苹果", normalize_group_members(self.MEMBERS))
        self.assertEqual(sender, "苹果哥(张老板)")
        self.assertEqual(rest, "两箱苹果")

    def test_longer_alias_wins(self):
        members = normalize_group_members([{"昵称": "张", "备注": ""}, {"昵称": "张老板", "备注": ""}])
        sender, rest = match_group_sender("张老板 两箱", members)
        self.assertEqual(sender, "张老板")
        self.assertEqual(rest, "两箱")

    def test_unknown_is_unresolved(self):
        sender, rest = match_group_sender("陌生人 你好", normalize_group_members(self.MEMBERS))
        self.assertIsNone(sender)
        self.assertEqual(rest, "陌生人 你好")


class ParseWithNicknameRemarkTests(unittest.TestCase):
    MEMBERS = [{"昵称": "张老板", "备注": "苹果哥"}]

    def test_chat_history_lists_both(self):
        _, senders, _, _ = parse_group_chat_history(
            [("张老板 两箱苹果 2026-10-2 12:21", VIEW)], [], self.MEMBERS)
        self.assertEqual(senders, ["苹果哥(张老板)"])

    def test_chat_history_remark_prefix(self):
        _, senders, _, _ = parse_group_chat_history(
            [("苹果哥 两箱苹果 2026-10-2 12:21", VIEW)], [], self.MEMBERS)
        self.assertEqual(senders, ["苹果哥(张老板)"])

    def test_chat_history_unknown_still_fallback(self):
        _, senders, _, _ = parse_group_chat_history(
            [("陌生人 你好 2026-10-2 12:21", VIEW)], [], self.MEMBERS)
        self.assertEqual(senders, [UNRESOLVED_SENDER])

    def test_old_plain_list_is_unchanged(self):
        _, senders, _, _ = parse_group_chat_history(
            [("张老板 两箱苹果 2026-10-2 12:21", VIEW)], [], ["张老板"])
        self.assertEqual(senders, ["张老板"])

    def test_group_messages_lists_both(self):
        _, senders, _ = parse_group_messages(
            [("苹果哥 两箱苹果", VIEW)], [], self.MEMBERS)
        self.assertEqual(senders, ["苹果哥(张老板)"])

    def test_group_messages_unknown_still_fallback(self):
        _, senders, _ = parse_group_messages(
            [("陌生人 你好", VIEW)], [], self.MEMBERS)
        self.assertEqual(senders, [UNRESOLVED_SENDER])


class GroupMembersDetailTests(unittest.TestCase):
    def test_remark_is_merged_from_friends_detail(self):
        from wechat_ai_assistant.WeChatAuto import Contacts

        friends = [
            {"昵称": "张老板", "备注": "苹果哥"},
            {"昵称": "李叔", "备注": "无"},
        ]
        with patch.object(Contacts, "get_groupMembers_info", return_value=["张老板", "李叔", "王姐"]):
            detail = Contacts.get_groupMembers_detail(group="测试群", friends_detail=friends)
        self.assertEqual(detail, [
            {"昵称": "张老板", "备注": "苹果哥"},
            {"昵称": "李叔", "备注": ""},
            {"昵称": "王姐", "备注": ""},
        ])

    def test_without_friends_detail_remark_is_empty(self):
        from wechat_ai_assistant.WeChatAuto import Contacts

        with patch.object(Contacts, "get_groupMembers_info", return_value=["张老板"]):
            detail = Contacts.get_groupMembers_detail(group="测试群")
        self.assertEqual(detail, [{"昵称": "张老板", "备注": ""}])


class AvatarClickPointTests(unittest.TestCase):
    def test_offset_lands_near_avatar_center(self):
        from types import SimpleNamespace

        class FakeItem:
            def rectangle(self):
                return SimpleNamespace(left=100, top=200, right=600, bottom=300)

        self.assertEqual(avatar_click_point(FakeItem()), (125, 225))
        self.assertEqual(avatar_click_point(FakeItem(), dx=10, dy=10), (110, 210))

    def _item_with_image(self, image, left=100, top=200, right=200, bottom=300):
        from types import SimpleNamespace

        class FakeItem:
            def rectangle(self):
                return SimpleNamespace(left=left, top=top, right=right, bottom=bottom)

            def capture_as_image(self):
                return image

        return FakeItem()

    def test_measured_blob_center_wins_over_offset(self):
        from PIL import Image

        img = Image.new("RGB", (100, 100), (250, 250, 250))
        for x in range(10, 30):
            for y in range(30, 50):
                img.putpixel((x, y), (200, 30, 30))
        got = avatar_click_point(self._item_with_image(img))
        self.assertEqual(got, (120, 240))

    def test_plain_background_falls_back_to_offset(self):
        from PIL import Image

        img = Image.new("RGB", (100, 100), (0x1E, 0x1E, 0x1F))
        self.assertEqual(avatar_click_point(self._item_with_image(img)), (125, 225))

    def test_dpi_scaling_is_applied(self):
        from PIL import Image

        img = Image.new("RGB", (50, 50), (250, 250, 250))
        for x in range(10, 20):
            for y in range(10, 20):
                img.putpixel((x, y), (30, 30, 200))
        got = avatar_click_point(
            self._item_with_image(img, left=100, top=200, right=200, bottom=300))
        self.assertEqual(got, (100 + 14 * 2, 200 + 14 * 2))

    def test_content_below_avatar_zone_is_ignored(self):
        """长图的下半部分再花哨也不能被当成头像。”
        """
        from PIL import Image

        img = Image.new("RGB", (100, 300), (250, 250, 250))
        for x in range(10, 50):
            for y in range(200, 280):
                img.putpixel((x, y), (200, 30, 30))
        self.assertEqual(
            avatar_click_point(self._item_with_image(img, right=200, bottom=500)),
            (125, 225))


class AvatarProfileParseTests(unittest.TestCase):
    def test_full_card(self):
        from wechat_ai_assistant.Uielements import Special_Labels

        texts = ["张老板", Special_Labels.WxNum, "wxid_abc",
                 Special_Labels.Remark, "苹果哥", Special_Labels.Region, "广东"]
        self.assertEqual(parse_avatar_profile_texts(texts),
                         {"昵称": "张老板", "备注": "苹果哥", "微信号": "wxid_abc"})

    def test_mini_card_with_nickname_only(self):
        self.assertEqual(parse_avatar_profile_texts(["张老板"]),
                         {"昵称": "张老板", "备注": "", "微信号": ""})

    def test_empty_card(self):
        self.assertEqual(parse_avatar_profile_texts([]),
                         {"昵称": "", "备注": "", "微信号": ""})
        self.assertEqual(parse_avatar_profile_texts(None),
                         {"昵称": "", "备注": "", "微信号": ""})

    def test_wu_remark_is_dropped(self):
        from wechat_ai_assistant.Uielements import Special_Labels

        texts = ["张老板", Special_Labels.Remark, "无"]
        info = parse_avatar_profile_texts(texts)
        self.assertEqual(info["备注"], "")

    def test_label_nickname_wins_over_first_line(self):
        from wechat_ai_assistant.Uielements import Special_Labels

        texts = ["显示名", Special_Labels.Nickname, "张老板"]
        self.assertEqual(parse_avatar_profile_texts(texts)["昵称"], "张老板")


class AvatarResolveGuardsTests(unittest.TestCase):
    """自己发的、系统消息不点；点不开的记 None 不炸。"""

    class FakeItem:
        def __init__(self, class_name, runtime_id):
            self._class_name = class_name
            self._runtime_id = runtime_id

        def class_name(self):
            return self._class_name

        def rectangle(self):
            from types import SimpleNamespace
            return SimpleNamespace(left=100, top=200, right=200, bottom=300)

        @property
        def element_info(self):
            from types import SimpleNamespace
            return SimpleNamespace(runtime_id=self._runtime_id)

    class FakeList:
        def rectangle(self):
            from types import SimpleNamespace
            return SimpleNamespace(left=0, top=0, right=1000, bottom=1000)

    def test_system_and_self_are_skipped_without_clicking(self):
        import wechat_ai_assistant.utils as utils_mod

        items = [self.FakeItem("mmui::ChatSystemInfoItemView", 1),
                 self.FakeItem("mmui::ChatTextItemView", 2),
                 self.FakeItem("mmui::ChatTextItemView", 3)]
        with patch.object(utils_mod.Tools, "is_my_bubble",
                          side_effect=[True, False]):
            with patch.object(utils_mod, "_click_avatar_and_read",
                              return_value={"昵称": "张老板", "备注": "", "微信号": ""}) as clicked:
                with patch.object(utils_mod, "_dismiss_avatar_popup"):
                    got = resolve_group_senders_via_avatar(self.FakeList(), items)
        self.assertEqual(got[0], None)
        self.assertEqual(got[1], None)
        self.assertEqual(got[2], {"昵称": "张老板", "备注": "", "微信号": ""})
        self.assertEqual(clicked.call_count, 1)

    def test_click_failure_is_none_not_raise(self):
        import wechat_ai_assistant.utils as utils_mod

        items = [self.FakeItem("mmui::ChatTextItemView", 9)]
        with patch.object(utils_mod.Tools, "is_my_bubble", return_value=False):
            with patch.object(utils_mod, "_click_avatar_and_read",
                              side_effect=ValueError("点头像后没有弹出资料卡")):
                with patch.object(utils_mod, "_dismiss_avatar_popup"):
                    got = resolve_group_senders_via_avatar(self.FakeList(), items)
        self.assertEqual(got, [None])

    def test_max_clicks_bounds_time(self):
        import wechat_ai_assistant.utils as utils_mod

        items = [self.FakeItem("mmui::ChatTextItemView", i) for i in range(5)]
        with patch.object(utils_mod.Tools, "is_my_bubble", return_value=False):
            with patch.object(utils_mod, "_click_avatar_and_read",
                              return_value={"昵称": "x", "备注": "", "微信号": ""}) as clicked:
                with patch.object(utils_mod, "_dismiss_avatar_popup"):
                    got = resolve_group_senders_via_avatar(self.FakeList(), items, max_clicks=2)
        self.assertEqual(clicked.call_count, 2)
        self.assertEqual(got[2], None)

    def test_offscreen_item_is_skipped_without_clicking(self):
        import wechat_ai_assistant.utils as utils_mod
        from types import SimpleNamespace

        class FarItem(self.FakeItem):
            def rectangle(self):
                return SimpleNamespace(left=2000, top=200, right=2100, bottom=300)

        with patch.object(utils_mod.Tools, "is_my_bubble", return_value=False):
            with patch.object(utils_mod, "_click_avatar_and_read",
                              return_value={"昵称": "x", "备注": "", "微信号": ""}) as clicked:
                with patch.object(utils_mod, "_dismiss_avatar_popup"):
                    got = resolve_group_senders_via_avatar(
                        self.FakeList(), [FarItem("mmui::ChatTextItemView", 1)])
        self.assertEqual(got, [None])
        self.assertEqual(clicked.call_count, 0)


class BubbleSideTests(unittest.TestCase):
    """气泡左右位置快判：分得清就不截图，分不清才判像素。"""

    def _rects(self, item_box, list_box=(0, 0, 900, 600)):
        from types import SimpleNamespace

        class FakeItem:
            def rectangle(self):
                left, top, right, bottom = item_box
                return SimpleNamespace(left=left, top=top, right=right, bottom=bottom)

        class FakeList:
            def rectangle(self):
                left, top, right, bottom = list_box
                return SimpleNamespace(left=left, top=top, right=right, bottom=bottom)

        return FakeItem(), FakeList()

    def test_peer_bubble_on_the_left(self):
        item, chat_list = self._rects((60, 100, 500, 160))
        self.assertEqual(bubble_side(item, chat_list), "left")

    def test_own_bubble_on_the_right(self):
        item, chat_list = self._rects((500, 100, 840, 160))
        self.assertEqual(bubble_side(item, chat_list), "right")

    def test_full_width_message_is_middle(self):
        item, chat_list = self._rects((60, 100, 840, 160))
        self.assertEqual(bubble_side(item, chat_list), "middle")

    def test_broken_rect_is_middle(self):
        class Broken:
            def rectangle(self):
                raise RuntimeError("拿不到")

        _, chat_list = self._rects((0, 0, 10, 10))
        self.assertEqual(bubble_side(Broken(), chat_list), "middle")

    def test_side_short_circuits_pixel_check(self):
        import wechat_ai_assistant.utils as utils_mod

        left, chat_list = self._rects((60, 100, 500, 160))
        right, _ = self._rects((500, 100, 840, 160))
        middle, _ = self._rects((60, 100, 840, 160))
        with patch.object(utils_mod.Tools, "is_my_bubble") as pixels:
            from wechat_ai_assistant.WeChatAuto import _mine_by_side_or_pixels
            self.assertFalse(_mine_by_side_or_pixels(left, chat_list))
            self.assertTrue(_mine_by_side_or_pixels(right, chat_list))
            self.assertEqual(pixels.call_count, 0)
            pixels.return_value = True
            self.assertTrue(_mine_by_side_or_pixels(middle, chat_list))
            self.assertEqual(pixels.call_count, 1)


class AvatarGroupKeyTests(unittest.TestCase):
    """头像指纹：同一头像同一 key，不同头像不同 key，太干净的返回 None。"""

    def _item(self, image):
        class FakeItem:
            def capture_as_image(self):
                return image

        return FakeItem()

    def _blob(self, color, box=(10, 10, 30, 30), size=(100, 100), bg=(250, 250, 250)):
        from PIL import Image

        img = Image.new("RGB", size, bg)
        for x in range(box[0], box[2]):
            for y in range(box[1], box[3]):
                img.putpixel((x, y), color)
        return img

    def test_same_avatar_same_key(self):
        img = self._blob((200, 30, 30))
        self.assertEqual(avatar_group_key(self._item(img)), avatar_group_key(self._item(img)))

    def test_different_avatars_different_keys(self):
        red = avatar_group_key(self._item(self._blob((200, 30, 30))))
        blue = avatar_group_key(self._item(self._blob((30, 30, 200))))
        self.assertIsNotNone(red)
        self.assertIsNotNone(blue)
        self.assertNotEqual(red, blue)

    def test_plain_background_is_none(self):
        from PIL import Image

        for bg in ((250, 250, 250), (0x1E, 0x1E, 0x1F)):
            with self.subTest(bg=bg):
                self.assertIsNone(avatar_group_key(self._item(Image.new("RGB", (100, 100), bg))))

    def test_broken_capture_is_none(self):
        class Broken:
            def capture_as_image(self):
                raise RuntimeError("截不到")

        self.assertIsNone(avatar_group_key(Broken()))


class MyNicknameCacheTests(unittest.TestCase):
    """本人昵称取一次就缓存：每跑一次读取都去点自己资料卡纯属浪费。"""

    def test_cached_name_skips_contacts(self):
        import wechat_ai_assistant.WeChatAuto as auto

        old = auto._MY_NICKNAME
        auto._MY_NICKNAME = "老板娘"
        try:
            with patch.object(auto.Contacts, "check_my_info",
                              side_effect=AssertionError("不该再打开资料卡")):
                self.assertEqual(auto._my_nickname(), "老板娘")
        finally:
            auto._MY_NICKNAME = old

    def test_first_fetch_is_cached(self):
        import wechat_ai_assistant.WeChatAuto as auto

        old = auto._MY_NICKNAME
        auto._MY_NICKNAME = None
        try:
            with patch.object(auto.Contacts, "check_my_info",
                              return_value={"昵称": "老板娘"}) as fetched:
                self.assertEqual(auto._my_nickname(), "老板娘")
                self.assertEqual(auto._my_nickname(), "老板娘")
                self.assertEqual(fetched.call_count, 1)
        finally:
            auto._MY_NICKNAME = old


if __name__ == "__main__":
    unittest.main()
