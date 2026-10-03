"""真实并发下的额度、幂等与平台预算验证。

这些测试**真的开多个线程同时打网关**，不是把顺序调用写长一点。串行调用
永远发现不了预留超发、重复预留和跨账号串额。

跑法：

    python -m unittest tests.test_concurrency_budget -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from pyweixin.gateway import ProductGateway  # noqa: E402
from pyweixin.gateway_server import GatewayHTTPRequestHandler  # noqa: E402
from pyweixin.model_catalog import (  # noqa: E402
    CatalogStatus,
    CatalogStore,
    OperatorModelPolicy,
    PricingClass,
    build_catalog,
)
from pyweixin.opencode import LocalRuleAdapter  # noqa: E402
from pyweixin.product import (  # noqa: E402
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

#: 当前网关的计费策略版本，随请求一起带上（真实客户端先读目录再提交）。
CURRENT_PRICING_VERSION = {"value": ""}


def request(message: str = "来两箱苹果", **extra) -> dict:
    payload = {
        "message": {"id": "m1", "sender": "张老板", "content": message, "kind": "text",
                    "sent_at": "2026-01-01T10:00:00+08:00"},
        "customer": {"id": "c1", "name": "张老板", "alias": "老张", "address": ""},
        "products": [{"sku": "SKU-1", "name": "苹果", "unit": "箱", "aliases": "苹果"}],
        "history": {"recent_messages": [], "customer_facts": [], "recent_orders": []},
        "order_state": {"status": "draft"},
        "pricing_version": CURRENT_PRICING_VERSION["value"],
    }
    payload.update(extra)
    return payload


def make_catalog(free_limit: int = 5, paid_cost: int = 1) -> CatalogStore:
    policies = {
        FREE: OperatorModelPolicy(
            alias=FREE, provider_id="p", model_id="m-free", pricing_class=PricingClass.METERED,
            daily_free_limit=free_limit, is_default=True, summary="适合日常订单",
            cost_units_per_job=0, free_quota_timezone="UTC", policy_version="p1",
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


def fire(base: str, method: str, path: str, body=None, headers=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request_obj = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request_obj, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def run_concurrently(functions) -> list:
    """真的并行。启动闸门让所有线程尽可能同一刻发起。"""
    gate = threading.Barrier(len(functions))
    results: list = [None] * len(functions)
    errors: list = []

    def worker(index: int, function) -> None:
        try:
            gate.wait(timeout=20)
            results[index] = function()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i, f), name=f"race-{i}") for i, f in enumerate(functions)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)
    if errors:
        raise errors[0]
    return results


class ConcurrencyHarness(unittest.TestCase):
    free_limit = 5
    paid_cost = 1
    budget: PlatformBudget | None = None

    def build(self):
        self.store = ProductStore(":memory:")
        self.gateway = ProductGateway(
            store=self.store, adapter=LocalRuleAdapter(VALID), secure_cookies=False,
            catalog=make_catalog(self.free_limit, self.paid_cost),
            platform_budget=self.budget,
        )
        CURRENT_PRICING_VERSION["value"] = self.gateway.catalog.for_client()["version"]
        if self.budget is not None:
            self.ledger = PlatformBudgetLedger(self.store, self.budget)

        class Handler(GatewayHTTPRequestHandler):
            pass

        Handler.gateway = self.gateway
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.accounts: dict[str, dict] = {}

    def tearDown(self):
        self.gateway.stop()
        self.httpd.shutdown()
        self.httpd.server_close()

    def account(self, name: str, quota: int = 100, concurrency: int = 8) -> str:
        if name in self.accounts:
            return self.accounts[name]
        created = self.store.create_account(
            name, "password-1234", display_name=name, quota_units=quota,
            max_concurrent_jobs=concurrency,
        )
        device_id, _ = self.store._upsert_device(created["account_id"], created["merchant_id"], name + "电脑")
        tokens = self.store._issue_tokens(created["account_id"], device_id)
        self.accounts[name] = {
            "account_id": created["account_id"],
            "headers": {"Authorization": "Bearer " + tokens.access_token,
                        "X-Agent-Id": "agent-1", "Content-Type": "application/json"},
        }
        return created["account_id"]

    def submit(self, key: str, body=None):
        account = self.accounts[_self_current]
        return fire(self.base, "POST", "/v1/order-parse-jobs", body or request(),
                    {**account["headers"], "Idempotency-Key": key})


_self_current = ""


class FreeQuotaRaceTests(ConcurrencyHarness):
    """1. 只剩 1 次免费额度时，多个不同任务并发提交，不能全部通过。"""

    free_limit = 1

    def setUp(self):
        global _self_current
        _self_current = "race-shop"
        self.build()
        self.account(_self_current)

    def test_concurrent_tasks_cannot_overdraw_the_last_free_slot(self):
        attempts = 8

        def submit(index: int):
            return lambda: self.submit(f"race-{index}", request(f"第{index}条消息"))

        results = run_concurrently([submit(i) for i in range(attempts)])
        accepted = [r for r in results if r[0] in (200, 202)]
        rejected = [r for r in results if r[0] not in (200, 202)]
        self.assertEqual(len(accepted), 1, f"只剩 1 次额度，却通过了 {len(accepted)} 个任务：{results}")
        self.assertEqual(len(rejected), attempts - 1)
        for _, body in rejected:
            self.assertEqual(body["error"], "free_quota_exhausted")
        state = self.store.free_quota_state(self.accounts[_self_current]["account_id"], 1)
        self.assertLessEqual(state["used"] + state["reserved"], 1, "额度被超发")


class IdempotencyRaceTests(ConcurrencyHarness):
    """2. 多个请求提交**同一个**幂等任务，只创建一次、预留一次。"""

    def setUp(self):
        global _self_current
        _self_current = "idem-shop"
        self.build()
        self.account(_self_current)

    def test_same_idempotency_key_creates_one_job_and_one_reservation(self):
        attempts = 8

        def once():
            return self.submit("same-key", request("同一条消息"))

        results = run_concurrently([once for _ in range(attempts)])
        job_ids = {r[1]["job"]["job_id"] for r in results if r[0] in (200, 202)}
        self.assertEqual(len(job_ids), 1, f"同一幂等键建了多个任务：{job_ids}")
        rows = self.store._connection.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE account_id=? AND idempotency_key='same-key'",
            (self.accounts[_self_current]["account_id"],),
        ).fetchone()
        self.assertEqual(int(rows["c"]), 1)
        reserves = self.store._connection.execute(
            "SELECT COUNT(*) AS c FROM usage_ledger WHERE job_id=? AND kind='reserve'",
            (list(job_ids)[0],),
        ).fetchone()
        self.assertLessEqual(int(reserves["c"]), 1, "重复预留")

    def test_same_key_different_body_or_model_conflicts(self):
        first = self.submit("dup", request("消息一"))
        self.assertIn(first[0], (200, 202))
        same = self.submit("dup", request("消息一"))
        self.assertIn(same[0], (200, 202))
        self.assertEqual(same[1]["job"]["job_id"], first[1]["job"]["job_id"])
        other_body = self.submit("dup", request("消息二"))
        self.assertEqual(other_body[0], 409)
        other_model = self.submit("dup", request("消息一", model=PAID))
        self.assertEqual(other_model[0], 409)


class CrossAccountTests(ConcurrencyHarness):
    """3. 两个账号并发互不消耗对方额度。"""

    free_limit = 1

    def setUp(self):
        global _self_current
        self.build()
        self.account("alpha")
        self.account("beta")

    def test_two_accounts_do_not_share_free_quota(self):
        alpha, beta = self.accounts["alpha"]["account_id"], self.accounts["beta"]["account_id"]
        # alpha 用完自己那一次
        self.store.reserve_free(alpha, 1)
        self.store._settle_free_raw(alpha, 1)
        self.assertEqual(self.store.free_quota_state(alpha, 1)["remaining"], 0)
        self.assertEqual(self.store.free_quota_state(beta, 1)["remaining"], 1, "beta 不该被 alpha 消耗")

        def job(account: str, index: int):
            return lambda: fire(self.base, "POST", "/v1/order-parse-jobs", request(f"{account}{index}"),
                                {**self.accounts[account]["headers"], "Idempotency-Key": f"{account}-{index}"})

        # 每边 2 个（模型并发上限 2），足够看出额度差异
        results = run_concurrently([job("alpha", i) for i in range(2)] + [job("beta", i) for i in range(2)])
        alpha_results, beta_results = results[:2], results[2:]
        # alpha 额度已用完：一个都进不去；beta 自己的额度还在：至少进得去一个。
        self.assertFalse(any(r[0] in (200, 202) for r in alpha_results), f"alpha 额度用完却通过：{alpha_results}")
        self.assertTrue(any(r[0] in (200, 202) for r in beta_results), f"beta 被 alpha 牵连：{beta_results}")
        self.assertEqual(self.store.free_quota_state(alpha, 1)["used"], 1)
        self.assertEqual(self.store.free_quota_state(beta, 1)["used"], 0, "beta 还没结算")


class SettlementRaceTests(ConcurrencyHarness):
    """4. 完成 / 失败 / 取消 / 重启恢复不重复结算。"""

    def setUp(self):
        global _self_current
        _self_current = "settle-shop"
        self.build()
        self.account(_self_current)

    def _job(self, key: str, text: str) -> tuple[str, str]:
        _, body = self.submit(key, request(text))
        return body["job"]["job_id"], self.accounts[_self_current]["account_id"]

    def test_concurrent_settle_settles_once(self):
        """6 个线程同时结算**同一个**任务，只能扣一次免费次数。

        这正是"重复结算会直接多扣客户的东西"的场景。
        """
        job_id, account = self._job("settle-1", "结算一次")
        before = self.store.free_quota_state(account, 10)
        self.assertEqual(before["reserved"], 1, "任务创建时应已预留一次")

        def settle():
            self.store.settle_free(job_id)
            return True

        run_concurrently([settle for _ in range(6)])
        state = self.store.free_quota_state(account, 10)
        self.assertEqual(state["used"], 1, f"重复结算：{state}")
        self.assertEqual(state["reserved"], 0, f"预留没释放：{state}")

    def test_settle_then_release_does_not_double_count(self):
        """先结算再释放（模拟迟到或取消）不能又加一次。"""
        job_id, account = self._job("mix-1", "混合路径")
        self.store.settle_free(job_id)
        self.store.release_free(job_id)
        state = self.store.free_quota_state(account, 10)
        self.assertEqual((state["used"], state["reserved"]), (1, 0))

    def test_cancelled_job_releases_without_counting(self):
        job_id, account = self._job("cancel-1", "待取消")
        self.store.release_free(job_id)
        state = self.store.free_quota_state(account, 10)
        self.assertEqual((state["used"], state["reserved"]), (0, 0), "取消应只释放不计数")
        # 重复释放不能重复扣
        run_concurrently([lambda: self.store.release_free(job_id) for _ in range(4)])
        self.assertEqual(self.store.free_quota_state(account, 10)["used"], 0)

    def test_repeated_settle_after_recovery_stays_one(self):
        """模拟网关重启后重复处理同一个任务：仍然只扣一次。"""
        job_id, account = self._job("restart-1", "重启任务")
        self.store._connection.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
        self.store._connection.commit()
        # 重启后恢复流程会再次尝试结算同一个任务
        run_concurrently([lambda: self.store.settle_free(job_id) for _ in range(4)])
        self.assertEqual(self.store.free_quota_state(account, 10)["used"], 1)


class ConcurrencyLimitTests(ConcurrencyHarness):
    """6. 并发运行数限制真正生效。"""

    def setUp(self):
        global _self_current
        _self_current = "conc-shop"
        self.build()
        self.account(_self_current, concurrency=2)

    def test_plan_concurrency_limit_blocks_the_third_job(self):
        results = run_concurrently([
            (lambda i=i: self.submit(f"c-{i}", request(f"消息{i}"))) for i in range(6)
        ])
        accepted = [r for r in results if r[0] in (200, 202)]
        self.assertEqual(len(accepted), 2, f"并发上限 2，却接受了 {len(accepted)}：{results}")
        errors = {b.get("error") for s, b in results if s not in (200, 202)}
        self.assertTrue(
            errors <= {"concurrency_limit", "model_concurrency_limit", "free_quota_exhausted", "subscription_inactive"},
            f"被拒的原因不对：{errors}",
        )


class PlatformBudgetTests(ConcurrencyHarness):
    """平台总预算用尽时停止接收新任务。"""

    paid_cost = 1

    def setUp(self):
        global _self_current
        _self_current = "budget-shop"
        self.budget = PlatformBudget(daily_paid_jobs=2, max_paid_concurrency=1, free_jobs_exempt=True)
        self.build()
        self.account(_self_current)
        # 网关拿的是**带账本**的那个，配置对象本身没有用量数据。
        self.gateway.platform_budget = PlatformBudgetLedger(self.store, self.budget)

    def test_platform_budget_stops_new_paid_jobs(self):
        self.ledger = PlatformBudgetLedger(self.store, self.budget)
        # 直接记两笔已结算，模拟今天已经用掉两次付费预算
        day = self.store._day_key()
        for _ in range(2):
            self.store._connection.execute(
                "INSERT INTO usage_ledger(id,job_id,account_id,kind,units,note,created_at,day,charged_units)"
                " VALUES(?,'',?,'settle',1,'x',0,?,1)",
                ("ul_" + __import__("secrets").token_hex(8), self.accounts[_self_current]["account_id"], day),
            )
        self.store._connection.commit()
        self.assertFalse(self.ledger.can_accept(1), "预算应已用尽")
        status, body = self.submit("paid-1", request("付费任务", model=PAID))
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "platform_budget_exhausted")

    def test_free_jobs_are_not_blocked_by_the_paid_budget(self):
        self.ledger = PlatformBudgetLedger(self.store, self.budget)
        day = self.store._day_key()
        for _ in range(5):
            self.store._connection.execute(
                "INSERT INTO usage_ledger(id,job_id,account_id,kind,units,note,created_at,day,charged_units)"
                " VALUES(?,'',?,'settle',1,'x',0,?,1)",
                ("ul_" + __import__("secrets").token_hex(8), self.accounts[_self_current]["account_id"], day),
            )
        self.store._connection.commit()
        self.assertFalse(self.ledger.can_accept(1), "付费预算用尽")
        self.assertTrue(self.ledger.can_accept(0), "免费任务不应被付费预算挡住")
        status, _ = self.submit("free-1", request("免费任务"))
        self.assertIn(status, (200, 202))

    def test_unknown_price_is_reported_as_unknown_not_invented(self):
        budget = PlatformBudget(daily_paid_jobs=10)
        ledger = PlatformBudgetLedger(self.store, budget)
        self.assertIsNone(budget.cost_cents_per_paid_job)
        self.assertIsNone(ledger.settled_cost_cents_today(), "不知道单价时不得编造成本")
        self.assertFalse(ledger.status()["cost_known"])


if __name__ == "__main__":
    unittest.main()
