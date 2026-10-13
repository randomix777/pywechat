"""用本地库读指定群的消息（只读内存，不点不截图）。

流程：备注名解群 wxid → 内存锚定捡 record → 发送人解备注名。
解不出就明说，不编造。打印只给时间/发送人/内容前 30 字 ——
这是商户自己的经营数据，验对不对用的。

跑法：
    .\\.venv\\Scripts\\python.exe -X utf8 inspect\\localdb_read.py "群名" [条数]

目标群最好先翻一翻（inspect/localdb_pagein.py），翻得越多捡得越多。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin import localdb  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print('用法：localdb_read.py "群名" [条数，默认50]')
        return 0
    name = args[0]
    limit = int(args[1]) if len(args) > 1 else 50

    print(f"解群 wxid：{name}…", flush=True)
    wxid = localdb.resolve_group_wxid(name)
    if not wxid:
        print("wxid 解不出：内存里没有这个群的资料页，先去群里翻翻再试。")
        return 1
    print(f"群 wxid：{wxid}，捡记录中…", flush=True)
    messages = localdb.read_conversation(wxid, limit=limit)
    print(f"捡到 {len(messages)} 条：", flush=True)
    for message in messages[:20]:
        sender = localdb.resolve_contact_name(message.sender_wxid) if message.sender_wxid else ""
        sent = ""
        if message.timestamp:
            try:
                from datetime import datetime
                sent = datetime.fromtimestamp(message.timestamp).astimezone().strftime("%m-%d %H:%M")
            except Exception:
                sent = ""
        content = (message.content or "")[:30].replace("\n", " ")
        print(f"  [{sent}] {sender or message.sender_wxid or '?'}：{content}", flush=True)
    if len(messages) > 20:
        print(f"  …还有 {len(messages) - 20} 条没打出来", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
