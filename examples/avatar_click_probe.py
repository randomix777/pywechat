"""只验证一件事：点群聊气泡左侧头像，能不能读出昵称和备注。

## 为什么要有这个脚本

4.x 的消息 UIA 文本里没有发送人（见 docs/sender-recognition-findings.md），
但气泡左侧头像是真实存在的（Tools.is_my_bubble 就是靠它判自己/对方），
而资料卡里的昵称备注是可读的（Contacts.get_friend_profile 已验证）。

所以「点头像 → 读资料卡 → 回填发送人」逻辑上是通的。这个脚本不做批量，
只点**一条**消息的头像，回答四个问题：

1. 按气泡坐标算出的头像位置，点下去弹出来的是不是资料卡
2. 资料卡里的昵称/备注能不能读到（前几行文本是什么）
3. ESC 能不能干净关掉它
4. 关掉后聊天列表还能不能继续遍历（焦点/位置还在不在）

四个都对，批量版才值得写；任何一个不对，先修这个，不碰主链路。

## 跑法

    .\\.venv\\Scripts\\python.exe -X utf8 examples/avatar_click_probe.py 群名 [dialog|history]

第二个参数选在哪种面板里点：``dialog`` 聊天窗口（默认），``history``
聊天记录面板。批量按发送人回填走的是 history 面板，所以 history 模式
的结果才作数 —— dialog 只验证坐标算法。
"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from pywinauto import mouse  # noqa: E402

from pyweixin.Config import GlobalConfig  # noqa: E402
from pyweixin.Uielements import Lists, Windows  # noqa: E402
from pyweixin.WeChatTools import Navigator, Tools, desktop  # noqa: E402


SYSTEM_CLASSES = {"mmui::ChatItemView", "mmui::ChatSystemInfoItemView"}

#: 头像在气泡内的相对位置：左侧、靠上。is_my_bubble 用最左 65px 判头像存在，
#: 头像本身约 40x40，取 (left+25, top+25) 落在头像中心附近。
AVATAR_OFFSET_X = 25
AVATAR_OFFSET_Y = 25


def _wait(seconds: float, note: str) -> None:
    print(f"  （{seconds:.0f} 秒后{note}——这期间别碰鼠标键盘）", flush=True)
    time.sleep(seconds)


def probe(group: str, source: str = "dialog") -> int:
    print(f"\n{'=' * 70}")
    print(f"群：{group}（只点一条消息的头像，面板：{source}）")
    print(f"{'=' * 70}\n")

    if source == "history":
        print("[1/5] 打开聊天记录面板…", flush=True)
        history_window = Navigator.open_chat_history(friend=group, is_maximize=False,
                                                     close_weixin=False)
        _wait(2.0, "等消息列表渲染")
        Tools.cancel_pin(history_window)
        chat_list = history_window.child_window(**Lists.ChatHistoryList)
        if not chat_list.exists(timeout=3):
            print("✗ 聊天记录列表没出现。")
            history_window.close()
            return 1
        main_window = None
    else:
        print("[1/5] 打开群聊天窗口…", flush=True)
        main_window = Navigator.open_dialog_window(friend=group, is_maximize=False,
                                                   search_pages=5)
        _wait(1.5, "等微信稳住")
        if not Tools.is_group_chat(main_window):
            print("✗ 这不是群聊，头像探针只针对群聊。")
            return 1
        chat_list = main_window.child_window(**Lists.FriendChatList)
        history_window = None

    print("[2/5] 找第一条对方发的消息（左侧有头像的）…", flush=True)
    try:
        chat_list.set_focus()
    except Exception:
        pass
    time.sleep(0.5)
    target = None
    scanned = 0
    for item in chat_list.children(control_type="ListItem"):
        scanned += 1
        if item.class_name() in SYSTEM_CLASSES:
            continue
        try:
            if Tools.is_my_bubble(item):
                continue
        except Exception as exc:
            print(f"      跳过一条（is_my_bubble 报错 {type(exc).__name__}），继续找")
            continue
        target = item
        break
    print(f"      扫了 {scanned} 个可见条目", flush=True)
    if target is None:
        print("✗ 可见范围内没找到对方的消息（全是自己发的或系统消息）。")
        print("  往上滚一滚再跑，或换个说话多的群。")
        return 1
    rect = target.rectangle()
    print(f"      命中：类名={target.class_name()}")
    print(f"      文本={str(target.window_text())[:60]!r}")
    print(f"      矩形=left={rect.left} top={rect.top} right={rect.right} bottom={rect.bottom}")

    avatar = (rect.left + AVATAR_OFFSET_X, rect.top + AVATAR_OFFSET_Y)
    try:
        from pyweixin.utils import avatar_click_point
        measured = avatar_click_point(target)
        print(f"      固定偏移坐标={avatar}，截图实测坐标={measured}")
        avatar = measured
    except Exception as exc:
        print(f"      实测定位失败（{type(exc).__name__}），用固定偏移 {avatar}")
    print(f"\n[3/5] 点击头像位置 {avatar} …", flush=True)
    before = {c.element_info.runtime_id for c in chat_list.children(control_type="ListItem")
              if _runtime_id(c) is not None}
    mouse.click(coords=avatar)
    _wait(2.0, "等资料卡弹出")

    print("[4/5] 看弹出来的是什么…", flush=True)
    profile = None
    popover = Tools.find_Popover()  # 找不到返回 None，不抛异常；内部已等 1 秒
    if popover is not None:
        print(f"      find_Popover 找到了：{popover.window_text()!r}")
        profile = popover
    else:
        print("      find_Popover 没找到，再找 PopUpProfileWindow…")
    if profile is None:
        popup = desktop.window(**Windows.PopUpProfileWindow)
        if popup.exists(timeout=2):
            print("      PopUpProfileWindow 存在。")
            profile = popup
        else:
            print("✗ 点完什么都没弹出来（或弹的不是资料卡）。")
            print("  可能：坐标偏了 / 微信版本点头像不弹卡 / 被别的窗口盖住。")
            print("  把 [3/5] 的矩形和头像坐标发出来，再调偏移。")
            return 1
    try:
        texts = [t.window_text() for t in profile.descendants(control_type="Text")]
    except Exception as exc:
        print(f"✗ 资料卡找到了但读不出文本：{type(exc).__name__}: {exc}")
        return 1
    texts = [t for t in texts if t]
    print(f"      资料卡共 {len(texts)} 行非空文本，前 10 行：")
    for line in texts[:10]:
        print(f"        {line!r}")
    if not texts:
        print("      文本树是空的（自绘卡片）—— 改走右键复制：", flush=True)
        from pyweixin.utils import _copy_avatar_card_text, parse_avatar_profile_texts
        try:
            copied = _copy_avatar_card_text(profile)
            print(f"      复制到 {len(copied)} 个字符，内容：")
            for line in copied.splitlines()[:10]:
                print(f"        {line!r}")
            info = parse_avatar_profile_texts(copied.splitlines())
            print(f"      解析出：{info}")
        except Exception as exc:
            print(f"✗ 复制也失败：{type(exc).__name__}: {exc}")
            print("  把右键菜单里有没有「复制」一项发出来。")
            return 1
    else:
        print("      → 第 1 行一般是昵称，往下找微信号/备注/地区 self-check 一下。")

    print("\n[5/5] ESC 关闭资料卡，检查聊天列表还在不在…", flush=True)
    try:
        import pyautogui
        pyautogui.press("esc")
    except Exception:
        pass
    time.sleep(1.0)
    try:
        after_items = chat_list.children(control_type="ListItem")
        after = {_runtime_id(c) for c in after_items if _runtime_id(c) is not None}
        overlap = len(before & after)
        print(f"      关闭前 {len(before)} 条 / 关闭后 {len(after_items)} 条，重合 {overlap} 条")
        if after_items:
            print("      列表还在，可继续遍历。")
        else:
            print("✗ 列表空了——批量版要先解决位置恢复问题。")
            return 1
    except Exception as exc:
        print(f"✗ 关闭后列表读不出来了：{type(exc).__name__}: {exc}")
        return 1

    print(f"\n{'=' * 70}")
    print("探针通过：点头像 → 资料卡可读 → 能干净关闭 → 列表可继续。")
    print(f"{'=' * 70}")
    if history_window is not None:
        history_window.close()
    return 0


def _runtime_id(item):
    try:
        return item.element_info.runtime_id
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    GlobalConfig.close_weixin = False
    GlobalConfig.is_maximize = False
    GlobalConfig.search_pages = 5
    print("这个脚本会驱动微信窗口（点一次头像）。跑之前：")
    print("  · 微信已经登录并打开")
    print("  · 别碰鼠标键盘")
    _wait(3.0, "开始")
    if not args:
        print('请传群名：examples\\avatar_click_probe.py "夜宵烧烤群" [dialog|history]')
        return 0
    source = args[1] if len(args) > 1 and args[1] in ("dialog", "history") else "dialog"
    return probe(args[0], source)


if __name__ == "__main__":
    raise SystemExit(main())
