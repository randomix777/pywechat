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

from wechat_ai_assistant import summary_task  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402

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
        source = (ROOT / "src/wechat_ai_assistant/summary_task.py").read_text(encoding="utf-8")
        self.assertIn("LIMIT", source)


class MessagesForHistoryTests(unittest.TestCase):
    """第 6 步「分析历史消息」走的取数路径。"""

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

    def test_history_messages_carry_ids(self):
        """回归：以前没 SELECT id，``_row_to_message`` 取 ``row['id']`` 直接
        抛 KeyError，点「是」分析历史消息当场炸，界面上看着就是没反应。"""
        self._add("h", [("张姐", "要两箱苹果", "text")])
        got = summary_task.messages_for_history(
            self.app, _result(self.store, self.tenant, total=1))
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]["id"], "历史消息必须带 id，水位线与导出都靠它")

    def test_history_takes_every_stored_message(self):
        self._add("old", [("张姐", "要两箱苹果", "text")] * 3)
        self._add("new", [("李叔", "李记酱油 2 瓶", "text")])
        result = _result(self.store, self.tenant, total=1, import_ids=[self.store.db.execute(
            "SELECT id FROM imports WHERE source='new' AND merchant_id=?",
            (self.tenant,)).fetchone()["id"]])
        got = summary_task.messages_for_history(self.app, result)
        self.assertEqual(len(got), 4)


class SummaryBatchPlannerTests(unittest.TestCase):
    def test_text_images_and_document_segments_are_separate_and_bounded(self):
        messages = [
            {"sender": "A", "content": "文字一", "kind": "text"},
            {"sender": "B", "content": "文字二", "kind": "text"},
            *[{"sender": "C", "content": f"图片说明{i}", "kind": "image",
               "attachments": [f"image-{i}.jpg"]} for i in range(6)],
            {"sender": "D", "content": "文件说明", "kind": "file",
             "attachments": ["orders.docx"]},
        ]
        document = "段落\n" + ("DOCX正文 " * 5000)
        with unittest.mock.patch("wechat_ai_assistant.media_attachments.document_text",
                                 return_value=document) as extract:
            batches = summary_task._summary_work_batches(
                messages, token_budget=20_000, max_document_chars=4_000)

        self.assertEqual(extract.call_count, 1)
        categories = [category for category, _ in batches]
        self.assertEqual(categories[0], "纯文字")
        self.assertEqual(categories.count("图片"), 2)
        self.assertGreater(categories.count("DOCX/PDF 文档"), 1)
        self.assertEqual(len(set(categories)), 3)
        for category, batch in batches:
            self.assertEqual({item["kind"] for item in batch}, {
                {"纯文字": "text", "图片": "image", "DOCX/PDF 文档": "file"}[category]})
            if category == "图片":
                self.assertLessEqual(len(batch), summary_task.SUMMARY_CHUNK_MAX_IMAGES)
            if category == "DOCX/PDF 文档":
                self.assertTrue(all(len(item["_analysis_document_text"]) <= 4_000
                                    for item in batch))
                self.assertTrue(all(item["_analysis_document_name"] == "orders.docx"
                                    for item in batch))
        recovered = "".join(item["_analysis_document_text"] for category, batch in batches
                            if category == "DOCX/PDF 文档" for item in batch)
        self.assertEqual(recovered, document)

        from wechat_ai_assistant.jyi_parse import add_document_text
        document_batch = next(batch for category, batch in batches
                              if category == "DOCX/PDF 文档")
        prompt = [{"role": "system", "content": "role"},
                  {"role": "user", "content": "分析文件"}]
        with unittest.mock.patch("wechat_ai_assistant.media_attachments.document_text",
                                 side_effect=AssertionError("prepared text should be reused")):
            enriched = add_document_text(prompt, document_batch)
        parts = enriched[-1]["content"]
        self.assertTrue(any("文件附件正文（orders.docx" in part.get("text", "")
                            for part in parts if isinstance(part, dict)))

    def test_audio_video_attachments_do_not_enter_model_batches(self):
        batches = summary_task._summary_work_batches([
            {"sender": "A", "content": "文字", "kind": "text"},
            {"sender": "B", "content": "语音", "kind": "voice", "attachments": ["x.silk"]},
            {"sender": "C", "content": "视频", "kind": "video", "attachments": ["x.mp4"]},
        ], token_budget=20_000, max_document_chars=4_000)
        self.assertEqual([category for category, _ in batches], ["纯文字"])
        self.assertEqual([item["content"] for _, batch in batches for item in batch], ["文字"])


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
                "wechat_ai_assistant.summary_task.messages_for_summary",
                return_value=[{"sender": "张姐", "content": "苹果两箱",
                               "sent_at": "2026-10-13 10:00", "kind": "text"}]):
            return app

    def test_logged_out_says_so(self):
        app = self._app(None)
        with unittest.mock.patch(
                "wechat_ai_assistant.summary_task.messages_for_summary",
                return_value=[{"sender": "张姐", "content": "苹果两箱",
                               "sent_at": "2026-10-13 10:00", "kind": "text"}]):
            outcome = summary_task.run_summary(app, object())
        self.assertEqual(outcome["summary"], "")
        self.assertIn("登录", outcome["error"])
        self.assertEqual(outcome["message_count"], 1)

    def test_jyi_runs_inline_without_billing(self):
        """JYI 直调：提示词同样复用，不计本地账。"""
        import types as _types

        from wechat_ai_assistant.jyi_client import JYIClient

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
                "wechat_ai_assistant.summary_task.messages_for_summary",
                return_value=[{"sender": "张姐", "content": "苹果两箱",
                               "sent_at": "2026-10-13 10:00", "kind": "text"}]):
            outcome = summary_task.run_summary(self._app(client), object())
        self.assertIn("【纯文字】", outcome["summary"])
        self.assertIn("张姐要两箱苹果", outcome["summary"])
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
                "wechat_ai_assistant.summary_task.messages_for_summary",
                return_value=[{"sender": "张姐", "content": "苹果两箱",
                               "sent_at": "2026-10-13 10:00", "kind": "text"}]), \
            unittest.mock.patch("wechat_ai_assistant.summary_task.SUMMARY_POLL_INTERVAL", 0):
            outcome = summary_task.run_summary(self._app(FakeClient()), object())
        self.assertEqual(outcome["summary"], "张姐要两箱苹果")
        self.assertEqual(outcome["error"], "")
        # cost 的单位是美元（界面按 `本次费用 ${cost:.6f}` 展示），
        # 网关按微美元结算，这里是 7 微美元。
        self.assertAlmostEqual(outcome["cost"], 7 / 1_000_000)

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
                "wechat_ai_assistant.summary_task.messages_for_summary",
                return_value=[{"sender": "张姐", "content": "苹果两箱",
                               "sent_at": "2026-10-13 10:00", "kind": "text"}]), \
            unittest.mock.patch("wechat_ai_assistant.summary_task.SUMMARY_POLL_INTERVAL", 0):
            outcome = summary_task.run_summary(self._app(FakeClient()), object())
        self.assertEqual(outcome["summary"], "")
        self.assertIn("服务暂时不可用", outcome["error"])


