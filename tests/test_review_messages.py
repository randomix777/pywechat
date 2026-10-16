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


class MessagesMediaTests(unittest.TestCase):
    """导出附件要靠 media_json 里的 md5 关联本机缓存文件。"""

    def setUp(self):
        self.store = OrderStore(":memory:")
        self.tenant = self.store.create_merchant("测试店铺")["merchant_id"]

    def tearDown(self):
        self.store.close()

    def test_media_is_returned_by_id(self):
        result = self.store.add_import(
            self.tenant, "鲜果群",
            [{"sender": "张姐", "content": "图片", "kind": "image",
              "sent_at": "2026-10-2 12:10", "source_id": "i1",
              "media": {"md5": "abc123"}}], "seed")
        mid = result["message_ids"][0]
        media = self.store.messages_media(self.tenant, [mid])
        self.assertEqual(media[mid]["md5"], "abc123")

    def test_unknown_ids_are_absent(self):
        self.assertEqual(self.store.messages_media(self.tenant, ["nope"]), {})
        self.assertEqual(self.store.messages_media(self.tenant, []), {})


class ExportBundleTests(unittest.TestCase):
    """完整包：明细表 + 分类媒体文件夹 + 说明文件，全在一个 zip 里。"""

    def _rows(self):
        return [
            {"id": "m1", "group": "鲜果群", "sender": "张姐",
             "content": "苹果2箱", "kind": "text", "sent_at": "2026-10-02 12:10"},
            {"id": "m2", "group": "鲜果群", "sender": "李叔",
             "content": "［图片］", "kind": "image", "sent_at": "2026-10-02 12:11"},
        ]

    def test_bundle_has_table_media_folder_and_readme(self):
        import io
        import zipfile

        from pyweixin.export_bundle import build_export_bundle

        data, info = build_export_bundle(
            self._rows(), {"m2": {}}, generated_at="2026-10-15 21:00")
        # media_by_id 空 media → 关联不到缓存，图片计数在 missing 里。
        self.assertEqual(info["rows"], 2)
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = archive.namelist()
        self.assertIn("明细.csv", names)
        self.assertIn("导出说明.txt", names)
        csv_text = archive.read("明细.csv").decode("utf-8-sig")
        self.assertIn("会话", csv_text)
        self.assertIn("苹果2箱", csv_text)
        readme = archive.read("导出说明.txt").decode("utf-8")
        self.assertIn("鲜果群", readme)

    def test_bundle_places_image_in_media_folder(self):
        import io
        import tempfile
        import zipfile
        from pathlib import Path

        from pyweixin.export_bundle import build_export_bundle

        with tempfile.TemporaryDirectory() as tmp:
            # 造一个真能被识别的 png 缓存文件，用微信的 md5 命名与关联。
            png = Path(tmp) / "deadbeef.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
            import unittest.mock as mock

            with mock.patch("pyweixin.media_attachments._file_index",
                            return_value={"deadbeef": [png]}), \
                 mock.patch("pyweixin.media_attachments._account_root",
                            return_value=Path(tmp)):
                data, info = build_export_bundle(
                    self._rows(), {"m2": {"md5": "deadbeef"}})
        archive = zipfile.ZipFile(io.BytesIO(data))
        media = [n for n in archive.namelist() if n.startswith("图片/")]
        self.assertEqual(len(media), 1)
        self.assertTrue(media[0].endswith(".png"), media[0])
        self.assertEqual(info["missing"], 0)


if __name__ == "__main__":
    unittest.main()
