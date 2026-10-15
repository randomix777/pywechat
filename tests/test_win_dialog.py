"""Win11 原生文件框的回归测试：只测不弹框的部分。

真弹框要人点，CI 里点不了 —— 弹框本身由用户在真机上验收。
这里钉死：取消与失败的区分、回落规则、结构体形状。
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from pyweixin import win_dialog  # noqa: E402


class CancelTests(unittest.TestCase):
    def test_cancel_codes_recognized(self):
        self.assertTrue(win_dialog._is_cancel(0x800704C7))
        self.assertTrue(win_dialog._is_cancel(0x800704C8))
        self.assertFalse(win_dialog._is_cancel(0))
        self.assertFalse(win_dialog._is_cancel(0x80070057))

    def test_cancel_returns_empty_not_none(self):
        with unittest.mock.patch.object(
                win_dialog, "_show_dialog",
                side_effect=win_dialog.UserCancelled()):
            self.assertEqual(win_dialog.ask_saveas_path(filename="a.csv"), "")
            self.assertEqual(win_dialog.ask_open_path(), "")

    def test_failure_returns_none_for_fallback(self):
        with unittest.mock.patch.object(
                win_dialog, "_show_dialog", side_effect=OSError("no com")):
            self.assertIsNone(win_dialog.ask_saveas_path(filename="a.csv"))
            self.assertIsNone(win_dialog.ask_open_path())


class StructTests(unittest.TestCase):
    def test_filter_spec_has_name_and_pattern(self):
        names = [name for name, _ in win_dialog.COMDLG_FILTERSPEC._fields_]
        self.assertEqual(names, ["pszName", "pszSpec"])

    def test_guid_round_trip(self):
        # 取值以 SDK 头文件 ShObjIdl_core.h 为准（凭记忆写错过，自己验过）。
        self.assertEqual(
            (win_dialog.CLSID_FileSaveDialog,
             win_dialog.IID_IFileSaveDialog[3]),
            ((0xC0B4E2F3, 0xBA21, 0x4773,
              (0x8D, 0xBA, 0x33, 0x5E, 0xC9, 0x46, 0xEB, 0x8B)),
             (0xAE, 0xA4, 0xAF, 0x64, 0xB8, 0x3D, 0x78, 0xAB)))


class MerchantFallbackTests(unittest.TestCase):
    def _window(self):
        from pyweixin.merchant_ui import MerchantWindow

        window = MerchantWindow.__new__(MerchantWindow)
        window._widgets = {}
        window.root = unittest.mock.Mock()
        window.root.winfo_id.return_value = 12345
        return window

    def test_none_falls_back_to_tkinter(self):
        from pyweixin import merchant_ui

        window = self._window()
        with unittest.mock.patch("pyweixin.win_dialog.ask_saveas_path",
                                 return_value=None) as native, \
            unittest.mock.patch.object(merchant_ui.filedialog,
                                       "asksaveasfilename",
                                       return_value="C:/x.csv") as tk:
            path = window._ask_saveas("t", "a.csv", [("CSV", "*.csv")], ".csv")
        self.assertEqual(path, "C:/x.csv")
        native.assert_called_once()
        tk.assert_called_once()

    def test_cancel_does_not_fall_back(self):
        from pyweixin import merchant_ui

        window = self._window()
        with unittest.mock.patch("pyweixin.win_dialog.ask_saveas_path",
                                 return_value="") as native, \
            unittest.mock.patch.object(merchant_ui.filedialog,
                                       "asksaveasfilename") as tk:
            path = window._ask_saveas("t", "a.csv", [("CSV", "*.csv")], ".csv")
        self.assertEqual(path, "")
        tk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
