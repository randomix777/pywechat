"""「更新」和「读取全部」之后，界面必须说同一件事。

## 出过什么事

同一个位置，两种读法报的是**不同性质**的量：

    读取全部  实际读到 500 条，时间范围 2026-01-01 至 2026-10-14
    更新      更新了 3 条新消息

一个是**总量**，一个是**增量**。商户刚读完一个 500 条的群，一点更新就
看到「更新了 3 条新消息」，无从判断这个群现在到底有多少。

更糟的两种：

* 逐会话明细里，条数那一列在「读取全部」下是 500，在「更新」下变成 3，
  **同一列两种含义**；而日期范围那一栏还是「2026-01-01 至 2026-10-14」——
  3 条消息配 9 个月的跨度。
* 「上次已读完」时那一列显示 **0 条**，看起来像这个群什么都没有，
  而库里其实有几百条。而且那条分支直接 return，表格根本不画，
  顶部说「这次没有读到任何消息」、「下一步」还被藏了 ——
  商户会以为软件坏了。

## 现在的约定

* 条数那一列**始终是总量**，增量写在括号里：`共 503 条（本次 +3）`
* 更新模式的顶部补一行累积总量，和读取全部回答同一个问题
* 「更新发现没有新消息」是**正常结果**，说出来，并且照样放行下一步
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

MERCHANT_UI = ROOT / "src/pyweixin/merchant_ui.py"


class SameColumnMeansTheSameThingTests(unittest.TestCase):
    def setUp(self):
        self.source = MERCHANT_UI.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)
        self.rows = next(n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)
                         and n.name == "_render_coverage_rows")

    def test_the_count_column_comes_from_the_database_total(self):
        body = ast.unparse(self.rows)
        self.assertIn("_accumulated_coverage", body,
                      "\u6761\u6570\u5217\u5fc5\u987b\u6765\u81ea\u5e93\u91cc\u7684\u603b\u91cf")
        self.assertIn("\u5171 ", body, "\u603b\u91cf\u8981\u5199\u6210\u300c\u5171 N \u6761\u300d")

    def test_the_delta_is_labelled_as_a_delta(self):
        body = ast.unparse(self.rows)
        self.assertIn("\u672c\u6b21 +", body,
                      "\u589e\u91cf\u5fc5\u987b\u6807\u660e\u662f\u672c\u6b21\u7684\uff0c\u5426\u5219\u4e0e\u603b\u91cf\u6df7\u6dc6")

    def test_it_still_falls_back_when_the_query_fails(self):
        """查库失败时退回原来的写法，不能什么都不显示。"""
        body = ast.unparse(self.rows)
        self.assertIn("else", body)
        self.assertIn("message_count", body)


class UpdateHeadlineShowsTheTotalTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(MERCHANT_UI.read_text(encoding="utf-8"))
        self.render = next(n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)
                           and n.name == "_render_read_state")

    def test_update_mode_adds_the_accumulated_total(self):
        """更新的 headline 只有增量，必须补一行总量。"""
        source = ast.unparse(self.render)
        self.assertIn("_accumulated_coverage", source)
        self.assertIn("UPDATE", source)

    def test_no_new_messages_is_reported_as_normal(self):
        """「没有新消息」不是「没读到」。"""
        source = ast.unparse(self.render)
        self.assertIn("already_have", source,
                      "\u5fc5\u987b\u5148\u770b\u5e93\u91cc\u5df2\u7ecf\u6709\u591a\u5c11")
        self.assertIn("\u6ca1\u6709\u65b0\u6d88\u606f", source)

    def test_no_new_messages_keeps_the_next_step_available(self):
        """这种时候「下一步」必须留着 —— 库里有几百条，路是通的。"""
        source = ast.unparse(self.render)
        branch = source[source.index("already_have"):]
        branch = branch[:branch.index("elif")] if "elif" in branch else branch
        self.assertIn("nxt", branch, "\u8981\u628a\u300c\u4e0b\u4e00\u6b65\u300d\u653e\u56de\u6765")
        self.assertIn("pack", branch)

    def test_that_branch_still_draws_the_detail_table(self):
        """顶部说「没有新消息」而底下一片空白，商户看不出有多少条。"""
        source = ast.unparse(self.render)
        branch = source[source.index("already_have"):]
        branch = branch[:branch.index("elif")] if "elif" in branch else branch
        self.assertIn("_render_coverage_rows", branch)


class AccumulatedQueryTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(MERCHANT_UI.read_text(encoding="utf-8"))
        self.fn = next(n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)
                       and n.name == "_accumulated_coverage")

    def test_it_groups_by_conversation(self):
        body = ast.unparse(self.fn)
        self.assertIn("group_id", body)
        self.assertIn("GROUP BY", body.upper())

    def test_a_query_failure_does_not_break_the_page(self):
        """一次查询失败不该让读取结果看不见。"""
        self.assertTrue(any(isinstance(n, ast.ExceptHandler) for n in ast.walk(self.fn)),
                        "\u5fc5\u987b\u5305\u4f4f\u5f02\u5e38\uff0c\u5426\u5219\u8bfb\u53d6\u7ed3\u679c\u6574\u9875\u90fd\u753b\u4e0d\u51fa\u6765")

    def test_it_does_not_assume_app_exists(self):
        """渲染会被单测用桩对象直接驱动，桩上没有 app。

        用结构检查而不是字符串匹配：``ast.unparse`` 会把引号统一成单引号，
        查 ``'getattr(self, "app"'`` 永远落空 —— 这个坑这一轮已经踩过两次。
        """
        calls = [n for n in ast.walk(self.fn) if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == "getattr"]
        self.assertTrue(
            any(c.args and ast.unparse(c.args[0]) == "self" and len(c.args) > 1
                and isinstance(c.args[1], ast.Constant) and c.args[1].value == "app"
                for c in calls),
            "\u5fc5\u987b\u7528 getattr(self, 'app', None)\uff0c\u4e0d\u80fd\u76f4\u63a5 self.app")
        self.assertNotIn("self.app", ast.unparse(self.fn))


if __name__ == "__main__":
    unittest.main()