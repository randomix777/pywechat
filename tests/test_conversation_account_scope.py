"""会话缓存按账号隔离：切号旧名单必须作废。

纯本地装配（OrderStore 内存库，不连微信不联网）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.application import MerchantApplication  # noqa: E402
from wechat_ai_assistant.conversations import Conversation, ConversationKind  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402


def _app(data_dir, store, tenant):
    return MerchantApplication(data_dir=data_dir, orders=store, tenant=tenant)


class AccountScopedCacheTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-acctscope-")
        self.dir = Path(self._tmp.name)
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.tenant = self.store.create_merchant("切号店")["merchant_id"]
        self.app = _app(self.dir, self.store, self.tenant)

    def tearDown(self):
        try:
            self.app.close()
        except Exception:
            pass
        self.store.close()
        self._tmp.cleanup()

    def _groups(self):
        return [Conversation(name="旧号群", kind=ConversationKind.GROUP, member_count=10)]

    def test_save_and_restore_same_account(self):
        self.app.wizard.settings.account_id = "wxid_old"
        self.app._save_conversation_cache(self._groups())
        self.app.conversations.refresh([])
        self.assertEqual(self.app._restore_conversation_cache(), 1)
        self.assertEqual([c.name for c in self.app.conversations.available], ["旧号群"])

    def test_restore_ignores_other_account(self):
        self.app.wizard.settings.account_id = "wxid_old"
        self.app._save_conversation_cache(self._groups())
        self.app.wizard.settings.account_id = "wxid_new"
        self.app.conversations.refresh([])
        self.assertEqual(self.app._restore_conversation_cache(), 0)
        self.assertEqual(list(self.app.conversations.available), [])

    def test_switch_clears_the_cache_file(self):
        self.app.wizard.settings.account_id = "wxid_old"
        self.app._save_conversation_cache(self._groups())
        self.assertTrue(self.app._conversations_cache_path().exists())
        self.app.wizard.settings.account_id = "wxid_new"
        self.app._clear_conversation_cache()
        self.assertFalse(self.app._conversations_cache_path().exists())

    def test_empty_result_never_overwrites(self):
        self.app.wizard.settings.account_id = "wxid_old"
        self.app._save_conversation_cache(self._groups())
        before = self.app._conversations_cache_path().read_text(encoding="utf-8")
        self.app._save_conversation_cache([])
        after = self.app._conversations_cache_path().read_text(encoding="utf-8")
        self.assertEqual(before, after)


class WxidAliasTests(unittest.TestCase):
    """wxid 显示名备注：内存解不出的，商户手备注一次一直有效。"""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-alias-")
        self.dir = Path(self._tmp.name)
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.tenant = self.store.create_merchant("备注店")["merchant_id"]
        from wechat_ai_assistant.application import MerchantApplication

        self.app = MerchantApplication(
            data_dir=self.dir, orders=self.store, tenant=self.tenant)

    def tearDown(self):
        try:
            self.app.close()
        except Exception:
            pass
        self.store.close()
        self._tmp.cleanup()

    def test_set_and_display(self):
        self.assertTrue(self.app.set_alias("123@chatroom", "水果群"))
        self.assertEqual(self.app.display_name_for("123@chatroom"), "水果群")
        self.assertEqual(self.app.display_name_for("没备注的"), "没备注的")
        self.assertEqual(self.app.display_name_for(""), "")

    def test_alias_survives_restart(self):
        from wechat_ai_assistant.application import MerchantApplication

        self.app.set_alias("123@chatroom", "水果群")
        rebooted = MerchantApplication(
            data_dir=self.dir, orders=self.store, tenant=self.tenant)
        try:
            self.assertEqual(rebooted.display_name_for("123@chatroom"), "水果群")
        finally:
            try:
                rebooted.close()
            except Exception:
                pass

    def test_empty_name_deletes(self):
        self.app.set_alias("123@chatroom", "水果群")
        self.app.set_alias("123@chatroom", "  ")
        self.assertEqual(self.app.display_name_for("123@chatroom"), "123@chatroom")
        self.assertFalse(self.app.set_alias("", "水果群"))


if __name__ == "__main__":
    unittest.main()
