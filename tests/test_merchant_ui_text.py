"""界面上不能出现技术词 —— 商户看不懂就是缺陷。

界面的文案是从代码里取的，所以直接断言"界面上会显示的每一段文字"都不含
API Key、token、模型、服务器这类词。这条测试的价值在于：将来有人加了新文案，
这里会立刻拦住。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent", "support_triage"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from pyweixin.application import MerchantApplication  # noqa: E402
from pyweixin.conversations import Conversation, ConversationKind, SimulatedConversations, search  # noqa: E402
from pyweixin.dashboard import STATUS_LABELS, DashboardBuilder  # noqa: E402
from pyweixin.history_read import HistoryRange, SimulatedHistory  # noqa: E402
from pyweixin.merchant_ui import (  # noqa: E402
    FORBIDDEN_TERMS,
    PRIVACY_TEXT,
    audit_text,
)
from pyweixin.orders import OrderStore  # noqa: E402
from pyweixin.runmode import AutoOrderSettings, RunMode  # noqa: E402
from pyweixin.wechat_state import STATE_COPY, SimulatedWeChat, WeChatState  # noqa: E402
from pyweixin.wizard import QuotaView  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


class MerchantTextTests(unittest.TestCase):
    """所有会出现在商户眼前的文字。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-ui-"))
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.merchant = self.store.create_merchant("测试商家")
        self.tenant = self.merchant["merchant_id"]
        self.group = "测试群"
        self.customer = self.store.add_customer(self.tenant, "张姐", "张姐", self.group)
        self.store.add_product(self.tenant, "APL", "苹果", "箱", 2600, ["苹果"])
        self.app: MerchantApplication | None = None

    def tearDown(self):
        if self.app is not None:
            self.app.close()
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_audit_text_actually_detects_leaks(self):
        self.assertIn("token", audit_text("这里有 token"))
        self.assertEqual(audit_text("好的，已记单：苹果 2箱"), [])

    def test_no_forbidden_term_appears_in_any_merchant_string(self):
        strings = [PRIVACY_TEXT]
        strings += [copy[0] for copy in STATE_COPY.values()]
        strings += [copy[1] for copy in STATE_COPY.values()]
        strings += [copy[2] for copy in STATE_COPY.values()]
        strings += list(STATUS_LABELS.values())
        strings += [QuotaView().headline(), QuotaView(remaining=5).headline(), QuotaView(logged_in=True, active=False).headline()]
        for value in (AutoOrderSettings().describe(), AutoOrderSettings(mode=RunMode.PAUSED).describe()):
            strings.append(value)
        for text in strings:
            found = audit_text(text)
            self.assertEqual(found, [], f"商户界面文案里出现了技术词 {found}：{text[:60]}")

    def test_not_started_offers_a_clickable_button(self):
        app = self._app(probe=SimulatedWeChat([WeChatState.NOT_STARTED]))
        status = app.check_wechat()
        title, detail, button = status.copy()
        self.assertEqual(button, "打开微信")
        self.assertEqual(audit_text(title + detail), [])

    def test_every_state_has_chinese_explanation(self):
        for state in WeChatState.ALL:
            title, detail, _ = STATE_COPY[state]
            self.assertTrue(title.strip(), state)
            self.assertTrue(detail.strip(), state)
            self.assertEqual(audit_text(title + detail), [], state)

    def test_home_view_text_is_merchant_readable(self):
        view = self._home()
        for text in (view.status_line(), view.summary_line(), view.mode_note, view.quota_headline):
            self.assertEqual(audit_text(text), [], text)
        for row in view.effective:
            self.assertEqual(audit_text(row.status + row.customer + row.summary), [])
        for stock in view.stock:
            self.assertEqual(audit_text(stock.name + stock.unit), [])

    def test_paused_home_says_paused(self):
        app = self._app()
        app.pause()
        view = app.refresh_home()
        self.assertEqual(view.mode, RunMode.PAUSED)
        self.assertEqual(audit_text(view.mode_note), [])

    def _app(self, probe=None, **kwargs) -> MerchantApplication:
        self.app = MerchantApplication(
            data_dir=self.dir, orders=self.store, tenant=self.tenant,
            probe=probe or SimulatedWeChat([WeChatState.LOGGED_IN]),
            conversation_source=SimulatedConversations([(self.group, ConversationKind.GROUP, 128)]),
            history_reader_fn=SimulatedHistory(default_count=0),
            sender=lambda t, x: None,
            **kwargs,
        )
        return self.app

    def _home(self):
        app = self._app()
        app.choose_live_conversation(app.refresh_conversations()[0])
        return app.refresh_home()

    def test_forbidden_terms_list_covers_the_obvious_leaks(self):
        for term in ("token", "api key", "opencode", "model"):
            self.assertIn(term, FORBIDDEN_TERMS)


