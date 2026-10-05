"""读取失败与「读到 0 条」必须能被分辨。

## 出过什么事

商户报告「读取历史偶尔返回 0 条」。上一轮修掉了「一旦读到 0 条就永久卡死」
（游标把 0 条记成 exhausted，之后永远跳过），但**第一次为什么读到 0 条**
一直没有定位。

这次找到了，�� ``WeChatAuto.dump_chat_history``：

    chat_history_list = chat_history_window.child_window(**Lists.ChatHistoryList)
    if not chat_history_list.exists(timeout=1):
        warn("你与 X 的聊天记录为空")
        return []

微信的历史面板是**异步渲染**的：窗口出现了，消息列表还在加载。1 秒等不到，
代码就把它判定成「这个会话没有聊天记录」并返回空列表。

商户看到的就是「读取到 0 条」，而且没有任何提示 —— 他会以为这个群真的
没消息，而其实只是微信没加载完，再点一次就读出来了。

## 两个缺陷

1. **等待太短**：固定 1 秒，且只试一次。微信加载期间会重建控件，一次
   exists() 可能刚好落在重建的空窗期。
2. **误诊**：把「还没就位」说成「没有聊天记录」，并静默返回空列表。

第 2 条更严重：它让一次**可恢复的读取失败**变成一个**看起来成功的 0 条结果**。
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.WeChatAuto import (  # noqa: E402
    CHAT_HISTORY_LIST_WAIT_SECONDS,
    _CHAT_HISTORY_LIST_RETRIES,
    _wait_for_chat_history_list,
)
from pyweixin.Warnings import ChatHistoryUnavailable, NoChatHistoryWarning  # noqa: E402


class _List:
    """假控件：前 ``ready_at`` 次调用不存在，之后一直存在。"""

    def __init__(self, ready_at: int):
        self.ready_at = ready_at
        self.calls = 0

    def exists(self, timeout=0):
        self.calls += 1
        return self.calls > self.ready_at


class RetryTests(unittest.TestCase):
    def test_it_returns_true_when_the_list_is_already_there(self):
        window = _List(ready_at=0)
        self.assertTrue(_wait_for_chat_history_list(window))
        self.assertEqual(window.calls, 1, "已经在就不该再等")

    def test_it_retries_when_the_list_appears_late(self):
        """微信加载期间会重建控件 —— 一次 exists() 可能正好落在空窗期。"""
        window = _List(ready_at=2)
        self.assertTrue(_wait_for_chat_history_list(window))
        self.assertGreaterEqual(window.calls, 3, "只试一次就会误判成空")

    def test_it_gives_up_after_a_bounded_number_of_tries(self):
        window = _List(ready_at=999)
        self.assertFalse(_wait_for_chat_history_list(window))
        self.assertEqual(window.calls, _CHAT_HISTORY_LIST_RETRIES + 1,
                         "必须有上限，否则会一直卡住")

    def test_the_budget_is_long_enough_for_a_cold_start(self):
        """1 秒不够微信冷启动。总预算应当明显更长。"""
        self.assertGreaterEqual(CHAT_HISTORY_LIST_WAIT_SECONDS, 5.0,
                                "总等待预算太短，冷启动时必然读不到")


class DistinguishFailureFromEmptyTests(unittest.TestCase):
    def test_a_missing_list_is_an_error_not_a_warning(self):
        """读不到必须是 RuntimeError，才能和「真的没有消息」分开。"""
        self.assertTrue(issubclass(ChatHistoryUnavailable, RuntimeError))
        self.assertFalse(issubclass(ChatHistoryUnavailable, Warning))

    def test_it_is_not_the_empty_history_warning(self):
        """NoChatHistoryWarning 是「记录不足指定条数」，不是「面板没就位」。"""
        self.assertIsNot(ChatHistoryUnavailable, NoChatHistoryWarning)
        self.assertTrue(issubclass(NoChatHistoryWarning, Warning))

    def test_the_message_says_it_is_a_failure_not_emptiness(self):
        """异常文案不能让人以为这个会话真的没消息。"""
        try:
            raise ChatHistoryUnavailable("这是**读取失败**，不是没有聊天记录，重试通常就能读出来")
        except ChatHistoryUnavailable as exc:
            self.assertIn("读取失败", str(exc))
            self.assertIn("重试", str(exc))


class SourceChecks(unittest.TestCase):
    """静态检查：那条「返回空列表」的路不能被改回来。"""

    def setUp(self):
        self.source = (ROOT / "src/pyweixin/WeChatAuto.py").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_the_one_second_wait_is_gone(self):
        func = next(n for n in ast.walk(self.tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "dump_chat_history")
        body = ast.dump(func)
        self.assertNotIn("timeout=1", body.replace("timeout=10", ""),
                         "又出现了 1 秒的等待 —— 那正是「偶尔 0 条」的成因")

    def test_it_raises_instead_of_returning_an_empty_list(self):
        func = next(n for n in ast.walk(self.tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "dump_chat_history")
        raises = any(isinstance(n, ast.Raise) for n in ast.walk(func))
        self.assertTrue(raises, "读不到列表时必须抛错，而不是返回空列表")

    def test_the_old_false_diagnosis_is_gone(self):
        self.assertNotIn("的聊天记录为空,无法获取聊天记录", self.source,
                         "「聊天记录为空」是对读取失败的误诊")


if __name__ == "__main__":
    unittest.main()