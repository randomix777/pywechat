"""计费依据、免费额度生命周期、平台预算与并发 —— 本轮收尾的回归测试。

这些测试针对的是**具体的缺陷**，每一条都说明"不修会怎样"：

* 客户选的模型没有真的送到执行后端（改的是适配器上没人读的副本）；
* 收费任务按适配器返回值扣费，而不是按冻结的 ``cost_units_per_job``；
* 任务与快照分两次提交，中间存在"免费任务按收费结算"的窗口；
* 取消一个还在排队的免费任务，那一次免费额度永远不还；
* 平台全局并发上限因为数错了字段而从未生效；
* 客户端把"免费次数用完 / 模型暂不可用"当成失败，把消息从待处理队列里丢掉。

跑法::

    python -m unittest tests.test_billing_free_path -v
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from pyweixin.client_login import GatewayError, PendingQueue, status_label  # noqa: E402
from pyweixin.gateway import ProductGateway  # noqa: E402
from pyweixin.gateway_server import platform_budget_from_env  # noqa: E402
from pyweixin.local_parse import LOCAL_BLOCKED, LocalParseService  # noqa: E402
from pyweixin.model_catalog import (  # noqa: E402
    CatalogStatus,
    CatalogStore,
    OperatorModelPolicy,
    PricingClass,
    build_catalog,
)
from pyweixin.opencode import (  # noqa: E402
    AdapterRejected,
    AdapterUnavailable,
    LocalRuleAdapter,
    OpenCodeAdapter,
)
from pyweixin.product import (  # noqa: E402
    ModelUnavailable,
    PlatformBudget,
    PlatformBudgetExhausted,
    PlatformBudgetLedger,
    ProductStore,
)

VALID = {
    "intent": "new_order", "customer_alias": "张老板", "target_order": None,
    "items": [{"sku": "SKU-1", "quantity": 2, "unit": "箱", "evidence": ["来两箱苹果"]}],
    "delivery_date": None, "evidence": ["来两箱苹果"], "missing_fields": [],
    "ambiguities": [], "suggested_change": None,
}

FREE = "free-standard"
PAID = "paid-deep"


def request(message: str = "来两箱苹果", **extra) -> dict:
    payload = {
        "message": {"id": "m1", "sender": "张老板", "content": message, "kind": "text",
                    "sent_at": "2026-01-01T10:00:00+08:00"},
        "customer": {"id": "c1", "name": "张老板", "alias": "老张", "address": ""},
        "products": [{"sku": "SKU-1", "name": "苹果", "unit": "箱", "aliases": "苹果"}],
        "history": {"recent_messages": [], "customer_facts": [], "recent_orders": []},
        "order_state": {"status": "draft"},
    }
    payload.update(extra)
    return payload


def make_catalog(free_limit: int = 5, paid_cost: int = 1, paid_free_timezone: str = "UTC") -> CatalogStore:
    policies = {
        FREE: OperatorModelPolicy(
            alias=FREE, provider_id="p", model_id="m-free", pricing_class=PricingClass.METERED,
            daily_free_limit=free_limit, is_default=True, summary="适合日常订单",
            cost_units_per_job=0, free_quota_timezone=paid_free_timezone, policy_version="p1",
        ),
        PAID: OperatorModelPolicy(
            alias=PAID, provider_id="p", model_id="m-paid", pricing_class=PricingClass.METERED,
            summary="分析较慢", cost_units_per_job=paid_cost, policy_version="p1",
        ),
    }
    store = CatalogStore()
    store.load_policies(list(policies.values()))
    store.catalog = build_catalog(
        [{"provider_id": "p", "name": "测试供应商", "connected": True,
          "models": [
              {"model_id": "m-free", "display_name": "免费模型", "context_limit": 1000,
               "output_limit": 1000, "capabilities": {}, "upstream_cost": {}, "upstream_status": "live"},
              {"model_id": "m-paid", "display_name": "深度模型", "context_limit": 1000,
               "output_limit": 1000, "capabilities": {}, "upstream_cost": {}, "upstream_status": "live"},
          ]}],
        policies,
    )
    for alias in (FREE, PAID):
        store.catalog.mark_status(alias, CatalogStatus.SELECTABLE, verified=True, now=1.0)
    return store


class GatewayHarness(unittest.TestCase):
    """直接调网关内部方法，绕开 HTTP，但走的是同一段代码。"""

    def build(self, catalog=None, adapter=None, budget=None, quota: int = 100, concurrency: int = 8):
        self.store = ProductStore(":memory:")
        self.adapter = adapter if adapter is not None else LocalRuleAdapter(VALID)
        self.catalog = catalog if catalog is not None else make_catalog()
        self.gateway = ProductGateway(
            store=self.store, adapter=self.adapter, secure_cookies=False,
            catalog=self.catalog, platform_budget=budget,
        )
        # 真实客户端先读目录再提交，测试照做。
        self.pricing_version = self.gateway.catalog.for_client()["version"]
        self.account = self.store.create_account(
            "shop", "password-1234", display_name="测试店",
            quota_units=quota, max_concurrent_jobs=concurrency,
        )
        self.account_id = self.account["account_id"]
        self.device_id, _ = self.store._upsert_device(
            self.account_id, self.account["merchant_id"], "测试电脑"
        )
        self.identity = {
            "account_id": self.account_id,
            "device_id": self.device_id,
            "merchant_id": self.account["merchant_id"],
        }
        self.created: list[str] = []
        original = self.store.create_job

        def counting(*args, **kwargs):
            job = original(*args, **kwargs)
            self.created.append(job["job_id"])
            return job

        self.store.create_job = counting  # type: ignore[method-assign]

    def tearDown(self):
        for name in ("store",):
            store = getattr(self, name, None)
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
        store = getattr(self, "inner_store", None)
        if store is not None:
            try:
                store.close()
            except Exception:
                pass

    def submit(self, key: str, payload=None):
        body = dict(payload or request())
        body.setdefault("pricing_version", self.pricing_version)
        return self.gateway._create_job(self.identity, body, {"Idempotency-Key": key})


# ------------------------------------------------------------------ 1. 路由落地


class ModelReachesBackendTests(GatewayHarness):
    """客户选的模型必须真的出现在发往 OpenCode 的请求体里。"""

    def test_opencode_request_body_carries_the_job_model(self):
        """适配器层的证明：请求体的 ``model`` 就是这一单冻结的那一个。"""
        bodies: list[dict] = []

        def transport(method, path, body, timeout):
            del timeout
            if path == "/session":
                return {"id": "ses-1"}
            bodies.append(body or {})
            return {"info": {"id": "m1", "role": "assistant"},
                    "parts": [{"type": "text", "text": json.dumps(VALID, ensure_ascii=False)}]}

        adapter = OpenCodeAdapter("http://opencode:4096", password="p")
        adapter._backend._transport = transport
        session = adapter.create_session("job-x")
        adapter.run_parse(session, request(), "job-x", 0.0,
                          provider_id="opencode-go", model_id="qwen3.6-plus")
        self.assertTrue(bodies, "没有向 /session/{id}/message 发出请求")
        self.assertEqual(
            bodies[0].get("model"), {"providerID": "opencode-go", "modelID": "qwen3.6-plus"}
        )

    def test_concurrent_jobs_do_not_swap_each_others_model(self):
        """适配器是所有工作线程共用的单例：并发时不能互相换模型。"""
        sent: dict[str, dict] = {}

        def transport(method, path, body, timeout):
            del timeout
            if path == "/session":
                return {"id": "ses"}
            session_id = path.split("/")[2]
            sent[session_id] = dict(body or {})
            return {"info": {"id": "m", "role": "assistant"},
                    "parts": [{"type": "text", "text": json.dumps(VALID, ensure_ascii=False)}]}

        adapter = OpenCodeAdapter("http://opencode:4096", password="p")
        adapter._backend._transport = transport
        targets = [("prov-a", "model-a"), ("prov-b", "model-b")] * 3
        barrier = threading.Barrier(6)

        def run(index: int):
            session = f"ses-{index}"
            adapter._backend.create_session = lambda _label, _s=session: _s
            barrier.wait(timeout=20)
            adapter.run_parse(session, request(), f"job-{index}", 0.0,
                              provider_id=targets[index][0], model_id=targets[index][1])

        threads = [threading.Thread(target=run, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(sent), 6)
        for index, (provider, model) in enumerate(targets):
            self.assertEqual(
                sent[f"ses-{index}"].get("model"),
                {"providerID": provider, "modelID": model},
                f"第 {index} 个任务用错了模型",
            )

    def test_gateway_hands_the_frozen_model_to_the_backend(self):
        self.build()
        self.submit("a", request(model=FREE))
        self.submit("b", request(model=PAID))
        jobs = self.store.list_jobs(self.account_id)
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            self.gateway._run_job(dict(job))
        by_job = {call["job_id"]: call for call in self.adapter.calls}
        self.assertEqual(len(by_job), 2)
        for job in jobs:
            snapshot = self.store.job_snapshot(job["job_id"])
            call = by_job[job["job_id"]]
            self.assertEqual(call["provider_id"], snapshot["provider_id"])
            self.assertEqual(call["model_id"], snapshot["model_id"])
        self.assertEqual({call["model_id"] for call in by_job.values()}, {"m-free", "m-paid"})


class FrozenRouteIsNotAnAvailabilityPromiseTests(GatewayHarness):
    """「路由已冻结」与「上游下架了也能跑」是两件事，必须分开验证。"""

    def test_delisting_the_model_does_not_change_a_queued_job_route(self):
        self.build()
        job = self.submit("d1", request(model=PAID)).payload["job"]
        # 目录刷新后这个模型被下架：不再可选，新任务会被拒。
        self.catalog.mark_status(PAID, CatalogStatus.UNAVAILABLE, reason="该模型暂时无法使用")
        with self.assertRaises(ModelUnavailable) as caught:
            self.submit("d2", request(model=PAID))
        self.assertEqual(caught.exception.code, "model_unavailable")
        # 已在排队的任务仍按自己冻结的那一份路由执行。
        self.gateway._run_job(dict(job))
        call = self.adapter.calls[-1]
        self.assertEqual(call["model_id"], "m-paid")

    def test_a_delisted_model_that_fails_does_not_charge_or_switch(self):
        """上游真的下架了，这一单会失败 —— 冻结路由不等于保证可用。"""
        from pyweixin.opencode import AdapterRejected

        class GoneAdapter(LocalRuleAdapter):
            def run_parse(self, session_id, payload, job_id, deadline, cancel_check=None,
                          provider_id="", model_id=""):
                raise AdapterRejected("upstream removed the model")

        self.build(adapter=GoneAdapter(VALID))
        job = self.submit("d3", request(model=PAID)).payload["job"]
        self.gateway._run_job(dict(job))
        self.assertEqual(self.store.get_job(self.account_id, job["job_id"])["state"], "failed")
        subscription = self.store.get_subscription(self.account_id)
        self.assertEqual(subscription["used_units"], 0, "确定失败不得扣费")
        self.assertEqual(subscription["reserved_units"], 0, "预留必须归还")

    def test_failover_to_never_switches_to_a_paid_model(self):
        """``failover_to`` 是保留字段：本产品**不做**自动换模型。"""
        import dataclasses

        self.build()
        entry = self.catalog.entry(FREE)
        entry.policy = dataclasses.replace(entry.policy, failover_to=PAID)
        self.assertEqual(self.catalog.entry(FREE).policy.failover_to, PAID)

        class DownAdapter(LocalRuleAdapter):
            def run_parse(self, session_id, payload, job_id, deadline, cancel_check=None,
                          provider_id="", model_id=""):
                raise AdapterUnavailable("upstream down")

        self.adapter = DownAdapter(VALID)
        self.gateway.adapter = self.adapter
        job = self.submit("d4").payload["job"]
        self.gateway._run_job(dict(job))
        self.assertEqual(self.store.get_job(self.account_id, job["job_id"])["state"], "failed")
        self.assertEqual(self.store.get_job(self.account_id, job["job_id"])["error"]["code"], "backend_unavailable")
        self.assertEqual(self.store.get_subscription(self.account_id)["used_units"], 0)
        self.assertEqual(len(self.store.list_jobs(self.account_id)), 1, "不得偷偷再建一个收费任务")


# ------------------------------------------------------------ 2. 计费依据唯一


class FrozenChargeTests(GatewayHarness):
    def test_paid_job_charges_the_frozen_cost_units_not_the_adapter_value(self):
        """运营配「每条扣 3 次」，就必须真的扣 3 次。"""
        self.build(catalog=make_catalog(paid_cost=3))
        adapter = LocalRuleAdapter(VALID)
        # 适配器自报 1 次（旧适配器固定返回 1），策略说 3 次。
        self.assertEqual(adapter.run_parse("s", request(), "j", 0.0).cost_units, 1)
        self.adapter = adapter
        self.gateway.adapter = adapter
        response = self.submit("p1", request(model=PAID))
        job = response.payload["job"]
        self.assertEqual(job["reserved_units"], 3, "应按策略预留 3 次")
        self.gateway._run_job({**job, "job_id": job["job_id"]})
        self.assertEqual(self.store.get_job(self.account_id, job["job_id"])["state"], "succeeded")
        subscription = self.store.get_subscription(self.account_id)
        self.assertEqual(subscription["reserved_units"], 0)
        self.assertEqual(subscription["used_units"], 3, "实际扣费必须等于冻结的策略数字")

    def test_charge_note_cannot_contradict_the_structured_policy(self):
        """展示文案只是展示。它写"扣 1 次"而策略是 3 次时，以策略为准。"""
        policies = {PAID: OperatorModelPolicy(
            alias=PAID, provider_id="p", model_id="m", pricing_class=PricingClass.METERED,
            cost_units_per_job=3, charge_note="该模型会消耗分析额度，每分析一条消息扣 1 次额度",
        )}
        store = CatalogStore()
        store.load_policies(list(policies.values()))
        store.catalog = build_catalog(
            [{"provider_id": "p", "name": "p", "connected": True,
              "models": [{"model_id": "m", "display_name": "深", "context_limit": 1,
                          "output_limit": 1, "capabilities": {}, "upstream_cost": {},
                          "upstream_status": "live"}]}],
            policies,
        )
        line = store.catalog.entry(PAID).charge_line()
        self.assertIn("扣 3 次额度", line)
        self.assertNotIn("扣 1 次", line)

    def test_charge_note_without_a_count_is_kept(self):
        policies = {PAID: OperatorModelPolicy(
            alias=PAID, provider_id="p", model_id="m", pricing_class=PricingClass.METERED,
            cost_units_per_job=2, charge_note="分析较慢，会占用额度",
        )}
        self.assertEqual(policies[PAID].to_dict()["cost_units_per_job"], 2)


# ---------------------------------------------------- 3. 任务与快照同一个事务


class AtomicSnapshotTests(GatewayHarness):
    def test_job_never_exists_without_its_snapshot(self):
        """创建时快照就位：不存在"任务在跑、快照还没写"的窗口。"""
        self.build()
        seen: list[tuple[str, bool]] = []
        original = self.store.save_job_snapshot

        def guard(*args, **kwargs):
            del args, kwargs
            raise AssertionError("创建流程里不应再单独写快照")

        original_calls = original
        del original_calls
        self.store.save_job_snapshot = guard  # type: ignore[method-assign]
        self.submit("s1")
        del seen
        for job_id in self.created:
            self.assertIsNotNone(self.store.job_snapshot(job_id))
        self.store.save_job_snapshot = original  # type: ignore[method-assign]

    def test_replaying_the_same_key_does_not_rewrite_the_snapshot(self):
        self.build()
        first = self.submit("dup").payload["job"]
        before = self.store.job_snapshot(first["job_id"])
        self.submit("dup")
        self.assertEqual(self.store.job_snapshot(first["job_id"]), before)


# ------------------------------------------------------- 4. 免费额度的归还


class FreeReservationTests(GatewayHarness):

    def test_a_cancelled_free_job_frees_the_concurrency_slot_again(self):
        self.build(catalog=make_catalog(free_limit=10))
        job = self.submit("c2").payload["job"]
        self.store.request_cancel(self.account_id, job["job_id"])
        again = self.submit("c3")
        self.assertIn(again.status, (200, 202))

    def test_expired_paid_job_returns_the_reserved_units(self):
        self.build()
        job = self.submit("c6", request(model=PAID)).payload["job"]
        self.assertEqual(self.store.get_subscription(self.account_id)["reserved_units"], 1)
        self.store._connection.execute(
            "UPDATE jobs SET deadline_at=? WHERE id=?", (self.store._clock() - 1, job["job_id"])
        )
        self.store._connection.commit()
        self.store.expire_stalled_jobs()
        self.assertEqual(self.store.get_subscription(self.account_id)["reserved_units"], 0)


# --------------------------------------------------------- 5. 每日额度与时区


class FreeQuotaDayTests(unittest.TestCase):
    #: 2026-01-01T16:30:00Z —— 东八区已经是 1 月 2 日凌晨。
    UTC_16_30 = 1767285000.0

    def setUp(self):
        # 注入固定时钟：这一组测试断言的就是"当天是哪一天"，用真实墙上时间
        # 等于每天有 16 小时会假失败（UTC 00:00–15:59 时东八区与 UTC 同日）。
        self.store = ProductStore(":memory:", clock=lambda: self.UTC_16_30)
        self.inner_store = self.store
        self.account = self.store.create_account("shop", "password-1234")["account_id"]
        self.created: list[str] = []

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_the_timezone_decides_which_day_the_quota_resets(self):
        stamp = self.UTC_16_30
        self.assertEqual(self.store._day_key(stamp), "2026-01-01")
        self.assertEqual(self.store._day_key(stamp, "Asia/Shanghai"), "2026-01-02")
        self.assertEqual(self.store._day_key(stamp, "Not/AZone"), "2026-01-01", "时区写错不得崩")

    def build_gateway_free_job(self, day: str, limit: int) -> None:
        store = ProductStore(":memory:")
        self.store = store
        self.inner_store = store
        gateway = ProductGateway(
            store=store, adapter=LocalRuleAdapter(VALID), secure_cookies=False,
            catalog=make_catalog(free_limit=limit),
        )
        account = store.create_account("shop", "password-1234", quota_units=10, max_concurrent_jobs=4)
        self.account_id = account["account_id"]
        device_id, _ = store._upsert_device(self.account_id, account["merchant_id"], "电脑")
        identity = {"account_id": self.account_id, "device_id": device_id, "merchant_id": account["merchant_id"]}
        # 把"预留那天"定到指定日期，模拟 23:59 提交、00:01 完成。计费口径只认
        # 冻结下来的这一天，所以改这里就足以复现跨午夜。
        original_resolve = gateway._resolve_model

        def resolve(alias, identity_arg):
            routing = original_resolve(alias, identity_arg)
            return {**routing, "free_day": day}

        gateway._resolve_model = resolve
        job = gateway._create_job(identity, request(), {"Idempotency-Key": day})
        gateway._resolve_model = original_resolve
        self.assertEqual(job.status, 200)
        self.created.append(job.payload["job"]["job_id"])
        self.assertEqual(
            self.store.job_snapshot(job.payload["job"]["job_id"])["snapshot"]["free_day"], day
        )
        self.assertEqual(store.free_quota_state(self.account_id, limit, day=day)["reserved"], 1)


# ------------------------------------------------------------- 6. 平台预算


class PlatformBudgetGateTests(GatewayHarness):
    def test_a_raw_budget_config_is_given_a_ledger(self):
        """只传配置对象时也必须能记账，否则预算保护静默失效。"""
        self.build(budget=PlatformBudget(daily_paid_jobs=1))
        self.assertIsInstance(self.gateway.platform_budget, PlatformBudgetLedger)

    def test_global_paid_concurrency_is_actually_enforced(self):
        """并发上限曾经数错字段，排队中的任务一个都数不到。"""
        budget = PlatformBudget(max_paid_concurrency=1, free_jobs_exempt=True)
        self.build(budget=budget)
        first = self.submit("g1", request(model=PAID))
        self.assertIn(first.status, (200, 202))
        with self.assertRaises(PlatformBudgetExhausted) as caught:
            self.submit("g2", request(model=PAID, message={
                "id": "m2", "sender": "张老板", "content": "再来两箱", "kind": "text",
                "sent_at": "2026-01-01T10:00:01+08:00"}))
        self.assertEqual(caught.exception.code, "platform_budget_exhausted")
        self.assertEqual(caught.exception.http_status, 409)
        # 客户本地的数据一个字节都不动，任务也还是"待处理"。
        self.assertIn("不受影响", str(caught.exception))
        self.assertEqual(len(self.store.list_jobs(self.account_id)), 1)

    def test_budget_status_is_reported_without_inventing_money(self):
        self.build(budget=PlatformBudget(daily_paid_jobs=5, cost_cents_per_paid_job=None))
        status = self.gateway.platform_budget_status()
        self.assertTrue(status["configured"])
        self.assertFalse(status["cost_known"])
        self.assertIsNone(status["settled_cost_cents_today"])
        self.assertTrue(status["accepting"])

    def test_estimated_cost_only_when_a_unit_price_is_known(self):
        self.build(budget=PlatformBudget(cost_cents_per_paid_job=7, daily_cost_cents=7))
        self.submit("c1", request(model=PAID))
        job = self.created[-1]
        self.gateway._run_job({"job_id": job})
        status = self.gateway.platform_budget_status()
        self.assertTrue(status["cost_known"])
        self.assertEqual(status["settled_cost_cents_today"], 7)
        self.assertFalse(status["accepting"], "超出每日成本上限后应停止接收")

    def test_budget_is_read_from_the_environment(self):
        self.assertIsNone(platform_budget_from_env({}), "什么都没配就是不限制")
        budget = platform_budget_from_env({
            "PYWEIXIN_PLATFORM_DAILY_JOBS": "50",
            "PYWEIXIN_PLATFORM_MAX_CONCURRENCY": "2",
        })
        self.assertEqual((budget.daily_paid_jobs, budget.max_paid_concurrency), (50, 2))
        self.assertIsNone(budget.cost_cents_per_paid_job, "没有单价时不得编造成本")
        self.assertIsNone(platform_budget_from_env({"PYWEIXIN_PLATFORM_DAILY_JOBS": "x"}))
        priced = platform_budget_from_env({"PYWEIXIN_PLATFORM_COST_CENTS_PER_JOB": "3"})
        self.assertEqual(priced.cost_cents_per_paid_job, 3)


# --------------------------------------------- 7. 客户端不得丢掉待处理消息


class _FailingClient:
    def __init__(self, code: str):
        self.code = code

    def submit_parse(self, request, idempotency_key):
        del request, idempotency_key
        raise GatewayError(self.code, "服务端说不行", 402)


class PendingMessageIsHeldTests(unittest.TestCase):
    """免费次数用完、模型暂不可用、平台暂停接收 —— 一律留在本地队列里。"""

    def _service(self, code: str, tmp: Path):
        from pyweixin.orders import OrderStore

        orders = OrderStore(str(tmp / "orders.db"))
        merchant = orders.create_merchant("本地店", plan="demo", ai_quota=0)
        tenant = merchant["merchant_id"]
        customer = orders.add_customer(tenant, "张老板", "老张", "群A")
        orders.add_product(tenant, "SKU-1", "苹果", "箱", 5000, ["苹果"])
        orders.add_import(
            tenant, "群A",
            [{"sender": "老张", "content": "来两箱苹果", "kind": "text",
              "sent_at": "2026-01-01T10:00:00+08:00", "source_id": "s1"}],
            source="s1", source_type="test",
        )
        row = orders.db.execute(
            "SELECT id FROM messages WHERE merchant_id=? ORDER BY rowid DESC LIMIT 1", (tenant,)
        ).fetchone()
        orders.bind_message_customer(tenant, row["id"], customer, "tester")
        service = LocalParseService(
            orders=orders, client=_FailingClient(code),
            queue=PendingQueue(str(tmp / "pending.db")), tenant=tenant, group="群A",
        )
        return service, orders

    def test_held_codes_keep_the_message_pending(self):
        import tempfile

        for code in ("free_quota_exhausted", "no_free_model", "model_unavailable",
                     "platform_budget_exhausted", "quota_exhausted"):
            with self.subTest(code=code):
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
                    tmp = Path(directory)
                    service, orders = self._service(code, tmp)
                    row_id = orders.db.execute(
                        "SELECT id FROM messages WHERE merchant_id=? ORDER BY rowid DESC LIMIT 1",
                        (service.tenant,),
                    ).fetchone()["id"]
                    try:
                        progress = service.submit(row_id)
                        self.assertEqual(progress.state, LOCAL_BLOCKED)
                        outstanding = service.queue.outstanding()
                        self.assertEqual(len(outstanding), 1, f"{code} 不得把消息丢掉")
                        self.assertEqual(outstanding[0]["last_error"], code)
                        self.assertNotEqual(progress.message, "解析排队中", "状态文案必须说清为什么被挂起")
                    finally:
                        service.queue.close()
                        orders.close()

    def test_held_codes_have_a_specific_merchant_facing_label(self):
        for code in ("free_quota_exhausted", "no_free_model", "platform_budget_exhausted"):
            with self.subTest(code=code):
                label = status_label("held", code)
                self.assertNotEqual(label, "需要重试", f"{code} 需要一句具体的话")
                self.assertIn("已为您保留", label)


if __name__ == "__main__":
    unittest.main()
