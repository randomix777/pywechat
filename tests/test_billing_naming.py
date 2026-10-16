"""额度相关的命名口径 —— 防止「改错」和「当成余额读」。

## 为什么需要这条

计价改成美元（整数微美元）之后，代码里留下一批**名字容易误导**的标识符：

* ``usage_units`` —— 是 **token 数**（input + output）。名字准确，
  改成 ``usage_micro_usd`` 反而是错的。
* ``ai_quota`` / ``ai_used`` —— `plans` 表的**死列**，只写不读，
  而真实余额在 product.db 的 subscriptions 里，单位是微美元。
* ``cost_micro_usd_per_job`` 等 —— 已改对。

真正的风险不是「名字不统一」，而是**有人把 ai_quota 当成余额读**：
它默认 10000，看起来"额度充足"，而实际余额可能已经是 0。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class UsageUnitsIsTokensTests(unittest.TestCase):
    """``usage_units`` 是 token 数，不是金额 —— 别改，也别当额度用。"""

    def setUp(self):
        self.source = (ROOT / "src/wechat_ai_assistant/orders.py").read_text(encoding="utf-8")

    def test_it_is_filled_with_input_plus_output_tokens(self):
        writes = re.findall(r"usage_units\s*=\s*\?\s*,\s*error=NULL", self.source)
        self.assertTrue(writes, "找不到写入 usage_units 的地方")
        self.assertIn('tokens["input"]+tokens["output"]', self.source,
                      "usage_units 应当写的是 token 用量，不是金额")

    def test_it_is_documented_as_tokens(self):
        module_doc = ast.get_docstring(ast.parse(self.source)) or ""
        self.assertIn("usage_units", module_doc,
                      "缺少说明，后人会把它当成额度改掉")
        self.assertIn("token", module_doc)


class DeadQuotaColumnTests(unittest.TestCase):
    """``ai_quota`` 是死列 —— 不能被当成余额读。"""

    def setUp(self):
        self.source = (ROOT / "src/wechat_ai_assistant/orders.py").read_text(encoding="utf-8")

    def test_it_is_never_selected(self):
        """只写不读。哪天有人 SELECT 它，这条就该重新审视。"""
        for line in self.source.splitlines():
            if line.strip().startswith("#"):
                continue          # 注释里会提到 SELECT，那是说明而不是读取
            if "ai_quota" in line or "ai_used" in line:
                self.assertNotIn("SELECT", line.upper(),
                                 "ai_quota 被读了 —— 它是死列，不该参与判断")

    def test_it_is_marked_as_dead_in_the_module_docstring(self):
        module_doc = ast.get_docstring(ast.parse(self.source)) or ""
        self.assertIn("ai_quota", module_doc,
                      "必须标明是死列，否则有人会当成额度读")

    def test_the_warning_names_where_the_real_balance_lives(self):
        self.assertIn("subscriptions", self.source,
                      "必须指出真正的余额在哪，否则读者会以为 ai_quota 就是额度")


class RealBalanceIsMicroUsdTests(unittest.TestCase):
    def test_the_balance_columns_are_named_in_micro_usd(self):
        source = (ROOT / "src/wechat_ai_assistant/product.py").read_text(encoding="utf-8")
        for column in ("quota_micro_usd", "used_micro_usd", "reserved_micro_usd"):
            self.assertIn(column, source, f"{column} 应当是真实的余额列")

    def test_no_bare_units_column_remains_in_the_subscription_schema(self):
        """订阅表里不该再有 quota_units / used_units 这类旧列名。"""
        source = (ROOT / "src/wechat_ai_assistant/product.py").read_text(encoding="utf-8")
        schema = source[source.index("CREATE TABLE IF NOT EXISTS subscriptions"):][:600]
        for stale in ("quota_units", "used_units", "reserved_units"):
            self.assertNotIn(stale, schema, f"{stale} 应当已改名为 *_micro_usd")


if __name__ == "__main__":
    unittest.main()