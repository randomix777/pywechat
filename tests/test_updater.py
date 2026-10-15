"""自动更新的回归测试：不联网（HTTP 全 mock），不碰真程序目录。"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from pyweixin import updater  # noqa: E402


def _release(version="1.9.9", assets=True):
    body = {"tag_name": "v" + version, "body": "修复若干问题",
            "assets": []}
    if assets:
        name = f"{updater.ASSET_PREFIX}{version}.zip"
        body["assets"] = [
            {"name": name,
             "browser_download_url": "https://example.com/" + name},
            {"name": name + ".sha256",
             "browser_download_url": "https://example.com/" + name + ".sha256"},
        ]
    return body


def _fake_app_zip(top_dir=True):
    """造一个像 onedir 解压物的 zip。"""
    buf = io.BytesIO()
    prefix = "appdir/" if top_dir else ""
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(prefix + "微信AI智能助理.exe", b"EXE")
        archive.writestr(prefix + "_internal/a.pyd", b"BIN")
    return buf.getvalue()


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v1.9.8"), (1, 9, 8))
        self.assertEqual(updater.parse_version("1.10.0"), (1, 10, 0))
        self.assertEqual(updater.parse_version("xxx"), ())

    def test_is_newer(self):
        self.assertTrue(updater.is_newer("1.9.9", "1.9.8"))
        self.assertTrue(updater.is_newer("v1.10.0", "1.9.8"))
        self.assertFalse(updater.is_newer("1.9.8", "1.9.8"))
        self.assertFalse(updater.is_newer("1.9.7", "1.9.8"))
        self.assertFalse(updater.is_newer("xxx", "1.9.8"))
        self.assertFalse(updater.is_newer("1.9.9", ""))


class CheckTests(unittest.TestCase):
    def _check(self, release, current="1.9.8"):
        with unittest.mock.patch.object(
                updater, "_http_get_json", return_value=release):
            return updater.check_for_update("some/repo", current)

    def test_newer_with_assets(self):
        info = self._check(_release("1.9.9"))
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["version"], "1.9.9")
        self.assertTrue(info["zip_url"].endswith(".zip"))

    def test_same_version_is_none(self):
        self.assertIsNone(self._check(_release("1.9.8"), "1.9.8"))

    def test_network_error_is_none(self):
        with unittest.mock.patch.object(
                updater, "_http_get_json", side_effect=OSError("down")):
            self.assertIsNone(updater.check_for_update("some/repo", "1.9.8"))

    def test_no_zip_asset_is_none(self):
        self.assertIsNone(self._check(_release("1.9.9", assets=False)))


class StageTests(unittest.TestCase):
    def _stage(self, data: bytes, sha: str):
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        info = {"version": "1.9.9", "notes": "",
                "zip_url": "https://example.com/a.zip",
                "sha_url": "https://example.com/a.zip.sha256"}
        with unittest.mock.patch.object(
                updater, "_http_get_bytes",
                side_effect=[data, (sha + "  a.zip\n").encode()]):
            staged = updater.download_and_stage(info, base)
        return base, staged

    def test_happy_path_stages_and_writes_pending(self):
        data = _fake_app_zip()
        digest = hashlib.sha256(data).hexdigest()
        base, staged = self._stage(data, digest)
        try:
            self.assertIsNotNone(staged)
            assert staged is not None
            self.assertTrue((staged / "微信AI智能助理.exe").is_file())
            pending = json.loads((base / updater.PENDING_NAME).read_text(encoding="utf-8"))
            self.assertEqual(pending["version"], "1.9.9")
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)

    def test_bad_hash_discards_everything(self):
        base, staged = self._stage(_fake_app_zip(), "0" * 64)
        try:
            self.assertIsNone(staged)
            self.assertFalse((base / updater.PENDING_NAME).exists())
            self.assertFalse((base / updater.UPDATES_DIR_NAME).exists())
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)

    def test_no_sha_file_refuses_install(self):
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        info = {"version": "1.9.9", "notes": "",
                "zip_url": "https://example.com/a.zip", "sha_url": ""}
        try:
            with unittest.mock.patch.object(
                    updater, "_http_get_bytes",
                    return_value=_fake_app_zip()):
                self.assertIsNone(updater.download_and_stage(info, base))
            self.assertFalse((base / updater.PENDING_NAME).exists())
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)

    def test_garbage_zip_is_discarded(self):
        base, staged = self._stage(b"not-a-zip", hashlib.sha256(b"not-a-zip").hexdigest())
        try:
            self.assertIsNone(staged)
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


class PendingTests(unittest.TestCase):
    def test_missing_or_broken_is_none(self):
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        try:
            self.assertIsNone(updater.pending_update(base))
            (base / updater.PENDING_NAME).write_text("garbage{", encoding="utf-8")
            self.assertIsNone(updater.pending_update(base))
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


class ScriptTests(unittest.TestCase):
    def test_script_waits_swaps_rolls_back(self):
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        try:
            info = {"version": "1.9.9", "dir": "1.9.9", "notes": ""}
            script = updater.build_updater_script(base, info, "微信AI智能助理.exe", 1234)
            text = script.read_text(encoding="utf-8")
            self.assertIn("1234", text)
            self.assertIn("微信AI智能助理.exe", text)
            # 等旧进程 → 搬目录 → 起新版 → 15 秒检查 → 起不来回滚 → 自删
            for needle in ("tasklist", "rmdir", "move", "timeout /t 15", "del \"%~f0\""):
                self.assertIn(needle, text, needle)
            self.assertTrue(str(script).endswith(".bat"))
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


class BackgroundTests(unittest.TestCase):
    def test_staged_callback_fires(self):
        info = {"version": "1.9.9", "notes": "", "zip_url": "x", "sha_url": "y"}
        seen = []
        with unittest.mock.patch.object(updater, "is_frozen_app", return_value=True), \
            unittest.mock.patch.object(updater, "check_for_update", return_value=info), \
            unittest.mock.patch.object(updater, "download_and_stage",
                                       return_value=Path("staged")):
            thread = updater.check_in_background(on_staged=seen.append)
            thread.join(timeout=10)
        self.assertEqual(seen, [info])

    def test_nothing_new_calls_nothing(self):
        with unittest.mock.patch.object(updater, "is_frozen_app", return_value=True), \
            unittest.mock.patch.object(updater, "check_for_update", return_value=None):
            thread = updater.check_in_background(
                on_staged=lambda info: (_ for _ in ()).throw(AssertionError("不该被调")))
            thread.join(timeout=10)


class ApplyTests(unittest.TestCase):
    def test_dev_mode_does_nothing(self):
        with unittest.mock.patch.object(updater.sys, "frozen", False, create=True):
            self.assertFalse(updater.is_frozen_app())
            self.assertFalse(updater.apply_pending_update(Path(".")))

    def test_frozen_with_pending_spawns_updater(self):
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        try:
            staged = base / updater.UPDATES_DIR_NAME / "1.9.9"
            staged.mkdir(parents=True)
            (staged / "微信AI智能助理.exe").write_bytes(b"EXE")
            (staged / "_internal").mkdir()
            (base / updater.PENDING_NAME).write_text(
                json.dumps({"version": "1.9.9", "dir": "1.9.9"}), encoding="utf-8")
            (base / "微信AI智能助理.exe").write_bytes(b"OLD")
            spawned = []
            with unittest.mock.patch.object(updater.sys, "frozen", True, create=True), \
                unittest.mock.patch("subprocess.Popen",
                                    side_effect=lambda *a, **k: spawned.append((a, k)) or unittest.mock.Mock()):
                self.assertTrue(updater.apply_pending_update(base))
            self.assertEqual(len(spawned), 1)
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


class UpdateNoteTests(unittest.TestCase):
    """新版下好后顶栏要留一句话，不然商户下次打开发现变了版本对不上。"""

    def _window(self):
        import tkinter as tk
        import types
        from tkinter import ttk

        from pyweixin.merchant_ui import MerchantWindow

        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        label = ttk.Label(root, text="")
        label.pack()
        window = MerchantWindow.__new__(MerchantWindow)
        window._widgets = {"update_note": label}
        window.app = types.SimpleNamespace()
        return window, label

    def test_staged_version_shows(self):
        window, label = self._window()
        window._on_update_staged({"version": "1.9.9"})
        self.assertIn("1.9.9", label.cget("text"))
        self.assertIn("下次", label.cget("text"))

    def test_empty_info_stays_quiet(self):
        window, label = self._window()
        window._on_update_staged({})
        self.assertEqual(label.cget("text"), "")


if __name__ == "__main__":
    unittest.main()
