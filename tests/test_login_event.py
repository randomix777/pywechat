"""登录成功必须通知界面，否则登录框永远写着"未登录"。

出过的事：attach() 成功后没人发事件，_refresh_login_block 只在查额度
和点账号时才跑 —— 刚在浏览器点完确认的商户回到软件，看到的还是未登录，
门禁也不放行。

注意：本文件只跑纯内存装配（ simulated probe，不连微信不联网）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyweixin.application import AppEvents, MerchantApplication  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402
from pyweixin.wechat_state import SimulatedWeChat, WeChatState  # noqa: E402


class LoggedInEventTests(unittest.TestCase):
    def _app(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-loggedin-")
        store = OrderStore(str(Path(self._tmp.name) / "orders.db"))
        app = MerchantApplication(
            data_dir=Path(self._tmp.name),
            orders=store,
            tenant="t",
            probe=SimulatedWeChat([WeChatState.LOGGED_IN]),
            events=AppEvents(),
            simulated=True,
        )
        return app, store

    def tearDown(self):
        for closer in ("app", "store"):
            obj = getattr(self, closer, None)
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:
                pass
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def test_attach_fires_logged_in(self):
        app, self.store = self._app()
        self.app = app
        seen: list = []
        app.events.on_logged_in = lambda: seen.append(True)
        app.attach(SimpleNamespace(merchant_name="店"), object(), object())
        self.assertEqual(seen, [True])

    def test_gate_opens_after_attach(self):
        app, self.store = self._app()
        self.app = app
        app.wizard.step = "wechat"
        self.assertFalse(app.wizard.product_gate_open())
        app.attach(SimpleNamespace(merchant_name="店"), object(), object())
        self.assertTrue(app.wizard.product_gate_open())


if __name__ == "__main__":
    unittest.main()
