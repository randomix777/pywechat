"""用与网关**完全相同**的输入，直接问一次真实模型，打印原始输出。

网关任务结束即删除会话，出错原因事后取不回来。这里用同样的 payload
复现一次，把模型的原话留下来，作为失败案例的证据。

    python examples\replay_one_case.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

SERVER = os.environ.get("PROBE_SERVER", "http://127.0.0.1:4096")
PASSWORD = os.environ.get("PROBE_SERVER_PASSWORD", "probe-local-only")
PROVIDER = os.environ.get("PROBE_PROVIDER", "opencode-go")
#: 多个候选时逐个跑，便于横向比较真实质量。
MODELS = [m for m in (os.environ.get("PROBE_MODELS") or MODEL_DEFAULT).split(",") if m.strip()]

NOW = datetime.now(timezone.utc)

MODEL_DEFAULT = "glm-5.3"

#: 与 examples/real_backend_check.py 相同的商品目录和客户。
CATALOG = [
    {"sku": "APL", "name": "苹果", "unit": "箱", "aliases": ["苹果", "A"]},
    {"sku": "EGG", "name": "鸡蛋", "unit": "箱", "aliases": ["鸡蛋"]},
]

CASES = [
    ("苹果两箱", []),
    ("再加一箱苹果", []),
    ("老样子，明天送", [{"order_id": "o_prev", "status": "confirmed",
                         "items": [{"sku": "APL", "quantity": 2, "unit": "箱"},
                                   {"sku": "EGG", "quantity": 1, "unit": "箱"}]}]),
]


def call(method: str, path: str, body=None, timeout: float = 180.0):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    auth = base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
    request = urllib.request.Request(
        SERVER + path, data=data, method=method,
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    return json.loads(urllib.request.urlopen(request, timeout=timeout).read())


def build(content: str, recent_orders: list) -> dict:
    """和 LocalParseService.build_request 同一个形状。"""
    return {
        "message": {"id": "m1", "sender": "张姐", "content": content, "kind": "text",
                    "sent_at": (NOW - timedelta(hours=1)).isoformat()},
        "customer": {"id": "c1", "name": "张姐", "alias": "张姐", "address": ""},
        "products": CATALOG,
        "history": {"recent_messages": [], "customer_facts": [],
                    "recent_orders": recent_orders},
        "order_state": {"open_orders": []},
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for model in MODELS:
        print("#" * 72)
        print(f"# 模型：{PROVIDER}/{model}")
        print("#" * 72)
        for content, recent in CASES:
            session = call("POST", "/session", {"title": f"replay-{content[:8]}"})
            document = {"task": "order_parse", "input": build(content, recent)}
            started = time.time()
            try:
                response = call(
                    "POST", f"/session/{session['id']}/message",
                    {"agent": "order-parse",
                     "model": {"providerID": PROVIDER, "modelID": model},
                     "parts": [{"type": "text", "text": json.dumps(document, ensure_ascii=False)}]},
                )
            except Exception as exc:
                print(f"客户消息：{content} → 请求失败 {type(exc).__name__}\n")
                continue
            info = response.get("info") or {}
            parts = response.get("parts") or []
            texts = [str(p.get("text", "")) for p in parts if p.get("type") == "text"]
            print(f"客户消息：{content}　（历史订单 {len(recent)} 条）")
            print(f"耗时 {time.time() - started:.1f}s　tokens={info.get('tokens')}")
            error = info.get("error")
            if error:
                print(f"模型错误：{json.dumps(error, ensure_ascii=False)[:200]}")
            for text in texts:
                print(f"原始输出：{text[:700]}")
            if not texts:
                print("原始输出：（无文本）")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
