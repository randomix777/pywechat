"""把聊天记录翻进内存（只滚动，不点、不读内容）。

背景：SQLite 只缓存碰过的页，消息表不在内存里就 carve 不到。
这个脚本只按 PGDN，让微信自己把历史消息页读进内存，读完就关窗口。
内容解析是另一步（localdb_schema.py / carve），不在这里做。

跑法：
    .\\.venv\\Scripts\\python.exe -X utf8 inspect\\localdb_pagein.py "群名" [页数]

页数默认 80（约两三千条）。跑的时候别碰鼠标键盘，微信窗口会被占用。
"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from pyweixin.Config import GlobalConfig  # noqa: E402
from pyweixin.Uielements import Lists  # noqa: E402
from pyweixin.WeChatTools import Navigator, Tools  # noqa: E402


def page_in(group: str, pages: int) -> int:
    GlobalConfig.close_weixin = False
    GlobalConfig.is_maximize = False
    GlobalConfig.search_pages = 5

    print(f"打开「{group}」聊天记录面板…", flush=True)
    history_window = Navigator.open_chat_history(friend=group, is_maximize=False,
                                                 close_weixin=False)
    history_list = history_window.child_window(**Lists.ChatHistoryList)
    if not history_list.exists(timeout=5):
        print("列表没出来，退出。")
        history_window.close()
        return 1
    Tools.cancel_pin(history_window)
    try:
        history_list.set_focus()
        history_list.type_keys("{HOME}")
    except Exception:
        pass
    time.sleep(1.0)
    print(f"从头开始往下翻 {pages} 页（只滚动，不点内容）…", flush=True)
    for index in range(1, pages + 1):
        try:
            history_list.type_keys("{PGDN}")
        except Exception as exc:
            print(f"第 {index} 页翻不动了（{type(exc).__name__}），收工。")
            break
        time.sleep(0.4)
        if index % 10 == 0:
            print(f"  …已翻 {index}/{pages} 页", flush=True)
    history_window.close()
    print("翻完，窗口已关。内存里应该有消息表了。", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print('用法：localdb_pagein.py "群名" [页数，默认80]')
        return 0
    print("这个脚本会占用微信窗口几分钟。跑之前：")
    print("  · 微信已经登录并打开")
    print("  · 别碰鼠标键盘")
    time.sleep(3.0)
    pages = 80
    if len(args) > 1:
        try:
            pages = max(1, int(args[1]))
        except ValueError:
            pass
    return page_in(args[0], pages)


if __name__ == "__main__":
    raise SystemExit(main())
