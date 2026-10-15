"""摘要与参考信息的**共用逻辑**。

## 为什么这些测试不测界面

取哪些消息、能不能放行、密钥从哪来 —— 这几件事原本写在 ``merchant_ui.py`` 里。
现在搬到 ``summary_task``，因为**两个界面都要用**：

* 商户端 Tk 界面
* 开发调试用的 Web 界面

## 为什么不用「读源码字符串」来测

原来那些测试是 ``ast`` 读函数体、断言里面有没有 ``GROUP BY``、有没有
``LIMIT``。逻辑一搬，它们就红了 —— 而**它们本该搬**，因为那些断言验的是
「这段代码在某个文件里」，不是「它的行为对不对」。

所以这里改成**直接 import 真的函数、真的调用**：桩上造出数据，断言拿到的结果。
行为对就过，行为错就红，与代码长什么样无关。

## 三处最容易出错的地方

1. **按会话取而不是按最后一批导入** —— 否则「更新」之后摘要范围塌缩
2. **多条候选时不猜** —— 匹配到多于一个必须留空，让商户选
3. **放行判据要看库里有没有**，不能只看本次读到几条
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
import tempfile
import types
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from pyweixin import summary_task  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402

GROUP = "鲜果群"


def _app_for(store, tenant):
    """一个够用的「应用对象」。只用 orders / tenant 两个属性。"""
    return types.SimpleNamespace(orders=store, tenant=tenant)


def _result(store, tenant, *, total, groups=(GROUP,), import_ids=()):
    """造一个形状和 HistoryResult 一样的读取结果。"""
    coverage = []
    for name in groups:
        item = types.SimpleNamespace(conversation=types.SimpleNamespace(name=name))
        coverage.append(types.SimpleNamespace(
            conversation=item.conversation, message_count=1,
            skipped=False, error="",
            earliest=dt.datetime(2026, 10, 14), latest=dt.datetime(2026, 10, 14)))
    return types.SimpleNamespace(total_messages=total, coverage=coverage,
                                 import_ids=list(import_ids))


class MessagesForSummaryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.app = _app_for(self.store, self.tenant)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _add(self, source, rows):
        return self.store.add_import(self.tenant, GROUP, [
            {"sender": s, "content": c, "kind": k,
             "sent_at": f"2026-10-14T09:{i:02d}:00-04:00",
             "source_id": f"{source}-{i}"}
            for i, (s, c, k) in enumerate(rows)], source, "demo")

    def test_it_takes_everything_the_conversation_has(self):
        """按会话取，不按「最后一批导入」取。"""
        self._add("all", [("张姐", "要两箱苹果", "text")] * 3)
        self._add("new", [("李叔", "李记酱油 2 瓶", "text")])
        result = _result(self.store, self.tenant, total=1,
                         import_ids=[self.store.db.execute(
                             "SELECT id FROM imports WHERE source='new' AND merchant_id=?",
                             (self.tenant,)).fetchone()["id"]])
        got = summary_task.messages_for_summary(self.app, result)
        self.assertEqual(len(got), 4, "更新之后应该看到全部 4 条，而不是只剩新那 1 条")

    def test_the_kind_travels_with_the_message(self):
        """图片/语音的 content 是占位符甚至空的，不带 kind 模型无从知道。"""
        self._add("mixed", [("李叔", "图片", "image"), ("王姨", "", "voice")])
        got = summary_task.messages_for_summary(self.app, _result(self.store, self.tenant, total=2))
        kinds = {m["sender"]: m["kind"] for m in got}
        self.assertEqual(kinds, {"李叔": "image", "王姨": "voice"})

    def test_it_falls_back_to_the_import_batch_without_coverage(self):
        """旧的结果对象可能没有 coverage —— 那条路不能塌。"""
        imported = self._add("only", [("张姐", "要两箱苹果", "text")])
        got = summary_task.messages_for_summary(
            self.app, types.SimpleNamespace(total_messages=1, coverage=[],
                                            import_ids=[imported["import_id"]]))
        self.assertEqual(len(got), 1)

    def test_no_coverage_and_no_imports_is_empty_not_an_error(self):
        got = summary_task.messages_for_summary(
            self.app, types.SimpleNamespace(total_messages=0, coverage=[], import_ids=[]))
        self.assertEqual(got, [])

    def test_the_order_is_chronological(self):
        """倒序取最近的那批，交给模型前要正回来。"""
        self._add("ordered", [("甲", "第一条", "text"), ("乙", "第二条", "text")])
        got = summary_task.messages_for_summary(self.app, _result(self.store, self.tenant, total=2))
        self.assertEqual([m["content"] for m in got], ["第一条", "第二条"])

    def test_the_limit_is_applied(self):
        self.assertGreater(summary_task.SUMMARY_MESSAGE_LIMIT, 0)
        source = (ROOT / "src/pyweixin/summary_task.py").read_text(encoding="utf-8")
        self.assertIn("LIMIT", source)


class HasMessagesForTests(unittest.TestCase):
    """放行的唯一判据。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.app = _app_for(self.store, self.tenant)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_this_read_found_messages(self):
        self.assertTrue(summary_task.has_messages_for(
            self.app, types.SimpleNamespace(total_messages=3, coverage=[], import_ids=[])))

    def test_nothing_found_and_nothing_stored_is_blocked(self):
        self.assertFalse(summary_task.has_messages_for(
            self.app, types.SimpleNamespace(total_messages=0, coverage=[], import_ids=[])))

    def test_no_result_is_blocked(self):
        self.assertFalse(summary_task.has_messages_for(self.app, None))

    def test_the_update_case_the_bug_was_about(self):
        """更新发现没有新消息（本次 0 条），但**库里已有** —— 必须放行。

        只看 total_messages 就会出现「按钮出现了，一点却弹还没有读到消息」。
        """
        self.store.add_import(self.tenant, GROUP, [
            {"sender": "张姐", "content": "要两箱苹果",
             "sent_at": "2026-10-14T09:00:00-04:00", "source_id": "a-0"}], "old", "demo")
        self.assertTrue(summary_task.has_messages_for(
            self.app, _result(self.store, self.tenant, total=0)),
            "库里明明有消息，却说不可以继续")


class AccumulatedCoverageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.app = _app_for(self.store, self.tenant)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_it_counts_by_conversation(self):
        self.store.add_import(self.tenant, GROUP, [
            {"sender": "张姐", "content": "第一条", "sent_at": "2026-10-01T09:00:00-04:00", "source_id": "x-0"},
            {"sender": "张姐", "content": "第二条", "sent_at": "2026-10-14T09:00:00-04:00", "source_id": "x-1"},
        ], "probe", "demo")
        got = summary_task.accumulated_coverage(self.app, _result(self.store, self.tenant, total=2))
        self.assertEqual(got[GROUP][0], 2)
        self.assertTrue(got[GROUP][1].startswith("2026-10-01"))
        self.assertTrue(got[GROUP][2].startswith("2026-10-14"))

    def test_a_broken_store_does_not_raise(self):
        """一次查询失败不该让读取结果整页画不出来。"""
        class Broken:
            @property
            def db(self):
                raise RuntimeError("db gone")
        broken = types.SimpleNamespace(orders=Broken(), tenant=self.tenant)
        self.assertEqual(summary_task.accumulated_coverage(
            broken, _result(self.store, self.tenant, total=1)), {})

    def test_a_window_without_an_app_is_tolerated(self):
        """渲染逻辑会被桩对象直接驱动，桩上可能没有 app。"""
        self.assertEqual(summary_task.accumulated_coverage(
            types.SimpleNamespace(), _result(self.store, self.tenant, total=1)), {})


class ImportReferenceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.app = _app_for(self.store, self.tenant)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_products_go_in(self):
        result = summary_task.import_reference(
            self.app, "products", "sku,name,unit,price\nP-APL,红富士苹果,箱,58.50\n")
        self.assertEqual(result["created"], 1)
        self.assertNotIn("error", result)

    def test_customers_go_in(self):
        result = summary_task.import_reference(
            self.app, "customers", "name,group\n张姐,鲜果群\n")
        self.assertEqual(result["created"], 1)
        self.assertNotIn("error", result)

    def test_an_unknown_kind_says_so(self):
        result = summary_task.import_reference(self.app, "widgets", "a\n1\n")
        self.assertIn("error", result)


