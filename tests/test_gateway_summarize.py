"""云摘要任务的回归测试：校验、执行、幂等、结算，订单老路不受影响。

网关和客户端从此多了一种任务（`summarize`）：提示词与校验的唯一来源是
:mod:`pyweixin.summary`，计费沿用订单同一套冻结结算（按 token 实结）。
脚手架复用 :mod:`tests.test_product_gateway` 的真实 HTTP 服务，
只把适配器换成同时会摘要的假执行器。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from tests.test_product_gateway import CURRENT_PRICING_VERSION, GatewayHarness  # noqa: E402
from pyweixin.opencode import ParseOutcome  # noqa: E402


class FakeSummaryAdapter:
    """两种任务都支持的假执行器：订单走固定答案，摘要回显首条。"""

    is_model = True

    def __init__(self, fail_summary_with: str = ""):
        self._fail = fail_summary_with
        self.summary_calls: list = []

    def health(self):
        return {"healthy": True}

    def probe(self):
        return {"ok": True}

    def create_session(self, job_id: str) -> str:
        return "sess_" + job_id

    def abort(self, session_id: str) -> bool:
        return True

    def delete_session(self, session_id: str) -> bool:
        return True

    def reconcile(self, session_id: str):
        return None

    def run_parse(self, session_id, payload, job_id, deadline,
                  cancel_check=None, provider_id="", model_id=""):
        from pyweixin.opencode import validate_parse_result
        return ParseOutcome(
            result=validate_parse_result({
                "intent": "unclear", "customer_alias": None, "target_order": None,
                "items": [], "delivery_date": None, "evidence": [],
                "missing_fields": [], "ambiguities": [], "suggested_change": None,
            }),
            input_tokens=10, output_tokens=5)

    def run_summary(self, session_id, payload, job_id, deadline,
                    cancel_check=None, provider_id="", model_id=""):
        from pyweixin import summary as summary_mod

        self.summary_calls.append(dict(payload))
        if self._fail:
            raise ValueError(self._fail)
        messages = (payload.get("messages") or [])
        first = str((messages[0] or {}).get("content", "")) if messages else ""
        text = summary_mod.validate_summary_result(f"简报：{first}")
        return ParseOutcome(result={"summary": text},
                            input_tokens=20, output_tokens=10)


class SummarizeTaskTests(GatewayHarness):
    def setUp(self) -> None:
        super().setUp()
        self.gateway.adapter = FakeSummaryAdapter()

    def _summary_payload(self, **extra):
        payload = {
            "task": "summarize",
            "model": "",
            "messages": [
                {"sender": "张姐", "content": "苹果两箱",
                 "sent_at": "2026-10-13 10:00", "kind": "text"},
                {"sender": "王老板", "content": "橙子一箱",
                 "sent_at": "2026-10-13 10:05", "kind": "text"},
            ],
            "pricing_version": CURRENT_PRICING_VERSION["value"],
        }
        payload.update(extra)
        return payload

    def _submit(self, payload, key):
        _, tokens, _, _ = self.logged_in_device("摘要电脑")
        status, body, _ = self.call(
            "POST", "/v1/summarize-jobs", payload,
            headers={**self.bearer(tokens), "Idempotency-Key": key})
        return status, body, tokens

    def _wait_state(self, job_id, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.store.get_job(self.merchant["account_id"], job_id)
            if job["state"] in ("succeeded", "failed", "cancelled"):
                return job
            time.sleep(0.05)
        return self.store.get_job(self.merchant["account_id"], job_id)

    def test_happy_path_returns_summary_and_billing(self):
        status, body, _ = self._submit(self._summary_payload(), "sum-1")
        self.assertEqual(status, 200, body)
        job_id = body["job"]["job_id"]
        self.gateway.start(workers=1)
        try:
            job = self._wait_state(job_id)
        finally:
            self.gateway.stop()
        self.assertEqual(job["state"], "succeeded", job)
        result = job["result"] if isinstance(job["result"], dict) else {}
        self.assertIn("苹果两箱", str(result.get("summary", "")))
        billing = self.store.job_snapshot(job_id) or {}
        self.assertTrue(billing, "结算快照必须有")

    def test_validation_errors(self):
        cases = [
            ({"task": "nope", "messages": []}, "unsupported_task"),
            ({"task": "summarize", "messages": []}, "missing_messages"),
            ({"task": "summarize",
              "messages": [{"sender": "x", "content": "   "}]}, "empty_message"),
            ({"task": "summarize",
              "messages": [{"sender": "x", "content": "y"}] * 201}, "too_many_messages"),
            ({"task": "summarize", "system_prompt": "hack",
              "messages": [{"sender": "x", "content": "y"}]}, "unsupported_field"),
        ]
        _, tokens, _, _ = self.logged_in_device("摘要电脑")
        for payload, code in cases:
            payload = {**payload, "pricing_version": CURRENT_PRICING_VERSION["value"]}
            status, body, _ = self.call(
                "POST", "/v1/summarize-jobs", payload,
                headers={**self.bearer(tokens), "Idempotency-Key": f"v-{code}"})
            with self.subTest(code=code):
                self.assertNotEqual(status, 200, body)
                self.assertEqual(body.get("error"), code, body)

    def test_idempotent_replay_returns_same_job(self):
        status, first, _ = self._submit(self._summary_payload(), "sum-dup")
        self.assertEqual(status, 200, first)
        status, second, _ = self._submit(self._summary_payload(), "sum-dup")
        self.assertEqual(status, 200, second)
        self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])

    def test_order_path_still_works(self):
        from tests.test_product_gateway import sample_request

        _, tokens, _, _ = self.logged_in_device("摘要电脑")
        status, body, _ = self.call(
            "POST", "/v1/order-parse-jobs", sample_request(),
            headers={**self.bearer(tokens), "Idempotency-Key": "ord-1"})
        self.assertEqual(status, 200, body)
        job_id = body["job"]["job_id"]
        self.gateway.start(workers=1)
        try:
            job = self._wait_state(job_id)
        finally:
            self.gateway.stop()
        self.assertEqual(job["state"], "succeeded", job)

    def test_adapter_without_summary_fails_clearly(self):
        from pyweixin.opencode import LocalRuleAdapter

        self.gateway.adapter = LocalRuleAdapter()
        status, body, _ = self._submit(self._summary_payload(), "sum-nocap")
        self.assertEqual(status, 200, body)
        job_id = body["job"]["job_id"]
        self.gateway.start(workers=1)
        try:
            job = self._wait_state(job_id)
        finally:
            self.gateway.stop()
        self.assertEqual(job["state"], "failed", job)


if __name__ == "__main__":
    unittest.main()
