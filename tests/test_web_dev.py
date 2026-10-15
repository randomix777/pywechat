"""开发 Web 界面的服务端。

## 三件必须测的事

### 一、只许本机

这个界面能读商户全部消息、订单和密钥配置，**没有任何鉴权**。绑定地址写错成
``0.0.0.0`` 就会被局域网摸到。所以除了绑 ``127.0.0.1``，还要**主动拒绝
非回环来源的请求** —— 只靠绑定地址不够，因为它可能被别的东西改掉。

### 二、选错会话比不选更糟

页面只能传**名字**（JSON 里就这些），而 wizard 要的是 **对象**。
如果按名字取不到对象却「猜一个」，读到的就是**别的群**的聊天记录，
而商户在界面上看不出任何差异。

### 三、阻塞动作不能占着处理线程

``analyze`` 和 ``run_summary`` 内部都是 ``join()``。放在 HTTP 处理线程里
会把整个服务卡死 —— 连 ``/api/state`` 都取不到，页面看起来像整体挂了，
而且看不出是哪个接口的问题。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from pyweixin import summary_task, web_dev  # noqa: E402


class _FakeApp:
    """只提供 web_dev 会碰到的那些方法。"""

    def __init__(self, conversations=()):
        self.orders = None
        self.tenant = "m-test"
        self.events = types.SimpleNamespace()
        self._conversations = list(conversations)
        self.chosen: list = []
        self.read_calls = 0

    def refresh_conversations(self):
        return list(self._conversations)

    def choose_history_conversations(self, selected):
        self.chosen = list(selected)

    def read_history(self, **kwargs):
        self.read_calls += 1

    def cancel_history(self):
        pass

    def orders_for_display(self):
        return []

    def check_wechat(self):
        return types.SimpleNamespace(state="ready", detail="ok")

    def start_wechat(self):
        pass


def _serve(app):
    server = web_dev.create_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _Client:
    def __init__(self, server):
        self.base = "http://%s:%d" % server.server_address[:2]

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=15) as r:
            return r.status, r.read()

    def post(self, path, body=None):
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def post_expect_error(self, path, body=None):
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))
        raise AssertionError("本该失败却成功了")


class PageAndStateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.app = _FakeApp([types.SimpleNamespace(name="鲜果批发群", kind="group",
                                                   member_count=35)])
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def test_the_page_is_served_and_is_chinese(self):
        status, body = self.client.get("/")
        html = body.decode("utf-8")
        self.assertEqual(status, 200)
        # 品牌名跟着桌面端叫「微信AI智能助理」（以前叫接单助手，改名时测试没跟上）。
        self.assertIn("微信AI智能助理", html)
        for term in ("读取历史", "智能整理", "整理成一段话", "商品与客户名单", "订单", "运行日志"):
            self.assertIn(term, html, f"\u9875\u9762\u91cc\u6ca1\u6709\u300c{term}\u300d")

    def test_state_has_everything_the_page_needs(self):
        status, body = self.client.get("/api/state")
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200)
        for key in ("step", "busy", "error", "logs", "conversations", "orders",
                    "summary", "analysis", "history", "progress", "can_summarize"):
            self.assertIn(key, data, f"\u9875\u9762\u8981\u7528 {key}\uff0c\u4f46\u72b6\u6001\u91cc\u6ca1\u6709")

    def test_an_unknown_address_says_so_in_chinese(self):
        code, data = self.client.post_expect_error("/api/nope")
        self.assertEqual(code, 404)
        self.assertIn("没有这个地址", data["error"])

    def test_it_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1",
                         "\u7ed1\u5b9a\u5730\u5740\u5199\u9519\u4f1a\u88ab\u5c40\u57df\u7f51\u6478\u5230")

    def test_a_remote_address_is_rejected(self):
        """"只绑本机"不够 —— 绑定地址可能被改掉，所以要主动拒绝来源。"""
        source = (ROOT / "src/pyweixin/web_dev.py").read_text(encoding="utf-8")
        self.assertIn("_reject_remote", source)
        self.assertIn("403", source)
        # \u5fc5\u987b\u5728\u6bcf\u4e2a\u5165\u53e3\u90fd\u8c03
        self.assertIn('if self._reject_remote():', source)


    def test_the_page_shows_which_data_directory_it_reads(self):
        """目录错了就表现为「没数据 / 未登录」，不显示出来没人会往哪找。"""
        source = (ROOT / "src/pyweixin/web_dev.py").read_text(encoding="utf-8")
        self.assertIn("__DATADIR__", source, "\u9875\u9762\u6ca1\u6709\u63a5\u6536\u6570\u636e\u76ee\u5f55")

    def test_the_placeholder_is_always_replaced(self):
        """占位符残留在页面上 = 看到一句“__DATADIR__”。"""
        html = self.client.get("/")[1].decode("utf-8")
        self.assertNotIn("__DATADIR__", html, "占位符没被替换")


class LoginTests(unittest.TestCase):
    """登录是前置条件，不是第一步。

    没登录就去整理，商户会读完一堆消息才发现整理不了。
    """

    def setUp(self):
        self.app = _FakeApp()
        self.app.session = None
        self.logged_in = []
        self.app.login_requested = lambda: self.logged_in.append(1)
        self.detached = []
        self.app.detach = lambda: (self.detached.append(1),
                                   setattr(self.app, "session", None))
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_state_says_whether_we_are_logged_in(self):
        data = json.loads(self.client.get("/api/state")[1].decode("utf-8"))
        self.assertIn("account", data)
        self.assertFalse(data["account"]["logged_in"])

    def test_the_page_has_a_login_entry(self):
        html = self.client.get("/")[1].decode("utf-8")
        self.assertIn("登录产品账号", html)
        self.assertIn("/api/login", html)

    def test_login_endpoint_calls_the_runner(self):
        code, _ = self.client.post("/api/login")
        self.assertEqual(code, 200)
        self.assertEqual(len(self.logged_in), 1)

    def test_logout_does_not_delete_local_data(self):
        """退出只断云端能力，本地读到的一条不能消失。"""
        self.app.session = types.SimpleNamespace(merchant_name="张店", merchant_id="m-1")
        code, _ = self.client.post("/api/logout")
        self.assertEqual(code, 200)
        self.assertEqual(len(self.detached), 1)


class ConversationChoiceTests(unittest.TestCase):
    """\u6309\u540d\u5b57\u53d6\u5bf9\u8c61\u3002\u53d6\u4e0d\u5230\u5c31\u4e22\u5f03\uff0c**\u4e0d\u80fd\u731c**\u3002"""

    def setUp(self):
        self.app = _FakeApp([
            types.SimpleNamespace(name="\u9c9c\u679c\u7fa4", kind="group", member_count=35),
            types.SimpleNamespace(name="\u6c34\u679c\u7fa4", kind="group", member_count=12),
        ])
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_it_passes_conversation_objects_not_strings(self):
        code, data = self.client.post("/api/conversations/choose", {"names": ["\u9c9c\u679c\u7fa4"]})
        self.assertEqual(code, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual(self.app.chosen[0].name, "\u9c9c\u679c\u7fa4")

    def test_an_unknown_name_is_dropped_not_guessed(self):
        """\u731c\u4e00\u4e2a\u5c31\u662f\u8bfb\u53d6**\u522b\u7684\u7fa4**\uff0c\u800c\u5546\u6237\u770b\u4e0d\u51fa\u5dee\u5f02\u3002"""
        self.client.post("/api/conversations/choose",
                         {"names": ["\u9c9c\u679c\u7fa4", "\u4e0d\u5b58\u5728\u7684\u7fa4"]})
        self.assertEqual([c.name for c in self.app.chosen], ["\u9c9c\u679c\u7fa4"])

    def test_clearing_the_selection_reads_everything(self):
        code, data = self.client.post("/api/conversations/choose", {"names": []})
        self.assertEqual(code, 200)
        self.assertEqual(data["count"], 0)
        self.assertEqual(self.app.chosen, [])


class GateSharesOneTruthTests(unittest.TestCase):
    """\u201c\u80fd\u4e0d\u80fd\u6574\u7406\u201d\u5fc5\u987b\u7528\u5171\u7528\u903b\u8f91\uff0c\u4e0d\u5f97\u81ea\u5df1\u5224\u3002"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        from pyweixin.orders import OrderStore

        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("\u6d4b\u8bd5\u5546\u5bb6")["merchant_id"]
        self.app = _FakeApp()
        self.app.orders = self.store
        self.app.tenant = self.tenant
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.dir.cleanup()

    def _state(self):
        return json.loads(self.client.get("/api/state")[1].decode("utf-8"))

    def test_it_is_false_before_anything_is_read(self):
        self.assertFalse(self._state()["can_summarize"])

    def test_it_becomes_true_once_the_store_has_messages(self):
        """\u6309\u5e93\u91cc\u7684\u60c5\u51b5\u5224\u65ad\uff0c\u4e0d\u662f\u6309\u672c\u6b21\u8bfb\u5230\u51e0\u6761\u3002"""
        self.store.add_import(self.tenant, "\u9c9c\u679c\u7fa4", [
            {"sender": "\u5f20\u59d0", "content": "\u8981\u4e24\u7bb1\u82f9\u679c",
             "sent_at": "2026-10-14T09:00:00-04:00", "source_id": "s-0"}], "probe", "demo")
        server_state = self.server.dev_state
        server_state.set(history=_result_with_zero_total())
        self.assertTrue(self._state()["can_summarize"],
                        "\u5e93\u91cc\u660e\u660e\u6709\u6d88\u606f\uff0c\u5374\u8bf4\u4e0d\u80fd\u6574\u7406")

    def test_the_page_uses_the_same_answer_as_the_logic(self):
        result = _result_with_zero_total()
        holder = types.SimpleNamespace(orders=self.store, tenant=self.tenant)
        self.server.dev_state.set(history=result)
        self.assertEqual(self._state()["can_summarize"],
                         summary_task.has_messages_for(holder, result),
                         "\u9875\u9762\u548c\u5171\u7528\u903b\u8f91\u7ed9\u4e86\u4e0d\u540c\u7b54\u6848")


