"""真实调用一次，把模型原始输出抓下来。

绕过所有校验层，只看「上游到底返回了什么」。这是定位
``InvalidParseOutput`` 的唯一办法 —— 校验层只会说「格式异常」，
看不到模型究竟写了什么。

    .\.venv\Scripts\python.exe -X utf8 examples/real_raw_output.py

**不打印密钥。**
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


def load_key() -> str:
    text = pathlib.Path(os.path.expanduser("~/.dsh/.credentials.yaml")).read_text(
        encoding="utf-8"
    )
    for line in text.splitlines():
        if line.strip().startswith("OPENROUTER_API_KEY:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def main() -> int:
    from business_agent.backends.deepseek import DeepSeekBackend

    from wechat_ai_assistant.deepseek_adapter import _build_agent_request

    key = load_key()
    if not key:
        print("找不到凭据")
        return 1

    # 包一层 transport，把原始响应记下来 —— 不改产品代码。
    captured: dict = {}

    def spy(body, timeout):
        result = backend._urllib_transport(body, timeout)
        captured["body"] = body
        captured["result"] = result
        return result

    backend = DeepSeekBackend(
        api_key=key,
        model=os.environ.get("WECHAT_AI_OPENROUTER_MODEL", "openai/gpt-4o-mini"),
        base_url="https://openrouter.ai/api/v1",
        timeout=90,
        transport=spy,
    )

    payload = {
        "messages": [{
            "role": "user",
            "content": "张姐：明天下午要 3 箱花工，约 300 元，运到山东市场",
        }],
        "merchant_id": "m1",
    }
    from wechat_ai_assistant.deepseek_adapter import _build_prompt

    prompt = _build_prompt(payload, "job-raw-1")
    request = _build_agent_request(payload, "job-raw-1", time.time() + 120)

    print("=== 送出去的请求体 ===")
    print(json.dumps(captured.get("body", {}), ensure_ascii=False, indent=2)[:1200])
    print()

    try:
        result = backend.run(prompt, request)
    except Exception as exc:
        print(f"调用失败：{type(exc).__name__}: {str(exc)[:300]}")
        return 1

    print("=== 上游返回的原始文本 ===")
    print(str(result.text)[:2000])
    print()
    print("=== 结构化字段 ===")
    print("raw =", json.dumps(result.raw, ensure_ascii=False)[:600] if result.raw else "(空)")
    print("input_tokens =", result.input_tokens, "output_tokens =", result.output_tokens)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())