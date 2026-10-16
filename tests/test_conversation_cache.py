"""会话列表缓存的回归测试：重启后列表还在。

重启前把可见名单落盘，启动时恢复。坏行跳过、空结果不覆盖，
都是纯内存逻辑，不碰微信不联网。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.conversations import (  # noqa: E402
    Conversation,
    ConversationKind,
    ConversationRegistry,
)


def _group(name, members=10):
    return Conversation(name=name, kind=ConversationKind.GROUP, member_count=members)


def _friend(name):
    return Conversation(name=name, kind=ConversationKind.FRIEND)


class ConversationCacheTests(unittest.TestCase):
    def test_round_trip_keeps_identities(self):
        registry = ConversationRegistry([_group("鲜果群", 424), _friend("王老板")])
        rows = registry.available_to_dicts()
        restored = ConversationRegistry()
        self.assertEqual(restored.restore_available(rows), 2)
        self.assertEqual([c.identity for c in restored.available],
                         [c.identity for c in registry.available])

    def test_bad_rows_are_skipped_not_fatal(self):
        restored = ConversationRegistry()
        count = restored.restore_available([
            {"name": "好群", "kind": "group", "member_count": 5},
            {"name": "", "kind": "group"},
            {"name": "坏类型", "kind": "not-a-kind"},
            None,
            "不是字典",
        ])
        self.assertEqual(count, 1)
        self.assertEqual(restored.available[0].name, "好群")

    def test_empty_or_missing_restores_nothing(self):
        restored = ConversationRegistry([_group("旧群")])
        self.assertEqual(restored.restore_available([]), 0)
        self.assertEqual(restored.restore_available(None), 0)


if __name__ == "__main__":
    unittest.main()
