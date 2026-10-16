"""登录与退出。

两件事以前都不成立：

* **登不出去。** ``client_login.logout()`` 一直存在，却没有任何地方调用它。
  商户登录一次之后就再也换不了账号 —— 收银电脑上换个人就登不进去。
* **登录后看不到自己登的是谁。** ``MerchantWindow.session`` 是类属性
  ``None``，**从来没有被赋值过**，所以 ``if self.session is not None`` 永远为假，
  顶栏登录后依然显示「账号：未登录」。

后一条是被"退出"这条测试撞出来的：断言登录后账号条会写名字，结果它一直说未登录。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "order_agent" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "business_agent" / "src"))

from wechat_ai_assistant.application import MerchantApplication  # noqa: E402
from wechat_ai_assistant.conversations import ConversationKind, SimulatedConversations  # noqa: E402
from wechat_ai_assistant.history_read import SimulatedHistory  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402


class Session:
    merchant_name = "一号店"


class FakeClient:
    def __init__(self, fail_on_logout: bool = False):
        self.logged_out = False
        self.fail_on_logout = fail_on_logout

    def logout(self):
        if self.fail_on_logout:
            raise RuntimeError("network down")
        self.logged_out = True


class DetachTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-detach-")
        data_dir = Path(self._tmp.name)
        self.store = OrderStore(str(data_dir / "orders.db"))
        self.tenant = self.store.create_merchant("登出测试店")["merchant_id"]
        self.app = MerchantApplication(
            data_dir=data_dir, orders=self.store, tenant=self.tenant,
            probe=None, launcher=None,
            conversation_source=SimulatedConversations(
                [("鲜果批发群", ConversationKind.GROUP, 8)]
            ),
            history_reader_fn=SimulatedHistory({}, delay=0.0), simulated=True,
        )
        self.client = FakeClient()
        self.app.attach(session=Session(), client=self.client, parse_service=object())

    def tearDown(self):
        for closer in (self.app.merger, self.app.cursor, self.app.agent_store):
            try:
                closer.close()
            except Exception:
                pass
        self.app.close()
        self.store.close()
        self._tmp.cleanup()

    def test_attach_stores_the_session(self):
        self.assertIsNotNone(self.app.session)

    def test_detach_clears_the_session(self):
        self.app.detach()
        self.assertIsNone(self.app.session)

    def test_detach_notifies_the_server(self):
        self.app.detach()
        self.assertTrue(self.client.logged_out, "没有通知服务端作废")

    def test_a_failed_server_logout_still_signs_out_locally(self):
        """断网时也要能退出来 —— 否则商户被锁在自己登录不上的状态里。"""
        self.app.client = FakeClient(fail_on_logout=True)
        self.app.detach()
        self.assertIsNone(self.app.session)

    def test_detach_drops_the_cloud_pipeline(self):
        self.app.detach()
        self.assertIsNone(self.app.parse_service)
        self.assertIsNone(self.app.wizard.pipeline)
        self.assertFalse(self.app.backend_is_model,
                         "退出后不得继续把结果标成真实模型")

    def test_detach_deletes_the_local_credentials(self):
        """退出必须删本机凭证：不清文件，下次启动 restore 又登回去 ——
        商户明明点了退出，重开发现还登着（真实报障）。"""
        from wechat_ai_assistant.client_login import CredentialStore, restore_session

        credentials = CredentialStore(str(Path(self._tmp.name) / "cred.bin"))
        credentials.save({"session": {"access_token": "a", "refresh_token": "r"}})
        self.app.credentials = credentials
        self.app.detach()
        self.assertIsNone(restore_session(credentials), "凭证文件没删干净")

    def test_detach_deletes_credentials_even_when_offline(self):
        """断网退出也要彻底：服务端没通知到，本地必须照删。"""
        from wechat_ai_assistant.client_login import CredentialStore, restore_session

        credentials = CredentialStore(str(Path(self._tmp.name) / "cred2.bin"))
        credentials.save({"session": {"access_token": "a", "refresh_token": "r"}})
        self.app.credentials = credentials
        self.app.client = FakeClient(fail_on_logout=True)
        self.app.detach()
        self.assertIsNone(restore_session(credentials))

    def test_detach_keeps_local_messages(self):
        """已读到的消息是商户自己经营出来的，退出登录不该删。"""
        # 走真实的入库路径，不要手写 INSERT —— messages 有一串非空外键列，
        # 手写的那份数据和真实写入的不是一回事。
        self.store.add_import(
            self.tenant, "c1",
            [{"sender": "张姐", "content": "苹果两箱",
              "sent_at": "2026-10-02 09:01", "source_id": "s1", "stable_source_id": True}],
            source="import-s1", source_type="test",
        )
        self.app.detach()
        count = self.store.db.execute(
            "SELECT COUNT(*) c FROM messages WHERE merchant_id=?", (self.tenant,)
        ).fetchone()["c"]
        self.assertEqual(count, 1, "退出登录把本地消息删了")

    def test_local_reading_still_works_after_signing_out(self):
        """退出云端登录不影响本地读取 —— 本地资料本来就不该要账号。"""
        self.app.detach()
        self.app.wizard.step = "history"
        self.app.wizard.choose_history_conversations(
            list(self.app.wizard.refresh_conversations())[:1]
        )
        self.assertIs(self.app.wizard.orders(), self.store)


class LogoutWordingTests(unittest.TestCase):
    """商户最误会的一件事：退出登录 = 数据没了。

    退出只断开云端能力。已读到的消息和订单一个字节都没少 —— 界面必须主动
    说出来，否则商户会去重读一遍真实数据，白费工夫还可能重复下单。
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).resolve().parent.parent
                      / "src" / "wechat_ai_assistant" / "merchant_ui.py").read_text(encoding="utf-8")

    def test_logout_says_the_local_data_is_kept(self):
        self.assertIn("都留在这台电脑上，没有删除", self.source)

    def test_logout_says_he_does_not_need_to_read_again(self):
        """重读一遍会重复占用额度，也可能重复下单。"""
        self.assertIn("不需要重新读取", self.source)
        self.assertIn("不会重复计费", self.source)

    def test_the_home_page_says_it_too(self):
        """退出后商户可能先回首页，那里的额度行不能只写"未登录"。"""
        self.assertIn("本地订单仍在，重新登录后可继续整理", self.source)

    def test_it_keeps_him_on_the_page_where_his_data_is(self):
        """不该被扔回第 1 步 —— 那等于逼他从头再走一遍。"""
        start = self.source.index("def _on_logout_from_center")
        block = self.source[start:start + 1400]
        self.assertIn("WizardStep.ANALYZE if self.app.wizard.history_result",
                      block)


