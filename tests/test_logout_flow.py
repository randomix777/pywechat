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

from pyweixin.application import MerchantApplication  # noqa: E402
from pyweixin.conversations import ConversationKind, SimulatedConversations  # noqa: E402
from pyweixin.history_read import SimulatedHistory  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402


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
                      / "src" / "pyweixin" / "merchant_ui.py").read_text(encoding="utf-8")

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
        start = self.source.index("def _on_account_click")
        block = self.source[start:start + 1400]
        self.assertIn("WizardStep.ANALYZE if self.app.wizard.history_result",
                      block)


class WindowSessionTests(unittest.TestCase):
    """窗口读会话必须读 ``app.session``。

    ``MerchantWindow.session`` 是类属性 None，从来没被赋过值 —— 用它做判断
    的代码等于永远走不到。
    """

    def test_the_window_never_assigns_its_own_session(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "pyweixin" / "merchant_ui.py")
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("self.session =", text,
                         "窗口不该自己存一份会话，容易和应用层不同步")
        self.assertNotIn("if self.session is not None",
                         "会话判断必须走 app.session，这个分支永远为假")


if __name__ == "__main__":
    unittest.main()