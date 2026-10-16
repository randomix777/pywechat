"""交给模型的参考信息必须**带上别名**。

## 出过什么事

解析时给模型的上下文里：

    "customer": {"id":..., "name":...}      只有当前这个人
    "products": [{"sku","name","unit"}]     ← 没有 aliases

后果是**商家教的东西模型看不到**：商家导入时给了别名、纠正时也学了别名，
模型手里却只有「海天生抽」四个字 —— 客户说「李记酱油」它照样匹配不上。
商家会觉得「我教了呀，怎么还是认不出来」。

客户名单则完全没有。客户身份没认出来时模型手里一个候选都没有，
只能编一个名字或者干脆不填。

## 现在的行为

* ``_catalog_for_model`` —— 商品带 aliases
* ``_roster_for_model`` —— 客户名单带 aliases，按群过滤
* 提示词明确要求：**对不上就填 null 并写进 ambiguities，绝对不要编**
"""
from __future__ import annotations

import ast
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from wechat_ai_assistant.orders import OrderStore  # noqa: E402

GROUP = "鲜果群"
PLUGIN = ROOT / "packages/order_agent/src/order_agent/plugin.py"


class CatalogForModelTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.sku = self.store.add_product(self.tenant, "P-SOY", "海天生抽", "瓶", 1200,
                                          aliases=["李记酱油"])

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_the_alias_reaches_the_model(self):
        """这一条就是整个反馈闭环能不能成立的关键。"""
        catalog = self.store._catalog_for_model(self.tenant)
        entry = next(c for c in catalog if c["sku"] == "P-SOY")
        self.assertIn("李记酱油", entry.get("aliases") or [],
                      "\u522b\u540d\u6ca1\u7ed9\u6a21\u578b \u2014\u2014 \u5546\u5bb6\u6559\u4e86\u4e5f\u767d\u6559")

    def test_the_official_name_is_not_repeated_as_an_alias(self):
        """name 已经在 name 字段里了，别名里再出现一次只是浪费 token。"""
        catalog = self.store._catalog_for_model(self.tenant)
        entry = next(c for c in catalog if c["sku"] == "P-SOY")
        self.assertNotIn("海天生抽", entry.get("aliases") or [])

    def test_a_product_without_aliases_has_no_aliases_key(self):
        self.store.add_product(self.tenant, "P-MIL", "纯牛奶", "箱", 4500)
        catalog = self.store._catalog_for_model(self.tenant)
        entry = next(c for c in catalog if c["sku"] == "P-MIL")
        self.assertNotIn("aliases", entry)

    def test_it_stays_within_the_limit(self):
        self.assertGreater(OrderStore.CATALOG_LIMIT, 0)
        source = (ROOT / "src/wechat_ai_assistant/orders.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "_catalog_for_model")
        self.assertIn("LIMIT", ast.unparse(fn).upper(),
                      "\u76ee\u5f55\u5f88\u5927\u65f6\u4e0d\u80fd\u628a\u6574\u672c\u585e\u7ed9\u6a21\u578b")


class RosterForModelTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.cid = self.store.add_customer(self.tenant, "李叔", "李叔", GROUP)
        self.store._bind_customer_alias(self.tenant, self.cid, GROUP, "李老板")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_the_roster_is_handed_over(self):
        roster = self.store._roster_for_model(self.tenant, GROUP)
        entry = next(c for c in roster if c["name"] == "李叔")
        self.assertEqual(entry["id"], self.cid)

    def test_customer_aliases_reach_the_model(self):
        roster = self.store._roster_for_model(self.tenant, GROUP)
        entry = next(c for c in roster if c["name"] == "李叔")
        self.assertIn("李老板", entry.get("aliases") or [])

    def test_a_wildcard_alias_applies_in_every_group(self):
        """导入时没指定群 = 通配，任何群都该认得出。"""
        self.store._bind_customer_alias(self.tenant, self.cid, "", "老李")
        roster = self.store._roster_for_model(self.tenant, "别的群")
        entry = next(c for c in roster if c["name"] == "李叔")
        self.assertIn("老李", entry.get("aliases") or [])

    def test_another_groups_alias_is_not_leaked(self):
        """A 群的叫法不该让 B 群也用 —— 那正是要按群隔离的原因。"""
        roster = self.store._roster_for_model(self.tenant, "别的群")
        entry = next(c for c in roster if c["name"] == "李叔")
        self.assertNotIn("李老板", entry.get("aliases") or [])


class PromptSaysWhatToDoTests(unittest.TestCase):
    """光给数据不够 —— 提示词得说清「对不上就别编」。"""

    def setUp(self):
        self.prompt = PLUGIN.read_text(encoding="utf-8")

    def test_it_says_aliases_are_as_trustworthy_as_the_name(self):
        self.assertIn("aliases", self.prompt)
        self.assertIn("李记酱油", self.prompt, "\u8981\u4e3e\u4f8b\u5b50\uff0c\u5426\u5219\u6a21\u578b\u4e0d\u77e5\u9053\u522b\u540d\u8be5\u600e\u4e48\u7528")

    def test_it_describes_the_customer_roster(self):
        self.assertIn("context.customers", self.prompt)

    def test_it_forbids_making_up_a_customer(self):
        """认错人比认不出严重得多：认不出商家点一下，认错了货可能已经送出去。"""
        self.assertIn("不要编一个客户名字", self.prompt)

    def test_it_says_what_to_do_when_unsure(self):
        self.assertIn("customer_alias", self.prompt)
        self.assertIn("ambiguities", self.prompt)


if __name__ == "__main__":
    unittest.main()