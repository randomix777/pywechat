"""读不到消息时的重试。

## 机制

`traverse_chat_history` 是**键盘驱动**的：靠 `has_keyboard_focus()` 找当前项，
靠 `pyautogui` 按方向键往下走。拿不到键盘焦点 → 连续三轮找不到有焦点的项 →
**直接返回空列表**，界面上就显示「这次没有读到任何消息」。

焦点是会时好时坏的（窗口刚最大化还没稳、有别的窗口抢焦点、微信自己在弹东西），
于是同一个群有时读到 7 条、有时读到 0 条。

## 修复针对症状，不赌根因

不猜"到底是什么抢了焦点"，只处理能确定的一件事：

> **列表里有消息，却因为没拿到焦点而返回空。**

这种情况重新对焦再试。列表真的空就立刻收工，不做无谓的重试。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class TraverseRetryTests(unittest.TestCase):
    """用假窗口驱动真实代码路径。"""

    def setUp(self):
        from wechat_ai_assistant import utils

        self.utils = utils

        class FakeItem:
            def __init__(self, name, focused, runtime_id):
                self._name = name
                self._focused = focused
                self._runtime_id = runtime_id

            def has_keyboard_focus(self):
                return self._focused

            def element_info(self):
                return type("E", (), {"runtime_id": self._runtime_id})()

            def class_name(self):
                return "mmui::ChatBubbleItemView"

            def window_text(self):
                return self._name

        class FakeList:
            """模拟焦点时有时无的列表。"""

            def __init__(self, items, focus_rounds):
                self.items = items
                self.focus_rounds = focus_rounds
                self.calls = 0
                self.focus_calls = 0
                self.pressed = 0

            def children(self):
                self.calls += 1
                # 前 focus_rounds 轮"拿不到焦点"，之后恢复正常。
                focused = self.calls > self.focus_rounds
                return [
                    FakeItem(f"消息{i}", focused and i == 0, i)
                    for i in range(len(self.items))
                ]

            def set_focus(self):
                self.focus_calls += 1
                # 重新对焦后焦点就好了 —— 模拟"再来一次就成"。
                self.focus_calls = self.focus_calls
                self.calls = max(self.calls, self.focus_rounds)
                return True

            def type_keys(self, *_a, **_k):
                return None

        self.FakeList = FakeList

    def _patch_pyautogui(self):
        from pyautogui import press  # noqa: F401

    def test_the_single_pass_resets_before_it_starts(self):
        """每遍开头先复位，所以焦点丢过也能读回来。

        早期版本把复位放在末尾且失败静默吞掉，于是下一遍的起点是随机的 ——
        实测同一个列表两遍拿到过 34 和 33 两个结果。
        """
        fake = self.FakeList([1, 2, 3], focus_rounds=99)
        result = self.utils._traverse_once(fake, False, 10, False, None, None, None, 0, 0)
        self.assertGreaterEqual(len(result), 1,
                                "每遍开头复位之后，第一次就该读到东西")
        self.assertGreater(fake.focus_calls, 0, "没有尝试对焦")

    def test_the_wrapper_retries_instead_of_giving_up(self):
        """外层负责重试 —— 这才是商户能感觉到的那一层。"""
        attempts = {"n": 0}
        fake = self.FakeList([1, 2, 3], focus_rounds=0)

        def fake_traverse(lst, *args):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return []          # 第一次没读到
            return [("消息", "mmui::ChatBubbleItemView", [])]

        original = self.utils._traverse_once
        self.utils._traverse_once = fake_traverse
        try:
            window = type("W", (), {"child_window": staticmethod(lambda **_k: fake)})()
            self.utils.traverse_chat_history(window, False, 10)
            self.assertEqual(attempts["n"], 2,
                             "读到空之后应当再试一次，而不是直接返回空")
        finally:
            self.utils._traverse_once = original

    def test_it_stops_immediately_when_the_list_is_really_empty(self):
        """列表里确实没有消息时，重试是浪费商户的时间。"""
        attempts = {"n": 0}
        empty = self.FakeList([], focus_rounds=0)

        def fake_traverse(lst, *args):
            attempts["n"] += 1
            return []

        original = self.utils._traverse_once
        self.utils._traverse_once = fake_traverse
        try:
            window = type("W", (), {"child_window": staticmethod(lambda **_k: empty)})()
            self.utils.traverse_chat_history(window, False, 10)
            self.assertEqual(attempts["n"], 1, "列表真的空却还在重试")
        finally:
            self.utils._traverse_once = original

    def test_a_successful_read_is_not_retried(self):
        attempts = {"n": 0}

        def fake_traverse(lst, *args):
            attempts["n"] += 1
            return [("消息", "mmui::ChatBubbleItemView", [])]

        fake = self.FakeList([1, 2], focus_rounds=0)
        original = self.utils._traverse_once
        self.utils._traverse_once = fake_traverse
        try:
            window = type("W", (), {"child_window": staticmethod(lambda **_k: fake)})()
            self.utils.traverse_chat_history(window, False, 10)
            self.assertEqual(attempts["n"], 1, "读到东西了就不该再来一遍")
        finally:
            self.utils._traverse_once = original


class RetryBudgetTests(unittest.TestCase):
    def test_the_retry_count_is_bounded(self):
        """重试必须有上限，否则商户会对着一个卡住的界面干等。"""
        from wechat_ai_assistant.utils import TRAVERSE_FOCUS_RETRIES

        self.assertGreaterEqual(TRAVERSE_FOCUS_RETRIES, 1)
        self.assertLessEqual(TRAVERSE_FOCUS_RETRIES, 3,
                             "重试太多次会把一次读取拖成几分钟")


class HonestDocTests(unittest.TestCase):
    """这个修复针对的是症状，不能把它写成"根因已找到"。"""

    def test_the_comment_says_it_does_not_claim_a_root_cause(self):
        source = (ROOT / "src" / "wechat_ai_assistant" / "utils.py").read_text(encoding="utf-8")
        start = source.index("def traverse_chat_history")
        block = source[start:source.index("def _traverse_once")]
        self.assertIn("症状", block, "应当明写这是针对症状，不是赌根因")


class DeterministicStartTests(unittest.TestCase):
    """每一遍都必须从**已知位置**起步。

    ## 证据

    装上日志之后，第一次真实读取就暴露出：

        遍历聊天记录结束：读到 34 条
        遍历聊天记录结束：读到 33 条     ← 同一个列表，两遍结果不一样

    同一个列表被遍历两遍、结果却不同。复位原先放在**每遍末尾**，失败会被
    静默吞掉，而且下一遍的起点取决于上一遍停在哪。

    改成放在**每遍开头**：不管之前发生过什么，起点都是确定的。改完同样
    的实验，两遍都读到 34 条。
    """

    def test_the_reset_happens_before_the_loop_not_after_it(self):
        source = (ROOT / "src" / "wechat_ai_assistant" / "utils.py").read_text(encoding="utf-8")
        start = source.index("def _traverse_once")
        body = source[start:source.index("class ", start)] if "class " in source[start:] else source[start:]
        reset_at = body.index("type_keys('{HOME}')")
        loop_at = body.index("while recorded_num<number")
        self.assertLess(reset_at, loop_at,
                        "复位必须在遍历之前：放在之后，下一遍的起点就是随机的")

    def test_a_failed_reset_does_not_abort_the_traversal(self):
        """复位失败只是"起点不保证"，不该让整次读取失败。"""
        source = (ROOT / "src" / "wechat_ai_assistant" / "utils.py").read_text(encoding="utf-8")
        start = source.index("def _traverse_once")
        body = source[start:start + 1200]
        self.assertIn("except Exception", body[:body.index("while recorded_num")],
                      "复位必须包在 try 里")

    def test_two_passes_over_one_list_agree(self):
        """真的跑两遍，结果必须一样。"""
        from wechat_ai_assistant import utils

        rows = [
            ("消息0", "mmui::ChatBubbleItemView"), ("消息1", "mmui::ChatBubbleItemView"),
            ("消息2", "mmui::ChatBubbleItemView"), ("消息3", "mmui::ChatBubbleItemView"),
        ]

        class Item:
            def __init__(self, text, cls, focused):
                self._text, self._cls, self._focused = text, cls, focused

            def has_keyboard_focus(self):
                return self._focused

            def element_info(self):
                return type("E", (), {"runtime_id": id(self)})()

            def class_name(self):
                return self._cls

            def window_text(self):
                return self._text

        class L:
            """起点随上一遍结束位置漂移的列表 —— 模拟真实的不确定性。"""

            def __init__(self):
                self.cursor = 0
                self.homes = 0

            def set_focus(self):
                return True

            def type_keys(self, key):
                if key == "{HOME}":
                    self.cursor = 0
                    self.homes += 1

            def children(self):
                # 只有 cursor 指向的那一项有焦点；按下键 cursor 前进。
                return [Item(t, c, i == self.cursor) for i, (t, c) in enumerate(rows)]

        listing = L()
        first = utils._traverse_once(listing, False, 50, False, None, None, None, 0, 0)
        # 模拟真实情况：第一遍停在末尾，不复位。
        listing.cursor = len(rows) - 1
        second = utils._traverse_once(listing, False, 50, False, None, None, None, 0, 0)
        self.assertEqual(len(first), len(second),
                         f"两遍结果不一致：{len(first)} vs {len(second)}")
        self.assertEqual([d[0] for d in first], [d[0] for d in second])


if __name__ == "__main__":
    unittest.main()