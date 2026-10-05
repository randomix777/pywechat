"""发送人解析的两条路，都被同一个参数掐断了。

## 两条路

`dump_chat_history` 解析群消息只有两条路：

1. **传了群成员** → ``parse_group_chat_history`` 直接用成员名单从正文里
   匹配出发送人（``utils.py`` 里 ``if groupMembers:`` 那一支）
2. **没传群成员** → 「两次遍历对比」：多选模式遍历一遍拿到**带发送人**的文本，
   非多选模式再遍历一遍拿到**只有内容**的文本，两者对齐后取前缀当发送人

第 2 条的整套设计都建立在「两次遍历拿到的文本不同」之上。

## 掐断它的地方

``WeChatAuto.dump_chat_history``：

    details_with_name = traverse_chat_history(win, select=save_detail, ...)

``select`` 直接用了 ``save_detail``。而我们的调用方
（``history_read._read_one``）传的是 ``save_detail=False`` —— 它不要存图片视频。

于是：

    details_with_name    ← select=False
    details_without_name ← select=False

**两次都是非多选模式。** 第二条路需要的那份「带发送人的文本」从来没被取过，
对齐拿到的永远是两份一模一样的东西。

## 为什么必然全错

拿两份相同列表对齐，每一行都能配到自己，于是每行的前缀都是空的：

    content = full_text            （两者本来就一样）
    full_text[:-len(content)] = "" （前缀为空）
    "" or None  →  None            （`_split_group_detail`）
    → 占位符

所以「一个群的发送人 100% 是占位符」不是偶发，是**必然**。

## 这一层不需要真微信

``traverse_chat_history`` 里 ``select`` 只决定要不要按回车存图片视频，
**不改变抓到的文本**（抓的是 ``selected[0].window_text()``）。
所以「两次遍历文本相同」这件事用假的列表就能证明。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from pyweixin.utils import UNRESOLVED_SENDER, parse_group_chat_history  # noqa: E402

DUMMY = pathlib.Path(__file__).resolve().parent.parent / "src/pyweixin/WeChatAuto.py"


class _FakeItem:
    """够 ``_traverse_once`` 用的假列表项。"""

    def __init__(self, text: str, class_name: str = "mmui::ChatBubbleItemView"):
        self._text = text
        self._class = class_name

    def has_keyboard_focus(self):
        return True

    def window_text(self):
        return self._text

    def class_name(self):
        return self._class

    @property
    def element_info(self):
        class _Info:
            runtime_id = 1
        return _Info()


class _FakeList:
    """``traverse_chat_history`` 会先 ``child_window`` 拿列表，所以这里要能自我返回。"""

    def __init__(self, texts):
        self._items = [_FakeItem(t) for t in texts]

    def child_window(self, **_k):
        return self

    def set_focus(self):
        pass

    def type_keys(self, *_a, **_k):
        pass

    def children(self, **_k):
        return list(self._items)


class SelectDoesNotChangeTheTextTests(unittest.TestCase):
    """`select` 不改变抓到的文本 —— 这是根因的机械部分。"""

    def setUp(self):
        from pyweixin import utils

        self.utils = utils
        self.texts = ["张姐 12:30\n要两箱苹果", "李叔 12:31\n酱油 2 瓶"]

    def _traverse(self, select):
        """跑一次真函数，只把「按回车存媒体」和「进入多选」这两个外部依赖换掉。"""
        from unittest import mock

        from pyweixin import utils

        pressed: list[str] = []

        class _Tools:
            @staticmethod
            def select_chatHistoryList(_win):
                return object()

        class _Pyautogui:
            @staticmethod
            def press(*a, **_k):
                pressed.append(a[0] if a else "")

        with mock.patch.object(utils, "Tools", _Tools), \
             mock.patch.object(utils, "pyautogui", _Pyautogui), \
             mock.patch.object(utils, "TRAVERSE_FOCUS_RETRIES", 0):
            return utils.traverse_chat_history(_FakeList(self.texts), select, 2)

    def test_both_modes_yield_the_same_text(self):
        without = self._traverse(False)
        with_select = self._traverse(True)
        self.assertEqual([row[0] for row in without],
                         [row[0] for row in with_select],
                         "select 不改变文本 —— 所以 select=save_detail 会让两次遍历完全一样")


class TwoIdenticalTraversalsCannotRecoverSendersTests(unittest.TestCase):
    """拿两份相同列表反推发送人，必然失败。这条不需要真微信。"""

    ROWS = [
        ("张姐 12:30\n要两箱苹果", "mmui::ChatBubbleItemView"),
        ("李叔 12:31\n酱油 2 瓶", "mmui::ChatBubbleItemView"),
        ("王姨 12:32\n明天来拿", "mmui::ChatBubbleItemView"),
    ]

    def test_it_yields_placeholders_for_every_row(self):
        _, senders, _, _ = parse_group_chat_history(list(self.ROWS), list(self.ROWS), [])
        self.assertEqual(senders, [UNRESOLVED_SENDER] * len(self.ROWS),
                         "两次遍历一样时不该假装认出发送人")

    def test_members_instead_of_alignment_does_work(self):
        """对照组：成员名单这条路**不依赖两次遍历**，所以能解析出来。"""
        _, senders, _, _ = parse_group_chat_history(
            list(self.ROWS), [],
            ["张姐", "李叔", "王姨"])
        self.assertEqual(senders, ["张姐", "李叔", "王姨"],
                         "\u4f20\u4e86\u6210\u5458\u5c31\u80fd\u76f4\u63a5\u89e3\u6790 \u2014\u2014 \u8fd9\u5c31\u662f\u4e3a\u4ec0\u4e48\u8981\u4f20")


class CallerUsesSaveDetailAsSelectTests(unittest.TestCase):
    """`select=save_detail` 就是根因本身。钉住它别再退回去。"""

    def test_the_two_traversals_must_not_share_one_flag(self):
        source = DUMMY.read_text(encoding="utf-8")
        line = next(l for l in source.splitlines() if "details_with_name=" in l)
        self.assertIn("select=True", line,
                      "取 details_with_name 必须真的开多选；它原来跟着 save_detail 走，"
                      "而我们传 save_detail=False，于是这条路从来没启用过")
        self.assertNotIn("select=save_detail", line)

    def test_our_caller_does_not_ask_for_media(self):
        """我们要的是发送人，不是图片视频 —— 所以不能靠 save_detail=True 去开多选。"""
        reader = (ROOT / "src/pyweixin/history_read.py").read_text(encoding="utf-8")
        self.assertIn("save_detail=False", reader)


if __name__ == "__main__":
    unittest.main()