"""读取过程中商户能看到什么。

## 这组测试来自一次真实投诉

商户反馈「读取完信息后没有下一步，也没有读取进度和完成状态提示」。逐条查下来是
**三个独立缺陷**，其中最要命的一个完全看不出来：

1. **后台线程的异常没有任何人接。** :meth:`MerchantApplication._spawn` 没有
   try/except，读历史抛异常后异常直接消失，界面无声地退回上一步 —— 商户既
   不知道发生了什么，也不知道该不该重试。
2. **微信状态监视器会打断正在进行的流程。** 它每几秒检测一次，检测到「已就绪」
   就无条件跳回选择页。以前不判断当前在哪一步，于是商户正在读历史（读微信本身
   就会改变窗口状态）时被弹走，表现为「读完没有下一步」、进度条不动。
3. **读完之后直接跳页。** 「实际读到多少条、覆盖哪段时间、有没有缺口」一闪而过，
   商户没机会看清，也不知道下一步该点哪里。

这些在合成数据 + 秒回的读取里全都看不出来：读取瞬间完成，进度条来不及显示，
监视器也来不及插一脚。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "order_agent" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "business_agent" / "src"))

from wechat_ai_assistant.application import AppEvents, MerchantApplication  # noqa: E402
from wechat_ai_assistant.wizard import WizardStep  # noqa: E402

GROUP = "鲜果批发群"


def build_app(tmp_path, history_reader_fn):
    import tempfile

    from wechat_ai_assistant.conversations import ConversationKind, SimulatedConversations
    from wechat_ai_assistant.orders import OrderStore

    data_dir = Path(tempfile.mkdtemp(prefix="pyw-read-", dir=str(tmp_path)))
    store = OrderStore(str(data_dir / "orders.db"))
    tenant = store.create_merchant("读取测试店")["merchant_id"]
    app = MerchantApplication(
        data_dir=data_dir, orders=store, tenant=tenant,
        probe=None, launcher=None,
        conversation_source=SimulatedConversations(
            [(GROUP, ConversationKind.GROUP, 128), ("社区团购", ConversationKind.GROUP, 64)]
        ),
        history_reader_fn=history_reader_fn, simulated=True,
        sender_source="members",
    )
    return app, store, data_dir


def rows():
    return {
        GROUP: [
            {"消息发送人": "张姐", "消息内容": f"苹果{i}箱", "消息类型": "文本",
             "消息发送时间": f"2026-10-2 1{i}:1{i}"}
            for i in range(12)
        ],
        "社区团购": [
            {"消息发送人": "李女士", "消息内容": "香蕉五箱", "消息类型": "文本",
             "消息发送时间": "2026-10-2 09:01"},
        ],
    }


class BackgroundFailureTests(unittest.TestCase):
    """缺陷 1：后台任务失败必须变成商户看得见的中文。"""

    def setUp(self):
        import tempfile
        from wechat_ai_assistant.conversations import ConversationKind, SimulatedConversations
        from wechat_ai_assistant.history_read import SimulatedHistory
        from wechat_ai_assistant.orders import OrderStore

        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-fail-")
        data_dir = Path(self._tmp.name)
        store = OrderStore(str(data_dir / "orders.db"))
        tenant = store.create_merchant("失败测试店")["merchant_id"]
        self.app = MerchantApplication(
            data_dir=data_dir, orders=store, tenant=tenant, probe=None, launcher=None,
            conversation_source=SimulatedConversations(
                [(GROUP, ConversationKind.GROUP, 128)]
            ),
            history_reader_fn=SimulatedHistory(rows()), simulated=True,
        )
        self.seen: list[str] = []
        self.app.events = AppEvents(on_error=self.seen.append)
        self.store = store

    def tearDown(self):
        # merge.db / cursor 都是 sqlite，Windows 上不关就删不掉临时目录。
        for closer in (self.app.merger, self.app.cursor, self.app.agent_store):
            try:
                closer.close()
            except Exception:
                pass
        self.app.close()
        self.store.close()
        self._tmp.cleanup()

    def test_a_failing_background_task_reaches_the_interface(self):
        def explode():
            raise RuntimeError("boom")

        self.app._spawn("history-read", explode)
        deadline = time.time() + 5
        while time.time() < deadline and not self.seen:
            time.sleep(0.05)
        self.assertTrue(self.seen, "后台异常没有通知界面，商户什么都看不到")
        text = self.seen[0]
        self.assertIn("保留", text, "要告诉商户已读到的内容没丢")
        self.assertNotIn("boom", text, "异常原文不该出现在商户界面")
        self.assertNotIn("RuntimeError", text)

    def test_every_background_task_has_a_message_of_its_own(self):
        for name in ("history-read", "quota", "models", "analyze", "login"):
            with self.subTest(task=name):
                self.assertIn(name, MerchantApplication.FAILURE_TEXT)

    def test_unknown_tasks_still_get_a_message(self):
        def explode():
            raise ValueError("x")

        self.seen.clear()
        self.app._spawn("some-future-task", explode)
        deadline = time.time() + 5
        while time.time() < deadline and not self.seen:
            time.sleep(0.05)
        self.assertTrue(self.seen, "未登记的后台任务失败时也不该沉默")


class MonitorMustNotInterruptTests(unittest.TestCase):
    """缺陷 2：微信状态好了，不该把商户从正在进行的流程里拽走。"""

    def setUp(self):
        import tempfile
        from wechat_ai_assistant.wechat_state import SimulatedWeChat, WeChatState

        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-mon-")
        app, store, _ = build_app(self._tmp.name, None)
        self.app = app
        self.store = store
        self.app.monitor.probe = SimulatedWeChat([WeChatState.LOGGED_IN])
        # 停在第 3 步确认页：商户正在选范围，不能被拽走
        self.app.wizard.step = WizardStep.HISTORY

    def tearDown(self):
        # merge.db / cursor 都是 sqlite，Windows 上不关就删不掉临时目录。
        for closer in (self.app.merger, self.app.cursor, self.app.agent_store):
            try:
                closer.close()
            except Exception:
                pass
        self.app.close()
        self.store.close()
        self._tmp.cleanup()

    def test_a_ready_probe_does_not_yank_the_user_back(self):
        for _ in range(3):
            self.app.wizard.check_wechat()
            time.sleep(0.05)
        self.assertEqual(self.app.wizard.step, WizardStep.HISTORY,
                         "后台检测把商户弹回了别的步骤 —— 这就是「读完没有下一步」")

    def test_it_still_advances_when_still_on_the_first_step(self):
        from types import SimpleNamespace

        self.app.session = SimpleNamespace(merchant_name="测试商家")
        self.app.wizard.step = WizardStep.WECHAT
        self.app.wizard.check_wechat()
        self.assertEqual(self.app.wizard.step, WizardStep.CONVERSATIONS,
                         "首次进入时必须照常推进")

    def test_losing_wechat_still_goes_back_to_step_one(self):
        from wechat_ai_assistant.wechat_state import SimulatedWeChat, WeChatState

        self.app.monitor.probe = SimulatedWeChat([WeChatState.NOT_STARTED])
        self.app.wizard.check_wechat()
        self.assertEqual(self.app.wizard.step, WizardStep.WECHAT,
                         "微信真的掉线时必须退回第 1 步")


class ReadCompletionTests(unittest.TestCase):
    """缺陷 3：读完要留在原地，把结果和下一步说清楚。"""

    def setUp(self):
        import tempfile
        from wechat_ai_assistant.history_read import SimulatedHistory

        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-done-")
        self.app, self.store, _ = build_app(self._tmp.name, SimulatedHistory(rows(), delay=0.01))
        self.results: list = []
        self.app.wizard.bind_callbacks(on_history_done=self.results.append)
        self.app.wizard.orders = lambda: self.store
        self.app.wizard.pipeline = type("P", (), {"orders": self.store})()
        self.app.wizard.step = WizardStep.HISTORY
        self.app.wizard.choose_history_conversations(
            list(self.app.wizard.refresh_conversations())[:1]
        )

    def tearDown(self):
        # merge.db / cursor 都是 sqlite，Windows 上不关就删不掉临时目录。
        for closer in (self.app.merger, self.app.cursor, self.app.agent_store):
            try:
                closer.close()
            except Exception:
                pass
        self.app.close()
        self.store.close()
        self._tmp.cleanup()

    def test_reading_reports_completion_instead_of_navigating_away(self):
        self.app.wizard.read_history(range_kind="last_week", max_messages=200)
        self.assertTrue(self.results, "界面没收到读取完成的通知")
        result = self.results[-1]
        self.assertEqual(self.app.wizard.step, WizardStep.READ,
                          "读完就自动跳走，商户看不到读到了什么")
        self.assertGreater(result.total_messages, 0)

    def test_the_result_states_how_much_and_which_period(self):
        self.app.wizard.read_history(range_kind="last_week", max_messages=200)
        headline = self.results[-1].headline()
        self.assertIn("实际读到", headline)
        self.assertIn("时间范围", headline)

    def test_gaps_are_reported_to_the_interface(self):
        """只报成功项、藏起缺口，等于没报。"""
        # 用一个确定会超过预算的读取器：返回 40 条，预算只有 5 条。
        many = {GROUP: [
            {"sender": "张姐", "content": f"苹果{i}箱", "kind": "text",
             "sent_at": f"2026-10-2 1{i // 10}:1{i % 10}"}
            for i in range(40)
        ]}
        self.app.wizard.history._reader = lambda **kw: list(many[GROUP])
        self.app.wizard.read_history(range_kind="last_week", max_messages=5)
        result = self.results[-1]
        self.assertTrue(result.coverage[0].truncated, "读满预算却没说可能没读全")
        self.assertTrue(result.gaps, "缺口没有传到界面")


# 「界面不能说假话」那组断言放在 examples/merchant_ui_check.py：
# 真实 Tk 窗口由那个脚本统一管理生命周期。在单元测试里反复建/销窗口会
# 撞上 "Tcl_AsyncDelete: async handler deleted by the wrong thread" ——
# 窗口没了而监视器、after 回调还在往 Tk 上发事件，整个测试进程会崩。

class EmptyReadHonestyTests(unittest.TestCase):
    """「读到 0 条」和「读到 N 条但一条没入库」必须分开说。

    真机上同一个群出现过一次 0 条、一次 35 条 —— 读取依赖微信窗口状态，
    可能空手而归。这时候只说「没有读到任何消息」，商户会以为是自己的操作
    问题，反复重来，却不知道自己刚才其实读到了 7 条。
    """

    def _render(self, total, counts):
        import tkinter as tk
        from types import SimpleNamespace

        from wechat_ai_assistant.merchant_ui import MerchantWindow

        root = tk.Tk()
        root.withdraw()
        try:
            window = object.__new__(MerchantWindow)
            window._widgets = {}
            label = tk.Label(root)
            window._widgets["read_state"] = label
            result = SimpleNamespace(
                total_messages=total,
                coverage=[SimpleNamespace(message_count=c) for c in counts],
                headline=lambda: "x", gaps=[], cancelled=False,
            )
            MerchantWindow._render_read_state(window, result)
            return label.cget("text")
        finally:
            root.destroy()

    def test_nothing_read_says_nothing_read(self):
        text = self._render(0, [0])
        self.assertIn("没有读到任何消息", text)
        self.assertNotIn("刚才读到了", text)

    def test_read_but_not_stored_says_so(self):
        text = self._render(0, [7])
        self.assertIn("刚才读到了 7 条", text)
        self.assertIn("本地订单里还是空的", text)

    def test_the_two_are_not_the_same_message(self):
        self.assertNotEqual(self._render(0, [0]), self._render(0, [7]))

    def test_it_tells_the_merchant_what_to_do(self):
        """光说事实不够，得给出下一步。"""
        text = self._render(0, [7])
        self.assertIn("再点一次", text)


if __name__ == "__main__":
    unittest.main()

class NoDeadEndTests(unittest.TestCase):
    """一条都没读到时，商户必须还有一条走得通的路。

    ## 商户的原话

        没有读取到新信息以后没有下一步

    修订记录：原来这里摆的是「先看看已经读到的内容」按钮及配套去处，
    商户反馈取消后的面板不对 —— 决定整个删掉。现在的出路是执行页常驻的
    「上一步：改范围」：0 条时不给下一步，但永远可以回去换读法重来，
    不会卡死。
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).resolve().parent.parent
                      / "src" / "wechat_ai_assistant" / "merchant_ui.py").read_text(encoding="utf-8")

    def test_the_old_exit_button_is_gone(self):
        self.assertNotIn('"read_empty_exit"', self.source)
        self.assertNotIn("先看看已经读到的内容", self.source)
        self.assertNotIn("def _go_home_from_history", self.source)
        self.assertNotIn("def _show_empty_exit", self.source)

    def test_zero_messages_keeps_the_way_back(self):
        """0 条时「上一步：改范围」必须在，不能只剩下一步。"""
        start = self.source.index("def _page_read")
        block = self.source[start:start + 6000]
        self.assertIn("上一步", block)

    def test_the_message_says_nothing_was_lost(self):
        """读不到多半不是操作问题，不能说成「你没弄好」。"""
        self.assertIn("之前读到的一个都没丢", self.source)


if __name__ == "__main__":
    unittest.main()
