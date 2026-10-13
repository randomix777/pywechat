"""UIA 存活门纯单测：假 Desktop，不碰真微信不联网。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyweixin import wechat_state  # noqa: E402
from pyweixin.wechat_state import (  # noqa: E402
    default_sender_source,
    reset_uia_cache,
    uia_automation_available,
)


class _FakeElement:
    def __init__(self, kids=()):
        self._kids = list(kids)
        self.element_info = object()

    def children(self):
        return list(self._kids)


class _FakeWindow(_FakeElement):
    def __init__(self, title, kids=()):
        super().__init__(kids)
        self._title = title

    def window_text(self):
        return self._title


class _FakeDesktop:
    def __init__(self, windows):
        self._windows = windows

    def windows(self):
        return list(self._windows)


def _bushy_window(title="Weixin", leaves=15):
    return _FakeWindow(title, [_FakeElement() for _ in range(leaves)])


class UiaProbeTests(unittest.TestCase):
    def setUp(self):
        reset_uia_cache()
        import pywinauto

        self._real_desktop = pywinauto.Desktop
        self.addCleanup(setattr, pywinauto, "Desktop", self._real_desktop)

    def _patch_desktop(self, windows):
        import pywinauto

        pywinauto.Desktop = lambda backend="uia": _FakeDesktop(windows)

    def test_bushy_tree_is_alive(self):
        self._patch_desktop([_bushy_window(leaves=15)])
        self.assertTrue(uia_automation_available("v1"))

    def test_two_node_black_box_is_dead(self):
        inner = _FakeElement([_FakeElement()])
        self._patch_desktop([_FakeWindow("Weixin", [inner])])
        self.assertFalse(uia_automation_available("v1"))

    def test_no_weixin_window_is_dead(self):
        self._patch_desktop([_bushy_window(title="记事本", leaves=50)])
        self.assertFalse(uia_automation_available("v1"))

    def test_result_is_cached_per_version(self):
        self._patch_desktop([_bushy_window(leaves=15)])
        self.assertTrue(uia_automation_available("v1"))
        self._patch_desktop([])
        self.assertTrue(uia_automation_available("v1"), "同版本必须走缓存")
        self.assertFalse(uia_automation_available("v2"), "换版本重探")

    def test_default_sender_follows_probe(self):
        import pyweixin.wechat_state as module

        original = module.uia_automation_available
        try:
            module.uia_automation_available = lambda *a, **k: True
            reset_uia_cache()
            self.assertEqual(default_sender_source(), "members")
            module.uia_automation_available = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("探针坏了"))
            self.assertEqual(default_sender_source(), "localdb")
        finally:
            module.uia_automation_available = original
            reset_uia_cache()


class UiGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.source = (root / "src" / "pyweixin" / "merchant_ui.py").read_text(
            encoding="utf-8")

    def test_listen_and_auto_go_through_the_gate(self):
        for method in ("def _on_toggle_listen", "def _on_enable_auto"):
            start = self.source.index(method)
            block = self.source[start:start + 1500]
            self.assertIn("_require_uia", block)

    def test_dead_ui_disables_list_matching(self):
        # 认人选项已砍掉：第 3 步只有数据库直读，没有"发送人怎么认"可选。
        self.assertNotIn("发送人怎么认：", self.source)
        start = self.source.index("def _on_read_history")
        block = self.source[start:start + 1500]
        self.assertIn('sender_source="wcdb"', block)


if __name__ == "__main__":
    unittest.main()
