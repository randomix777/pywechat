"""核实 OpenCode 实际提供的模型目录接口，并检查是否泄露凭据。

    python examples\inspect_provider_api.py

输出：接口路径、字段结构、以及**哪些字段绝不能返回给客户端**。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

SERVER = os.environ.get("PROBE_SERVER", "http://127.0.0.1:4096")
PASSWORD = os.environ.get("PROBE_SERVER_PASSWORD", "probe-local-only")

#: 出现这些键就说明是凭据，绝不能下发给客户端。
SECRET_MARKERS = ("key", "token", "secret", "password", "credential", "authorization")


def get(path: str, timeout: float = 60.0):
    auth = base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
    request = urllib.request.Request(
        SERVER + path, headers={"Authorization": "Basic " + auth, "Accept": "application/json"}
    )
    return json.loads(urllib.request.urlopen(request, timeout=timeout).read())


def find_secret_keys(node, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if any(marker in str(key).lower() for marker in SECRET_MARKERS) and value:
                hits.append(here)
            hits.extend(find_secret_keys(value, here))
    elif isinstance(node, list):
        for index, item in enumerate(node[:3]):
            hits.extend(find_secret_keys(item, f"{path}[{index}]"))
    return hits


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    spec = get("/doc")
    paths = spec.get("paths", {})
    print(f"OpenAPI 路径数：{len(paths)}")
    print("与模型/供应商相关的路径：")
    for path in sorted(paths):
        if any(word in path.lower() for word in ("provider", "model", "config", "auth")):
            print(f"   {path}  方法：{sorted(k.upper() for k in paths[path] if k in {'get','post','put','patch','delete'})}")
    print()

    payload = get("/provider")
    print(f"/provider 顶层键：{sorted(payload.keys())}")
    groups = payload.get("all") or []
    print(f"all：{len(groups)} 个供应商；connected：{len(payload.get('connected') or [])} 个已连接")
    print()

    connected = set(payload.get("connected") or [])
    for entry in groups:
        pid = str(entry.get("id", ""))
        if pid not in connected:
            continue
        models = entry.get("models") or {}
        print(f"已连接供应商 {pid}（{entry.get('name')}）模型 {len(models)}")
        print(f"   供应商字段：{sorted(entry.keys())}")
        options = entry.get("options") or {}
        print(f"   options 字段：{sorted(options.keys())}  ← 绝不能返回客户端")
        for model_id, meta in list(models.items())[:2]:
            print(f"   模型 {model_id}")
            print(f"      字段：{sorted(meta.keys())}")
            print(f"      limit：{(meta.get('limit') or {})}")
        break
    print()

    secrets = find_secret_keys({"all": groups[:1]})
    print("凭据类字段（必须过滤）：", secrets[:10] or "（该供应商未回显）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
