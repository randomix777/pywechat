"""整理角色的回归测试：注册表、规则引擎、向导分流，全走合成数据。

覆盖：
* 三个内置角色都在，未知 id 回通用整理；
* 活动群消息按报名/时间地点/物资/通知归类，拿不准进闲聊；
* 非文字消息进非文字桶并写明要人工看；
* 时间地点抽取只抠有的，没有不编造；
* 向导按角色分流：订单走订单链路（要过额度门），其它走本机引擎
  （不过额度门、不扣费、不建单、不占"已整理"状态）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from pyweixin import roles  # noqa: E402
from pyweixin.roles import (  # noqa: E402
    ROLE_ACTIVITY,
    ROLE_GENERIC,
    ROLE_ORDER,
    extract_facts,
    get_role,
    list_roles,
    organize,
)


def _msg(mid, content, sender="张姐", kind="text", group="义工群",
         sent_at="2026-10-13 10:00"):
    return {"id": mid, "group": group, "sender": sender, "content": content,
            "sent_at": sent_at, "kind": kind}


class RegistryTests(unittest.TestCase):
    def test_three_builtins_in_order(self):
        self.assertEqual([r.id for r in list_roles()],
                         [ROLE_ORDER, ROLE_ACTIVITY, ROLE_GENERIC])
        for role in list_roles():
            self.assertTrue(role.name.strip())
            self.assertTrue(role.description.strip())

    def test_unknown_role_falls_back_to_generic(self):
        self.assertEqual(get_role("不存在").id, ROLE_GENERIC)
        self.assertEqual(get_role("").id, ROLE_GENERIC)

    def test_order_role_has_no_rule_categories(self):
        # 订单走订单链路，规则引擎不该碰它（误传进来也按通用跑，不崩）。
        report = organize(ROLE_ORDER, [_msg("1", "苹果两箱")])
        self.assertEqual(report.role_id, ROLE_GENERIC)


class ActivityTests(unittest.TestCase):
    def test_signup_schedule_supply_notice(self):
        report = organize(ROLE_ACTIVITY, [
            _msg("1", "接龙：周六义工＋1"),
            _msg("2", "周六上午9点在公园门口集合，别迟到"),
            _msg("3", "谁有大喇叭？需要借一个"),
            _msg("4", "通知：本周活动改到周日，请大家周知"),
            _msg("5", "哈哈哈今天天气不错"),
        ])
        buckets = report.buckets
        self.assertEqual([i.message_id for i in buckets["报名接龙"]], ["1"])
        self.assertEqual([i.message_id for i in buckets["时间地点"]], ["2"])
        self.assertEqual([i.message_id for i in buckets["物资需求"]], ["3"])
        self.assertEqual([i.message_id for i in buckets["通知公告"]], ["4"])
        self.assertEqual([i.message_id for i in buckets["闲聊"]], ["5"])
        self.assertEqual(report.total, 5)
        self.assertIn("报名接龙 1 条", report.headline())

    def test_facts_come_along(self):
        report = organize(ROLE_ACTIVITY, [
            _msg("1", "周六上午9点在公园门口集合"),
        ])
        item = report.buckets["时间地点"][0]
        self.assertIn("9", item.facts.get("time", ""))
        self.assertTrue(report.to_dict()["buckets"])

    def test_non_text_goes_to_visible_bucket(self):
        report = organize(ROLE_ACTIVITY, [_msg("1", "", kind="image")])
        labels = list(report.buckets)
        self.assertEqual(len(labels), 1)
        self.assertIn("人工看", labels[0])
        self.assertEqual(report.buckets[labels[0]][0].content, "")


class GenericTests(unittest.TestCase):
    def test_question_task_notice(self):
        report = organize(ROLE_GENERIC, [
            _msg("1", "请问明天几点开始？"),
            _msg("2", "麻烦大家记得带身份证"),
            _msg("3", "通知：下周放假安排"),
            _msg("4", "晚上一起吃饭啊"),
        ])
        buckets = report.buckets
        self.assertEqual([i.message_id for i in buckets["有人提问"]], ["1"])
        self.assertEqual([i.message_id for i in buckets["待办事项"]], ["2"])
        self.assertEqual([i.message_id for i in buckets["通知事项"]], ["3"])
        self.assertEqual([i.message_id for i in buckets["闲聊"]], ["4"])


class FactsTests(unittest.TestCase):
    def test_time_and_place(self):
        facts = extract_facts("10月12日上午9点，地点：市中心广场集合")
        self.assertIn("10月12日", facts.get("time", ""))
        self.assertIn("市中心广场", facts.get("place", ""))

    def test_nothing_made_up(self):
        self.assertEqual(extract_facts("哈哈哈"), {})
        self.assertEqual(extract_facts(""), {})


class WizardRoleDispatchTests(unittest.TestCase):
    """向导按角色分流：订单要过额度门，其它直走本机引擎。"""

    def _wizard(self):
        import tempfile

        from pyweixin.orders import OrderStore
        from pyweixin.runmode import SettingsStore
        from pyweixin.conversations import ConversationRegistry
        from pyweixin.history_read import HistoryReader
        from pyweixin.wechat_state import WeChatMonitor, SimulatedWeChat
        from pyweixin.wizard import Wizard

        tmp = tempfile.TemporaryDirectory(prefix="pyw-role-")
        store = OrderStore(str(Path(tmp.name) / "orders.db"))
        tenant = store.create_merchant("t")["merchant_id"]
        store.add_import(tenant, "义工群", [
            {"sender": "张姐", "content": "周六义工接龙", "kind": "text",
             "sent_at": "2026-10-13 10:00", "source_id": "r1"}],
            "seed-src")
        wizard = Wizard(
            SettingsStore(str(Path(tmp.name) / "settings.json")),
            WeChatMonitor(SimulatedWeChat(["logged_in"])),
            ConversationRegistry(),
            HistoryReader(lambda **kw: []),
            tenant=tenant,
            orders=store,
        )
        return wizard, store, tmp

    def test_activity_skips_quota_and_builds_buckets(self):
        wizard, store, tmp = self._wizard()
        try:
            summary = wizard.analyze(role=ROLE_ACTIVITY)
            self.assertEqual(summary["role_id"], ROLE_ACTIVITY)
            self.assertEqual(summary["submitted_jobs"], 0)
            self.assertEqual(summary["charged_micro_usd"], 0)
            self.assertIn("报名接龙", summary["role_report"]["buckets"])
            # 角色整理不占"已整理"状态：换角色可重跑。
            self.assertEqual(wizard.already_analyzed_count(), 0)
        finally:
            store.close()
            tmp.cleanup()

    def test_activity_marks_processed_without_touching_order_state(self):
        """角色整理记"处理过"（导出放行），但不占订单的"已整理"状态。"""
        wizard, store, tmp = self._wizard()
        try:
            message_id = store.db.execute("SELECT id FROM messages").fetchone()["id"]
            summary = wizard.analyze(role=ROLE_ACTIVITY)
            self.assertEqual(summary["role_id"], ROLE_ACTIVITY)
            processed = store.processed_message_ids(wizard.tenant)
            self.assertIn(message_id, processed)
            # 订单链路不受影响：换订单助手还能正常提交，不会判 duplicate。
            self.assertEqual(wizard.already_analyzed_count(), 0)
            self.assertEqual(store.analyzed_message_ids(wizard.tenant), set())
        finally:
            store.close()
            tmp.cleanup()

    def test_order_still_requires_quota(self):
        from pyweixin.wizard import QuotaBlocked

        wizard, store, tmp = self._wizard()
        try:
            with self.assertRaises(QuotaBlocked):
                wizard.analyze(role=ROLE_ORDER)
        finally:
            store.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
