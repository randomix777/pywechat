"""群成员名单滚一遍进内存（只滚动，不点内容）。

历史窗口在 4.1.15 上还有 UIA（翻消息那步验证过），成员 tab 理论上摸得到。
把成员资料行翻进内存，发送人备注才有票可投。走不通就直说。
"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.Config import GlobalConfig  # noqa: E402
from pyweixin.Uielements import Lists, TabItems  # noqa: E402
from pyweixin.WeChatTools import Navigator, Tools  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print('用法：members_pagein.py "群名"')
        return 0
    group = args[0]
    GlobalConfig.close_weixin = False
    GlobalConfig.is_maximize = False
    GlobalConfig.search_pages = 5

    print(f"打开「{group}」聊天记录面板…", flush=True)
    try:
        window = Navigator.open_chat_history(friend=group, is_maximize=False,
                                             close_weixin=False)
    except Exception as exc:
        print(f"打不开：{type(exc).__name__}: {exc}")
        return 1
    history_list = window.child_window(**Lists.ChatHistoryList)
    if not history_list.exists(timeout=5):
        print("列表没出来，退出。")
        window.close()
        return 1
    Tools.cancel_pin(window)
    try:
        import win32gui
        import win32con
        win32gui.SendMessage(window.handle, win32con.WM_SYSCOMMAND, win32con.SC_MAXIMIZE, 0)
    except Exception:
        pass
    tab = window.child_window(**TabItems.GroupMembersTabItem)
    try:
        exists = tab.exists(timeout=3)
    except Exception:
        exists = False
    if not exists:
        print("成员 tab 摸不到：这个版本连历史窗成员栏都没 UIA。收工。")
        window.close()
        return 1
    print("成员 tab 在，点开滚一遍…", flush=True)
    try:
        tab.click_input()
        time.sleep(1.0)
        popover = Tools.find_Popover()
        if popover is None:
            print("成员弹层没出来。收工。")
            window.close()
            return 1
        member_list = popover.child_window(control_type="List")
        member_list.type_keys("{HOME}")
        time.sleep(0.5)
        idle = 0
        last = None
        while idle < 3:
            try:
                items = member_list.children(control_type="ListItem")
                tail = items[-1].window_text() if items else None
            except Exception:
                break
            if tail == last:
                idle += 1
            else:
                idle = 0
            last = tail
            try:
                import pyautogui
                pyautogui.press("down", _pause=False)
            except Exception:
                break
            time.sleep(0.05)
        print("成员滚完，关窗口。", flush=True)
        try:
            popover.close()
        except Exception:
            pass
    except Exception as exc:
        print(f"滚成员时出错：{type(exc).__name__}: {exc}")
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
