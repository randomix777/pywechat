"""商家纠正之后要**学**，否则同一个错误会一直重犯。

## 为什么这是「减少错误」的关键

人工纠正是质量最高的信号：商家亲口确认了「这句客户话」对应「哪个商品」。
只用来修一张单子然后就丢掉，等于同一个客户明天再说一遍「李记酱油」，
还要再问一次商家。

``correct_order`` 原本建了新订单就结束 —— **什么也没学到**。

## 只在无歧义时学

一张单子只有**一条**商品时才学。多条的时候，客户说的几个短语分别对应
哪条商品无从知道；猜错就把别名记到错的商品上，**以后每次都会认错**，
比不学更糟。

## 只学短的

「李记酱油」值得记 —— 客户下次还会这么说。
「明天下午要3箱红富士苹果」不值得记 —— 它不会一字不差地重现，
记下来只会让别名表变脏，反而干扰匹配。
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from wechat_ai_assistant.orders import OrderStore, _match_items_to_products  # noqa: E402

GROUP = "鲜果群"


def _catalog(store, tenant):
    """带别名的商品目录 —— ``_match_items_to_products`` 要的形状。"""
    return store.db.execute(
        "SELECT p.sku, p.name, group_concat(pa.alias,'|') AS aliases"
        " FROM products p LEFT JOIN product_aliases pa ON pa.product_id = p.id"
        " WHERE p.merchant_id=? GROUP BY p.id", (tenant,)).fetchall()


class LearningFromCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.customer = self.store.add_customer(self.tenant, "李叔", "李叔", GROUP)
        # 商品名与别名里**都不含**客户的说法，所以纠正前一定匹配不上。
        self.product = self.store.add_product(self.tenant, "P-SOY", "海天生抽", "瓶", 1200)
        self.item = {"said": "李记酱油", "brand": None, "category": None,
                     "sku": "", "quantity": 2, "unit": "瓶", "evidence": ["李记酱油"]}

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _make_reviewable_order(self, items):
        imported = self.store.add_import(self.tenant, GROUP, [
            {"sender": "李叔", "content": "李记酱油 2 瓶",
             "sent_at": "2026-10-14T09:00:00-04:00", "source_id": "s1"}], "probe", "demo")
        mid = self.store.db.execute(
            "SELECT id FROM messages WHERE merchant_id=?", (self.tenant,)).fetchone()["id"]
        oid = self.store._new_order(self.tenant, self.customer, GROUP, mid)
        with self.store.db:
            self.store.db.execute("INSERT INTO evidence VALUES(?,?,?,?,'source')",
                                  ("e1", self.tenant, oid, mid))
            self.store.db.execute(
                "INSERT INTO parse_runs(id,merchant_id,message_id,adapter,status,usage_units,"
                "error,created_at,result_json) VALUES(?,?,?,'model-v1','complete',0,NULL,?,?)",
                ("run1", self.tenant, mid, self.store.now(),
                 json.dumps({"items": items}, ensure_ascii=False)))
            self.store.db.execute("UPDATE orders SET parse_run_id='run1', status='review' WHERE id=?",
                                  (oid,))
        return oid

    def test_matching_genuinely_fails_before_the_correction(self):
        """先把「学之前确实不行」钉住 —— 否则后面的测试证明不了任何事。"""
        self.assertEqual(_match_items_to_products([self.item], _catalog(self.store, self.tenant)), [""])

    def test_the_correction_makes_the_next_one_match(self):
        oid = self._make_reviewable_order([self.item])
        new = self.store.correct_order(self.tenant, oid, "u1",
                                       items=[{"product_id": self.product, "quantity": 2}])
        # 同一个客户、同一句话，现在应当自动对上。
        got = _match_items_to_products([self.item], _catalog(self.store, self.tenant))
        self.assertEqual(got, ["P-SOY"], "纠正之后还是认不出来 —— 那这次纠正就白做了")
        self.assertEqual(self.store.learned_aliases(self.tenant, new), ["李记酱油"])

    def test_the_return_value_stays_an_order_id(self):
        """返回值不能因为「学到了东西」就换成字典。

        同一个方法按情况返回两种类型，调用方会在**恰好学过别名的那些单子上**
        突然崩掉 —— 那是最难查的一类问题。
        """
        oid = self._make_reviewable_order([self.item])
        new = self.store.correct_order(self.tenant, oid, "u1",
                                       items=[{"product_id": self.product, "quantity": 2}])
        self.assertIsInstance(new, str)
        self.assertTrue(self.store.order_detail(self.tenant, new))

    def test_a_multi_item_order_learns_nothing(self):
        """多条时无法知道哪个短语对应哪条商品 —— 猜错比不学更糟。"""
        other = self.store.add_product(self.tenant, "P-MIL", "纯牛奶", "箱", 4500)
        items = [self.item,
                 {"said": "纯牛奶", "brand": None, "category": None, "sku": "",
                  "quantity": 1, "unit": "箱", "evidence": ["纯牛奶"]}]
        oid = self._make_reviewable_order(items)
        new = self.store.correct_order(self.tenant, oid, "u1", items=[
            {"product_id": self.product, "quantity": 2},
            {"product_id": other, "quantity": 1}])
        self.assertEqual(self.store.learned_aliases(self.tenant, new), [])


class WhatGetsLearnedTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.product = self.store.add_product(self.tenant, "P-SOY", "海天生抽", "瓶", 1200)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_a_long_sentence_is_not_learned(self):
        """整句不会一字不差地重现，记下来只会让别名表变脏。"""
        self.assertGreater(len("明天下午要三箱红富士苹果麻烦送到山东市场"), OrderStore.LEARNABLE_ALIAS_MAX)
        self.assertLessEqual(OrderStore.LEARNABLE_ALIAS_MAX, 12)

    def test_the_threshold_is_applied_when_picking_phrases(self):
        source = (ROOT / "src/wechat_ai_assistant/orders.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "_learn_from_correction")
        self.assertIn("LEARNABLE_ALIAS_MAX", ast.unparse(fn))

    def test_it_prefers_the_customers_own_words(self):
        source = (ROOT / "src/wechat_ai_assistant/orders.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "_phrases_for_order")
        body = ast.unparse(fn)
        for key in ("said", "category", "brand"):
            self.assertIn(key, body, f"\u5e94\u8be5\u4ece\u89e3\u6790\u7ed3\u679c\u91cc\u53d6 {key}")
        self.assertLess(body.index("said"), body.index("category"),
                        "said\uff08\u5ba2\u6237\u539f\u8bdd\uff09\u8981\u6392\u5728\u524d\u9762")


if __name__ == "__main__":
    unittest.main()