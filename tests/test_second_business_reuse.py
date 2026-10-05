"""The reusability claim, tested rather than asserted.

If this file is deleted, ``business_agent`` is no longer proven reusable by
anything in this repository. It runs a *second, unrelated* business domain on
the same core, and asserts the two domains share no code beyond the core.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent", "support_triage"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from business_agent import AgentCore, FakeBackend, RunStatus  # noqa: E402
from order_agent import OrderPlugin  # noqa: E402
from support_triage import SupportTriagePlugin, triage  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


class SecondDomainTests(unittest.TestCase):
    def test_a_completely_different_domain_runs_on_the_same_core(self):
        result = triage("我的订单还没到")
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.output["category"], "order_status")
        self.assertEqual([a.kind for a in result.suggested_actions], ["support.auto_reply"])

    def test_refunds_always_reach_a_human(self):
        result = triage("我要退款")
        self.assertEqual(result.status, RunStatus.NEEDS_REVIEW)
        self.assertEqual(result.suggested_actions, ())

    def test_a_simulated_result_is_never_reported_as_a_model_run(self):
        """The single most important honesty property in this repository.

        A passing local test must never be mistakable for evidence about model
        quality, so the flag has to be False everywhere it is not a model.
        """
        for result in (triage("我的订单还没到"), triage("我要退款")):
            self.assertFalse(result.usage.model_executed)
            self.assertFalse(result.usage.input_tokens)
            self.assertFalse(result.usage.output_tokens)

    def test_the_two_domains_share_only_the_core(self):
        order_source = (_ROOT / "packages" / "order_agent").rglob("*.py")
        triage_source = (_ROOT / "packages" / "support_triage").rglob("*.py")
        for path in list(order_source) + list(triage_source):
            source = path.read_text(encoding="utf-8")
            other = "support_triage" if "order_agent" in str(path) else "order_agent"
            self.assertNotIn(f"import {other}", source, f"{path} reaches into the other domain")
        # And neither reaches into the product.
        for path in list(triage_source):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("pyweixin", source, f"{path} depends on the product")

    def test_the_example_runs_with_no_product_on_the_path(self):
        """A clean interpreter with only the two packages importable."""
        env_paths = ";".join(
            str(_ROOT / "packages" / name / "src") for name in ("business_agent", "order_agent", "support_triage")
        )
        result = subprocess.run(
            [sys.executable, "-m", "support_triage", "--text", "我要退款", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={"PYTHONPATH": env_paths, "PYTHONIOENCODING": "utf-8", "PATH": ""},
            cwd=str(Path(_ROOT).parent),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "needs_review")
        self.assertFalse(payload["usage"]["model_executed"])

    def test_both_domains_produce_valid_serialisable_results(self):
        order_result = AgentCore(
            backend=FakeBackend(
                responses=[
                    {
                        "intent": "new_order",
                        "customer_alias": "张姐",
                        "target_order": None,
                        "items": [{"said": '苹果两箱', "brand": None, "category": None, "sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
                        "delivery_date": None,
                        "evidence": ["苹果两箱"],
                        "missing_fields": [],
                        "ambiguities": [],
                        "suggested_change": None,
                    }
                ]
            ),
            plugin=OrderPlugin(),
        ).run(
            _order_request(),
        )
        triage_result = triage("我的订单还没到")
        for result in (order_result, triage_result):
            payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False, default=str))
            self.assertIn(payload["status"], RunStatus.ALL)
            self.assertIn("usage", payload)


def _order_request():
    from business_agent import AgentRequest, AsOf, Budget, ContextBundle, ContextEntry, Event, Policy, Scope
    from order_agent.plugin import SOURCE_CATALOG, SOURCE_CUSTOMER

    return AgentRequest(
        request_id="r1",
        idempotency_key="i1",
        task_type="order_parse",
        scope=Scope(tenant_id="m1"),
        events=(Event(event_id="e1", body="苹果两箱", occurred_at=NOW, actor="张姐"),),
        as_of=AsOf(instant=NOW),
        context=ContextBundle(
            (
                ContextEntry(key="customer", value={"id": "c1"}, source=SOURCE_CUSTOMER, confirmed=True),
                ContextEntry(key="p", value={"sku": "APL"}, source=SOURCE_CATALOG, confirmed=True),
            )
        ),
        policy=Policy(name="t"),
        budget=Budget(timeout_seconds=5),
    )


if __name__ == "__main__":
    unittest.main()
