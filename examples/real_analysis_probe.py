"""真实模型分析一条群订单消息 —— 用产品真实的载荷形状。

    $env:PYTHONIOENCODING="utf-8"
    .\.venv\Scripts\python.exe -X utf8 examples/real_analysis_probe.py

## 为什么强调「真实的载荷形状」

第一次跑这个脚本时传的是 ``{"message": {...}}``，而 :func:`_build_prompt`
读的是 ``payload["messages"]``。于是模型收到一个**空**任务，回报
``intent=unclear, items=[]``。

而这个输出**能通过契约校验** —— 校验只看结构，不看内容。脚本于是显示
「3/3 条由真实模型解析成功」。

**那是假通过。** 一个什么都没解析出来的结果，长得和正常结果一模一样。

所以这里必须用 ``orders.py:1132`` 里的真实形状，而且除了「结构合法」之外
还要断言**真的解析出了东西** —— 否则换一套更宽松的校验就会掩盖问题。

**不打印密钥。**
"""
from __future__ import annotations

import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))


def load_key() -> str:
    text = pathlib.Path(os.path.expanduser("~/.dsh/.credentials.yaml")).read_text(
        encoding="utf-8"
    )
    for line in text.splitlines():
        if line.strip().startswith("OPENROUTER_API_KEY:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


#: 模拟一个只有两三个商品的商户目录。客户说的多半不在里面 ——
#: 真实场景就是这样，群里客户不会照着目录报。
CATALOG = [
    {"sku": "P-APPLE", "name": "红富士苹果", "unit": "箱"},
    {"sku": "P-SOY", "name": "酱油", "unit": "瓶"},
]

CASES = [
    ("张姐：明天下午要 3 箱红富士苹果，约 300 元，运到山东市场", "new_order"),
    ("李叔：李记酱油 2 瓶，下午送过来", "new_order"),
    ("王姐：那个上次买的，先不要发", None),
]


def main() -> int:
    from wechat_ai_assistant.openrouter_adapter import DEFAULT_BASE_URL, OpenRouterAdapter

    key = load_key()
    if not key:
        print("找不到 OpenRouter 凭据，无法做真实验证。")
        return 1
    model = os.environ.get("WECHAT_AI_OPENROUTER_MODEL", "openai/gpt-4o-mini")
    print(f"OpenRouter 凭据已加载（长度 {len(key)}，不打印内容）")
    print(f"上游 {DEFAULT_BASE_URL} | 模型 {model}")
    print(f"商品目录：{[p['name'] for p in CATALOG]}")
    print("=" * 74)

    structural_ok = 0
    meaningful_ok = 0
    for index, (text, expect) in enumerate(CASES, 1):
        # 产品真实的载荷形状：见 orders.py:1132
        payload = {
            "messages": [{"role": "user", "content": text}],
            "context": {
                "message": {
                    "id": f"m{index}", "sender": "客户", "content": text,
                    "sent_at": "2026-01-01T10:00:00+08:00", "group_id": "g1",
                },
                "recent_customer_messages": [],
                "open_orders": [],
                "customer": {"id": "c1", "name": "客户"},
                "products": CATALOG,
                "last_confirmed_order": None,
                "rules": {
                    "prices_are_server_owned": True,
                    "history_cutoff": "2026-01-01T10:00:00+08:00",
                    "untrusted_message_data": True,
                },
            },
            "merchant_id": "m1",
        }
        adapter = OpenRouterAdapter(api_key=key, model=model, timeout=90)
        print(f"\n【{index}】{text}")
        started = time.time()
        try:
            outcome = adapter.run_parse("s", payload, f"job-{index}", time.time() + 120)
        except Exception as exc:
            print(f"  结构校验失败：{type(exc).__name__}: {str(exc)[:160]}")
            print(f"  耗时 {time.time() - started:.1f}s\n")
            continue
        structural_ok += 1
        result = outcome.result
        items = result.get("items") or []
        print(f"  耗时 {time.time() - started:.1f}s | {outcome.input_tokens}->{outcome.output_tokens} tok")
        print(f"  intent={result.get('intent')}  items={len(items)}")
        for item in items:
            print(f"    - said={item.get('said')!r} brand={item.get('brand')!r} "
                  f"category={item.get('category')!r} qty={item.get('quantity')}{item.get('unit') or ''} "
                  f"sku={item.get('sku')!r}")
        if expect is None:
            meaningful_ok += 1
            print("    （非订单输入，不校验 intent）")
        elif result.get("intent") == expect and items:
            meaningful_ok += 1
            print(f"    符合预期 intent={expect}")
        else:
            print(f"    不符合预期：期望 intent={expect} 且至少一个商品")

    total = len(CASES)
    print("\n" + "=" * 74)
    print(f"结构合法 {structural_ok}/{total}")
    print(f"解析出实际内容 {meaningful_ok}/{total}")
    if structural_ok == total and meaningful_ok == total:
        print("真实调用通过")
        return 0
    print("**不算通过** —— 结构合法不代表解析出了东西")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())