"""读群聊历史必须把**群成员**一起传下去，否则认不出发送人。

## 出过什么事

商户读到一堆消息，但**不知道每句是谁说的**。

根因在 ``HistoryReader._read_one``：它把 ``groupMembers`` **写死成空列表**。
而读取器自己的文档写着：

    如果 friend 是群聊且传入了该参数则直接解析发送人（**无需遍历二次**）

不传的时候，读取器只能靠「开多选遍历一次、不开再遍历一次」两份结果对比，
从文本前缀里把发送人切出来；那一步对不上就返回 ``None``，最终落到
``'红包或转账(无法获取发送人)'`` 这个**固定占位串** —— 每条消息的发送人
都是它，商户看到的就是一堆不知道谁说的话。

同一份群成员，项目里别的路径**都传了**：

* ``live_listen``（持续监听）→ ``groupMembers=list(self.group_members)``
* ``agent``（采集器）→ ``groupMembers=list(self.config.group_members)``
* ``history_read``（读历史）→ ``groupMembers=[]`` ← 只有这里漏了

## 现在的行为

* 群聊 → 取一次成员昵称并**按会话缓存**，之后复用
* 单聊 → 不取（没有成员名单这回事）
* 取失败 → 退回空列表，**不让整次读取失败**（认不出人 ≠ 读不到消息）
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.conversations import Conversation, ConversationKind  # noqa: E402
from pyweixin.history_read import HistoryReader  # noqa: E402

HISTORY_READ = ROOT / "src/pyweixin/history_read.py"
APPLICATION = ROOT / "src/pyweixin/application.py"


class MembersArePassedTests(unittest.TestCase):
    def setUp(self):
        self.calls: list[dict] = []

        def reader(**kwargs):
            self.calls.append(kwargs)
            return []

        self.reader = reader
        self.group = Conversation(name="鲜果群", kind=ConversationKind.GROUP, member_count=35)
        self.friend = Conversation(name="张姐", kind=ConversationKind.FRIEND)

    def test_a_group_read_gets_the_member_list(self):
        reader = HistoryReader(self.reader, group_members=lambda conv: ["张姐", "李叔"],
                               sender_source="members")
        reader._read_one(self.group, 20)
        self.assertEqual(self.calls[-1]["groupMembers"], ["张姐", "李叔"],
                         "\u7fa4\u804a\u6ca1\u628a\u6210\u5458\u4f20\u4e0b\u53bb \u2014\u2014 \u8ba4\u4e0d\u51fa\u53d1\u9001\u4eba")

    def test_a_direct_chat_does_not_ask_for_members(self):
        asked = []

        def members(conv):
            asked.append(conv.name)
            return ["不该被叫到"]

        reader = HistoryReader(self.reader, group_members=members, sender_source="members")
        reader._read_one(self.friend, 20)
        self.assertEqual(self.calls[-1]["groupMembers"], [])
        self.assertEqual(asked, [], "\u5355\u804a\u6ca1\u6709\u6210\u5458\u540d\u5355\u8fd9\u56de\u4e8b\uff0c\u4e0d\u8be5\u53bb\u53d6")

    def test_members_are_asked_for_once_per_conversation(self):
        asked = []

        def members(conv):
            asked.append(conv.name)
            return ["张姐"]

        reader = HistoryReader(self.reader, group_members=members, sender_source="members")
        reader._read_one(self.group, 20)
        reader._read_one(self.group, 20)
        self.assertEqual(len(asked), 1, "\u6bcf\u6b21\u8bfb\u53d6\u90fd\u53bb\u53d6\u4e00\u904d\u592a\u6162\uff0c\u5e94\u8be5\u7f13\u5b58")

    def test_without_a_resolver_it_still_works(self):
        """没配解析器时退回老行为，不能报错。"""
        reader = HistoryReader(self.reader, sender_source="members")
        reader._read_one(self.group, 20)
        self.assertEqual(self.calls[-1]["groupMembers"], [])


class FailureIsToleratedTests(unittest.TestCase):
    """取成员失败**不能**让整次读取失败。"""

    def test_a_failing_resolver_falls_back_to_empty(self):
        def boom(conv):
            raise RuntimeError("取不到")

        calls = []

        def reader(**kwargs):
            calls.append(kwargs)
            return []

        reader_obj = HistoryReader(reader, group_members=boom, sender_source="members")
        group = Conversation(name="鲜果群", kind=ConversationKind.GROUP)
        reader_obj._read_one(group, 20)
        self.assertEqual(calls[-1]["groupMembers"], [],
                         "\u53d6\u4e0d\u5230\u6210\u5458\u53ea\u662f\u8ba4\u4e0d\u51fa\u4eba\uff0c\u6d88\u606f\u672c\u8eab\u8fd8\u80fd\u8bfb")


class WiringTests(unittest.TestCase):
    """应用必须真的把解析器接上去。"""

    def test_the_reader_is_constructed_with_the_resolver(self):
        source = APPLICATION.read_text(encoding="utf-8")
        self.assertIn("group_members=group_members_fn or self._default_group_members", source,
                      "\u6ca1\u628a\u7fa4\u6210\u5458\u89e3\u6790\u5668\u63a5\u7ed9 HistoryReader")

    def test_the_resolver_is_injectable(self):
        """和 history_reader_fn 一样可注入。

        取群成员要开微信界面（实测一次 2 秒上下，没微信的环境直接失败）。
        测试拿假读取器却走真解析器，时序会被它拖垮 —— 这是真实踩过的。
        """
        source = APPLICATION.read_text(encoding="utf-8")
        self.assertIn("group_members_fn", source,
                      "\u5fc5\u987b\u80fd\u6ce8\u5165\uff0c\u5426\u5219\u6d4b\u8bd5\u88ab\u771f\u5fae\u4fe1\u62d6\u6162")

    def test_the_resolver_uses_the_real_contacts_api(self):
        tree = ast.parse(APPLICATION.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "_default_group_members")
        self.assertIn("get_groupMembers_info", ast.unparse(fn))

    def test_the_resolver_does_not_raise(self):
        """取成员要开微信界面，可能超时。不能让它把读取带崩。"""
        tree = ast.parse(APPLICATION.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "_default_group_members")
        self.assertTrue(any(isinstance(n, ast.Try) for n in ast.walk(fn)))

    def test_every_reader_path_passes_members(self):
        """静态核查：不该再有**调用处**把群成员写死成空。

        用 AST 找函数调用里的 ``groupMembers=[]`` 关键字实参。
        只看字符串会把「局部变量初始化」（``groupMembers=[]`` 单独一行）
        也当成传参误报 —— 那和这是两回事。
        """
        offenders = []
        for path in (ROOT / "src/pyweixin").rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "groupMembers":
                        continue
                    value = keyword.value
                    empty = (isinstance(value, ast.List) and not value.elts) or \
                            (isinstance(value, ast.Constant) and value.value == ())
                    if empty:
                        offenders.append(f"{path.name}:{keyword.value.lineno}")
        self.assertEqual(offenders, [],
                         f"\u8fd9\u4e9b\u8c03\u7528\u5904\u628a\u7fa4\u6210\u5458\u5199\u6b7b\u6210\u7a7a\uff1a{offenders}")


if __name__ == "__main__":
    unittest.main()