"""出问题时能拿出证据 —— 以及**绝不能**写出什么。

这是个驱动另一个程序的桌面应用。读消息要动微信窗口、发回执要动微信窗口，
任何一环出问题，商户看到的只有「读取失败」四个字。

这个模块之前 `merchant_app.py` 里 `logging` 出现 0 次：整个应用没有落盘
日志，所有 ``LOGGER.exception`` 全部落空，而客户端跑在 ``pythonw`` 下，
连 stderr 也丢。**产品报不出自己的故障。**

## 但日志本身也是风险

日志会被商户转发，也可能落在别人能翻到的目录。所以这组测试盯的**不只是
"有没有记"，还有"绝不能记什么"**。
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin import diagnostics  # noqa: E402


class LogFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-log-")
        self.data_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._saved_handlers = list(logging.getLogger().handlers)
        self._saved_level = logging.getLogger().level
        logging.getLogger().handlers = []
        self.addCleanup(self._restore)

    def _restore(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if handler not in self._saved_handlers:
                handler.close()
        root.handlers = self._saved_handlers
        root.setLevel(self._saved_level)

    def _install(self):
        path = diagnostics.install(self.data_dir)
        return path, diagnostics.path_of(self.data_dir)

    def test_it_creates_a_log_file(self):
        path, expected = self._install()
        self.assertIsNotNone(path)
        self.assertTrue(expected.exists(), "没有生成日志文件")

    def test_messages_actually_land_in_it(self):
        _path, expected = self._install()
        logging.getLogger("pyweixin.demo").warning("读取消息失败")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = expected.read_text(encoding="utf-8")
        self.assertIn("读取消息失败", text)

    def test_it_is_installed_before_anything_else_starts(self):
        """日志必须最先装好 —— 装晚了，前面出错的部分就没了。

        只在 ``main()`` 里找：模块顶部有这些名字的 import 和函数定义，
        拿它们的位置比是比错的。
        """
        source = (ROOT / "src" / "pyweixin" / "merchant_app.py").read_text(encoding="utf-8")
        main_body = source[source.index("def main("):]
        install_at = main_body.index("diagnostics.install(")
        for later in ("ensure_local_store(", "build_application(", "MerchantWindow("):
            self.assertLess(install_at, main_body.index(later),
                            f"日志装得太晚：{later} 之前出错的日志就丢了")

    def test_failing_to_install_does_not_stop_the_program(self):
        """日志是手段，程序是目的。接不上就让程序照常跑。"""
        unwritable = Path("Z:/definitely-not-a-real-drive/x")
        self.assertIsNone(diagnostics.install(unwritable))


class RedactionTests(unittest.TestCase):
    """日志里绝不能出现凭据。"""

    def test_it_redacts_a_deepseek_style_key(self):
        self.assertNotIn("sk-abcdef1234567890abcdef",
                         diagnostics._redact("key=sk-abcdef1234567890abcdef"))

    def test_it_redacts_a_bearer_token(self):
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9abc",
                         diagnostics._redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abc"))

    def test_it_redacts_assigned_secrets(self):
        for text in ("password=hunter2", "api_key: abcdef123456",
                     "TOKEN=zzzzzzzzzz", "secret = qwertyuiop"):
            with self.subTest(text=text):
                cleaned = diagnostics._redact(text)
                self.assertIn("***", cleaned, f"没脱敏：{cleaned}")
                secret = text.split("=")[-1].split(":")[-1].strip()
                self.assertNotIn(secret, cleaned, f"凭据泄漏：{text}")

    def test_it_leaves_ordinary_text_alone(self):
        for text in ("读取消息失败", "实际读到 7 条", "路径 C:/Users/x/orders.db"):
            self.assertEqual(diagnostics._redact(text), text)


class SizeTests(unittest.TestCase):
    def test_the_file_is_capped(self):
        self.assertLessEqual(diagnostics.MAX_BYTES, 5 * 1024 * 1024,
                             "单文件上限太大，日志会变成垃圾堆")
        self.assertGreater(diagnostics.MAX_BYTES, 0)

    def test_only_a_few_backups_are_kept(self):
        self.assertLessEqual(diagnostics.BACKUP_COUNT, 5)


class NoisyModuleTests(unittest.TestCase):
    def test_high_frequency_modules_are_quiet(self):
        """每 2 秒探测一次微信，记 INFO 会把真问题淹掉。"""
        self.assertIn("pyweixin.wechat_state", diagnostics.QUIET_BY_DEFAULT)
        self.assertIn("pywinauto", diagnostics.QUIET_BY_DEFAULT)

    def test_quieting_them_does_not_raise_their_level_above_warning(self):
        diagnostics.quiet_noisy_modules()
        for name in diagnostics.QUIET_BY_DEFAULT:
            level = logging.getLogger(name).level
            self.assertGreaterEqual(level, logging.WARNING,
                                    f"{name} 被设成了 {level}，出问题都不会记")


class SourceTests(unittest.TestCase):
    def test_the_app_actually_calls_install(self):
        source = (ROOT / "src" / "pyweixin" / "merchant_app.py").read_text(encoding="utf-8")
        self.assertIn("diagnostics.install(", source,
                      "入口没装日志 = 改了一堆 LOGGER 也不会有文件")
        self.assertIn("diagnostics.quiet_noisy_modules()", source)


if __name__ == "__main__":
    unittest.main()