class ReduceOverflowTests(unittest.TestCase):
    """合并超长不能整单失败：保留分段，并列返回。

    回归真机 case：Bond 群 50 条消息，map 段段都很长，reduce 一合并就过
    8000 字上限，整单失败 —— 而且输入不变，重试必败，等于这个群永远分析
    不出来。兜底后每段各自合规，只是没能压成一份。
    """

    class _LongClient:
        def chat_completions(self, model, messages, **kwargs):
            return {"choices": [{"message": {"content": "x" * 9000}}]}

    class _EmptyClient:
        def chat_completions(self, model, messages, **kwargs):
            return {"choices": [{"message": {"content": ""}}]}

    def test_an_overlong_merge_keeps_both_segments(self):
        out = summary_task._reduce_summary_reports(
            self._LongClient(), "m", "", "g",
            ["【纯文字】\n第一段", "【图片】\n第二段"], {}, None, 8000)
        self.assertIn("第一段", out)
        self.assertIn("第二段", out)

    def test_an_empty_merge_still_raises(self):
        """拿不到内容是模型故障，不是超长 —— 不能拿空段充数，必须抛。"""
        with self.assertRaises(ValueError):
            summary_task._reduce_summary_reports(
                self._EmptyClient(), "m", "", "g", ["a", "b"], {}, None, 8000)


