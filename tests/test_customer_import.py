"""客户数据上传。

## 为什么客户名单比商品名单更要紧

群里说话的是**人**。商户认人靠「张姐」「老李」这些称呼，而同一个客户
在不同群里叫法可能不同（A 群叫「张姐」，B 群叫「小张」）。

名单进来了，模型才能把称呼对上人；对不上时也能说「这个称呼我不认识」，
而不是自己编一个人出来。

## 两条约束

1. **别名按群生效**。同一个人在 A 群叫「张姐」、B 群叫「小张」，
   这是两条记录 —— 所以 ``group`` 列参与去重。
2. **重复导入不许让别名膨胀**。``aliases`` 表的查询用的是
   ``valid_to IS NULL``；同一个称呼在同一个群里写入多次，
   认人时就成了「多个候选」，本来能自动认出的消息反而转人工。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from wechat_ai_assistant.orders import OrderStore  # noqa: E402

CSV = """name,group,address,preference,aliases
张姐,鲜果群,山东市场A12,喜欢大箱,张大姐、老张家的
李叔,鲜果群,山东市场B7,,李老板
王姨,酱油群,,,王阿姨
"""


class ImportCustomersTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _aliases(self):
        return {(r["name"], r["group_id"], r["alias"]) for r in self.store.db.execute(
            "SELECT a.alias, a.group_id, c.name FROM aliases a"
            " JOIN customers c ON c.id = a.customer_id WHERE a.merchant_id=?",
            (self.tenant,))}

    def test_it_creates_customers_with_address_and_preference(self):
        result = self.store.import_customers_csv(self.tenant, CSV)
        self.assertEqual(result["created"], 3)
        rows = {r["name"]: r for r in self.store.db.execute(
            "SELECT name, address, preference FROM customers WHERE merchant_id=?",
            (self.tenant,))}
        self.assertEqual(rows["张姐"]["address"], "山东市场A12")
        self.assertEqual(rows["张姐"]["preference"], "喜欢大箱")

    def test_the_name_itself_becomes_an_alias(self):
        """不写这一条，消息里叫「张姐」反而认不出来。"""
        self.store.import_customers_csv(self.tenant, CSV)
        self.assertIn(("张姐", "鲜果群", "张姐"), self._aliases())

    def test_extra_aliases_are_split_on_chinese_punctuation(self):
        self.store.import_customers_csv(self.tenant, CSV)
        aliases = self._aliases()
        self.assertIn(("张姐", "鲜果群", "张大姐"), aliases)
        self.assertIn(("张姐", "鲜果群", "老张家的"), aliases)

    def test_aliases_are_per_group(self):
        """同一个人在两个群里叫法不同 —— 那是两条记录。"""
        self.store.import_customers_csv(self.tenant, CSV)
        self.assertIn(("王姨", "酱油群", "王阿姨"), self._aliases())
        self.assertNotIn(("王姨", "鲜果群", "王阿姨"), self._aliases())

    def test_reimporting_does_not_duplicate_aliases(self):
        """膨胀成多个候选会让本来能自动认出的消息转人工。"""
        self.store.import_customers_csv(self.tenant, CSV)
        before = len(self._aliases())
        result = self.store.import_customers_csv(self.tenant, CSV)
        self.assertEqual(len(self._aliases()), before)
        self.assertEqual(result["created"], 0, "第二次不该再新增客户")
        self.assertEqual(result["updated"], 3)

    def test_a_row_without_a_name_is_skipped_with_a_reason(self):
        """一行脏数据不该让整批失败，但**必须说出来是哪一行**。"""
        result = self.store.import_customers_csv(self.tenant, CSV + ",鲜果群,,,\n")
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["created"], 3, "其余三行照常导入")
        self.assertEqual(len(result["problems"]), 1)
        problem = result["problems"][0]
        self.assertIn("缺少客户称呼", problem)
        self.assertIn("4", problem, "要说清是第几行，否则商户没法回去改")

    def test_the_file_may_have_a_byte_order_mark(self):
        """Excel 导出的 CSV 常带 BOM，不处理的话表头第一个字段会带不可见字符。"""
        result = self.store.import_customers_csv(self.tenant, "\ufeff" + CSV)
        self.assertEqual(result["created"], 3)

    def test_an_empty_file_is_not_an_error(self):
        result = self.store.import_customers_csv(self.tenant, "name,group\n")
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["problems"], [])


class AliasDeduplicationTests(unittest.TestCase):
    """``_bind_customer_alias`` 只管插入去重，不改已有记录。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.cid = self.store.add_customer(self.tenant, "张姐", "张姐", "鲜果群")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_binding_the_same_alias_twice_keeps_one_row(self):
        self.store._bind_customer_alias(self.tenant, self.cid, "鲜果群", "阿姐")
        self.store._bind_customer_alias(self.tenant, self.cid, "鲜果群", "阿姐")
        rows = self.store.db.execute(
            "SELECT COUNT(*) AS n FROM aliases WHERE merchant_id=? AND alias=? AND valid_to IS NULL",
            (self.tenant, "阿姐")).fetchone()
        self.assertEqual(rows["n"], 1)

    def test_an_empty_alias_is_ignored(self):
        self.store._bind_customer_alias(self.tenant, self.cid, "鲜果群", "")
        rows = self.store.db.execute(
            "SELECT COUNT(*) AS n FROM aliases WHERE merchant_id=? AND alias=''",
            (self.tenant,)).fetchone()
        self.assertEqual(rows["n"], 0)


if __name__ == "__main__":
    unittest.main()