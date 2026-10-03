"""真实模型目录联调：网关拉取 OpenCode 目录 → 运营配置 → 客户可见目录。

    python examples\catalog_live_check.py

验证的是**真实上游**，不是合成数据：目录真的从 OpenCode 拉，订单能力验证
真的发请求。只报告可用性与结果，不打印任何凭据。
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

from pyweixin.gateway import ProductGateway  # noqa: E402
from pyweixin.model_catalog import CatalogStatus, OperatorModelPolicy, PricingClass  # noqa: E402
from pyweixin.opencode import LocalRuleAdapter, OpenCodeAdapter  # noqa: E402
from pyweixin.product import ProductStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catalog-live-check")
    parser.add_argument("--opencode", default=os.environ.get("PYWEIXIN_OPENCODE_URL", "http://127.0.0.1:4096"))
    parser.add_argument("--password", default=os.environ.get("PYWEIXIN_OPENCODE_PASSWORD", ""))
    parser.add_argument("--provider", default=os.environ.get("CATALOG_PROVIDER", "opencode-go"))
    parser.add_argument("--model", default=os.environ.get("CATALOG_MODEL", "qwen3.6-plus"))
    parser.add_argument("--alias", default=os.environ.get("CATALOG_ALIAS", "free-standard"))
    parser.add_argument("--skip-verify", action="store_true", help="只拉目录，不发真实验证请求")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    workdir = Path(os.environ.get("TEMP", ".")) / "pyw_catalog_live"
    workdir.mkdir(parents=True, exist_ok=True)
    store = ProductStore(str(workdir / "product.db"))
    adapter = OpenCodeAdapter(
        args.opencode, password=args.password, agent="order-parse",
        provider_id=args.provider, model_id=args.model,
    )

    print("① 拉取真实上游目录（OpenCode GET /provider）")
    report = adapter.probe()
    print(f"   后端 ok={report.get('ok')} 版本={report.get('version')}")
    if not report.get("ok"):
        print(f"   无法连接 {args.opencode}：{report.get('error')}")
        return 1

    gateway = ProductGateway(store=store, adapter=adapter)
    print(f"   拉取前目录条目：{len(gateway.catalog)}")
    gateway.catalog.bind(lambda: adapter.list_providers(), {})
    gateway.catalog.refresh(force=True)
    print(f"   已注册 provider 数：{len(adapter.list_providers())}")
    print(f"   未配置策略时的目录：{len(gateway.catalog)} 条（应为 0：未放行的模型不下发）")

    print()
    print("② 运营配置：把这个模型放为默认免费模型")
    policy = OperatorModelPolicy(
        alias=args.alias,
        provider_id=args.provider,
        model_id=args.model,
        pricing_class=PricingClass.FREE,
        daily_free_limit=50,
        is_default=True,
        summary="适合日常订单",
    )
    result = gateway._operator_set_policies({"models": [policy.to_dict()]})
    print(f"   目录版本={result['version']} 默认={result['default_alias']}")
    entry = gateway.catalog.entry(args.alias)
    print(f"   状态={entry.status}（discovered 表示还没验证过，不可选）")
    print(f"   客户默认={gateway.catalog.default_entry() and gateway.catalog.default_entry().alias}")
    print(f"   合格免费模型数={len(gateway.catalog.qualified_free())}（未验证时应为 0）")

    print()
    print("③ 客户看到的目录（只有别名）")
    client = gateway.catalog.for_client()
    print("   " + json.dumps(client, ensure_ascii=False)[:400])
    blob = json.dumps(client, ensure_ascii=False)
    for leak in (args.provider, args.model, "upstream", "baseURL", "key"):
        if leak in blob:
            print(f"   !! 泄露：{leak}")
            return 1
    print("   未发现上游标识泄露")

    if args.skip_verify:
        print()
        print("（已跳过真实验证：模型保持 discovered，不会进入客户可选列表）")
        return 0

    print()
    print("④ 订单能力验证（合成数据，真实调用）")
    started = time.time()
    verified = gateway._operator_verify_model({"alias": args.alias})
    print(f"   耗时 {time.time() - started:.1f}s　通过={verified['passed']}")
    for case in verified["checks"].get("cases", []):
        mark = "PASS" if case.get("ok") else "FAIL"
        problems = "；".join(case.get("problems", []))[:150]
        error = str(case.get("error", ""))[:150]
        detail = problems or error
        print(f"   {mark} {case['case']:<20} {detail}")
    print(f"   备注：{verified['note']}")

    entry = gateway.catalog.entry(args.alias)
    print(f"   验证后状态={entry.status}")
    print(f"   现在是默认免费模型吗：{gateway.catalog.default_entry() and gateway.catalog.default_entry().alias}")
    client = gateway.catalog.for_client()
    for model in client["models"]:
        print(f"   客户视图：{model['display_name']} 免费={model['free']} "
              f"{model['free_quota_text']} 默认={model['is_default']} 状态={model['status_label']}")
    store.save_catalog(gateway.catalog.catalog)
    store.close()
    return 0 if verified["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
