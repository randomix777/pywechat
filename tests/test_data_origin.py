"""屏幕上的东西是从哪来的。

商户必须能分清两件**互相独立**的事：

* 读到的消息是不是真的（演示数据源 vs 真实微信）
* 订单是不是模型分析出来的（真模型 vs 按固定规则整理的）

「真实微信 + 规则引擎」是完全可能的组合 —— 没登录、没配密钥、额度用完，
或者 DeepSeek 调用失败退回本地规则。这时候消息是真读到的、订单也是真的，
只是**不是模型理解出来的**。

界面上以前只有一句「当前为演示数据，非真实分析结果」，挂在待发送回执后面。
那个说法把两件事糊成一件：上面那个组合会被说成"演示数据"，商户会以为
读到的东西全是假的，然后去重新读一遍真实数据 —— 白费工夫。

另外，正常使用时这句话必须**不显示**。一直挂着警告，真警告就没人看了。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyweixin.dashboard import HomeView  # noqa: E402
from pyweixin.merchant_ui import MerchantWindow  # noqa: E402


class OriginNoteTests(unittest.TestCase):
    def note(self, simulated: bool, model_executed: bool) -> str:
        return MerchantWindow._data_origin_note(
            HomeView(simulated=simulated, model_executed=model_executed)
        )

    def test_real_data_and_real_model_says_nothing(self):
        """正常情况不必挂警告。一直亮着，真警告就没人看了。"""
        self.assertEqual(self.note(False, True), "")

    def test_real_messages_with_rule_engine_is_not_called_demo_data(self):
        """最关键的一条。

        消息是真读到的，只是没跑模型。这时候说"演示数据"是**错的** ——
        商户会以为读到的东西全是假的。
        """
        text = self.note(False, False)
        self.assertNotIn("演示", text, "真实消息被说成演示数据")
        self.assertIn("不是模型分析出来的", text)

    def test_a_demo_source_says_so_explicitly(self):
        text = self.note(True, False)
        self.assertIn("不是真实微信里的内容", text)

    def test_the_two_facts_are_both_shown_when_both_apply(self):
        text = self.note(True, False)
        self.assertIn("演示", text)
        self.assertIn("不是模型分析出来的", text)

    def test_a_demo_source_with_a_real_model_only_flags_the_source(self):
        """消息是假的但分析确实跑了模型 —— 只说消息的事。"""
        text = self.note(True, True)
        self.assertIn("不是真实微信里的内容", text)
        self.assertNotIn("不是模型分析出来的", text)

    def test_it_never_blames_the_data_when_the_data_is_real(self):
        for model in (True, False):
            with self.subTest(model_executed=model):
                self.assertNotIn("演示", self.note(False, model))

    def test_it_contains_no_technical_words(self):
        for simulated in (True, False):
            for model in (True, False):
                text = self.note(simulated, model).lower()
                for term in ("token", "api", "key", "provider", "session",
                             "model", "sqlite", "simulated"):
                    self.assertNotIn(term, text, f"{term} 出现在商户可见文案里")


class HomeViewCarriesTheFactTests(unittest.TestCase):
    def test_the_view_can_say_whether_a_model_ran(self):
        view = HomeView()
        self.assertTrue(hasattr(view, "model_executed"))
        self.assertFalse(view.model_executed, "默认必须是'没有证据表明跑了模型'")

    def test_model_executed_is_independent_of_simulated(self):
        """两个字段必须能取不同的值，否则又会被糊回一句。"""
        view = HomeView(simulated=False, model_executed=False)
        self.assertFalse(view.simulated)
        self.assertFalse(view.model_executed)
        view = HomeView(simulated=False, model_executed=True)
        self.assertFalse(view.simulated)
        self.assertTrue(view.model_executed)


class AttachmentTests(unittest.TestCase):
    """来源说明不能再挂在「待发送回执」后面 —— 那会让人以为只有回执是假的。"""

    def setUp(self):
        self.source = (Path(__file__).resolve().parent.parent
                       / "src" / "pyweixin" / "merchant_ui.py").read_text(encoding="utf-8")
        self.code = "\n".join(line.split("#")[0] for line in self.source.splitlines())

    def test_the_vague_wording_is_gone(self):
        self.assertNotIn("当前为演示数据，非真实分析结果", self.code)

    def test_the_origin_note_has_its_own_line(self):
        self.assertIn("st_origin", self.code)


if __name__ == "__main__":
    unittest.main()