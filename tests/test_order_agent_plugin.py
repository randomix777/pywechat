"""Tests for the order domain plugin.

The recurring theme: a rule is worth a test only if getting it wrong costs a
merchant something real -- a wrong customer, a duplicate order, a wrong total,
or a question the customer has already answered.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent", "support_triage"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from business_agent import (  # noqa: E402
    AgentCore,
    AgentRequest,
    AsOf,
    Budget,
    ContextBundle,
    ContextEntry,
    ErrorCategory,
    Event,
    FakeBackend,
    Policy,
    RunStatus,
    Scope,
)
from business_agent.contracts.errors import OutputInvalid  # noqa: E402
from order_agent import (  # noqa: E402
    ALWAYS_BLOCKING,
    CUSTOMER_NOT_BOUND,
    DUPLICATE_CANDIDATE_OPEN,
    EVIDENCE_NOT_FOUND,
    NO_ELIGIBLE_HISTORY,
    PAYMENT_NOT_RECEIVED,
    QUANTITY_MISSING,
    SKU_NOT_IN_CATALOG,
    TARGET_ORDER_UNRESOLVED,
    FollowUpQuestion,
    OrderPlugin,
    RecordingLimits,
    build_follow_up,
    decide_recording,
    format_money,
    render_receipt,
    validate_output,
)
from order_agent.plugin import SOURCE_CATALOG, SOURCE_CUSTOMER, SOURCE_HISTORY  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

CATALOG = (
    ContextEntry(key="product:APL", value={"sku": "APL", "name": "苹果", "unit": "箱"}, source=SOURCE_CATALOG, confirmed=True),
    ContextEntry(key="product:EGG", value={"sku": "EGG", "name": "鸡蛋", "unit": "箱"}, source=SOURCE_CATALOG, confirmed=True),
)
CUSTOMER = ContextEntry(key="customer", value={"id": "c1", "name": "张姐"}, source=SOURCE_CUSTOMER, confirmed=True)
HISTORY = ContextEntry(
    key="order:o1",
    value={"order_id": "o1", "status": "confirmed", "delivery_date": "2026-09-24", "items": [{"sku": "APL", "quantity": 2, "unit": "箱"}]},
    source=SOURCE_HISTORY,
    confirmed=True,
    occurred_at=NOW - timedelta(days=1),
)


def answer(**overrides) -> dict:
    base = {
        "intent": "new_order",
        "customer_alias": "张姐",
        "target_order": None,
        "items": [{"said": '苹果两箱', "brand": None, "category": None, "sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
        "delivery_date": "2026-09-26",
        "evidence": ["苹果两箱"],
        "missing_fields": [],
        "ambiguities": [],
        "suggested_change": None,
    }
    base.update(overrides)
    return base


def request_for(body: str, **kwargs) -> AgentRequest:
    defaults = dict(
        request_id="r1",
        idempotency_key="i1",
        task_type="order_parse",
        scope=Scope(tenant_id="m1", conversation_id="g1"),
        events=(Event(event_id="e1", body=body, occurred_at=NOW, actor="张姐", channel="g1"),),
        as_of=AsOf(instant=NOW),
        context=ContextBundle((CUSTOMER, *CATALOG)),
    )
    defaults.update(kwargs)
    return AgentRequest(**defaults)


def run(body: str, payload: dict, **kwargs):
    return AgentCore(backend=FakeBackend(responses=[payload]), plugin=OrderPlugin()).run(request_for(body, **kwargs))


class OutputContractTests(unittest.TestCase):
    def test_a_valid_suggestion_passes(self):
        validate_output(answer())

    def test_unknown_intent_is_rejected(self):
        with self.assertRaises(OutputInvalid):
            validate_output(answer(intent="drop_database"))

    def test_missing_key_is_rejected(self):
        broken = answer()
        del broken["evidence"]
        with self.assertRaises(OutputInvalid):
            validate_output(broken)

    def test_a_payment_claim_may_not_carry_items(self):
        """A customer saying 'I paid' must never look like a new order."""
        with self.assertRaises(OutputInvalid):
            validate_output(answer(intent="payment_claim"))

    def test_boolean_is_not_a_quantity(self):
        with self.assertRaises(OutputInvalid):
            validate_output(answer(items=[{"said": "测试商品", "brand": None, "category": None, "sku": "APL", "quantity": True, "unit": "箱", "evidence": []}]))


class AutoRecordingTests(unittest.TestCase):
    def test_a_complete_order_is_recorded_without_asking(self):
        result = run("苹果两箱", answer())
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual([a.kind for a in result.suggested_actions], ["order.new_order"])

    def test_an_unbound_customer_blocks_recording(self):
        result = run("苹果两箱", answer(), context=ContextBundle(CATALOG))
        self.assertEqual(result.status, RunStatus.NEEDS_REVIEW)
        self.assertIn(CUSTOMER_NOT_BOUND, {a.code for a in result.ambiguities})
        self.assertEqual(result.suggested_actions, ())

    def test_an_open_duplicate_candidate_blocks_recording(self):
        """Choosing one of two identical messages is how an order is entered twice."""
        result = run("苹果两箱", answer(), attributes={"duplicate_candidates_open": True})
        self.assertEqual(result.status, RunStatus.NEEDS_REVIEW)
        self.assertIn(DUPLICATE_CANDIDATE_OPEN, {a.code for a in result.ambiguities})

    def test_a_sku_outside_the_catalogue_blocks_recording(self):
        result = run("来十箱龙虾", answer(items=[{"said": '十箱龙虾', "brand": None, "category": None, "sku": "LOB", "quantity": 10, "unit": "箱", "evidence": ["十箱龙虾"]}]))
        self.assertEqual(result.status, RunStatus.NEEDS_REVIEW)
        self.assertIn(SKU_NOT_IN_CATALOG, {a.code for a in result.ambiguities})

    def test_a_quote_nobody_said_blocks_recording(self):
        """A citation that is not in the source is a fabricated justification."""
        result = run("苹果两箱", answer(evidence=["客户明确要求明天中午送到"]))
        self.assertEqual(result.status, RunStatus.NEEDS_REVIEW)
        self.assertIn(EVIDENCE_NOT_FOUND, {a.code for a in result.ambiguities})
        self.assertEqual(result.suggested_actions, ())

    def test_a_payment_claim_never_becomes_a_settled_order(self):
        result = run("钱转了", answer(intent="payment_claim", items=[]))
        self.assertEqual(result.status, RunStatus.NEEDS_REVIEW)
        self.assertIn(PAYMENT_NOT_RECEIVED, {a.code for a in result.ambiguities})
        self.assertEqual(result.suggested_actions, ())

    def test_an_inquiry_needs_no_confirmation(self):
        result = run("苹果多少钱", answer(intent="inquiry", items=[]))
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.suggested_actions, ())

    def test_a_modify_without_a_resolved_target_blocks(self):
        result = run("改成三箱", answer(intent="modify", target_order="made-up"), attributes={"known_order_ids": ("o1",)})
        self.assertEqual(result.status, RunStatus.NEEDS_REVIEW)
        self.assertIn(TARGET_ORDER_UNRESOLVED, {a.code for a in result.ambiguities})

    def test_a_modify_with_a_known_target_is_recorded(self):
        result = run(
            "改成三箱",
            answer(intent="modify", target_order="o1", evidence=["三箱"], items=[{"said": '三箱', "brand": None, "category": None, "sku": "APL", "quantity": 3, "unit": "箱", "evidence": ["三箱"]}]),
            attributes={"known_order_ids": ("o1",)},
        )
        self.assertEqual(result.status, RunStatus.SUCCEEDED)

    def test_an_oversized_quantity_is_escalated(self):
        result = AgentCore(
            backend=FakeBackend(responses=[answer(items=[{"said": "测试商品", "brand": None, "category": None, "sku": "APL", "quantity": 5000, "unit": "箱", "evidence": []}])]),
            plugin=OrderPlugin(limits=RecordingLimits(max_line_quantity=100)),
        ).run(request_for("苹果五千箱"))
        self.assertEqual(result.status, RunStatus.NEEDS_REVIEW)

    def test_model_confidence_is_never_the_decider(self):
        """A confident model with no catalogue entry still does not get recorded."""
        result = run("苹果两箱", answer(ambiguities=["I am 99% sure"]))
        # The literal string is not a recognised ambiguity code, so it must not
        # be able to unlock anything -- and must not block by itself either.
        self.assertEqual(result.status, RunStatus.SUCCEEDED)


class FollowUpTests(unittest.TestCase):
    def test_asks_nothing_when_the_history_answers_it(self):
        question = build_follow_up([NO_ELIGIBLE_HISTORY], customer_bound=True, resolvable_from_history=(NO_ELIGIBLE_HISTORY,))
        self.assertIsNone(question)

    def test_asks_at_most_one_question(self):
        question = build_follow_up([SKU_NOT_IN_CATALOG, QUANTITY_MISSING], customer_bound=True)
        self.assertIsInstance(question, FollowUpQuestion)
        self.assertEqual(len(question.fields), 1)

    def test_asks_who_when_the_customer_is_unknown(self):
        question = build_follow_up([], customer_bound=False)
        self.assertEqual(question.fields, ("customer",))

    def test_does_not_ask_the_customer_for_missing_system_context(self):
        """Missing local data is the host's job, not the customer's."""
        question = build_follow_up([], customer_bound=True, missing_information=["本地订单库中没有这条消息"])
        self.assertIsNone(question)

    def test_plugin_exposes_the_question_for_the_ui(self):
        plugin = OrderPlugin()
        payload = validate_output(answer(ambiguities=[SKU_NOT_IN_CATALOG]))
        question = plugin.follow_up_question(payload, request_for("苹果两箱"))
        self.assertIsNotNone(question)


class MemoryTests(unittest.TestCase):
    def test_inferred_memory_is_never_marked_confirmed(self):
        result = run("苹果两箱", answer())
        for proposal in result.memory_proposals:
            self.assertEqual(proposal.confirmation, "model_inferred")
            self.assertFalse(proposal.is_trusted)

    def test_no_memory_is_proposed_for_an_unreadable_order(self):
        result = run("苹果两箱", answer(ambiguities=[SKU_NOT_IN_CATALOG]))
        self.assertEqual(result.memory_proposals, ())

    def test_memory_proposals_stay_in_the_tenant(self):
        result = run("苹果两箱", answer())
        for proposal in result.memory_proposals:
            self.assertEqual(proposal.scope.tenant_id, "m1")

    def test_an_inquiry_proposes_no_order_memory(self):
        result = run("多少钱", answer(intent="inquiry", items=[]))
        self.assertEqual(result.memory_proposals, ())


class ReceiptTests(unittest.TestCase):
    def test_a_receipt_is_rendered_from_validated_fields(self):
        text = render_receipt(
            items=[{"name": "苹果", "quantity": 2, "unit": "箱", "unit_price_cents": 2600}],
            delivery_date="2026-09-26",
        )
        self.assertIn("苹果 2箱", text)
        self.assertIn("52.00", text)
        self.assertIn("直接回复即可修改", text)

    def test_a_pending_price_is_stated_rather_than_invented(self):
        text = render_receipt(items=[{"name": "苹果", "quantity": 2, "unit": "箱"}], price_pending=True)
        self.assertIn("价格稍后确认", text)
        self.assertNotIn("0.00", text)

    def test_a_missing_price_raises_instead_of_defaulting(self):
        """A defaulted price of zero reaches the customer as a wrong total."""
        with self.assertRaises(ValueError):
            render_receipt(items=[{"name": "苹果", "quantity": 2, "unit": "箱"}])

    def test_an_empty_receipt_is_refused(self):
        with self.assertRaises(ValueError):
            render_receipt(items=[])

    def test_money_formatting(self):
        self.assertEqual(format_money(2600), "26.00")
        self.assertEqual(format_money(5), "0.05")


class PolicyUnitTests(unittest.TestCase):
    def test_informational_intents_do_not_record_and_do_not_block(self):
        decision = decide_recording(
            intent="inquiry", items=[], ambiguities=[], customer_bound=True, catalog_skus=["APL"]
        )
        self.assertFalse(decision.record)
        self.assertEqual(decision.blocking, ())

    def test_price_pending_is_only_allowed_when_the_merchant_allows_it(self):
        strict = decide_recording(
            intent="new_order", items=[{"sku": "APL", "quantity": 1, "unit": "箱"}],
            ambiguities=[], customer_bound=True, catalog_skus=["APL"], amount_cents=None,
        )
        self.assertTrue(strict.record)
        loose = decide_recording(
            intent="new_order", items=[{"sku": "APL", "quantity": 1, "unit": "箱"}],
            ambiguities=["price_rule_unknown"], customer_bound=True, catalog_skus=["APL"],
        )
        self.assertFalse(loose.record)
        allowed = decide_recording(
            intent="new_order", items=[{"sku": "APL", "quantity": 1, "unit": "箱"}],
            ambiguities=["price_rule_unknown"], customer_bound=True, catalog_skus=["APL"],
            limits=RecordingLimits(allow_price_pending=True),
        )
        self.assertTrue(allowed.record)
        self.assertIn("price_rule_unknown", allowed.advisories)

    def test_always_blocking_codes_are_reported_in_one_stable_list(self):
        self.assertIn(CUSTOMER_NOT_BOUND, ALWAYS_BLOCKING)
        self.assertIn(DUPLICATE_CANDIDATE_OPEN, ALWAYS_BLOCKING)
        self.assertIn(PAYMENT_NOT_RECEIVED, ALWAYS_BLOCKING)


if __name__ == "__main__":
    unittest.main()