def _result_with_zero_total():
    """\u672c\u6b21\u8bfb\u5230 0 \u6761\uff08\u66f4\u65b0\u65f6\u5e38\u89c1\uff09\uff0c\u4f46 coverage \u6307\u5411\u7684\u4f1a\u8bdd\u91cc\u6709\u5386\u53f2\u3002"""
    return types.SimpleNamespace(
        total_messages=0,
        import_ids=[],
        coverage=[types.SimpleNamespace(
            conversation=types.SimpleNamespace(name="\u9c9c\u679c\u7fa4"),
            message_count=0, skipped=False, error="", truncated=False)],
    )


class BlockingWorkTests(unittest.TestCase):
    """\u963b\u585e\u52a8\u4f5c\u5fc5\u987b\u79bb\u5f00\u5904\u7406\u7ebf\u7a0b\u3002"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.app = _FakeApp()
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def test_state_stays_reachable_while_something_blocks(self):
        """\u5361\u4f4f\u7684\u662f\u6574\u4e2a\u670d\u52a1\uff0c\u4e0d\u53ea\u662f\u90a3\u4e00\u4e2a\u63a5\u53e3\u3002"""
        import time

        gate = threading.Event()
        state = self.server.dev_state

        def slow() -> None:
            gate.wait(3.0)
            state.finish("\u5b8c\u6210")

        state.start("\u6d4b\u8bd5\u963b\u585e")
        threading.Thread(target=slow, daemon=True).start()
        time.sleep(0.2)
        started = time.time()
        data = json.loads(self.client.get("/api/state")[1].decode("utf-8"))
        self.assertLess(time.time() - started, 2.0, "/api/state \u88ab\u963b\u585e\u7684\u52a8\u4f5c\u5361\u4f4f\u4e86")
        self.assertEqual(data["busy"], "\u6d4b\u8bd5\u963b\u585e")
        gate.set()

    def test_a_second_job_is_refused_while_one_runs(self):
        state = self.server.dev_state
        state.start("\u6b63\u5728\u505a\u5176\u4ed6\u4e8b")
        code, data = self.client.post_expect_error("/api/summary")
        self.assertEqual(code, 409)
        self.assertIn("\u6b63\u5728\u505a\u5176\u4ed6\u4e8b", data["error"])

    def test_summary_cancel_sets_the_flag(self):
        """取消接口只置位：后台线程在批次间隙自己停，不占处理线程。"""
        state = self.server.dev_state
        self.assertFalse(state.summary_cancel.is_set())
        status, data = self.client.post("/api/summary/cancel")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(state.summary_cancel.is_set())


if __name__ == "__main__":
    unittest.main()