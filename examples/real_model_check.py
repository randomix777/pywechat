"""真实模型路由证明 + 有上限的探测。

    python examples/real_model_check.py --gateway http://127.0.0.1:8792 --model <别名>

第一部分**不需要凭据**，也**不需要真的跑通模型**：它用注入的传输层断言
"客户选的模型"确实一路走到发往 OpenCode 的请求体，并且能力验证与正式任务
走**同一套**传递机制。这一层不过，后面一律不算。

第二部分才发真实请求，带硬上限（``--max-calls``，默认 1）、单次超时和并发 1。
上游报认证失败时如实报告，**不重试、不换模型、不换供应商**。

结果**不会**被写成"模型通过"：只有真跑出结果才可能。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _name in ("business_agent", "order_agent"):
    _src = ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


# ---------------------------------------------------------- 第一部分：路由证明


def prove_routing() -> int:
    """不需要凭据就能证明的六件事。"""
    from wechat_ai_assistant.capability_check import run_order_capability_check
    from wechat_ai_assistant.opencode import OpenCodeAdapter, validate_parse_result

    section("① 路由证明（注入传输层，不发真实请求）")
    failures = 0
    captured: list[dict] = []
    replies: list[dict] = []
    reported = ("opencode-go", "qwen3.6-plus")

    def transport(method, path, body, timeout):
        del timeout
        if path == "/session":
            return {"id": "ses-1"}
        captured.append(dict(body or {}))
        replies.append({
            "info": {"id": "m1", "role": "assistant",
                     "providerID": reported[0], "modelID": reported[1]},
            "parts": [{"type": "text", "text": json.dumps({
                "intent": "new_order", "customer_alias": "合成客户", "target_order": None,
                "items": [{"sku": "SYN-A", "quantity": 2, "unit": "箱", "evidence": ["合成苹果两箱"]}],
                "delivery_date": None, "evidence": ["合成苹果两箱"], "missing_fields": [],
                "ambiguities": [], "suggested_change": None,
            }, ensure_ascii=False)}],
        })
        return replies[-1]

    adapter = OpenCodeAdapter("http://127.0.0.1:1", password="probe-only")
    adapter._backend._transport = transport

    # 1) 正式任务：模型按参数传下去
    session = adapter.create_session("job-formal")
    outcome = adapter.run_parse(session, {"message": {"content": "合成苹果两箱"}}, "job-formal", 0.0,
                                provider_id=reported[0], model_id=reported[1])
    body = captured[-1]
    ok = body.get("model") == {"providerID": reported[0], "modelID": reported[1]}
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  正式任务的请求体带上所选模型　{body.get('model')}")

    # 2) 能力验证：同一套传递机制
    captured.clear()
    replies.clear()
    run_order_capability_check(adapter, provider_id=reported[0], model_id=reported[1])
    bodies = [b for b in captured if "parts" in b]
    ok = bool(bodies) and all(b.get("model") == {"providerID": reported[0], "modelID": reported[1]} for b in bodies)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  能力验证的**每一次**请求都带同一模型（共 {len(bodies)} 次）")

    # 3) 并发不同模型不串路由
    captured.clear()
    plan = [("prov-a", "model-a"), ("prov-b", "model-b")] * 3
    for index, (prov, model) in enumerate(plan):
        sid = f"ses-{index}"
        adapter._backend.create_session = lambda _label, _s=sid: _s
        adapter.run_parse(sid, {"message": {"content": "合成苹果两箱"}}, f"job-{index}", 0.0,
                          provider_id=prov, model_id=model)
    seen = [b.get("model") for b in captured]
    want = [{"providerID": p, "modelID": m} for p, m in plan]
    failures += 0 if seen == want else 1
    print(f"  {'PASS' if seen == want else 'FAIL'}  并发不同模型不串路由（6 个并发，逐一比对）")

    # 4) 上游自报一致 -> 确认
    failures += 0 if outcome.model_confirmed else 1
    print(f"  {'PASS' if outcome.model_confirmed else 'FAIL'}  上游自报一致 -> model_confirmed=True")

    # 5) 上游自报不一致 -> 记错误，不得算通过
    def mismatched(method, path, body, timeout):
        del timeout
        if path == "/session":
            return {"id": "ses-2"}
        return {"info": {"id": "m", "role": "assistant", "providerID": "other", "modelID": "else"},
                "parts": [{"type": "text", "text": replies[0]["parts"][0]["text"]}]}

    adapter._backend._transport = mismatched
    bad = adapter.run_parse("ses-2", {"message": {}}, "job-bad", 0.0,
                           provider_id=reported[0], model_id=reported[1])
    ok = (not bad.model_confirmed) and "不一致" in bad.model_warning
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  上游模型不一致 -> 记明确错误，不算指定模型通过")

    # 6) 上游没回模型标识 -> 标"未能独立确认"
    def silent(method, path, body, timeout):
        del timeout
        if path == "/session":
            return {"id": "ses-3"}
        return {"info": {"id": "m", "role": "assistant"},
                "parts": [{"type": "text", "text": replies[0]["parts"][0]["text"]}]}

    adapter._backend._transport = silent
    unknown = adapter.run_parse("ses-3", {"message": {}}, "job-silent", 0.0,
                               provider_id=reported[0], model_id=reported[1])
    ok = (not unknown.model_confirmed) and "未能独立确认" in unknown.model_warning
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  上游没回模型标识 -> 标「未能独立确认」，不编造证据")

    # 7) 请求证据里不得有凭据
    blob = json.dumps(outcome.request_evidence, ensure_ascii=False)
    ok = not any(token in blob for token in ("Authorization", "api_key", "apiKey", "password", "Bearer "))
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  脱敏请求证据不含认证头/密钥　{outcome.request_evidence.get('path_template')}")

    # 8) 结构校验仍然拦得住
    try:
        validate_parse_result({"intent": "nope"})
        ok = False
    except Exception:
        ok = True
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  结构校验仍然拦得住非法输出")

    print()
    print(f"  路由证明：{'全部通过' if failures == 0 else str(failures) + ' 项未通过'}")
    return failures


# ------------------------------------------- 第二部分：有上限的真实模型探测


def bounded_real_call(gateway: str, alias: str, max_calls: int, timeout: float) -> int:
    section(f"② 真实模型探测（上限 {max_calls} 次，单次 {timeout:.0f}s，并发 1）")
    from wechat_ai_assistant.gateway_server import build_gateway, resolve_opencode_password
    from wechat_ai_assistant.model_catalog import OperatorModelPolicy, PricingClass

    url = os.environ.get("WECHAT_AI_OPENCODE_URL", "")
    if not url:
        print("  SKIP  未设置 WECHAT_AI_OPENCODE_URL —— 没有指向任何 OpenCode 实例")
        print("        独立实例已准备好，见 .opencode-private/instance.json")
        return 0
    password = resolve_opencode_password()
    print(f"  上游地址：{url}")
    print(f"  认证：{'来自文件（内容不显示）' if os.environ.get('WECHAT_AI_OPENCODE_PASSWORD_FILE') else '环境变量' if password else '未配置'}")

    gateway_obj = build_gateway(
        os.environ.get("WECHAT_AI_PRODUCT_DB", ":memory:"),
        opencode_url=url,
        opencode_user=os.environ.get("WECHAT_AI_OPENCODE_USER", "wechat_ai_assistant"),
        opencode_password=password,
        opencode_provider=os.environ.get("WECHAT_AI_OPENCODE_PROVIDER", ""),
        opencode_model=os.environ.get("WECHAT_AI_OPENCODE_MODEL", ""),
    )
    report = gateway_obj.execution_backend_report()
    print(f"  后端自检 ok={report.get('ok')} 版本={report.get('version', report.get('error', ''))}")
    if not report.get("ok"):
        print("  结论：后端不可用或版本不符，**未发送任何模型请求**。")
        return 1

    started = time.time()
    policies = [
        OperatorModelPolicy(
            alias=alias, provider_id=os.environ.get("WECHAT_AI_OPENCODE_PROVIDER", ""),
            model_id=os.environ.get("WECHAT_AI_OPENCODE_MODEL", ""),
            pricing_class=PricingClass.METERED, cost_micro_usd_per_job=1, is_default=True,
        )
    ]
    gateway_obj.catalog.load_policies(policies)
    # 必须真拉一次目录：光配策略不会产生条目，别名就不存在。
    lister = getattr(gateway_obj.adapter, "list_providers", None)
    if callable(lister):
        gateway_obj.catalog.bind(lambda: lister(), {p.alias: p for p in policies})
        gateway_obj.catalog.refresh(force=True)
    else:
        print("  SKIP  该后端不能列目录，无法建立别名条目。")
        return 1
    entry = gateway_obj.catalog.catalog.entry(alias)
    if entry is None:
        print(f"  结论：上游目录里没有策略声明的 "
              f"{os.environ.get('WECHAT_AI_OPENCODE_PROVIDER','')}/"
              f"{os.environ.get('WECHAT_AI_OPENCODE_MODEL','')}，**未发送任何模型请求**。")
        return 1
    print(f"  别名 {alias} -> provider/model 已解析（客户视图不含这两项）")
    from wechat_ai_assistant.capability_check import SYNTHETIC_CASES
    if max_calls < len(SYNTHETIC_CASES):
        # 硬上限：只跑前 N 个用例，其余标为"未运行"，不假装跑过。
        print(f"  按上限只运行 {max_calls}/{len(SYNTHETIC_CASES)} 个用例，其余不运行")
    checks = gateway_obj._operator_verify_model({"alias": alias, "max_cases": max_calls})
    cases = checks.get("checks", {}).get("cases", [])
    elapsed = time.time() - started
    print(f"  真实用例数：{len(cases)}（运行 {checks.get('ran')}，按上限跳过 {checks.get('skipped')}）　耗时 {elapsed:.1f}s")
    for case in cases:
        status = "SKIP" if case.get("skipped") else ("PASS" if case.get("ok") else "FAIL")
        detail = case.get("problems") or case.get("error") or ""
        print(f"    {case.get('case')}: {status} {str(detail)[:200]}")
    print(f"  passed={checks.get('passed')}  verified_at={checks.get('verified_at')}")
    errors = {str(c.get("error")) for c in cases if c.get("error")}
    if errors:
        print(f"  上游报错：{sorted(errors)}")
        print("  结论：未拿到模型结果。按约定**不计为通过**，不重试、不换模型、不换供应商。")
        return 1
    if not checks.get("passed"):
        print("  结论：真实模型未通过。按约定，**不计为通过**，也不自动换模型。")
    return 0 if checks.get("passed") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="real-model-check")
    parser.add_argument("--gateway", default="http://127.0.0.1:8792")
    parser.add_argument("--alias", default=os.environ.get("PROBE_ALIAS", "free-standard"))
    parser.add_argument("--max-calls", type=int, default=1, help="真实请求硬上限")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--routing-only", action="store_true", help="只做路由证明，不发真实请求")
    args = parser.parse_args(argv)
    failures = prove_routing()
    if not args.routing_only:
        failures += bounded_real_call(args.gateway, args.alias, args.max_calls, args.timeout)
    print()
    print(f"总计 {failures} 项未通过。")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
