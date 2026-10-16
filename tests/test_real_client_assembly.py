"""真实客户端的装配：读历史必须在**没有**云端登录时也能用。

## 这个缺陷为什么 700 多项测试都没发现

读历史的入库要拿到本地订单库，而 :meth:`Wizard.orders` 以前只认 ``pipeline.orders``。
测试里每个用例会手工补一句::

    app.wizard.pipeline = type("P", (), {"orders": store})()

这一句**恰好把缺的那根线接上了**，于是缺陷在测试里永远看不见；而真实客户端
没这一句，于是商户第一次点「开始读取」必然失败。

所以这里刻意**不挂 pipeline** —— 按真实客户端的样子装配，就当自己没登录。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.application import MerchantApplication  # noqa: E402
from wechat_ai_assistant.conversations import ConversationKind, SimulatedConversations  # noqa: E402
from wechat_ai_assistant.history_read import SimulatedHistory  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402
from wechat_ai_assistant.wechat_state import SimulatedWeChat, WeChatMonitor, WeChatState  # noqa: E402
from wechat_ai_assistant.wizard import Wizard, WizardStep  # noqa: E402

GROUP = "鲜果批发群"


def rows():
    return {
        GROUP: [
            {"消息发送人": "张姐", "消息内容": "苹果两箱", "消息类型": "文本",
             "消息发送时间": "2026-10-2 09:01"},
            {"消息发送人": "张姐", "消息内容": "再加一箱", "消息类型": "文本",
             "消息发送时间": "2026-10-2 09:02"},
        ]
    }


class WizardHasLocalStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-wire-")
        self.dir = Path(self._tmp.name)
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.tenant = self.store.create_merchant("接线测试店")["merchant_id"]

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _wizard(self, orders):
        from wechat_ai_assistant.conversations import ConversationRegistry
        from wechat_ai_assistant.history_read import HistoryReader
        from wechat_ai_assistant.wechat_state import SimulatedWeChat, WeChatMonitor

        monitor = WeChatMonitor(SimulatedWeChat([WeChatState.LOGGED_IN]), None)
        return Wizard(
            self._settings(), monitor, ConversationRegistry(),
            HistoryReader(lambda **kw: list(rows()[GROUP]), sender_source="members"),
            tenant=self.tenant,
            orders=orders,
        )

    def _settings(self):
        from wechat_ai_assistant.runmode import SettingsStore

        return SettingsStore(self.dir / "settings.json")

    def test_the_wizard_gets_the_store_from_the_host(self):
        wizard = self._wizard(self.store)
        self.assertIs(wizard.orders(), self.store)

    def test_no_pipeline_is_needed_for_reading_history(self):
        """以前没有 pipeline 就抛异常，真实客户端首读必失败。"""
        wizard = self._wizard(self.store)
        self.assertIsNone(wizard.pipeline, "这个用例的前提就是没登录云端")
        self.assertIs(wizard.orders(), self.store)

    def test_a_wizard_with_no_store_at_all_says_so_clearly(self):
        wizard = self._wizard(None)
        with self.assertRaises(RuntimeError) as caught:
            wizard.orders()
        self.assertIn("no local order store", str(caught.exception))


class RealClientAssemblyTests(unittest.TestCase):
    """按 merchant_app 的真实装配走一遍。"""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-real-")
        self.dir = Path(self._tmp.name)
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.tenant = self.store.create_merchant("真实装配店")["merchant_id"]
        self.app = MerchantApplication(
            data_dir=self.dir, orders=self.store, tenant=self.tenant,
            probe=None, launcher=None,
            conversation_source=SimulatedConversations(
                [(GROUP, ConversationKind.GROUP, 128)]
            ),
            history_reader_fn=SimulatedHistory(rows(), delay=0.01),
            simulated=True,
        )

    def tearDown(self):
        for closer in (self.app.merger, self.app.cursor, self.app.agent_store):
            try:
                closer.close()
            except Exception:
                pass
        self.app.close()
        self.store.close()
        self._tmp.cleanup()

    def test_reading_history_works_before_any_cloud_login(self):
        """商户第一次用就是没登录的状态。这条路径必须能跑通。"""
        self.assertIsNone(self.app.pipeline, "前提：没有云端会话")
        self.app.wizard.step = WizardStep.HISTORY
        self.app.wizard.choose_history_conversations(
            list(self.app.wizard.refresh_conversations())[:1]
        )
        result = self.app.wizard.read_history(range_kind="last_week", max_messages=200)
        self.assertGreater(result.total_messages, 0, "一条都没读到")
        self.assertGreater(result.imported, 0, "读到了但没入库")

    def test_messages_really_land_in_the_local_database(self):
        self.app.wizard.step = WizardStep.HISTORY
        self.app.wizard.choose_history_conversations(
            list(self.app.wizard.refresh_conversations())[:1]
        )
        self.app.wizard.read_history(range_kind="last_week", max_messages=200)
        count = self.store.db.execute(
            "SELECT COUNT(*) c FROM messages WHERE merchant_id=?", (self.tenant,)
        ).fetchone()["c"]
        self.assertGreater(count, 0, "本地订单库里没有消息")

    def test_the_wizard_and_the_app_share_one_store(self):
        """两处各开一个库会写出两份互不可见的数据。"""
        self.assertIs(self.app.wizard.orders(), self.store)

    def test_viewing_local_data_needs_no_login(self):
        self.app.wizard.step = WizardStep.HISTORY
        self.app.wizard.choose_history_conversations(
            list(self.app.wizard.refresh_conversations())[:1]
        )
        self.app.wizard.read_history(range_kind="last_week", max_messages=200)
        view = self.app.refresh_home()
        self.assertIsNotNone(view, "未登录时连本地首页都打不开")


if __name__ == "__main__":
    unittest.main()