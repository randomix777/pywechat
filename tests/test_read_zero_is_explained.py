"""「本次 0 条」必须说清**是哪一种 0**。

## 实测到的事

读取跑完了，日志写「读取完成，本次 0 条」，**没有报任何错**。

但库里**确实是空的**（0 消息、0 导入），所以不是被去重挡掉的，
是读取器从微信那边拿回来 0 条。

## 两种 0 在界面上曾经一模一样

* 微信里确实没有新消息 —— **正常**
* 读取器根本没取到（窗口没聚焦、群名搜不到、时间窗不匹配）—— **失败**

都显示「本次 0 条」，`error` 是空的。商户看到「完成」只会以为群里没消息，
永远不会去查「读取是不是没成功」。

这两件事我都发生过：那次 `NoSuchFriendError` 就是取不到群成员，
而界面连一个错都没报。

## 所以

`ConversationCoverage` 必须同时带：

* ``message_count`` —— 进库后的条数（商户真正看到的）
* ``fetched_count`` —— **进库之前**读取器拿回来的条数

有 ``fetched_count`` 才能区分「没读到」和「读到了但没进库」，
界面也才能说人话。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from wechat_ai_assistant import summary_task  # noqa: E402
from wechat_ai_assistant.history_read import ConversationCoverage  # noqa: E402


class CoverageCarriesBothCountsTests(unittest.TestCase):
    def test_the_field_exists_and_defaults_to_zero(self):
        self.assertEqual(ConversationCoverage(conversation=None).fetched_count, 0)

    def test_the_reader_fills_it_in(self):
        source = (ROOT / "src/wechat_ai_assistant/history_read.py").read_text(encoding="utf-8")
        self.assertIn("fetched_count=len(messages)", source,
                      "\u5fc5\u987b\u8bb0\u300c\u8bfb\u53d6\u5668\u62ff\u56de\u6765\u7684\u300d\uff0c\u4e0d\u662f\u8fdb\u5e93\u540e\u7684")
        self.assertIn("fetched_count: int = 0", source)

    def test_the_two_counts_are_independent(self):
        """\u4e00\u4e2a\u8bb0\u300c\u8bfb\u53d6\u5668\u62ff\u56de\u6765\u591a\u5c11\u300d\uff0c\u53e6\u4e00\u4e2a\u8bb0\u300c\u8fdb\u5e93\u4e86\u591a\u5c11\u300d\u3002
        \u628a\u5b83\u4eec\u5f53\u6210\u4e00\u4e2a\uff0c\u5c31\u6ca1\u6cd5\u533a\u5206\u4e0a\u9762\u90a3\u4e24\u79cd 0 \u4e86\u3002"""
        coverage = ConversationCoverage(conversation=None)
        # \u4e24\u8005\u5fc5\u987b\u662f\u5404\u81ea\u72ec\u7acb\u7684\u5b57\u6bb5\uff0c\u4e0d\u80fd\u662f\u540c\u4e00\u4e2a\u503c\u7684\u522b\u540d
        #\uff08\u9ed8\u8ba4\u90fd\u662f 0\uff0c\u5355\u770b\u9ed8\u8ba4\u503c\u5206\u4e0d\u51fa\u6765\uff09\u3002
        self.assertIn("fetched_count", ConversationCoverage.__dataclass_fields__)
        self.assertIn("message_count", ConversationCoverage.__dataclass_fields__)
        coverage.fetched_count = 12
        self.assertEqual(coverage.message_count, 0, "\u8bfb\u53d6\u5668\u62ff\u5230 12 \u6761\u4e0d\u4ee3\u8868\u8fdb\u4e86\u5e93")


class BothInterfacesExplainZeroTests(unittest.TestCase):
    """"\u5b8c\u6210\uff0c0 \u6761"\u8fd9\u53e5\u8bdd\u4f1a\u6b32\u3002\u4e24\u4e2a\u754c\u9762\u90fd\u5f97\u8bf4\u6e05\u695a\u3002"""

    def test_the_tk_ui_explains_zero(self):
        ui = (ROOT / "src/wechat_ai_assistant/merchant_ui.py").read_text(encoding="utf-8")
        self.assertIn("fetched_count", ui)
        self.assertIn("\u8fd9\u4e0d\u7b49\u4e8e\u7fa4\u91cc\u6ca1\u6d88\u606f", ui,
                      "\u9875\u9762\u4e0a\u4ecd\u7136\u53ea\u8bf4\u300c\u672c\u6b21 0 \u6761\u300d")

    def test_the_web_ui_explains_zero(self):
        dev = (ROOT / "src/wechat_ai_assistant/web_dev.py").read_text(encoding="utf-8")
        page = (ROOT / "src/wechat_ai_assistant/web_page.py").read_text(encoding="utf-8")
        self.assertIn('"fetched"', dev)
        self.assertIn("row.fetched", page)

    def test_a_failed_read_is_not_claimed_as_success(self):
        """"\u4e00\u6761\u90fd\u6ca1\u8bfb\u5230\u300d\u65f6\u4e0d\u5f97\u53ea\u8bf4\u300c\u5b8c\u6210\u300d\u3002"""
        ui = (ROOT / "src/wechat_ai_assistant/merchant_ui.py").read_text(encoding="utf-8")
        i = ui.index("\u8fd9\u4e0d\u7b49\u4e8e\u7fa4\u91cc\u6ca1\u6d88\u606f")
        around = ui[max(0, i - 700):i]
        self.assertIn("not item.error", around, "\u8bfb\u5931\u8d25\u65f6\u5e94\u8be5\u53ea\u8bf4\u300c\u8bfb\u5931\u8d25\u300d")
        self.assertIn("not item.skipped", around, "\u4e0a\u6b21\u5df2\u8bfb\u5b8c\u65f6\u4e0d\u5e94\u62a5\u300c\u6ca1\u8bfb\u5230\u300d")


class ZeroDoesNotBlockTheSummaryTests(unittest.TestCase):
    """\u53e6\u4e00\u4e2a\u8fb9\u754c\uff1a\u6ca1\u8bfb\u5230\u4e0d\u5f97\u53bb\u5f3a\u62a6\u3002"""

    def test_the_gate_is_the_shared_one(self):
        """"\u80fd\u4e0d\u80fd\u6574\u7406"\u5fc5\u987b\u53ea\u6709\u4e00\u4efd\u5224\u636e\uff0c\u4e24\u4e2a\u754c\u9762\u5171\u7528\u3002"""
        source = (ROOT / "src/wechat_ai_assistant/summary_task.py").read_text(encoding="utf-8")
        self.assertIn("def has_messages_for", source)
        self.assertIn("accumulated_coverage", source)

    def test_it_reports_the_helper_by_name(self):
        self.assertTrue(callable(summary_task.has_messages_for))
        self.assertTrue(callable(summary_task.accumulated_coverage))


if __name__ == "__main__":
    unittest.main()