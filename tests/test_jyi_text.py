"""JYI 文字链路的回归测试：提示词复用、输出校验、服务分流、额度门。

约定：钥匙用 JYI 登录态，不读本地密钥；计费不在本地算；
提示词与校验和服务端/产品网关用同一份，不另写。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "packages" / "order_agent" / "src"))
sys.path.insert(0, str(_ROOT / "packages" / "business_agent" / "src"))

from pyweixin import jyi_parse  # noqa: E402
from pyweixin.jyi_client import JYI_AUTH_ORIGIN, JYI_GATEWAY, JYIClient  # noqa: E402


def _chat_body(text, prompt_tokens=10, completion_tokens=5):
    return {"choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": prompt_tokens,
                      "completion_tokens": completion_tokens}}


from tests.test_product_gateway import VALID_PARSE_RESULT  # noqa: E402

VALID_ORDER_JSON = json.dumps(VALID_PARSE_RESULT, ensure_ascii=False)


class PromptTests(unittest.TestCase):
    def test_order_prompt_reuses_plugin(self):
        from order_agent.plugin import SYSTEM_PROMPT

        messages = jyi_parse.build_order_messages({"a": 1})
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], SYSTEM_PROMPT)
        self.assertEqual(json.loads(messages[1]["content"]), {"a": 1})

    def test_summary_prompt_reuses_summary(self):
        from pyweixin.summary import SUMMARY_SYSTEM_PROMPT

        messages = jyi_parse.build_summary_messages([
            {"sender": "张姐", "content": "苹果两箱",
             "sent_at": "2026-10-13 10:00", "kind": "text"}])
        self.assertEqual(messages[0]["content"], SUMMARY_SYSTEM_PROMPT)
        self.assertIn("张姐", messages[1]["content"])

    def test_extract_text_and_usage(self):
        self.assertEqual(
            jyi_parse.extract_text(_chat_body("hi")), "hi")
        self.assertEqual(jyi_parse.extract_text({}), "")
        self.assertEqual(
            jyi_parse.extract_usage(_chat_body("hi")),
            {"input_tokens": 10, "output_tokens": 5})
        self.assertEqual(jyi_parse.extract_usage({}),
                         {"input_tokens": 0, "output_tokens": 0})

    def test_parse_order_json_validates(self):
        from pyweixin.opencode import InvalidParseOutput

        result = jyi_parse.parse_order_json(VALID_ORDER_JSON)
        self.assertEqual(result["intent"], "new_order")
        with self.assertRaises(InvalidParseOutput):
            jyi_parse.parse_order_json("不是 json")
        with self.assertRaises(InvalidParseOutput):
            jyi_parse.parse_order_json("```json\n" + VALID_ORDER_JSON + "\n```"
                                       if False else "```\n乱写\n```")


class ServiceTests(unittest.TestCase):
    def _service(self, chat):
        from pyweixin.client_login import PendingQueue
        from pyweixin.orders import OrderStore

        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-jyi-")
        store = OrderStore(str(Path(self._tmp.name) / "o.db"))
        tenant = store.create_merchant("t")["merchant_id"]
        store.add_customer(tenant, "张老板", "老张", "鲜果群")
        store.add_product(tenant, "SKU-1", "苹果", "箱", 5000, ["苹果"])
        customer = store.db.execute(
            "SELECT id FROM customers WHERE merchant_id=?",
            (tenant,)).fetchone()["id"]
        client = JYIClient()
        client.chat_completions = chat
        queue = PendingQueue(str(Path(self._tmp.name) / "q.db"))
        service = jyi_parse.JYIParseService(
            orders=store, client=client, queue=queue, tenant=tenant)
        self._queue = queue
        return service, store, tenant, customer

    def tearDown(self):
        queue = getattr(self, "_queue", None)
        if queue is not None:
            try:
                queue.close()
            except Exception:
                pass
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def _seed(self, store, tenant, customer):
        store.add_import(tenant, "鲜果群", [
            {"sender": "老张", "content": "来两箱苹果", "kind": "text",
             "sent_at": "2026-01-02T09:00:00+00:00", "source_id": "m1"}],
            source="seed", source_type="test")
        row = store.db.execute(
            "SELECT * FROM messages WHERE merchant_id=? ORDER BY rowid DESC LIMIT 1",
            (tenant,)).fetchone()
        store.bind_message_customer(tenant, row["id"], customer, "tester")
        return row["id"]

    def test_submit_applies_order(self):
        service, store, tenant, customer = self._service(
            lambda *a, **k: _chat_body(VALID_ORDER_JSON))
        message_id = self._seed(store, tenant, customer)
        progress = service.submit(message_id)
        self.assertEqual(progress.state, "done")
        self.assertTrue(progress.order_id)
        self.assertEqual(service.last_usage(),
                         {"input_tokens": 10, "output_tokens": 5})
        # settle 直接返回 submit 的结果，不再跑一遍。
        settled = service.settle()
        self.assertEqual(settled.order_id, progress.order_id)
        store.close()

    def test_invalid_json_goes_to_review(self):
        service, store, tenant, customer = self._service(
            lambda *a, **k: _chat_body("胡话"))
        message_id = self._seed(store, tenant, customer)
        progress = service.submit(message_id)
        self.assertTrue(progress.needs_review)
        store.close()

    def test_network_error_is_retryable(self):
        from pyweixin.client_login import GatewayError
        from pyweixin.local_parse import LOCAL_NEEDS_RETRY

        def _down(*a, **k):
            raise GatewayError("network_unavailable", "网络暂不可用")

        service, store, tenant, customer = self._service(_down)
        message_id = self._seed(store, tenant, customer)
        progress = service.submit(message_id)
        self.assertEqual(progress.state, LOCAL_NEEDS_RETRY)
        store.close()


class WiringTests(unittest.TestCase):
    def test_factory_picks_jyi_service(self):
        from pyweixin.client_login import Session
        from pyweixin.merchant_app import build_service_factory
        from pyweixin.local_parse import PendingQueue
        from pyweixin.orders import OrderStore

        tmp = tempfile.TemporaryDirectory(prefix="pyw-jyif-")
        queue = None
        try:
            store = OrderStore(str(Path(tmp.name) / "o.db"))
            tenant = store.create_merchant("t")["merchant_id"]
            queue = PendingQueue(str(Path(tmp.name) / "q.db"))
            factory = build_service_factory(store, tenant, JYI_GATEWAY, queue)
            session = Session(
                "jyi_sk_x", "d", "c", provider="jyi",
                gateway_url=JYI_GATEWAY, auth_origin=JYI_AUTH_ORIGIN)
            service = factory(session)
            self.assertIsInstance(service, jyi_parse.JYIParseService)
            store.close()
        finally:
            if queue is not None:
                try:
                    queue.close()
                except Exception:
                    pass
            tmp.cleanup()

    def test_quota_gate_skips_jyi(self):
        import tempfile

        from pyweixin.conversations import ConversationRegistry
        from pyweixin.history_read import HistoryReader
        from pyweixin.orders import OrderStore
        from pyweixin.runmode import SettingsStore
        from pyweixin.wechat_state import SimulatedWeChat, WeChatMonitor
        from pyweixin.wizard import Wizard

        tmp = tempfile.TemporaryDirectory(prefix="pyw-jyiq-")
        try:
            store = OrderStore(str(Path(tmp.name) / "o.db"))
            tenant = store.create_merchant("t")["merchant_id"]
            wizard = Wizard(
                SettingsStore(str(Path(tmp.name) / "s.json")),
                WeChatMonitor(SimulatedWeChat(["logged_in"])),
                ConversationRegistry(),
                HistoryReader(lambda **kw: []),
                tenant=tenant, orders=store)
            wizard.client = JYIClient()
            # 无额度数据也不抛：JYI 按账号计量，不过本地门。
            wizard.require_quota()
            store.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
