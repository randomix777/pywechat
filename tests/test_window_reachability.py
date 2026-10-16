"""窗口缩到最小也不能让功能消失。

Tk **不会滚动**。窗口高度给不够时，最后排的控件根本不会被布局出来 ——
不是被切掉半截，是**在屏幕上任何位置都找不到**，商户也就永远点不到。

真机实测（`examples/probe_window_sizes.py`）：

    760x560  →  开始持续监听、监听区块、查看订单  全部消失
    800x620  →  开始持续监听 消失

商户把窗口拖小一点，就永久失去「开启持续监听」和「查看订单」。这不是
"排版不好看"，是功能没了。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "packages" / "order_agent" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "business_agent" / "src"))


class WindowSizeSourceTests(unittest.TestCase):
    """静态检查：窗口尺寸相关的约定写在源码里。"""

    def setUp(self):
        self.source = (ROOT / "src" / "wechat_ai_assistant" / "merchant_ui.py").read_text(encoding="utf-8")

    def test_the_minimum_height_is_not_below_the_default(self):
        """最小高度低于默认高度 = 允许缩到装不下。

        这是缺陷当时的根因：minsize(760, 560) 配 geometry("900x680")。
        """
        import re

        geometry = re.search(r'root\.geometry\("(\d+)x(\d+)"\)', self.source)
        minsize = re.search(r"root\.minsize\((\d+),\s*(\d+)\)", self.source)
        self.assertIsNotNone(geometry, "找不到窗口默认尺寸")
        self.assertIsNotNone(minsize, "找不到窗口最小尺寸")
        self.assertGreaterEqual(
            int(minsize.group(2)), int(geometry.group(2)),
            "最小高度低于默认高度：商户能缩到一个装不下内容的尺寸，"
            "最后排的按钮会直接消失",
        )

    def test_the_reason_is_written_down(self):
        """这个数字看起来像可以随手调小。写清理由，才不会有人"优化"掉。"""
        self.assertIn("Tk 不会滚动", self.source)
        self.assertIn("查看订单", self.source)

    def test_the_stock_table_height_follows_its_content(self):
        """固定 6 行会白占高度，而高度是从下面按钮的预算里扣的。"""
        self.assertIn("tree.configure(height=max(2, min(6, len(view.stock) + 1)))",
                      self.source)


class MinimumSizeReachableTests(unittest.TestCase):
    """真窗口里量：缩到最小，按钮还在不在。"""

    @classmethod
    def setUpClass(cls):
        from wechat_ai_assistant.application import MerchantApplication
        from wechat_ai_assistant.conversations import ConversationKind, SimulatedConversations
        from wechat_ai_assistant.history_read import SimulatedHistory
        from wechat_ai_assistant.merchant_ui import MerchantWindow
        from wechat_ai_assistant.orders import OrderStore

        cls._tmp = tempfile.TemporaryDirectory(prefix="pyw-minwin-")
        data_dir = Path(cls._tmp.name)
        store = OrderStore(str(data_dir / "orders.db"))
        tenant = store.create_merchant("最小窗口测试")["merchant_id"]
        app = MerchantApplication(
            data_dir=data_dir, orders=store, tenant=tenant, probe=None, launcher=None,
            conversation_source=SimulatedConversations(
                [("鲜果批发群", ConversationKind.GROUP, 8)]
            ),
            history_reader_fn=SimulatedHistory({}, delay=0.0), simulated=True,
        )
        cls.app, cls.store = app, store
        # 商户要先选了会话，监听区块才会建出来 —— 这是真实路径。
        conversations = list(app.wizard.refresh_conversations())
        app.wizard.choose_history_conversations(conversations[:1])
        app.choose_live_conversation(conversations[0])
        cls.window = MerchantWindow(app)
        cls.window._on_goto("done")
        cls.window.root.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.window.root.destroy()
        except Exception:
            pass
        # 先关 sqlite 再删目录：Windows 上文件被占着就删不掉。
        for closer in (cls.app.merger, cls.app.cursor, cls.app.agent_store):
            try:
                closer.close()
            except Exception:
                pass
        try:
            cls.app.close()
        except Exception:
            pass
        cls.store.close()
        shutil.rmtree(cls._tmp.name, ignore_errors=True)

    def _pump(self):
        for _ in range(8):
            self.window.root.update()
        self.window.root.update_idletasks()

    def test_the_minimum_height_is_what_gets_used(self):
        _w, min_h = self.window.root.minsize()
        self.window.root.geometry(f"760x{min_h}")
        self._pump()
        self.assertGreaterEqual(
            self.window.root.winfo_height(), min_h,
            "窗口没能缩到声明的最小高度，测试环境有问题",
        )

    def test_the_listen_button_survives_the_smallest_window(self):
        """持续监听入口已撤掉：首页再没有这个按钮，也不需要它活下来。"""
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                try:
                    if child.winfo_class() == "TButton" and "持续监听" in str(child.cget("text")):
                        found.append(child)
                except Exception:
                    pass
                walk(child)

        walk(self.window.root)
        self.assertFalse(found, "死入口又出现了")

    def test_the_view_orders_button_survives_the_smallest_window(self):
        _w, min_h = self.window.root.minsize()
        self.window.root.geometry(f"760x{min_h}")
        self._pump()
        self.window._scroll_page_to_end()
        self._pump()
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                try:
                    if child.winfo_class() == "TButton" and "查看订单" in str(child.cget("text")):
                        found.append(child)
                except Exception:
                    pass
                walk(child)

        walk(self.window.root)
        self.assertTrue(found, "找不到「查看订单」按钮")
        self.assertTrue(found[0].winfo_ismapped(),
                        "缩到最小窗口时「查看订单」消失了")


if __name__ == "__main__":
    unittest.main()