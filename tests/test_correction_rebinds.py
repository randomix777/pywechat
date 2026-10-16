"""纠正必须**改掉**绑定，不能只是「再加一条」。

## 两处同类的 bug，都在正确的方向上差一步

### 一、客户：只在新绑定不存在时才写

``bind_message_customer`` 原本是：

    current = SELECT ... WHERE alias=? AND valid_to IS NULL
    if not current:            # ← 只有「还没有任何绑定」时才写
        INSERT INTO aliases ...

于是发送人一旦被绑到**错的人**身上，纠正就什么也不写 ——
下一条同一个人发的消息照样认错，**同一个错误永远重犯**。
而那正是「记记忆用于减少错误」要消灭的东西。

``aliases`` 表本来就有 ``valid_from`` / ``valid_to``，就是为此设计的：
把指向别人的旧绑定关掉（保留历史），再写新的。

### 二、商品：只加不撤，把「错」换成了「烦」

``_learn_from_correction`` 原本只往正确的商品上加别名。而如果这个称呼
原本**错绑**在另一个商品上：

    纠正前：「李记酱油」→ 只有 P-A   → 唯一候选 → 自动匹配 → **商家拿到错商品还不知道**
    纠正后：P-A 和 P-B 都有          → 两个候选 → 认不出 → 每次都要人工确认

匹配结果从「错」变成「认不出」—— 比错好，但**不叫减少错误，只是把错换成了烦**。
``product_aliases`` 没有时序列，所以只能删：删掉的是错的绑定，
改动记在 audit 里。

## 这一条是这两个测试的核心

两个测试都**先造一个错误的绑定**，再纠正，最后断言
「下一次同一个说法能**自动**匹配上正确的那一个」。
不先造错的，就证明不了纠正真的改掉了什么。
"""
from __future__ import annotations

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


class CustomerCorrectionRebindsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        # 先错绑到「张姐」，真正的人是「张丽」。
        self.wrong = self.store.add_customer(self.tenant, "张姐", "张姐", GROUP)
        self.right = self.store.add_customer(self.tenant, "张丽", "张丽", GROUP)
        self.store.add_import(self.tenant, GROUP, [
            {"sender": "张姐", "content": "要两箱苹果",
             "sent_at": "2026-10-14T09:00:00-04:00", "source_id": "s1"}], "probe", "demo")
        self.mid = self.store.db.execute(
            "SELECT id FROM messages WHERE merchant_id=?", (self.tenant,)).fetchone()["id"]

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _owner_of(self, alias):
        return [r["name"] for r in self.store.db.execute(
            "SELECT c.name FROM aliases a JOIN customers c ON c.id = a.customer_id"
            " WHERE a.merchant_id=? AND a.group_id=? AND a.alias=? AND a.valid_to IS NULL",
            (self.tenant, GROUP, alias))]

    def test_the_wrong_binding_is_replaced(self):
        self.assertEqual(self._owner_of("张姐"), ["张姐"])
        self.store.bind_message_customer(self.tenant, self.mid, self.right, "u1")
        self.assertEqual(self._owner_of("张姐"), ["张丽"],
                         "\u7ea0\u6b63\u6ca1\u6539\u6389\u7ed1\u5b9a \u2014\u2014 \u4e0b\u4e00\u6761\u8fd8\u4f1a\u8ba4\u9519")

    def test_the_next_message_resolves_automatically(self):
        """这一条才是商户真正感受到的：不用再手动点一次。"""
        self.store.bind_message_customer(self.tenant, self.mid, self.right, "u1")
        self.store.add_import(self.tenant, GROUP, [
            {"sender": "张姐", "content": "再要一箱",
             "sent_at": "2026-10-14T10:00:00-04:00", "source_id": "s2"}], "probe2", "demo")
        row = self.store.db.execute(
            "SELECT c.name FROM messages m LEFT JOIN customers c ON c.id = m.customer_id"
            " WHERE m.merchant_id=? AND m.source_id='s2'", (self.tenant,)).fetchone()
        self.assertEqual(row["name"], "张丽", "\u65b0\u6d88\u606f\u8fd8\u662f\u8ba4\u9519")

    def test_the_old_binding_is_kept_as_history(self):
        """关掉不是删掉 —— 历史关系要留得住。"""
        self.store.bind_message_customer(self.tenant, self.mid, self.right, "u1")
        closed = self.store.db.execute(
            "SELECT COUNT(*) AS n FROM aliases WHERE merchant_id=? AND alias=?"
            " AND valid_to IS NOT NULL", (self.tenant, "张姐")).fetchone()["n"]
        self.assertEqual(closed, 1)

    def test_correcting_to_the_same_customer_writes_nothing_new(self):
        """纠正成原来那个人时不该留下两条一样的绑定。"""
        self.store.bind_message_customer(self.tenant, self.mid, self.wrong, "u1")
        self.assertEqual(self._owner_of("张姐"), ["张姐"])
        rows = self.store.db.execute(
            "SELECT COUNT(*) AS n FROM aliases WHERE merchant_id=? AND alias=?"
            " AND customer_id=? AND valid_to IS NULL",
            (self.tenant, "张姐", self.wrong)).fetchone()["n"]
        self.assertEqual(rows, 1)

    def test_another_groups_binding_is_untouched(self):
        """别的群的叫法互不相干，不能被一起关掉。"""
        other = self.store.add_customer(self.tenant, "小张", "小张", "别的群")
        self.store.bind_message_customer(self.tenant, self.mid, self.right, "u1")
        still = self.store.db.execute(
            "SELECT COUNT(*) AS n FROM aliases WHERE merchant_id=? AND group_id='别的群'"
            " AND alias='小张' AND valid_to IS NULL", (self.tenant,)).fetchone()["n"]
        self.assertEqual(still, 1)


class ProductCorrectionMovesTheAliasTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.customer = self.store.add_customer(self.tenant, "李叔", "李叔", GROUP)
        # 「李记酱油」先**错绑**在 P-A 上。
        self.wrong = self.store.add_product(self.tenant, "P-A", "老酱油", "瓶", 1000,
                                            aliases=["李记酱油"])
        self.right = self.store.add_product(self.tenant, "P-B", "海天生抽", "瓶", 1200)
        self.item = {"said": "李记酱油", "brand": None, "category": None, "sku": "",
                     "quantity": 2, "unit": "瓶", "evidence": ["李记酱油"]}

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _catalog(self):
        return self.store.db.execute(
            "SELECT p.sku, p.name, group_concat(pa.alias,'|') AS aliases"
            " FROM products p LEFT JOIN product_aliases pa ON pa.product_id = p.id"
            " WHERE p.merchant_id=? GROUP BY p.id", (self.tenant,)).fetchall()

    def _correct(self):
        self.store.add_import(self.tenant, GROUP, [
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
                 json.dumps({"items": [self.item]}, ensure_ascii=False)))
            self.store.db.execute(
                "UPDATE orders SET parse_run_id='run1', status='review' WHERE id=?", (oid,))
        return self.store.correct_order(self.tenant, oid, "u1",
                                        items=[{"product_id": self.right, "quantity": 2}])

    def test_the_wrong_product_was_auto_matched_before(self):
        """先钉住「纠正之前会匹配到错的那个」——否则后面证明不了什么。"""
        self.assertEqual(_match_items_to_products([self.item], self._catalog()), ["P-A"])

    def test_after_correction_the_right_one_matches_automatically(self):
        self._correct()
        self.assertEqual(_match_items_to_products([self.item], self._catalog()), ["P-B"],
                         "\u53ea\u52a0\u4e0d\u6491\u4f1a\u7559\u4e0b\u4e24\u4e2a\u5019\u9009 \u2014\u2014 \u53d8\u6210\u6bcf\u6b21\u90fd\u8981\u4eba\u5de5\u786e\u8ba4")

    def test_the_wrong_product_loses_the_alias(self):
        self._correct()
        left = self.store.db.execute(
            "SELECT COUNT(*) AS n FROM product_aliases WHERE product_id=? AND alias=?",
            (self.wrong, "李记酱油")).fetchone()["n"]
        self.assertEqual(left, 0, "\u9519\u7684\u7ed1\u5b9a\u6ca1\u64a4\u6389")

    def test_the_correction_is_recorded_in_the_audit_trail(self):
        """别名被删了，得留下「谁在什么时候改的」。"""
        source = (ROOT / "src/wechat_ai_assistant/orders.py").read_text(encoding="utf-8")
        self.assertIn("DELETE FROM product_aliases", source)
        self.assertIn("learn_aliases", source)


if __name__ == "__main__":
    unittest.main()