class CloudSummaryTests(unittest.TestCase):
    """摘要走云网关：没登录直说，本机不再读任何密钥。"""

    def _app(self, client):
        return types.SimpleNamespace(client=client, orders=None, tenant="t")

    def _messages(self, app):
        with unittest.mock.patch(
                "pyweixin.summary_task.messages_for_summary",
                return_value=[{"sender": "张姐", "content": "苹果两箱",
                               "sent_at": "2026-10-13 10:00", "kind": "text"}]):
            return app

    def test_logged_out_says_so(self):
        app = self._app(None)
        with unittest.mock.patch(
                "pyweixin.summary_task.messages_for_summary",
                return_value=[{"sender": "张姐", "content": "苹果两箱",
                               "sent_at": "2026-10-13 10:00", "kind": "text"}]):
            outcome = summary_task.run_summary(app, object())
        self.assertEqual(outcome["summary"], "")
        self.assertIn("登录", outcome["error"])
        self.assertEqual(outcome["message_count"], 1)

    def test_jyi_runs_inline_without_billing(self):
        """JYI 直调：提示词同样复用，不计本地账。"""
        import types as _types

        from pyweixin.jyi_client import JYIClient

        client = JYIClient()
        client.session = _types.SimpleNamespace(access_token="tok")
        seen = {}

        def _chat(model, messages, **kwargs):
            seen["model"] = model
            seen["n"] = len(messages)
            return {"choices": [{"message": {"content": "张姐要两箱苹果"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 5}}

        client.chat_completions = _chat
        with unittest.mock.patch(
                "pyweixin.summary_task.messages_for_summary",
                return_value=[{"sender": "张姐", "content": "苹果两箱",
                               "sent_at": "2026-10-13 10:00", "kind": "text"}]):
            outcome = summary_task.run_summary(self._app(client), object())
        self.assertEqual(outcome["summary"], "张姐要两箱苹果")
        self.assertEqual(outcome["error"], "")
        self.assertIsNone(outcome["cost"])
        self.assertTrue(seen["model"])
        self.assertEqual(seen["n"], 2)

    def test_submit_poll_succeed(self):
        states = [
            {"job": {"job_id": "job1", "state": "running"}, "billing": {}},
            {"job": {"job_id": "job1", "state": "succeeded",
                     "result": {"summary": "张姐要两箱苹果"}},
             "billing": {"available": True, "charged_micro_usd": 7}},
        ]

        class FakeClient:
            session = types.SimpleNamespace(access_token="tok")

            def submit_summary(self, request, key):
                assert request["task"] == "summarize" if "task" in request else True
                assert key.startswith("sum-")
                assert request["messages"][0]["content"] == "苹果两箱"
                return {"job": {"job_id": "job1"}}

            def summary_job(self, job_id):
                assert job_id == "job1"
                return states.pop(0)

        with unittest.mock.patch(
                "pyweixin.summary_task.messages_for_summary",
                return_value=[{"sender": "张姐", "content": "苹果两箱",
                               "sent_at": "2026-10-13 10:00", "kind": "text"}]), \
            unittest.mock.patch("pyweixin.summary_task.SUMMARY_POLL_INTERVAL", 0):
            outcome = summary_task.run_summary(self._app(FakeClient()), object())
        self.assertEqual(outcome["summary"], "张姐要两箱苹果")
        self.assertEqual(outcome["error"], "")
        self.assertEqual(outcome["cost"], 7)

    def test_failed_job_reports_message(self):
        class FakeClient:
            session = types.SimpleNamespace(access_token="tok")

            def submit_summary(self, request, key):
                return {"job": {"job_id": "job9"}}

            def summary_job(self, job_id):
                return {"job": {"job_id": "job9", "state": "failed",
                                "error": {"code": "backend_unavailable",
                                          "message": "服务暂时不可用"}},
                        "billing": {}}

        with unittest.mock.patch(
                "pyweixin.summary_task.messages_for_summary",
                return_value=[{"sender": "张姐", "content": "苹果两箱",
                               "sent_at": "2026-10-13 10:00", "kind": "text"}]), \
            unittest.mock.patch("pyweixin.summary_task.SUMMARY_POLL_INTERVAL", 0):
            outcome = summary_task.run_summary(self._app(FakeClient()), object())
        self.assertEqual(outcome["summary"], "")
        self.assertIn("服务暂时不可用", outcome["error"])


class GatewayErrorTextTests(unittest.TestCase):
    """网关原话不能掉地上：读错字段就是一句"提交失败"打发人。"""

    def test_merchant_message_is_used(self):
        from pyweixin.client_login import GatewayError

        exc = GatewayError("upstream_quota_used",
                           "This connection has used its quota. Raise its slice.")
        text = summary_task._gateway_error_text(exc, max_len=300)
        self.assertIn("used its quota", text)
        self.assertNotIn("请稍后重试", text)

    def test_unknown_technical_error_stays_generic(self):
        text = summary_task._gateway_error_text(OSError("boom"))
        self.assertEqual(text, "提交失败，请稍后重试")


if __name__ == "__main__":
    unittest.main()