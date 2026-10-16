"""表格里显示长文本的行为。

用真实群里的消息量过：客户常把数量、地址、要求写在句子后半段。28 字的上限
会把「修改后对之前的订单数据有影响吗？」切成「…有影…」—— **问题本身正好
被吃掉**。商户看到半句话，会以为客户没问那件事。

这组测试盯两件事：截断要**看得出来**（带省略号），以及被截掉的原文
**拿得回来**（双击看全文）。只截不给人看，等于丢信息。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.merchant_ui import CELL_LIMIT, _cell  # noqa: E402


class CellTruncationTests(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(_cell("苹果两箱"), "苹果两箱")

    def test_none_renders_as_empty_not_the_word_none(self):
        self.assertEqual(_cell(None), "")
        self.assertEqual(_cell(""), "")

    def test_long_text_is_marked_as_truncated(self):
        text = "苹果" * 60
        shown = _cell(text)
        self.assertTrue(shown.endswith("…"), "超长文字没有省略号，商户看不出被截过")
        self.assertLess(len(shown), len(text))

    def test_the_real_question_survives_the_default_limit(self):
        """真实群里这条 31 字。上限必须盖得住它，否则问题本身被吃掉。"""
        from wechat_ai_assistant.merchant_ui import _cell as cell

        self.assertEqual(cell("抱歉这周太忙了，太太有货柜到，修改后对之前的订单数据有影响吗？"),
                         "抱歉这周太忙了，太太有货柜到，修改后对之前的订单数据有影响吗？")

    def test_the_default_limit_covers_a_real_long_message(self):
        from wechat_ai_assistant.merchant_ui import _cell as cell

        text = ("@Daniel黄大为 您好 根据您 后期打算加入的功能，系统需要进行大面积重构，"
                "您看我是先保留大统华PDF导入和在线下单功能让你们先用着还是直接改完再说？")
        shown = cell(text)
        self.assertEqual(shown.count("…"), 1)
        # 至少要让商户看见诉求本身（这里是「重构」和问句）。
        self.assertIn("重构", shown)

    def test_newlines_do_not_break_the_row(self):
        """换行必须压成一行，否则表格高度会被撑开。"""
        shown = _cell("第一行\n第二行\n第三行")
        self.assertNotIn("\n", shown)

    def test_spaces_inside_the_text_do_not_shift_columns(self):
        """以前把内容拼成字符串再用全角空格切回来，内容里有空格就串列。

        这是纯字符串处理，改成按字段填列之后就不该再有这种风险 ——
        这里钉住 _cell 不做会破坏内容的加工。
        """
        text = "苹果　香蕉　橘子"
        self.assertEqual(_cell(text), "苹果　香蕉　橘子")


class OrderWindowTests(unittest.TestCase):
    """订单列表窗口的构造。它不再走"拼字符串再 split 回来"那条路。

    断言刻意针对**代码形态**而不是字面量：早先一版直接 grep ``orders[:60]``，
    结果我自己在注释里解释"以前是 orders[:60]"就把断言撞红了 —— 注释里
    出现一个字符串，等于这条断言永远无法成立。
    """

    def setUp(self):
        self.source = (Path(__file__).resolve().parent.parent
                       / "src" / "wechat_ai_assistant" / "merchant_ui.py").read_text(encoding="utf-8")
        code = [line.split("#")[0] for line in self.source.splitlines()]
        self.code = "\n".join(code)

    def test_it_no_longer_fills_columns_by_splitting_a_display_string(self):
        """按全角空格切回列会在内容含空格时串列；填充必须是逐字段的。"""
        self.assertNotIn("values=line", self.code)
        self.assertNotIn("tree.insert(\"\", \"end\", values=line", self.source)

    def test_it_no_longer_silently_drops_orders_past_a_limit(self):
        self.assertNotIn("in orders[:60]", self.code)
        self.assertNotIn("in orders[:50]", self.code)

    def test_it_states_how_many_orders_there_are(self):
        self.assertIn("共 {len(orders)} 单", self.code)

    def test_the_full_text_is_one_double_click_away(self):
        self.assertIn("<Double-1>", self.code, "截断后必须能拿回全文，否则等于丢信息")
        self.assertIn("订单详情", self.code)

    def test_it_does_not_create_an_empty_label(self):
        self.assertNotIn("text[:0]", self.code, "算了一整段文字却塞进空标签，是死代码")


if __name__ == "__main__":
    unittest.main()