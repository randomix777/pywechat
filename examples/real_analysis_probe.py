"""真实模型分析一条群订单消息。

    .\.venv\Scripts\python.exe -X utf8 examples/real_analysis_probe.py

**不打印密钥**：从 DSH 凭据文件读进内存，只报告长度。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))


def load_openrouter_key() -> str:
    text = pathlib.Path(os.path.expanduser("~/.dsh/.credentials.yaml")).read_text(
        encoding="utf-8"
    )
    for line in text.splitlines():
        if line.strip().startswith("OPENROUTER_API_KEY:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""



def _last_raw() -> str:
    """取上一次请求的原始输出。

    validate_parse_result 拒绝时只说「格式异常」，看不到模型究竟返回了什么。
    """
    from business_agent.backends.deepseek import DeepSeekBackend

    return str(getattr(DeepSeekBackend, "_probe_last_raw", "") or "")


MESSAGES = [
    "\u5f20\u59d0\uff1a\u660e\u5929\u4e0b\u5348\u8981 3 \u7bb1\u82b1\u5de5\uff0c\u7ea6 300 \u5143\uff0c\u8fd0\u5230\u5c71\u4e1c\u5e02\u573a",
    "\u674e\u53d4\uff1a\u8d60\u7d20\u82b1 2 \u7bb1 \u8305\u88c5\u597d\u9001\u8fc7\u6765\uff0c\u4e0b\u5348\u5230",
    "\u738b\u59d0\uff1a\u8c46\u4e1c 5 \u888b\uff0c\u90a3\u4e2a\u65b0\u7684\uff0c\u5148\u4e0d\u8981\u53d1",
]


def main() -> int:
    from pyweixin.openrouter_adapter import DEFAULT_BASE_URL, OpenRouterAdapter

    key = load_openrouter_key()
    if not key:
        print("找不到 OpenRouter 凭据，无法做真实验证。")
        return 1
    print(f"OpenRouter 凭据已加载（长度 {len(key)}，不打印内容）")
    print(f"上游 {DEFAULT_BASE_URL}")

    model = os.environ.get("PYWEIXIN_OPENROUTER_MODEL", "openai/gpt-4o-mini")
    print(f"模型 {model}\n")
    print("=" * 74)

    failures = 0
    for index, text in enumerate(MESSAGES, 1):
        adapter = OpenRouterAdapter(api_key=key, model=model, timeout=90)
        payload = {
            "message": {
                "id": f"m{index}", "sender": "\u5ba2\u6237",
                "content": text, "kind": "text",
                "sent_at": "2026-01-01T10:00:00+08:00",
            },
            "customer": {"id": "c1", "name": "\u5ba2\u6237", "alias": "\u5ba2\u6237", "address": ""},
            "products": [
                {"sku": "SKU-HUA", "name": "\u82b1\u5de5", "unit": "\u7bb1", "aliases": "\u82b1\u5de5,\u82b1"},
                {"sku": "SKU-DOU", "name": "\u8c46\u4e1c", "unit": "\u888b", "aliases": "\u8c46\u4e1c,\u8c46"},
            ],
        }
        print(f"\n【{index}】{text}")
        started = time.time()
        try:
            outcome = adapter.run_parse("s", payload, f"job-real-{index}", time.time() + 120)
        except Exception as exc:
            failures += 1
            print(f"  失败：{type(exc).__name__}: {str(exc)[:200]}")
            print(f"  耗时 {time.time() - started:.1f}s")
            raw = _last_raw()
            if raw:
                print("  模型原始输出：")
                print("  " + raw[:1200].replace("\n", "\n  "))
            print()
            continue
        elapsed = time.time() - started
        print(f"  耗时 {elapsed:.1f}s | 输入 {outcome.input_tokens} tok | 输出 {outcome.output_tokens} tok")
        print(f"  模型标识：{outcome.model_requested or '（未返回）'}")
        print(f"  结果：{json.dumps(outcome.result, ensure_ascii=False)}")

    print("\n" + "=" * 74)
    if failures:
        print(f"{failures}/{len(MESSAGES)} 条失败 —— **不算通过**")
        return 1
    print(f"{len(MESSAGES)}/{len(MESSAGES)} 条由真实模型解析成功")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())