class WindowSessionTests(unittest.TestCase):
    """窗口读会话必须读 ``app.session``。

    ``MerchantWindow.session`` 是类属性 None，从来没被赋过值 —— 用它做判断
    的代码等于永远走不到。
    """

    def test_the_window_never_assigns_its_own_session(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "wechat_ai_assistant" / "merchant_ui.py")
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("self.session =", text,
                         "窗口不该自己存一份会话，容易和应用层不同步")
        self.assertNotIn("if self.session is not None",
                         "会话判断必须走 app.session，这个分支永远为假")


class AccountCenterDataTests(unittest.TestCase):
    """账号中心显示的数据：名字、套餐、用量条、有效期，一个不能少。

    用量只给百分比不给具体数；数据只许来自缓存，方法里不许查网。
    """

    def _window(self, session, quota=None):
        import types

        from wechat_ai_assistant.merchant_ui import MerchantWindow

        window = MerchantWindow.__new__(MerchantWindow)
        window._widgets = {}
        window.app = types.SimpleNamespace(
            session=session,
            wizard=types.SimpleNamespace(quota=quota))
        return window

    def test_percent_from_cached_quota(self):
        import types

        from wechat_ai_assistant.wizard import QuotaView

        session = types.SimpleNamespace(
            merchant_name="一号店",
            subscription={"plan_code": "trial",
                          "period_end": "2026-11-12T00:00:00"})
        quota = QuotaView(logged_in=True, active=True, remaining=80,
                          total_micro_usd=100, used_micro_usd=15,
                          reserved_micro_usd=5)
        data = self._window(session, quota)._account_center_data()
        self.assertEqual(data["name"], "一号店")
        self.assertEqual(data["plan"], "trial")
        self.assertEqual(data["period_end"], "2026-11-12")
        self.assertEqual(data["quota"], {"kind": "percent", "percent": 20})

    def test_unlimited_has_no_bar(self):
        import types

        from wechat_ai_assistant.wizard import QuotaView

        quota = QuotaView(logged_in=True, active=True, remaining=1,
                          total_micro_usd=0)
        data = self._window(types.SimpleNamespace(), quota)._account_center_data()
        self.assertEqual(data["quota"]["kind"], "unlimited")

    def test_subscription_fallback_when_no_quota(self):
        session = {"merchant_name": "一号店",
                   "subscription": {"quota_micro_usd": 200,
                                    "used_micro_usd": 50,
                                    "reserved_micro_usd": 0}}
        import types
        data = self._window(types.SimpleNamespace(**session))._account_center_data()
        self.assertEqual(data["quota"], {"kind": "percent", "percent": 25})

    def test_nothing_usable_is_unknown_not_zero(self):
        import types

        data = self._window(types.SimpleNamespace())._account_center_data()
        self.assertEqual(data["name"], "当前账号")
        self.assertEqual(data["quota"]["kind"], "unknown")

    def test_jyi_quota_fields_drive_the_bar(self):
        """JYI 给了配额字段就画条：总量/已用/预留都在 credential 里。"""
        import types

        session = types.SimpleNamespace(
            merchant_name="JYI测试店", provider="jyi",
            subscription={"quota_micro_usd": 200, "used_micro_usd": 50,
                          "reserved_micro_usd": 0, "plan_code": "pro"})
        data = self._window(session)._account_center_data()
        self.assertEqual(data["quota"], {"kind": "percent", "percent": 25})
        self.assertEqual(data["plan"], "pro")

    def test_never_touches_the_network(self):
        """下拉点开即看：在 UI 线程里等网络就是卡死（真实报障）。"""
        import inspect

        from wechat_ai_assistant import merchant_ui

        source = inspect.getsource(merchant_ui.MerchantWindow._account_center_data)
        self.assertNotIn("check_quota", source)