class DisplayContentTests(unittest.TestCase):
    """空内容按类型给占位：图片行不能看着像坏了。只测显示，不碰库。"""

    def test_text_shows_as_is(self):
        from pyweixin.merchant_ui import display_content

        self.assertEqual(display_content("text", "苹果两箱"), "苹果两箱")

    def test_empty_content_shows_kind_label(self):
        from pyweixin.merchant_ui import display_content

        self.assertEqual(display_content("image", ""), "［图片］")
        self.assertEqual(display_content("voice", None), "［语音］")
        self.assertEqual(display_content("video", "  "), "［视频］")
        self.assertEqual(display_content("other", ""), "［非文字消息］")
        self.assertEqual(display_content("whatever", ""), "［非文字消息］")
        self.assertEqual(display_content("", ""), "［非文字消息］")

    def test_export_uses_display_text(self):
        """导出和核对页一个口径：图片/语音行导出占位，不导空白格。"""
        import types

        from pyweixin.merchant_ui import MerchantWindow

        window = MerchantWindow.__new__(MerchantWindow)
        window._last_review_rows = [
            {"id": "m1", "group": "群", "sender": "a", "content": "",
             "kind": "image", "sent_at": "t"},
            {"id": "m2", "group": "群", "sender": "b", "content": "苹果两箱",
             "kind": "text", "sent_at": "t"},
        ]
        window.app = types.SimpleNamespace(
            orders=types.SimpleNamespace(
                processed_message_ids=lambda tenant: {"m1", "m2"}),
            tenant="t")
        rows = window._export_review_rows()
        self.assertEqual(rows[0]["content"], "［图片］")
        self.assertEqual(rows[1]["content"], "苹果两箱")


class ExportGateTests(unittest.TestCase):
    """导出只放过了一遍 AI 的：没整理的要么指路，要么问完只导已整理的。"""

    def _window(self, rows, processed):
        import types

        from pyweixin.merchant_ui import MerchantWindow

        window = MerchantWindow.__new__(MerchantWindow)
        window._last_review_rows = list(rows)
        store = types.SimpleNamespace(
            processed_message_ids=lambda tenant: set(processed))
        window.app = types.SimpleNamespace(orders=store, tenant="t")
        return window

    def _row(self, mid, content="苹果两箱"):
        return {"id": mid, "group": "群", "sender": "a",
                "content": content, "kind": "text", "sent_at": "t"}

    def test_all_processed_passes_quietly(self):
        import unittest.mock

        window = self._window([self._row("m1")], {"m1"})
        with unittest.mock.patch(
                "pyweixin.merchant_ui.messagebox") as box:
            rows = window._check_exportable()
        self.assertEqual(len(rows), 1)
        box.showinfo.assert_not_called()
        box.askyesno.assert_not_called()

    def test_nothing_processed_points_to_analyze(self):
        import unittest.mock

        window = self._window([self._row("m1")], set())
        with unittest.mock.patch(
                "pyweixin.merchant_ui.messagebox") as box:
            self.assertIsNone(window._check_exportable())
        box.showinfo.assert_called_once()
        self.assertIn("智能整理", box.showinfo.call_args.args[1])

    def test_partial_asks_and_exports_subset(self):
        import unittest.mock

        window = self._window(
            [self._row("m1"), self._row("m2")], {"m1"})
        with unittest.mock.patch(
                "pyweixin.merchant_ui.messagebox") as box:
            box.askyesno.return_value = True
            rows = window._check_exportable()
        self.assertEqual([row["id"] for row in rows], ["m1"])

    def test_partial_decline_exports_nothing(self):
        import unittest.mock

        window = self._window(
            [self._row("m1"), self._row("m2")], {"m1"})
        with unittest.mock.patch(
                "pyweixin.merchant_ui.messagebox") as box:
            box.askyesno.return_value = False
            self.assertIsNone(window._check_exportable())


