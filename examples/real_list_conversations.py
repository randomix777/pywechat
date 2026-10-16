"""列出本机真实微信会话（只读，不打开任何会话、不读聊天内容）。

用于在读历史之前先确定"测哪个会话"。会短暂操作微信界面以取到最近群聊列表。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.conversations import RealConversationSource  # noqa: E402
from wechat_ai_assistant.merchant_app import _force_utf8_stdout  # noqa: E402
from wechat_ai_assistant.wechat_state import RealWeChatProbe  # noqa: E402


def main() -> int:
    _force_utf8_stdout()
    probe = RealWeChatProbe()
    status = probe.probe()
    print(f"微信状态：{status.state}")
    if status.state != "logged_in":
        print(f"详情：{status.to_dict()}")
        print("未登录，不列会话。")
        return 1

    source = RealConversationSource(ui_lock=None)
    entries = source.list_conversations()
    print(f"共 {len(entries)} 个会话：")
    for index, item in enumerate(entries, 1):
        kind = "群" if item.kind == "group" else "好友"
        count = f" 人数={item.member_count}" if item.member_count else ""
        suffix = f"  [{item.disambiguation}]" if item.disambiguation else ""
        print(f"  {index:>3}. [{kind}] {item.name}{count}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())