"""订单能力验证：用合成数据发真实请求，检查模型是否够格。

准入标准。**免费不能以牺牲订单正确性为代价**，所以一个模型要成为默认免费
模型，至少要通过这些检查：

1. 中文数量转成 JSON 数字（"两箱" -> 2）
2. 商品与单位正确匹配目录
3. 不编造来源证据（引用的原文必须逐字出现）
4. 「老样子」能引用到历史订单明细
5. 追加 / 改量 / 查询意图能区分
6. 结构化输出能通过现有 schema
7. 信息不足时返回歧义而不是硬猜

只用合成数据。**不自动上传客户真实聊天。**
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from ._packages import ensure_packages_on_path

ensure_packages_on_path()

from order_agent.contract import validate_output  # noqa: E402

#: 合成目录。名字刻意用不指向任何真实商家的词。
SYNTHETIC_CATALOG = [
    {"sku": "SYN-A", "name": "合成苹果", "unit": "箱", "aliases": ["合成苹果", "A"]},
    {"sku": "SYN-B", "name": "合成鸡蛋", "unit": "箱", "aliases": ["合成鸡蛋"]},
]

SYNTHETIC_CUSTOMER = {"id": "syn-c1", "name": "合成客户", "alias": "合成客户", "address": ""}

#: (用例名, 消息, 历史订单, 期望)
SYNTHETIC_CASES = (
    ("chinese_number", "合成苹果两箱", [], {"intent": "new_order", "sku": "SYN-A", "quantity": 2, "unit": "箱"}),
    ("append", "再加一箱合成苹果", [], {"intent": "append", "quantity": 1}),
    ("inquiry", "合成苹果多少钱", [], {"intent": "inquiry"}),
    (
        "history_reference",
        "老样子",
        [{"order_id": "syn-o1", "status": "confirmed",
          "items": [{"sku": "SYN-A", "quantity": 2, "unit": "箱"}]}],
        {"intent": "new_order", "target_order": "syn-o1", "quantity": 2},
    ),
    ("insufficient", "来个那种", [], {"ambiguity_expected": True}),
)


def _payload(message: str, recent_orders: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "message": {"id": "syn-m", "sender": "合成客户", "content": message, "kind": "text", "sent_at": "2026-01-01T00:00:00+00:00"},
        "customer": dict(SYNTHETIC_CUSTOMER),
        "products": [dict(item) for item in SYNTHETIC_CATALOG],
        "history": {"recent_messages": [], "customer_facts": [], "recent_orders": recent_orders},
        "order_state": {"open_orders": []},
    }


def run_order_capability_check(
    adapter: Any, *, provider_id: str = "", model_id: str = "", max_cases: int = 0
) -> dict[str, Any]:
    """跑一遍全部用例，返回逐项结果。

    任何一项失败都不会让整次验证崩掉 —— 我们要的是「哪一项不达标」的
    证据，而不是第一个异常。

    ``max_cases`` 给真实探测用：只跑前 N 个用例，其余**不运行**并在结果里
    标出来。宁可少跑，也不要在一个没有凭据的实例上无上限地试探。
    """
    from business_agent.contracts.protocols import PromptPackage  # noqa: F401

    cases: list[dict[str, Any]] = []
    passed_all = True
    selected = SYNTHETIC_CASES if max_cases <= 0 else SYNTHETIC_CASES[:max_cases]
    for name, message, history, expected in selected:
        record: dict[str, Any] = {"case": name, "expected": expected}
        try:
            result = _run_case(adapter, message, history, provider_id, model_id)
        except Exception as exc:
            record.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            passed_all = False
            cases.append(record)
            continue
        checks = _check_case(message, history, result, expected)
        record.update(checks)
        if not checks.get("ok"):
            passed_all = False
        cases.append(record)
    for skipped in SYNTHETIC_CASES[len(selected):]:
        cases.append({"case": skipped[0], "ok": False, "skipped": True,
                      "error": f"按调用上限未运行（本轮上限 {max_cases}）"})

    return {
        "passed": passed_all,
        "provider_id": provider_id,
        "model_id": model_id,
        "cases": cases,
        "ran": len(selected),
        "skipped": len(SYNTHETIC_CASES) - len(selected),
        "note": "冒烟验证，只用合成数据；通过不代表生产准确率已达标",
    }


def _run_case(adapter: Any, message: str, history: list[dict[str, Any]], provider_id: str, model_id: str) -> dict[str, Any]:
    from business_agent.contracts.protocols import PromptPackage
    from business_agent.contracts.request import AgentRequest, AsOf, Budget, Event, Policy, Scope
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    request = AgentRequest(
        request_id="syn-verify",
        idempotency_key="syn-verify",
        task_type="order_parse",
        scope=Scope(tenant_id="synthetic"),
        events=(Event(event_id="syn-m", body=message, occurred_at=now, actor="合成客户"),),
        as_of=AsOf(instant=now),
        policy=Policy(name="synthetic"),
        budget=Budget(timeout_seconds=180.0),
    )
    payload = _payload(message, history)
    prompt = PromptPackage(system="", payload=payload, max_output_tokens=1500, run_label="syn-verify")
    # 会话必须真实创建：直接编一个 id 会让上游 500，验证结果就变成"模型不行"
    # 的假象。用完即删，与网关的会话清理一致。
    # 路由按**参数**传下去，不改适配器属性 —— 改属性时，改的其实是一份没人
    # 读的副本，验证的仍然是默认模型，结论就成了假通过。
    session_id = adapter.create_session("syn-verify")
    try:
        outcome = adapter.run_parse(
            session_id, payload, "syn-verify", now.timestamp() + 300.0,
            provider_id=provider_id, model_id=model_id,
        )
    finally:
        try:
            adapter.delete_session(session_id)
        except Exception:
            pass
    return outcome.result


def _check_case(message: str, history: list[dict[str, Any]], result: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    """逐项对照。返回的第一条失败就是商户会看到的问题。"""
    problems: list[str] = []

    # 6) 结构化输出必须能通过现有 schema
    try:
        validate_output(result)
    except Exception as exc:
        return {"ok": False, "schema_valid": False, "problems": [f"schema 不通过：{exc}"], "raw": json.dumps(dict(result), ensure_ascii=False)[:300]}

    # 3) 证据必须逐字出现在消息里
    haystack = message
    for quote in result.get("evidence", []):
        if quote not in haystack:
            problems.append(f"编造了来源证据：{quote!r}")

    # 5) 意图
    if "intent" in expected and result.get("intent") != expected["intent"]:
        problems.append(f"意图应为 {expected['intent']}，实际 {result.get('intent')}")

    # 1) 中文数量 → JSON 数字
    items = result.get("items") or []
    if "quantity" in expected:
        if not items:
            problems.append("应当有商品明细")
        else:
            quantity = items[0].get("quantity")
            if quantity is None:
                problems.append("数量为空")
            elif not isinstance(quantity, (int, float)) or isinstance(quantity, bool):
                problems.append(f"数量必须是 JSON 数字，实际是 {type(quantity).__name__}: {quantity!r}")
            elif float(quantity) != float(expected["quantity"]):
                problems.append(f"数量应为 {expected['quantity']}，实际 {quantity}")

    # 2) 商品与单位
    if "sku" in expected:
        skus = [item.get("sku") for item in items]
        if expected["sku"] not in skus:
            problems.append(f"商品应为 {expected['sku']}，实际 {skus}")
    if "unit" in expected and items:
        units = [item.get("unit") for item in items]
        if expected["unit"] not in units:
            problems.append(f"单位应为 {expected['unit']}，实际 {units}")

    # 4) 老样子引用历史
    if "target_order" in expected:
        if result.get("target_order") != expected["target_order"]:
            problems.append(f"应引用 {expected['target_order']}，实际 {result.get('target_order')}")

    # 7) 信息不足时给歧义
    if expected.get("ambiguity_expected"):
        if not result.get("ambiguities") and not result.get("missing_fields"):
            problems.append("信息不足时应返回歧义或缺失字段，却什么都没说")

    return {
        "ok": not problems,
        "schema_valid": True,
        "problems": problems,
        "intent": result.get("intent"),
        "raw": json.dumps(dict(result), ensure_ascii=False)[:300],
    }


__all__ = ["SYNTHETIC_CASES", "SYNTHETIC_CATALOG", "run_order_capability_check"]
