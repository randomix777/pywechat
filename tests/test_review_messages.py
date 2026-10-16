"""核对页数据源的回归测试：所选会话的最新消息只读不碰解析。

本文件只用内存库，不碰微信不联网。
"""
from __future__ import annotations

import sys
import tempfile
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

            with mock.patch("pyweixin.media_attachments._scan_caches",
                            return_value=({"deadbeef": [png]}, {})), \
                 mock.patch("pyweixin.media_attachments._account_root",
                            return_value=Path(tmp)):
                data, info = build_export_bundle(
                    self._rows(), {"m2": {"md5": "deadbeef"}})
        archive = zipfile.ZipFile(io.BytesIO(data))
        media = [n for n in archive.namelist() if n.startswith("图片/")]
        self.assertEqual(len(media), 1)
        self.assertTrue(media[0].endswith(".png"), media[0])
        self.assertEqual(info["missing"], 0)

    def test_encrypted_image_is_reported_not_as_missing(self):
        import io
        import tempfile
        import unittest.mock as mock
        import zipfile
        from pathlib import Path

        from pyweixin.export_bundle import build_export_bundle

        with tempfile.TemporaryDirectory() as tmp:
            dat = Path(tmp) / "msg" / "attach" / "room" / "2026-10" / "Img" / "abc.dat"
            dat.parent.mkdir(parents=True)
            dat.write_bytes(b"\x07\x08V2\x08\x07" + b"0" * 32)
            with mock.patch("pyweixin.media_attachments._scan_caches",
                            return_value=({}, {})), \
                 mock.patch("pyweixin.media_attachments._account_root",
                            return_value=Path(tmp)):
                data, info = build_export_bundle(
                    self._rows(), {"m2": {"md5": "abc"}})
        self.assertEqual(info["encrypted"], 1)
        self.assertEqual(info["missing"], 0)
        readme = zipfile.ZipFile(io.BytesIO(data)).read("导出说明.txt").decode("utf-8")
        self.assertIn("加密", readme)
        self.assertNotIn("都已随包导出", readme)


class AttachCachedFilesTests(unittest.TestCase):
    """本地缓存附件关联：能拿到的接上，4.0 加密图片如实标注。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _attach(self, messages, by_id=None, by_name=None):
        import unittest.mock as mock

        from pyweixin.media_attachments import attach_cached_files

        with mock.patch("pyweixin.media_attachments._scan_caches",
                        return_value=(by_id or {}, by_name or {})), \
             mock.patch("pyweixin.media_attachments._account_root",
                        return_value=self.root):
            return attach_cached_files(messages)

    def test_encrypted_v4_image_is_not_attached(self):
        dat = self.root / "abc.dat"
        dat.write_bytes(b"\x07\x08V2\x08\x07" + b"0" * 32)
        [message] = self._attach(
            [{"kind": "image", "media": {"md5": "abc"}}], by_id={"abc": [dat]}, by_name={})
        self.assertEqual(message["attachments"], [])
        self.assertIn("加密", message["media_status"])

    def test_video_thumbnail_is_attached(self):
        thumb = self.root / "vid1_thumb.jpg"
        thumb.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 16)
        [message] = self._attach(
            [{"kind": "video", "media": {"md5": "vid1"}}],
            by_id={"vid1": [thumb], "vid1_thumb": [thumb]}, by_name={})
        self.assertEqual(message["attachments"], [str(thumb)])

    def test_file_matched_by_name(self):
        doc = self.root / "invoice.pdf"
        doc.write_bytes(b"%PDF-1.4\n")
        [message] = self._attach(
            [{"kind": "file", "media": {"filename": "invoice.pdf"}}],
            by_id={}, by_name={"invoice.pdf": [doc]})
        self.assertEqual(message["attachments"], [str(doc)])


if __name__ == "__main__":
    unittest.main()
