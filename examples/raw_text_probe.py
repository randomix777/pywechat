"""只做一件事：把**原始文本**打出来。

## 为什么要有这个脚本

发送人解析有两条路，我上一轮证明了第 2 条路在逻辑上被参数掐断了，
也修了。但**证明不了**的是：微信在多选模式下到底渲不渲染发送人名。

上游作者的注释说会，整套「两次遍历对比」的设计也依赖于此 —— 但那是
**他的话，不是我验证过的事实**。

所以这个脚本不跑整链、不入库、不解析。它只做：

    打开会话 → 开多选 → 遍历 → **把每一行的原始文本原样打印出来**

看到那几行文本，才能判断第 2 条路到底可用不可用。

## 跑法

    .\\.venv\\Scripts\\python.exe -X utf8 examples/raw_text_probe.py 群名 [条数]

不带群名时会列出可选会话（只列，不打开）。条数默认 5 —— 多了刷屏，
而我们要看的只是**前几行长什么样**。

## 读它的时候看什么

* 多选那一遍，文本里**有没有发送人名字**（「张姐 12:30 ...」）
* 非多选那一遍，文本里**有没有**（应该没有）
* 两遍**是不是真的不同**

三个都对，第 2 条路才成立。
"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from wechat_ai_assistant.Config import GlobalConfig  # noqa: E402
from wechat_ai_assistant.Uielements import Lists  # noqa: E402
from wechat_ai_assistant.WeChatTools import Navigator, Tools  # noqa: E402
from wechat_ai_assistant.utils import traverse_chat_history  # noqa: E402

import pyautogui  # noqa: E402


def _wait(seconds: float, note: str) -> None:
    print(f"  （{seconds:.0f} 秒后{note}——这期间别碰鼠标键盘）", flush=True)
    time.sleep(seconds)


def list_conversations() -> None:
    """不带参数时只说明用法，**一个窗口都不打开**。

    **不猜方法名。** 这个脚本第一版写了个 ``Contacts.get_friends_list()``，
    那个方法在 ``Contacts`` 上并不存在 —— 猜的 API 要到运行时才炸，
    而那时人已经等着看输出了。

    列会话本身就得驱动微信界面，那会把「只看不碰」的承诺打破。所以不列。
    """
    from wechat_ai_assistant.WeChatTools import Contacts

    available = [name for name in dir(Contacts) if "friend" in name.lower()]
    print("\nContacts 上确实存在的「friend」相关方法：")
    for name in available or ["（没有）"]:
        print(f"  {name}")
    print(
        "\n这个探针**不列会话** —— 列会话本身就要驱动微信界面。\n"
        "请直接把群名作为参数传进来：\n"
        '    examples\\raw_text_probe.py "夜宵烧烤群" 5'
    )


def probe(friend: str, number: int) -> int:
    """打开会话 → 遍历两遍 → **打印原始文本**。不解析、不入库。"""
    GlobalConfig.close_wechat = False
    GlobalConfig.is_maximize = False
    GlobalConfig.search_pages = 5

    print(f"\n{'=' * 70}")
    print(f"会话：{friend}")
    print(f"{'=' * 70}\n")

    print("[1/4] 打开会话窗口…", flush=True)
    main_window = Navigator.open_dialog_window(friend=friend, is_maximize=False,
                                                search_pages=5)
    _wait(1.5, "等微信稳住")
    is_group = Tools.is_group_chat(main_window)
    print(f"      是群聊吗：{is_group}")

    print("[2/4] 打开聊天记录面板…", flush=True)
    history_window = Navigator.open_chat_history(friend=friend, is_maximize=False,
                                                 close_weixin=False)
    _wait(2.0, "等消息列表渲染")

    history_list = history_window.child_window(**Lists.ChatHistoryList)
    if not history_list.exists(timeout=3):
        print("\n✗ 聊天记录列表没出现。这本身就是读取失败，不是「没有消息」。")
        print("  常见原因：微信没登录、窗口没聚焦、这个会话名搜不到。")
        history_window.close()
        return 1

    print("[3/4] 第一遍：多选模式遍历…", flush=True)
    Tools.cancel_pin(history_window)
    try:
        selected = Tools.select_chatHistoryList(history_window)
    except Exception as exc:
        print(f"      进多选失败：{type(exc).__name__}: {exc}")
        selected = None
    if selected is None:
        print("      这个会话一条都选不中（纯系统消息？）——第 2 条路用不上。")
        with_name: list = []
    else:
        _wait(1.0, "等多选态渲染")
        with_name = traverse_chat_history(history_window, select=True, number=number)

    print("[4/4] 第二遍：退出多选，普通模式遍历…", flush=True)
    try:
        pyautogui.press("esc")
    except Exception:
        pass
    _wait(1.0, "等退出多选")
    without_name = traverse_chat_history(history_window, select=False, number=number)

    print()
    _dump("多选那一遍 details_with_name", with_name)
    _dump("普通那一遍 details_without_name", without_name)

    texts_with = [row[0] for row in with_name]
    texts_without = [row[0] for row in without_name]
    print("=" * 70)
    print(f"多选 {len(texts_with)} 条 / 普通 {len(texts_without)} 条")
    if texts_with and texts_without:
        same = texts_with == texts_without
        print(f"两遍文本是否完全相同：{'是 ← 第 2 条路废了' if same else '否 ← 第 2 条路可用'}")
    elif texts_with:
        print("普通那遍是空的 —— 对齐无从下手，第 2 条路同样用不上。")
    else:
        print("两遍都是空的 —— 这次没读到东西，别据此判断第 2 条路。")
    print("=" * 70)

    history_window.close()
    return 0


def _dump(label: str, rows) -> None:
    print(f"\n--- {label}（{len(rows)} 行）---")
    if not rows:
        print("  （空）")
        return
    for i, row in enumerate(rows[:5], 1):
        text = str(row[0])
        print(f"  [{i}] 类名={row[1]}")
        # 原样打印，不截断 —— 我们要看的正是有没有发送人名
        print(f"      文本={text!r}")
    if len(rows) > 5:
        print(f"  …还有 {len(rows) - 5} 行")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    print("这个脚本会驱动微信窗口。跑之前：")
    print("  · 微信已经登录并打开")
    print("  · 别碰鼠标键盘")
    _wait(3.0, "开始")
    if not args:
        list_conversations()
        return 0
    return probe(args[0], int(args[1]) if len(args) > 1 else 5)


if __name__ == "__main__":
    raise SystemExit(main())