class MarkAnalyzedMessagesTests(unittest.TestCase):
    """AI 分析跑完要把消息记成「整理过」，否则第 5 步导出被门禁卡住。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-mark-"))
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.group = "测试群"

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _window(self):
        import types

        from pyweixin.merchant_ui import MerchantWindow

        window = MerchantWindow.__new__(MerchantWindow)
        window.app = types.SimpleNamespace(orders=self.store, tenant=self.tenant)
        return window

    def _messages(self, contents):
        self.store.add_import(self.tenant, self.group, [
            {"sender": "张姐", "content": c, "kind": "text",
             "sent_at": f"2026-10-14T09:{i:02d}:00-04:00", "source_id": f"s{i}"}
            for i, c in enumerate(contents)], "src", "demo")
        rows = self.store.db.execute(
            "SELECT id, group_id FROM messages WHERE merchant_id=? ORDER BY id",
            (self.tenant,)).fetchall()
        return [{"id": str(r["id"]), "group_id": str(r["group_id"])} for r in rows]

    def test_analyzed_messages_become_exportable(self):
        messages = self._messages(["要两箱苹果", "再要一箱"])
        window = self._window()
        window._mark_analyzed_messages(messages, {"completed_groups": []}, 2, "order")
        processed = self.store.processed_message_ids(self.tenant)
        self.assertTrue({m["id"] for m in messages} <= processed)

    def test_nothing_processed_marks_nothing(self):
        messages = self._messages(["要两箱苹果"])
        window = self._window()
        window._mark_analyzed_messages(messages, {"completed_groups": []}, 0, "order")
        self.assertEqual(self.store.processed_message_ids(self.tenant), set())

    def test_only_completed_groups_are_marked(self):
        messages = self._messages(["甲", "乙"])
        messages[0]["group_id"] = "完成群"
        messages[1]["group_id"] = "失败群"
        window = self._window()
        window._mark_analyzed_messages(messages, {"completed_groups": ["完成群"]}, 1, "order")
        processed = self.store.processed_message_ids(self.tenant)
        self.assertIn(messages[0]["id"], processed)
        self.assertNotIn(messages[1]["id"], processed)


class HomeContentTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-home-"))
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.merchant = self.store.create_merchant("测试商家")
        self.tenant = self.merchant["merchant_id"]
        self.group = "测试群"
        self.customer = self.store.add_customer(self.tenant, "张姐", "张姐", self.group)
        self.store.add_product(self.tenant, "APL", "苹果", "箱", 2600, ["苹果"])
        self.store.add_product(self.tenant, "EGG", "鸡蛋", "箱", 3400, ["鸡蛋"])
        self.app: MerchantApplication | None = None

    def tearDown(self):
        if self.app is not None:
            self.app.close()
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _order(self, customer_id, sku, quantity, status="draft", version=1, tenant=None):
        import uuid

        tenant = tenant or self.tenant
        oid = "o_" + uuid.uuid4().hex[:12]
        self.store.db.execute(
            "INSERT INTO orders(id,merchant_id,customer_id,group_id,status,delivery_date,source_import_id,"
            "created_at,updated_at,version,change_type,business_at_epoch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, tenant, customer_id, self.group, status, "2026-09-26", None,
             NOW.isoformat(), NOW.isoformat(), version, "new", NOW.timestamp()),
        )
        product = self.store.db.execute(
            "SELECT id,price_cents,unit FROM products WHERE merchant_id=? AND sku=?", (tenant, sku)
        ).fetchone()
        self.store.db.execute(
            "INSERT INTO order_items(id,order_id,product_id,quantity,unit,unit_price_cents) VALUES(?,?,?,?,?,?)",
            ("i_" + uuid.uuid4().hex[:12], oid, product["id"], quantity, product["unit"], product["price_cents"]),
        )
        self.store.db.commit()
        return oid

    def test_home_shows_orders_stock_and_exceptions(self):
        other = self.store.add_customer(self.tenant, "王老板", "王老板", self.group)
        self._order(self.customer, "APL", 2)
        self._order(other, "APL", 1)
        self._order(self.customer, "EGG", 3)
        builder = DashboardBuilder(self.store, self.tenant, clock=lambda: NOW)
        orders = builder.effective_orders()
        self.assertEqual(len(orders), 3)
        stock = builder.stock_summary(orders)
        by_sku = {row.sku: row for row in stock}
        self.assertEqual(by_sku["APL"].quantity, 3, "同一商品跨客户合并备货")
        self.assertEqual(by_sku["APL"].order_count, 2)
        self.assertEqual(set(by_sku["APL"].customers), {"王老板", "张姐"})
        self.assertEqual(by_sku["EGG"].quantity, 3)

    def test_confirmed_orders_count_as_effective_but_not_actionable(self):
        """已确认是真实经营结果，算有效订单；商户不需要再动手，所以不算待办。"""
        self._order(self.customer, "APL", 2, status="confirmed")
        self._order(self.customer, "EGG", 1, status="draft")
        builder = DashboardBuilder(self.store, self.tenant, clock=lambda: NOW)
        statuses = {row.status for row in builder.effective_orders()}
        self.assertIn("confirmed", statuses)
        self.assertEqual(builder.actionable_orders(), [], "已确认的订单不应出现在待办里")
        stock = {row.sku for row in builder.stock_summary()}
        self.assertEqual(stock, {"APL", "EGG"}, "备货必须算上已确认的订单，否则会少采购")

    def test_cancelled_and_superseded_orders_count_nowhere(self):
        self._order(self.customer, "APL", 2, status="cancelled")
        self._order(self.customer, "EGG", 1, status="superseded")
        builder = DashboardBuilder(self.store, self.tenant, clock=lambda: NOW)
        self.assertEqual(builder.effective_orders(), [])
        self.assertEqual(builder.stock_summary(), [], "取消和被替代的版本不得进入备货汇总")

    def test_unconfirmed_is_counted_separately_from_payment(self):
        self._order(self.customer, "APL", 2)
        builder = DashboardBuilder(self.store, self.tenant, clock=lambda: NOW)
        self.assertEqual(builder.unconfirmed_count(), 1)
        self.assertEqual(builder.payment_claims(), [], "没回复不等于已付款")

    def test_payment_claims_are_listed_but_never_marked_paid(self):
        oid = self._order(self.customer, "APL", 2)
        self.store.db.execute("UPDATE orders SET claim_payment=1 WHERE id=?", (oid,))
        self.store.db.commit()
        claims = DashboardBuilder(self.store, self.tenant, clock=lambda: NOW).payment_claims()
        self.assertEqual(len(claims), 1)
        self.assertIn("人工核对", claims[0]["note"])
        row = self.store.db.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
        self.assertNotEqual(row["status"], "paid")

    def test_dashboard_does_not_leak_across_tenants(self):
        other_merchant = self.store.create_merchant("另一家")
        other_tenant = other_merchant["merchant_id"]
        other_customer = self.store.add_customer(other_tenant, "外人", "外人", self.group)
        # 另一家商家自己没有商品目录，所以先建一个；否则这单根本无法成立。
        self.store.add_product(other_tenant, "APL", "苹果", "箱", 2600, ["苹果"])
        # 订单必须真的属于另一家商家；只换一个 customer_id 不足以构造越权数据。
        self._order(other_customer, "APL", 9, tenant=other_tenant)
        self._order(self.customer, "APL", 2)
        rows = DashboardBuilder(self.store, self.tenant, clock=lambda: NOW).effective_orders()
        self.assertEqual(len(rows), 1, "首页不得显示别家商家的订单")
        self.assertNotIn("外人", [row.customer for row in rows])


class ConversationTabTests(unittest.TestCase):
    """会话页分栏：群和联系人必须分开，而且一条都不能丢。

    真机上实测是 10 个群对 246 个联系人。混在一个列表里，商户要翻两百多行
    才能找到做生意的群 —— 这就是分栏的理由。

    这里只测纯逻辑，不建 Tk 窗口：真正的窗口由 ``examples/merchant_ui_check.py``
    驱动（44 项，含真实按钮回调）。
    """

    class _Query:
        """只提供 ``get()`` 的搜索框替身，免掉 Tk 依赖。"""

        def __init__(self, text: str = ""):
            self.text = text

        def get(self) -> str:
            return self.text

    def _window(self, items, query: str = ""):
        from pyweixin.merchant_ui import MerchantWindow

        window = MerchantWindow.__new__(MerchantWindow)  # 不跑 __init__，也就没有 Tk
        window._conversations = list(items)
        window._widgets = {"conv_search": self._Query(query)}
        window._conv_visible = {}
        return window

    @staticmethod
    def _group(name: str, members: int = 128) -> Conversation:
        return Conversation(name=name, kind=ConversationKind.GROUP, member_count=members)

    @staticmethod
    def _friend(name: str) -> Conversation:
        return Conversation(name=name, kind=ConversationKind.FRIEND)

    def test_groups_and_contacts_land_in_separate_tabs(self):
        items = [self._group("鲜果批发群", 424), self._friend("王老板"),
                 self._group("社区团购", 64), self._friend("李女士")]
        buckets = self._window(items)._split_visible()
        self.assertEqual([c.name for c in buckets["groups"]], ["鲜果批发群", "社区团购"])
        self.assertEqual([c.name for c in buckets["friends"]], ["王老板", "李女士"])

    def test_the_real_ten_to_two_hundred_ratio_keeps_every_group_visible(self):
        """真机比例：10 个群对 246 个联系人。群必须一次看全。"""
        items = [self._group(f"群{i}", 100) for i in range(10)]
        items += [self._friend(f"联系人{i}") for i in range(246)]
        buckets = self._window(items)._split_visible()
        self.assertEqual(len(buckets["groups"]), 10)
        self.assertEqual(len(buckets["friends"]), 246)

    def test_an_unrecognised_kind_is_kept_not_dropped(self):
        """认不出来的会话也要有地方去：商户勾中的东西不能凭空消失。"""
        odd = Conversation(name="说不清是什么", kind=ConversationKind.UNKNOWN)
        buckets = self._window([odd, self._group("群A")])._split_visible()
        everything = buckets["groups"] + buckets["friends"]
        self.assertEqual(len(everything), 2, "有一条会话不见了")
        self.assertIn(odd, everything)

    def test_search_filters_before_the_split(self):
        items = [self._group("鲜果批发群", 424), self._group("夜宵烧烤群", 42),
                 self._friend("王老板")]
        buckets = self._window(items, "夜宵")._split_visible()
        self.assertEqual([c.name for c in buckets["groups"]], ["夜宵烧烤群"])
        self.assertEqual(buckets["friends"], [])

    def test_nothing_is_duplicated_or_lost_across_the_split(self):
        """这是行号映射正确的前提：两栏合起来必须正好是筛选后的全集。"""
        items = [self._group("群A"), self._friend("甲"), self._group("群B"),
                 self._friend("乙"), self._friend("丙")]
        for query in ("", "群", "甲"):
            with self.subTest(query=query):
                buckets = self._window(items, query)._split_visible()
                merged = buckets["groups"] + buckets["friends"]
                self.assertEqual(len(merged), len(set(id(c) for c in merged)), "有重复")
                self.assertEqual(len(merged), len(search(items, query)), "筛选结果被改动了")

    def test_the_tab_order_is_stable(self):
        """页签顺序写死在 CONV_TABS 里，取值和画界面都依赖它。"""
        from pyweixin.merchant_ui import MerchantWindow

        self.assertEqual([key for key, _t, _k in MerchantWindow.CONV_TABS], ["groups", "friends"])
        self.assertEqual([title for _k, title, _kind in MerchantWindow.CONV_TABS], ["群聊", "联系人"])


class EmptySpaceTests(unittest.TestCase):
    """界面不该用空白占版面。这些是"看起来像坏了"的那些地方。"""

    def test_the_list_height_follows_its_content(self):
        """只有 2 个群却占 12 行高度，页面下半截全是空白。"""
        from pyweixin.merchant_ui import CONV_ROWS_MAX, CONV_ROWS_MIN, conv_rows_height

        self.assertEqual(conv_rows_height(0), CONV_ROWS_MIN, "空列表也不该塌成 0 行")
        self.assertEqual(conv_rows_height(2), CONV_ROWS_MIN, "2 行数据不该占满 12 行")
        self.assertEqual(conv_rows_height(10), 10)
        self.assertEqual(conv_rows_height(9999), CONV_ROWS_MAX, "再长也不能超过上限")

    def test_the_count_column_is_named_for_what_it_shows(self):
        """那一列放的是人数。以前标题叫「说明」，里面写着「64 人」。"""
        import inspect

        from pyweixin import merchant_ui

        source = inspect.getsource(merchant_ui.MerchantWindow._page_conversations)
        self.assertIn('"members", "人数"', source)
        self.assertNotIn('"detail", "说明"', source)

    def test_the_empty_home_tables_can_be_hidden(self):
        """「待澄清」和「系统解析失败」平时都是空的，不该一直占着首页。"""
        import inspect

        from pyweixin import merchant_ui

        self.assertTrue(hasattr(merchant_ui.MerchantWindow, "_toggle_box"))
        render = inspect.getsource(merchant_ui.MerchantWindow._render_home)
        self.assertIn('_toggle_box("clar_box"', render)
        self.assertIn('_toggle_box("fail_box"', render)

    def test_the_primary_button_looks_different_from_the_others(self):
        """主按钮只有 padding 没有别的，一眼看不出该点哪个。"""
        import inspect

        from pyweixin import ui_theme

        build = inspect.getsource(ui_theme.apply_theme)
        big = build[build.index('for name in ("Big.TButton"'):build.index('"Subtle.TButton"')]
        self.assertIn("font", big, "主按钮没有字体层级")
        self.assertIn("borderwidth", big, "主按钮没有描边")


class FlowSimplicityTests(unittest.TestCase):
    """流程本身要短。如实上报必须真的被看见。"""

    def test_the_merchant_walks_seven_steps_not_more(self):
        """确认范围、真正执行、核对明细各占一步，额度查询与智能整理合成一步。"""
        from pyweixin.wizard import WizardStep

        self.assertEqual(len(WizardStep.ORDER), 7, "商户要走的步骤变多了")
        self.assertNotIn(WizardStep.QUOTA, WizardStep.ORDER, "额度查询不该再单独占一步")
        self.assertEqual(
            WizardStep.ORDER,
            (WizardStep.WECHAT, WizardStep.CONVERSATIONS, WizardStep.HISTORY,
             WizardStep.READ, WizardStep.REVIEW, WizardStep.ANALYZE, WizardStep.DONE),
        )
        self.assertEqual(WizardStep.TITLES[WizardStep.HISTORY], "确认读取范围")
        self.assertEqual(WizardStep.TITLES[WizardStep.READ], "读取历史消息")
        self.assertEqual(WizardStep.TITLES[WizardStep.REVIEW], "核对读取明细")

    def test_both_steps_render_the_same_screen(self):
        """额度与整理是同一件事的两半，拆成两屏只是多一次点击。"""
        import inspect

        from pyweixin import merchant_ui

        source = inspect.getsource(merchant_ui.MerchantWindow._show_step)
        self.assertIn("WizardStep.QUOTA: self._page_analyze", source)
        self.assertIn("WizardStep.ANALYZE: self._page_analyze", source)

    def test_the_read_result_is_drawn_on_the_screen_they_land_on(self):
        """第 6 步只留：角色单选、开始整理、角色报告。

        "刚才读到的消息"、"分析额度"、"选择分析模型"都已拿掉：
        读到多少第 4 步写了，额度不足点整理会直接说明，模型由云端配置。
        """
        import inspect

        from pyweixin import merchant_ui

        source = inspect.getsource(merchant_ui.MerchantWindow._page_analyze)
        self.assertNotIn("_build_history_summary", source)
        self.assertNotIn("_build_quota_block", source)
        self.assertNotIn("_build_model_picker", source)
        self.assertNotIn("选择分析模型", source)
        self.assertIn("_build_role_picker", source)

    def test_checking_quota_does_not_navigate_away(self):
        """额度够用就跳走，读到的条数和缺口就看不见了。"""
        import inspect

        from pyweixin.wizard import Wizard

        source = inspect.getsource(Wizard.check_quota)
        self.assertNotIn("self._go(", source, "查额度时还在跳转")

    def test_the_merged_step_says_it_is_step_four(self):
        from pyweixin.wizard import WizardStep

        self.assertEqual(WizardStep.TITLES[WizardStep.ANALYZE], "智能整理")
        self.assertEqual(WizardStep.TITLES[WizardStep.QUOTA], "智能整理")

    def test_first_paint_is_checking_not_failed(self):
        """还没查过就摆失败脸，商户以为一上来就坏了。"""
        import inspect

        from pyweixin import merchant_ui

        source = inspect.getsource(merchant_ui.MerchantWindow._page_wechat)
        self.assertIn("正在检测微信状态", source)


class NoLeakedExceptionTests(unittest.TestCase):
    """Python 异常原文不允许出现在商户界面上。

    :data:`FORBIDDEN_TERMS` 拦的是 token/model 这类词，拦不住类名和方法名。
    这里补的是**另一类**泄漏：``'DemoGateway' object has no attribute 'list_models'``。
    商户看不懂，只会觉得软件坏了。
    """

    #: 界面上出现这些就说明有代码/异常细节漏出来了。
    LEAKS = ("object has no attribute", "NoneType", "Traceback", "__init__",
             "object at 0x", "AttributeError", "KeyError", "ValueError", "Exception")

    def test_no_python_exception_shape_reaches_the_merchant(self):
        from pyweixin.application import MODEL_STALE_NOTICE

        text = MODEL_STALE_NOTICE
        for leak in self.LEAKS:
            self.assertNotIn(leak, text, f"商户文案里出现了 {leak}：{text}")

    def test_the_stale_notice_is_plain_chinese_and_actionable(self):
        from pyweixin.application import MODEL_STALE_NOTICE

        self.assertIn("保留", MODEL_STALE_NOTICE, "要让商户知道消息没丢")
        self.assertIn("刷新", MODEL_STALE_NOTICE, "要告诉商户下一步能做什么")
        self.assertEqual(audit_text(MODEL_STALE_NOTICE), [])

    def test_a_failing_model_refresh_never_writes_the_exception_into_the_notice(self):
        """真触发一次失败：客户端抛异常，界面上不许出现异常原文。"""
        from pyweixin.application import MerchantApplication

        class Exploding:
            def list_models(self):
                raise AttributeError("'DemoGateway' object has no attribute 'list_models'")

        app = MerchantApplication.__new__(MerchantApplication)
        app.client = Exploding()
        app.model_catalog = None
        app.events = type("E", (), {"on_log": None})()
        app._reconcile_model_choice = lambda: None
        app._adopt_pricing_version = lambda: None

        catalog = MerchantApplication.load_models(app)
        self.assertTrue(catalog["stale"], "失败必须标记为不是最新")
        notice = str(catalog["notice"])
        for leak in self.LEAKS:
            self.assertNotIn(leak, notice, f"异常泄漏到界面：{notice}")
        self.assertEqual(audit_text(notice), [])


class NextStepTargetTests(unittest.TestCase):
    """核对页的"下一步"必须进智能整理，不能原地踏步。

    回归真机 case：第 5 步点"下一步：智能整理"毫无反应 ——
    它和第 4 步共用 handler，目标写死 REVIEW，在第 5 步点就是原地刷新。
    """

    def _window(self, result):
        from types import SimpleNamespace

        from pyweixin.merchant_ui import MerchantWindow
        from pyweixin.wizard import WizardStep

        window = MerchantWindow.__new__(MerchantWindow)
        window._last_read_result = result
        calls: list = []
        window.app = SimpleNamespace(
            wizard=SimpleNamespace(_go=calls.append, history_result=result))
        window._WizardStep = WizardStep
        return window, calls

    def test_review_page_advances_to_analyze(self):
        from pyweixin.wizard import WizardStep

        window, calls = self._window(object())
        with unittest.mock.patch.object(
                window, "_has_messages_for", return_value=True):
            window._go_next_from_history(WizardStep.ANALYZE)
        self.assertEqual(calls, [WizardStep.ANALYZE])

    def test_execute_page_still_goes_to_review(self):
        from pyweixin.wizard import WizardStep

        window, calls = self._window(object())
        with unittest.mock.patch.object(
                window, "_has_messages_for", return_value=True):
            window._go_next_from_history()
        self.assertEqual(calls, [WizardStep.REVIEW])

    def test_no_messages_blocks_with_a_dialog(self):
        from pyweixin.wizard import WizardStep

        window, calls = self._window(None)
        with unittest.mock.patch.object(
                window, "_has_messages_for", return_value=False), \
            unittest.mock.patch("pyweixin.merchant_ui.messagebox") as box:
            window._go_next_from_history(WizardStep.ANALYZE)
        self.assertEqual(calls, [])
        self.assertTrue(box.showinfo.called)


if __name__ == "__main__":
    unittest.main()
