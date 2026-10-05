"""放行判据不能只看「本次读到几条」。

## 出过什么事

界面自相矛盾：按钮放出来了，一点却弹「还没有读到消息，请先点开始读取」。

原因是有**两处**各自判断放行，而且都只看 ``result.total_messages``：

* ``_render_read_state`` 决定「下一步」按钮显不显示
* ``_go_next_from_history`` 决定点了之后放不放行

更新时发现没有新消息，``total_messages`` 就是 0 —— 但库里可能已经积累
了几百条。于是第一处（我改过了）把按钮放出来，第二处（还是老判据）
把它拦回去。商户刚读完一大群，被告知「还没有读到消息」。

摘要那个按钮卡的是同一个坑。

## 现在的判据

统一走 ``_has_messages_for``：本次读到了 → 有；本次 0 条但库里已有 → **也算有**。
真的什么都没有时才拦 —— 那才是死路。
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

MERCHANT_UI = ROOT / "src/pyweixin/merchant_ui.py"


class SharedGateTests(unittest.TestCase):
    def setUp(self):
        self.source = MERCHANT_UI.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def _fn(self, name):
        return next(n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)
                    and n.name == name)

    def test_the_gate_exists_and_is_shared(self):
        """必须有一个共同判据，不能各判各的。"""
        fn = self._fn("_has_messages_for")
        body = ast.unparse(fn)
        self.assertIn("total_messages", body)
        self.assertIn("_accumulated_coverage", body,
                      "\u672c\u6b21 0 \u6761\u4f46\u5e93\u91cc\u6709\uff0c\u4e5f\u7b97\u6709\u6d88\u606f")

    def test_both_entry_points_use_the_shared_gate(self):
        """两处都必须走它，否则又会一处放行一处拦。"""
        for name in ("_go_next_from_history", "_on_summarize"):
            body = ast.unparse(self._fn(name))
            self.assertIn("_has_messages_for", body,
                          f"{name} \u6ca1\u7528\u5171\u540c\u5224\u636e \u2014\u2014 \u4e24\u5904\u4f1a\u4e0d\u4e00\u81f4")
            self.assertNotIn("not result.total_messages", body,
                             f"{name} \u53c8\u53ea\u770b\u672c\u6b21\u8bfb\u5230\u51e0\u6761\u4e86")

    def test_the_gate_treats_no_result_as_a_no(self):
        fn = self._fn("_has_messages_for")
        body = ast.unparse(fn)
        self.assertIn("result is None", body, "\u6ca1\u6709\u7ed3\u679c\u65f6\u8981\u62e6\u4f4f")


class TheGateActuallyWorksTests(unittest.TestCase):
    """真的调一次 —— 静态检查证明不了行为。"""

    def setUp(self):
        import types
        self.types = types
        tree = ast.parse(MERCHANT_UI.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "_has_messages_for")
        # \u628a\u65b9\u6cd5\u5355\u72ec\u6267\u884c\uff0c\u6302\u4e00\u4e2a\u5047\u7684\u7d2f\u79ef\u67e5\u8be2
        self.ns: dict = {}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<gate>", "exec"), self.ns)
        self.gate = self.ns["_has_messages_for"]

    def _window(self, accumulated):
        class W:
            _accumulated_coverage = staticmethod(lambda result: accumulated)
        return W()

    def test_this_read_found_messages(self):
        result = self.types.SimpleNamespace(total_messages=3)
        self.assertTrue(self.gate(self._window({}), result))

    def test_this_read_found_nothing_but_the_store_has_messages(self):
        """这就是出问题的那种情况。"""
        result = self.types.SimpleNamespace(total_messages=0)
        window = self._window({"群A": (500, "2026-01-01", "2026-10-14")})
        self.assertTrue(self.gate(window, result),
                        "\u672c\u6b21 0 \u6761\u4f46\u5e93\u91cc\u6709 500 \u6761 \u2014\u2014 \u4e0d\u80fd\u62e6")

    def test_nothing_anywhere_is_still_blocked(self):
        result = self.types.SimpleNamespace(total_messages=0)
        self.assertFalse(self.gate(self._window({}), result))

    def test_no_result_is_blocked(self):
        self.assertFalse(self.gate(self._window({}), None))


if __name__ == "__main__":
    unittest.main()