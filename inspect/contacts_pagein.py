"""通讯录翻页进内存（只滚动，不点内容）。

主窗口在 4.1.15 上是黑盒，但子窗口可能还有 UIA —— 能走就走，
走不通就直说，不硬撑。翻页把联系人资料行翻进内存，
群名/备注解析才有票可投。
"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant.Config import GlobalConfig  # noqa: E402
from wechat_ai_assistant.WeChatTools import Navigator  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    pages = 60
    if args:
        try:
            pages = max(1, int(args[0]))
        except ValueError:
            pass
    GlobalConfig.close_weixin = False
    GlobalConfig.is_maximize = False
    print("开通讯录（只滚动，不点内容）…", flush=True)
    try:
        contact_list, main_window = Navigator.open_contacts(is_maximize=False)
    except Exception as exc:
        print(f"通讯录打不开：{type(exc).__name__}: {exc}")
        print("结论：这个版本通讯录界面没有 UIA，走备注改名。")
        return 1
    try:
        if not contact_list.exists(timeout=5):
            print("通讯录列表没出来。结论同上，走备注改名。")
            return 1
    except Exception as exc:
        print(f"列表检测失败：{type(exc).__name__}")
        return 1
    print(f"列表在，往下翻 {pages} 页…", flush=True)
    try:
        contact_list.type_keys("{HOME}")
    except Exception:
        pass
    time.sleep(1.0)
    for index in range(1, pages + 1):
        try:
            contact_list.type_keys("{PGDN}")
        except Exception as exc:
            print(f"第 {index} 页翻不动了（{type(exc).__name__}），收工。")
            break
        time.sleep(0.4)
        if index % 10 == 0:
            print(f"  …已翻 {index}/{pages} 页", flush=True)
    print("翻完。重跑群列表看名字多没多。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
