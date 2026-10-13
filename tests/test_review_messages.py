"""核对页数据源的回归测试：所选会话的最新消息只读不碰解析。

本文件只用内存库，不碰微信不联网。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyweixin.orders import OrderStore  # noqa: E402


def _seed(store, tenant):
    store.add_import(
        tenant, "鲜果群",
        [{"sender": "张姐", "content": f"苹果{i}箱", "kind": "text",
          "sent_at": f"2026-10-2 12:{10 + i:02d}", "source_id": f"g{i}"} for i in range(5)],
        "seed",
    )
    store.add_import(
        tenant, "别群",
        [{"sender": "李女士", "content": "香蕉", "kind": "text",
          "sent_at": "2026-10-2 09:01", "source_id": "o1"}],
        "seed",
    )


class RecentMessagesTests(unittest.TestCase):
    def setUp(self):
        self.store = OrderStore(":memory:")
        self.tenant = self.store.create_merchant("测试店铺")["merchant_id"]
        _seed(self.store, self.tenant)

    def tearDown(self):
        self.store.close()

    def test_latest_first_with_limit(self):
        rows = self.store.recent_messages(self.tenant, ["鲜果群"], 3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["content"], "苹果4箱")
        self.assertEqual(rows[0]["sender"], "张姐")
        self.assertEqual(rows[0]["group"], "鲜果群")
        self.assertIn("sent_at", rows[0])

    def test_other_groups_are_excluded(self):
        rows = self.store.recent_messages(self.tenant, ["鲜果群"], 50)
        self.assertTrue(rows)
        self.assertTrue(all(row["group"] == "鲜果群" for row in rows))

    def test_empty_or_unknown_groups_give_empty(self):
        self.assertEqual(self.store.recent_messages(self.tenant, [], 50), [])
        self.assertEqual(self.store.recent_messages(self.tenant, None, 50), [])
        self.assertEqual(self.store.recent_messages(self.tenant, ["不存在的群"], 50), [])


class ReviewXlsxTests(unittest.TestCase):
    """xlsx 手写包的回归测试：标准库 zip，保证 Excel 能打开。"""

    def test_workbook_contains_headers_and_rows(self):
        import zipfile

        from pyweixin.merchant_ui import build_review_xlsx

        rows = [
            {"group": "鲜果群", "sender": "张姐<>&\"",
             "content": "苹果2箱", "kind": "文本", "sent_at": "2026-10-02 12:10"},
        ]
        names = zipfile.ZipFile(__import__("io").BytesIO(build_review_xlsx(rows))).namelist()
        self.assertIn("xl/worksheets/sheet1.xml", names)
        self.assertIn("xl/workbook.xml", names)
        sheet = zipfile.ZipFile(
            __import__("io").BytesIO(build_review_xlsx(rows))
        ).read("xl/worksheets/sheet1.xml").decode("utf-8")
        for text in ("会话", "发送人", "内容", "鲜果群", "苹果2箱"):
            self.assertIn(text, sheet)
        # 特殊字符必须转义，否则 Excel 报文件损坏。
        self.assertIn("张姐&lt;&gt;&amp;&quot;", sheet)
        self.assertNotIn("张姐<>&\"", sheet)

    def test_empty_rows_still_valid_workbook(self):
        import zipfile

        from pyweixin.merchant_ui import build_review_xlsx

        sheet = zipfile.ZipFile(
            __import__("io").BytesIO(build_review_xlsx([]))
        ).read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("会话", sheet)


class ReviewDocxTests(unittest.TestCase):
    """docx 手写包的回归测试：标准库 zip，保证 Word 能打开。"""

    def test_document_contains_table_and_rows(self):
        import zipfile

        from pyweixin.merchant_ui import build_review_docx

        rows = [
            {"group": "鲜果群", "sender": "张姐<>&\"",
             "content": "苹果2箱", "kind": "文本", "sent_at": "2026-10-02 12:10"},
        ]
        archive = zipfile.ZipFile(__import__("io").BytesIO(build_review_docx(rows)))
        self.assertIn("word/document.xml", archive.namelist())
        document = archive.read("word/document.xml").decode("utf-8")
        for text in ("会话", "发送人", "内容", "鲜果群", "苹果2箱"):
            self.assertIn(text, document)
        self.assertIn("张姐&lt;&gt;&amp;&quot;", document)
        self.assertNotIn("张姐<>&\"", document)

    def test_empty_rows_still_valid_document(self):
        import zipfile

        from pyweixin.merchant_ui import build_review_docx

        document = zipfile.ZipFile(
            __import__("io").BytesIO(build_review_docx([]))
        ).read("word/document.xml").decode("utf-8")
        self.assertIn("会话", document)


if __name__ == "__main__":
    unittest.main()
