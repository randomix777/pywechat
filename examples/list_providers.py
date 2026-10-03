"""列出 OpenCode 实际注册了哪些 provider，以及 go 订阅的模型名。

    python examples\list_providers.py

只输出 provider / 模型名，**不打印任何凭据**。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

SERVER = os.environ.get("PROBE_SERVER", "http://127.0.0.1:4096")
PASSWORD = os.environ.get("PROBE_SERVER_PASSWORD", "probe-local-only")


def get(path: str, timeout: float = 60.0):
    auth = base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
    request = urllib.request.Request(
        SERVER + path, headers={"Authorization": "Basic " + auth, "Accept": "application/json"}
    )
    return json.loads(urllib.request.urlopen(request, timeout=timeout).read())


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    payload = get("/provider")
    groups = payload.get("all") or []
    connected = set(payload.get("connected") or [])
    print(f"已注册 provider：{len(groups)} 个；已连接：{sorted(connected)}")
    print()
    wanted = os.environ.get("PROBE_PROVIDER", "")
    for entry in groups:
        pid = str(entry.get("id", ""))
        if wanted and wanted not in pid:
            continue
        if not wanted and "opencode" not in pid.lower():
            continue
        models = list((entry.get("models") or {}).keys())
        print(f"{pid:<26} {entry.get('name','')!r:<28} 模型 {len(models)}"
              f"{'  [已连接]' if pid in connected else ''}")
        for model in models:
            print(f"      · {model}")
    print()
    missing = sorted(pid for pid in connected if pid not in {str(e.get('id')) for e in groups})
    if missing:
        print("已连接但未出现在 all 列表中的 provider：", missing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
