"""从 OpenCode 取回某次运行的**原始模型输出**，用于判定失败原因。

    python examples\fetch_raw_model_output.py <job_id> [job_id ...]

只打印模型输出，不打印任何凭据。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

SERVER = os.environ.get("PROBE_SERVER", "http://127.0.0.1:4096")
PASSWORD = os.environ.get("PROBE_SERVER_PASSWORD", "probe-local-only")


def call(path: str, timeout: float = 60.0):
    auth = base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
    request = urllib.request.Request(
        SERVER + path, headers={"Authorization": "Basic " + auth, "Accept": "application/json"}
    )
    return json.loads(urllib.request.urlopen(request, timeout=timeout).read())


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    wanted = set(argv[1:])
    sessions = call("/session")
    print(f"OpenCode 现有会话 {len(sessions)} 个\n")
    for session in sessions:
        title = str(session.get("title") or "")
        if wanted and title not in wanted:
            continue
        print(f"=== {session.get('id','')[:20]}  title={title}")
        try:
            messages = call(f"/session/{session['id']}/message")
        except Exception as exc:
            print(f"    读取失败 {type(exc).__name__}")
            continue
        for message in messages:
            info = message.get("info") or {}
            if info.get("role") != "assistant":
                continue
            error = info.get("error")
            parts = message.get("parts") or []
            texts = [str(p.get("text", "")) for p in parts if p.get("type") == "text"]
            print(f"    tokens={info.get('tokens')} error={json.dumps(error, ensure_ascii=False)[:160] if error else None}")
            for text in texts:
                print(f"    输出：{text[:500]!r}")
            if not texts:
                print("    输出：（无文本 parts）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