class CancelAnalysisTests(unittest.TestCase):
    """分析必须能取消：已完成的分段保留，没开始的不花钱。

    回归真机 case：Bond 群分析卡在 reduce，第二批就是合并不上，
    界面只能干等 —— 以前连停都停不下来，只能等 180 秒超时。
    取消是"停新的、留旧的"：已完成分段入缓存，重试复用。
    """

    def _messages(self):
        return [{"sender": "A", "content": "甲要苹果",
                 "sent_at": "2026-10-13 10:00", "kind": "text",
                 "group_id": "g1", "group_label": "群一"},
                {"sender": "B", "content": "乙要梨",
                 "sent_at": "2026-10-13 10:01", "kind": "text",
                 "group_id": "g2", "group_label": "群二"}]

    def _jyi_app(self, client):
        return types.SimpleNamespace(client=client, orders=None, tenant="t")

    def test_jyi_cancel_keeps_the_completed_group(self):
        from wechat_ai_assistant.jyi_client import JYIClient

        client = JYIClient()
        client.session = types.SimpleNamespace(access_token="tok")
        calls = []

        def _chat(model, messages, **kwargs):
            calls.append(model)
            return {"choices": [{"message": {"content": "报告%d" % len(calls)}}]}

        client.chat_completions = _chat
        with unittest.mock.patch(
                "wechat_ai_assistant.summary_task.messages_for_summary",
                return_value=self._messages()):
            outcome = summary_task.run_summary(
                self._jyi_app(client), object(),
                is_cancelled=lambda: len(calls) >= 1)
        self.assertTrue(outcome.get("cancelled"), outcome)
        self.assertEqual(outcome.get("error"), "")
        self.assertIn("报告1", outcome["summary"])
        self.assertNotIn("报告2", outcome["summary"])
        self.assertEqual(outcome["completed_groups"], ["g1"])

    def test_cancel_before_start_makes_no_model_calls(self):
        from wechat_ai_assistant.jyi_client import JYIClient

        client = JYIClient()
        client.session = types.SimpleNamespace(access_token="tok")
        calls = []

        def _chat(model, messages, **kwargs):
            calls.append(model)
            return {"choices": [{"message": {"content": "报告"}}]}

        client.chat_completions = _chat
        with unittest.mock.patch(
                "wechat_ai_assistant.summary_task.messages_for_summary",
                return_value=self._messages()):
            outcome = summary_task.run_summary(
                self._jyi_app(client), object(),
                is_cancelled=lambda: True)
        self.assertTrue(outcome.get("cancelled"))
        self.assertEqual(calls, [])
        self.assertEqual(outcome["summary"], "")
        self.assertEqual(outcome.get("error"), "")

    def test_gateway_cancel_reaches_the_server_job(self):
        cancelled_jobs = []
        flag = {"stop": False}

        class FakeClient:
            session = types.SimpleNamespace(access_token="tok")

            def submit_summary(self, request, key):
                flag["stop"] = True
                return {"job": {"job_id": "job1"}}

            def summary_job(self, job_id):
                return {"job": {"job_id": "job1", "state": "running"},
                        "billing": {}}

            def cancel_summary(self, job_id):
                cancelled_jobs.append(job_id)
                return {}

        with unittest.mock.patch(
                "wechat_ai_assistant.summary_task.messages_for_summary",
                return_value=[{"sender": "张姐", "content": "苹果两箱",
                               "sent_at": "2026-10-13 10:00", "kind": "text"}]), \
            unittest.mock.patch("wechat_ai_assistant.summary_task.SUMMARY_POLL_INTERVAL", 0):
            outcome = summary_task.run_summary(
                self._jyi_app(FakeClient()), object(),
                is_cancelled=lambda: flag["stop"])
        self.assertTrue(outcome.get("cancelled"), outcome)
        self.assertEqual(outcome.get("error"), "")
        self.assertEqual(cancelled_jobs, ["job1"],
                         "取消必须顺手撤掉服务端的 job，不然它在那边空转")

    def test_reduce_cancel_raises_instead_of_merging(self):
        class FakeClient:
            def chat_completions(self, model, messages, **kwargs):
                return {"choices": [{"message": {"content": "合并结果"}}]}

        with self.assertRaises(summary_task.AnalysisCancelled):
            summary_task._reduce_summary_reports(
                FakeClient(), "m", "", "g", ["分段一", "分段二"],
                {}, None, 8000, is_cancelled=lambda: True)


