"""``MerchantWindow`` 不是 Tk 控件 —— 定时器必须挂在 ``self.root`` 上。

## 出过什么事

摘要的「已等待 N 秒」停在 0 秒不动。根因不是计时逻辑，是**调用了一个不存在的方法**：

``MerchantWindow`` 是个普通 Python 类，它自己持有 ``self.root = tk.Tk()``，
**本身没有 ``after``**。全文件原本 4 处都写 ``self.root.after(...)``，
只有我新加的摘要代码写成了 ``self.after(...)``：

* ``self.after(500, self._summary_tick)`` → ``AttributeError``
  → 计时器设完「0 秒」就死掉，之后再不更新。
* ``self.after(0, lambda: self._summary_finished(...))`` → ``AttributeError``
  → **``_summary_finished`` 永远不执行**：界面卡在「正在整理……」，
  ``_summary_running`` 永远为 True，按钮从此再也点不动。

第二个比第一个严重得多：商户点了按钮，界面一直显示在处理，
既没有结果也没有报错，而且**再也点不了第二次**。

## 为什么单测没抓到

``after`` 是 Tk 的方法，单测里没有真窗口，那一行根本不会被执行到。
只有真窗口 + 真点击才会走到。

所以这里做**静态检查**：本文件里所有 ``after(`` 调用都必须是 ``self.root.after(``。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

MERCHANT_UI = ROOT / "src/pyweixin/merchant_ui.py"


class TkCallsAreAnchoredTests(unittest.TestCase):
    def setUp(self):
        self.source = MERCHANT_UI.read_text(encoding="utf-8")
        self.lines = self.source.splitlines()

    def test_the_class_is_not_a_widget(self):
        """前提：这个类不是 Tk 控件。哪天它改成继承 tk.Tk，这条会提醒复核。"""
        tree = ast.parse(self.source)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "MerchantWindow")
        self.assertEqual([ast.unparse(b) for b in cls.bases], [],
                         "MerchantWindow 变成 Tk 子类了 —— 那么 self.after 可用，"
                         "但请先确认所有定时器都仍然挂在同一个对象上")
        self.assertIn("self.root = tk.Tk()", self.source)

    def test_every_after_call_goes_through_root(self):
        """本文件里不许出现裸的 ``self.after(``。"""
        offenders = []
        for index, line in enumerate(self.lines, 1):
            if re.search(r"(?<!root)self\.after\(", line):
                offenders.append(f"L{index}: {line.strip()[:70]}")
        self.assertEqual(offenders, [],
                         f"\u8fd9\u4e9b\u5b9a\u65f6\u5668\u6302\u9519\u4e86\u5bf9\u8c61"
                         f"\uff08\u5e94\u8be5\u662f self.root.after\uff09\uff1a{offenders}")

    def test_the_summary_timer_reschedules_through_root(self):
        """具体盯住摘要计时器 —— 它就是出问题的那一处。"""
        tick = next(n for n in ast.walk(ast.parse(self.source))
                    if isinstance(n, ast.FunctionDef) and n.name == "_summary_tick")
        self.assertIn("self.root.after", ast.unparse(tick))

    def test_finishing_is_scheduled_through_root(self):
        """这一处出错会让界面永远停在「正在整理」，比计时器停住更严重。"""
        on_summarize = next(n for n in ast.walk(ast.parse(self.source))
                            if isinstance(n, ast.FunctionDef) and n.name == "_on_summarize")
        body = ast.unparse(on_summarize)
        self.assertIn("self.root.after", body)
        self.assertNotIn("self.after(", body)


class FieldsAreInitialisedTests(unittest.TestCase):
    """摘要用到的字段必须在 __init__ 里先建好。

    页面会因导航重建，重建时如果字段不存在，回调一进来就 AttributeError。
    """

    def setUp(self):
        self.source = MERCHANT_UI.read_text(encoding="utf-8")
        tree = ast.parse(self.source)
        init = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        self.init_source = ast.unparse(init)

    def test_every_summary_field_has_a_default(self):
        for field in ("_summary_running", "_summary_text", "_summary_status",
                      "_summary_bar", "_summary_elapsed", "_summary_started",
                      "_summary_elapsed_label"):
            self.assertIn(field, self.init_source,
                          f"{field} \u6ca1\u6709\u521d\u59cb\u503c\uff0c\u9875\u9762\u91cd\u5efa\u540e\u4f1a AttributeError")


if __name__ == "__main__":
    unittest.main()