class AccountDropdownTests(unittest.TestCase):
    """下拉菜单：点的按钮必须真触发（FocusOut 不能先把菜单吃了）。

    回归：充值点了没反应 —— 焦点离开菜单即销毁，按钮的点击还没送达。
    """

    def _window(self):
        import tkinter as tk
        import types
        from tkinter import ttk

        from wechat_ai_assistant.merchant_ui import MerchantWindow

        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        window = MerchantWindow.__new__(MerchantWindow)
        window.root = root
        window._widgets = {}
        anchor = ttk.Button(root, text="x")
        anchor.pack()
        window._widgets["account"] = anchor
        session = types.SimpleNamespace(
            merchant_name="店", provider="jyi", subscription={})
        window.app = types.SimpleNamespace(session=session)
        root.update()
        return window, root

    def _buttons(self, root):
        found = {}

        def walk(widget):
            for child in widget.winfo_children():
                try:
                    if child.winfo_class() == "TButton":
                        found[str(child.cget("text"))] = child
                except Exception:
                    pass
                walk(child)

        walk(root)
        return found

    def test_recharge_click_opens_billing(self):
        import unittest.mock

        window, root = self._window()
        window._show_account_center()
        root.update()
        buttons = self._buttons(root)
        self.assertIn("充值", buttons)
        with unittest.mock.patch("webbrowser.open", return_value=True) as opened:
            buttons["充值"].invoke()
            root.update()
        opened.assert_called_once()
        self.assertIn("/console/billing", opened.call_args.args[0])

    def test_recharge_fallback_when_browser_fails(self):
        import unittest.mock

        from wechat_ai_assistant import merchant_ui

        window, root = self._window()
        window._show_account_center()
        root.update()
        buttons = self._buttons(root)
        with unittest.mock.patch("webbrowser.open", return_value=False), \
            unittest.mock.patch.object(
                merchant_ui.messagebox, "showinfo") as info:
            buttons["充值"].invoke()
            root.update()
        info.assert_called_once()

    def test_logout_click_asks_first(self):
        import unittest.mock

        window, root = self._window()
        window._show_account_center()
        root.update()
        buttons = self._buttons(root)
        self.assertIn("退出登录", buttons)
        with unittest.mock.patch.object(
                window.app, "detach", create=True,
                side_effect=AssertionError("没确认就退出了")):
            with unittest.mock.patch(
                    "wechat_ai_assistant.merchant_ui.messagebox.askyesno",
                    return_value=False):
                buttons["退出登录"].invoke()
                root.update()

    def test_logout_confirm_completes(self):
        """点了确认就退干净：先关下拉（确认框不能挂在它下面），
        再问再退再刷，中间哪步炸了也不把退出本身丢掉。"""
        import unittest.mock

        window, root = self._window()
        window._show_account_center()
        root.update()
        buttons = self._buttons(root)
        calls = []
        window.app.wizard = unittest.mock.Mock(history_result=None)
        window.app.detach = lambda: calls.append("detach")
        window._render_account = lambda: calls.append("render_account")
        window._refresh_login_block = lambda: calls.append("refresh_login_block")
        window._refresh_analyze_button = lambda: calls.append("refresh_analyze_button")
        window._show_step = lambda step: calls.append(("show_step", step))
        with unittest.mock.patch(
                "wechat_ai_assistant.merchant_ui.messagebox.askyesno",
                return_value=True), \
            unittest.mock.patch(
                "wechat_ai_assistant.merchant_ui.messagebox.showinfo"):
            buttons["退出登录"].invoke()
            root.update()
        self.assertIn("detach", calls)
        self.assertIn("render_account", calls)


if __name__ == "__main__":
    unittest.main()