class WatermarkTests(unittest.TestCase):
    """增量分析的水位线：首次全量，之后只取新增；角色隔离。

    回归真机 case：Bond 群 50 条消息已分析过，第二天又来了 5 条新消息，
    商户再点分析不该重跑那 50 条，只该分析新增的 5 条并追加到旧报告。
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.app = _app_for(self.store, self.tenant)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _add(self, source, rows, time_offset=0):
        return self.store.add_import(self.tenant, GROUP, [
            {"sender": s, "content": c, "kind": k,
             "sent_at": f"2026-10-14T09:{i+time_offset:02d}:00-04:00",
             "source_id": f"{source}-{i}"}
            for i, (s, c, k) in enumerate(rows)], source, "demo")

    def test_incremental_takes_only_new_messages(self):
        """首次取全量，之后只取水位线后的新增。"""
        self._add("all", [("张姐", "要两箱苹果", "text")] * 3)
        result = _result(self.store, self.tenant, total=3)
        first = summary_task.messages_for_summary(self.app, result, role_id="r1")
        self.assertEqual(len(first), 3)
        # 再加一条新消息（时间更晚），增量应只取这 1 条
        self._add("new", [("李叔", "李记酱油 2 瓶", "text")], time_offset=3)
        second = summary_task.messages_for_summary(self.app, result, role_id="r1")
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["content"], "李记酱油 2 瓶")
        # 水位线已推进，第三次再取应为空
        third = summary_task.messages_for_summary(self.app, result, role_id="r1")
        self.assertEqual(len(third), 0)

    def test_role_change_refetches_full(self):
        """换角色重新取全量，不受旧角色水位线影响。"""
        self._add("all", [("张姐", "要两箱苹果", "text")] * 2)
        result = _result(self.store, self.tenant, total=2)
        first_r1 = summary_task.messages_for_summary(self.app, result, role_id="r1")
        self.assertEqual(len(first_r1), 2)
        first_r2 = summary_task.messages_for_summary(self.app, result, role_id="r2")
        self.assertEqual(len(first_r2), 2)

    def test_no_role_id_takes_full_and_writes_no_watermark(self):
        """不传 role_id 走老路径：取全量，不写水位线，不影响增量。"""
        self._add("all", [("张姐", "要两箱苹果", "text")] * 2)
        result = _result(self.store, self.tenant, total=2)
        full = summary_task.messages_for_summary(self.app, result)
        self.assertEqual(len(full), 2)
        # 再加新消息，不传 role_id 应该还取全量（3 条）
        self._add("new", [("李叔", "李记酱油 2 瓶", "text")])
        full_again = summary_task.messages_for_summary(self.app, result)
        self.assertEqual(len(full_again), 3)
        # 这时候再用 role_id 增量，水位线是空的，应该取全量 3 条
        first_inc = summary_task.messages_for_summary(self.app, result, role_id="r1")
        self.assertEqual(len(first_inc), 3)

    def test_report_persist_and_read(self):
        """报告持久化与读取，不同 (报告键, 角色) 隔离。"""
        summary_task._set_report(self.store, self.tenant, "群A", "r1", "旧报告", 5)
        self.assertEqual(summary_task._get_report(self.store, self.tenant, "群A", "r1"), "旧报告")
        # 覆盖
        summary_task._set_report(self.store, self.tenant, "群A", "r1", "新报告", 8)
        self.assertEqual(summary_task._get_report(self.store, self.tenant, "群A", "r1"), "新报告")
        # 不同 role_id 不互相影响
        self.assertEqual(summary_task._get_report(self.store, self.tenant, "群A", "r2"), "")
        # 不同报告键不互相影响
        self.assertEqual(summary_task._get_report(self.store, self.tenant, "群B", "r1"), "")


class GatewayErrorTextTests(unittest.TestCase):
    """网关原话不能掉地上：读错字段就是一句"提交失败"打发人。"""

    def test_merchant_message_is_used(self):
        from wechat_ai_assistant.client_login import GatewayError

        exc = GatewayError("upstream_quota_used",
                           "This connection has used its quota. Raise its slice.")
        text = summary_task._gateway_error_text(exc, max_len=300)
        self.assertIn("used its quota", text)
        self.assertNotIn("请稍后重试", text)

    def test_unknown_technical_error_stays_generic(self):
        text = summary_task._gateway_error_text(OSError("boom"))
        self.assertEqual(text, "提交失败，请稍后重试")

    def test_unsettled_upstream_requests_use_neutral_wait_message(self):
        from wechat_ai_assistant.client_login import GatewayError

        text = summary_task._analysis_error(GatewayError(
            "too_many_concurrent", "Too many unsettled requests. Wait for them to finish.", 429))
        self.assertIn("稍等片刻", text)
        self.assertIn("重试时复用", text)
        self.assertNotIn("unsettled", text.lower())


if __name__ == "__main__":
    unittest.main()
