"""交付前的安全与链路核实：不打印任何密钥。

    python examples/closing_checks.py

检查四件事：
1. 供应商凭据不会出现在目录、错误响应或日志里。
2. 客户选定的模型**真的**传到了执行后端（不是只在界面上换了标签）。
3. 目录/快照采用字段允许列表，而不是"过滤黑名单"。
4. 别名一路走到 OpenCode 的请求体，中间没有"改了但没人读"的环节。
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _name in ("business_agent", "order_agent"):
    _src = ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from business_agent.backends.opencode import OpenCodeBackend  # noqa: E402
from pyweixin.gateway import ProductGateway  # noqa: E402
from pyweixin.model_catalog import (  # noqa: E402
    CatalogStatus,
    CatalogStore,
    OperatorModelPolicy,
    PricingClass,
    build_catalog,
)
from pyweixin.opencode import LocalRuleAdapter  # noqa: E402
from pyweixin.product import ProductStore  # noqa: E402

#: 用来确认"确实没泄露"的哨兵值。它不是真密钥，只是一个不会自然出现的标记。
SENTINEL = "SENTINEL-CREDENTIAL-VALUE-9f2a"

SECRET_KEYS = ("key", "token", "secret", "password", "credential", "authorization", "apikey", "api_key")


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('　' + detail) if detail else ''}")
    return ok


def collect_secret_keys(node, path="", out=None) -> list[str]:
    """找出任何看起来像凭据的字段路径。"""
    if out is None:
        out = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            lowered = str(key).lower()
            if any(marker in lowered for marker in SECRET_KEYS) and value not in (None, "", [], {}):
                out.append(here)
            collect_secret_keys(value, here, out)
    elif isinstance(node, list):
        for index, item in enumerate(node[:5]):
            collect_secret_keys(item, f"{path}[{index}]", out)
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    failures = 0

    print("① 上游目录规范化：凭据在这一层就被丢掉")
    payload = {
        "all": [{
            "id": "p", "name": "供应商", "key": SENTINEL,
            "options": {"baseURL": "https://api.example/v1", "headers": {"X-Api-Key": SENTINEL}},
            "env": ["PROVIDER_KEY"],
            "models": [{"id": "m", "name": "模型", "limit": {"context": 100, "output": 50},
                        "cost": {"input": 1}, "capabilities": {}, "status": "live"}],
        }],
        "connected": ["p"],
    }
    captured: dict = {}

    def transport(method, path, body, timeout):
        captured["path"] = path
        return payload

    backend = OpenCodeBackend("http://127.0.0.1:1", password="x", transport=transport)
    providers = backend.list_providers()
    blob = json.dumps(providers, ensure_ascii=False)
    failures += 0 if check("凭据不出现在 list_providers() 的返回值里", SENTINEL not in blob) else 1
    failures += 0 if check("上游地址不出现在返回值里", "api.example" not in blob) else 1
    found = collect_secret_keys(providers)
    failures += 0 if check("返回值里没有任何凭据类字段", not found, "、".join(found)) else 1

    print()
    print("② 客户目录：字段允许列表（构造新对象，而不是黑名单过滤）")
    policy = OperatorModelPolicy(
        alias="free-standard", provider_id="p", model_id="m", pricing_class=PricingClass.FREE,
        daily_free_limit=50, is_default=True, summary="适合日常订单",
    )
    store = CatalogStore()
    store.load_policies([policy.to_dict()])
    catalog = build_catalog(providers, {policy.alias: policy}, now=1.0)
    catalog.mark_status(policy.alias, CatalogStatus.SELECTABLE, verified=True, now=1.0)
    client_view = catalog.for_client()
    allowed = {
        "version", "default_alias", "free_available", "stale", "updated_at",
        "last_success_at", "notice", "models", "free_quota",
    }
    unexpected = set(client_view) - allowed
    failures += 0 if check("顶层字段全部在允许列表内", not unexpected, "、".join(sorted(unexpected))) else 1
    model_fields = {
        "alias", "display_name", "summary", "free", "free_label", "charge_line",
        "daily_free_limit", "free_quota_text", "status", "status_label",
        "status_reason", "is_default", "capabilities", "context_limit", "slow",
    }
    for item in client_view["models"]:
        extra = set(item) - model_fields
        failures += 0 if check(f"模型卡片字段在允许列表内（{item['alias']}）", not extra, "、".join(sorted(extra))) else 1
    failures += 0 if check("客户目录不含上游 provider/model id", "provider_id" not in json.dumps(client_view)) else 1

    print()
    print("③ 错误响应不含凭据")
    from pyweixin.model_catalog import reject_client_model_override
    from pyweixin.client_login import GatewayError

    try:
        reject_client_model_override({"provider": "p", "api_key": SENTINEL, "upstream_url": "http://x"})
    except GatewayError as exc:
        text = f"{exc} {getattr(exc, 'code', '')}"
        failures += 0 if check("被拒请求的回显里没有凭据值", SENTINEL not in text, text[:70]) else 1
        failures += 0 if check("错误只回显字段名不回显值", "provider" in text) else 1

    print()
    print("④ 日志不含凭据")
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Capture()
    root = logging.getLogger("pyweixin")
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    gw = ProductGateway(store=ProductStore(":memory:"), adapter=LocalRuleAdapter(), catalog=CatalogStore())
    try:
        reject_client_model_override({"api_key": SENTINEL})
    except GatewayError:
        pass
    gw._operator_refresh_catalog({"models": [dict(policy.to_dict())]})
    logging.getLogger("pyweixin.catalog").warning("forced %s", SENTINEL)
    root.removeHandler(handler)
    log_text = "\n".join(r.getMessage() for r in records)
    # 我们自己写的日志不应包含凭据；故意打的那条会被排除。
    own = [r for r in records if "forced" not in r.getMessage()]
    own_text = "\n".join(r.getMessage() for r in own)
    failures += 0 if check("产品日志不含凭据值", SENTINEL not in own_text, own_text[:60].replace("\n", " ")) else 1

    print()
    print("⑤ 客户选定的模型真的传到了执行后端")
    from pyweixin.client_login import PendingQueue
    from pyweixin.local_parse import LocalParseService
    from pyweixin.client_login import MerchantClient, Session
    from pyweixin.orders import OrderStore

    calls: list[dict] = []

    class FakeClient(MerchantClient):
        def submit_parse(self, request, idempotency_key):
            calls.append(dict(request))
            return {"job": {"job_id": "j1", "state": "queued"}}

    orders = OrderStore(":memory:")
    tenant = orders.create_merchant("核实商家")["merchant_id"]
    orders.add_customer(tenant, "张姐", "张姐", "测试群")
    orders.add_product(tenant, "APL", "苹果", "箱", 2600, ["苹果"])
    orders.add_import(tenant, "测试群", [{
        "sender": "张姐", "content": "苹果两箱", "kind": "text",
        "sent_at": "2026-01-01T00:00:00+00:00", "source_id": "s1", "stable_source_id": True,
    }], "closing", "test")
    orders.db.commit()
    message_id = orders.db.execute(
        "SELECT id FROM messages WHERE merchant_id=?", (tenant,)
    ).fetchone()["id"]

    session = Session(access_token="t", refresh_token="r")
    service = LocalParseService(
        orders=orders,
        client=FakeClient("http://127.0.0.1:1", session=session, allow_insecure_remote=True),
        queue=PendingQueue(":memory:"),
        tenant=tenant, group="测试群", model_alias="free-standard",
    )
    built = service.build_request(message_id)
    failures += 0 if check(
        "build_request 带上客户选定的模型别名", built.get("model") == "free-standard", str(built.get("model"))
    ) else 1

    service.model_alias = ""
    failures += 0 if check(
        "未选模型时留空，由网关用它的默认免费模型",
        service.build_request(message_id).get("model") == "",
    ) else 1

    service.model_alias = "paid-deep"
    service.submit(message_id)
    failures += 0 if check(
        "提交给网关的请求体里确实带着该别名", calls and calls[-1].get("model") == "paid-deep",
        str(calls[-1].get("model")) if calls else "未提交",
    ) else 1

    print()
    print("⑥ 别名一路走到 OpenCode 请求体（不只是界面上换了标签）")
    # 上面只证明了"别名到了网关"。这一段证明网关把它还原成真正的
    # provider/model，并写进发往 OpenCode 的请求体 —— 中间任何一段
    # "改属性但没人读"都会在这里露馅。
    bodies: list[dict] = []

    def transport(method, path, body, timeout):
        del timeout
        if path == "/session":
            return {"id": "ses-e2e"}
        bodies.append(dict(body or {}))
        return {"info": {"id": "m", "role": "assistant"},
                "parts": [{"type": "text", "text": json.dumps({
                    "intent": "new_order", "customer_alias": "张姐", "target_order": None,
                    "items": [{"sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
                    "delivery_date": None, "evidence": ["苹果两箱"], "missing_fields": [],
                    "ambiguities": [], "suggested_change": None,
                }, ensure_ascii=False)}]}

    policies = {
        "free-standard": OperatorModelPolicy(
            alias="free-standard", provider_id="up-免费", model_id="模型-免费",
            pricing_class=PricingClass.FREE, daily_free_limit=5, is_default=True,
        ),
    }
    catalog = CatalogStore()
    catalog.load_policies(list(policies.values()))
    catalog.catalog = build_catalog(
        [{"provider_id": "up-免费", "name": "上游", "connected": True,
          "models": [{"model_id": "模型-免费", "display_name": "免费模型", "context_limit": 8000,
                      "output_limit": 1000, "capabilities": {}, "upstream_cost": {},
                      "upstream_status": "live"}]}],
        policies,
    )
    catalog.mark_status("free-standard", CatalogStatus.SELECTABLE, verified=True, now=1.0)

    from pyweixin.opencode import OpenCodeAdapter

    adapter = OpenCodeAdapter("http://127.0.0.1:1", password="only-for-this-check")
    adapter._backend._transport = transport
    store = ProductStore(":memory:")
    gw = ProductGateway(store=store, adapter=adapter, secure_cookies=False, catalog=catalog)
    account = store.create_account("closing-shop", "password-1234", quota_units=5, max_concurrent_jobs=2)
    device_id, _ = store._upsert_device(account["account_id"], account["merchant_id"], "核实电脑")
    identity = {
        "account_id": account["account_id"], "device_id": device_id,
        "merchant_id": account["merchant_id"],
    }
    job = gw._create_job(identity, {
        "model": "free-standard",
        "message": {"id": "m-e2e", "sender": "张姐", "content": "苹果两箱", "kind": "text",
                    "sent_at": "2026-01-01T00:00:00+08:00"},
        "customer": {"id": "c-e2e", "name": "张姐", "alias": "张姐", "address": ""},
        "products": [{"sku": "APL", "name": "苹果", "unit": "箱", "aliases": "苹果"}],
        "history": {"recent_messages": [], "customer_facts": [], "recent_orders": []},
        "order_state": {},
    }, {"Idempotency-Key": "closing-e2e"}).payload["job"]
    gw._run_job(dict(job))
    sent = bodies[-1].get("model") if bodies else None
    failures += 0 if check(
        "请求体里的 model 就是策略声明的上游目标",
        sent == {"providerID": "up-免费", "modelID": "模型-免费"}, str(sent),
    ) else 1
    snapshot = store.job_snapshot(job["job_id"]) or {}
    failures += 0 if check(
        "任务冻结的快照与实际发出的模型一致",
        (snapshot.get("provider_id"), snapshot.get("model_id")) == ("up-免费", "模型-免费"),
    ) else 1
    failures += 0 if check(
        "这一单是免费任务，订阅额度没有被扣",
        store.get_subscription(account["account_id"])["used_units"] == 0
        and store.free_quota_state(account["account_id"], 5)["used"] == 1,
    ) else 1
    store.close()

    print()
    print(f"{'全部通过' if failures == 0 else str(failures) + ' 项未通过'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
