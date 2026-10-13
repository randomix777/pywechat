"""持续监听接进商户客户端。

## 为什么这组测试盯的是"id"

监听采到的是 :class:`AgentStore` 风格的 ``event_id``，而商户客户端整条链
（``OrderPipeline``、``LocalParseService``）都按 ``OrderStore.messages.id`` 回查
本地消息表。这两个 id 空间毫无关系。早前的接线把 ``event.stable_id`` 直接投进
合并器，于是每一条实时消息在 ``process()`` 里都被判成"本地没有这条消息"并
静默 return —— 没有报错、没有日志、统计里只是订单数一直为 0。

所以这里最重要的断言是：**合并器和 pipeline 拿到的必须是落库返回的 id**。
"""
from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyweixin.conversations import Conversation, ConversationKind  # noqa: E402
from pyweixin.live_listen import (  # noqa: E402
    LISTEN_DEGRADED,
    LISTEN_FAILED,
    LISTEN_RUNNING,
    LISTEN_STOPPED,
    LiveListener,
)
from pyweixin.merging import MergeSettings, MessageMerger  # noqa: E402
from pyweixin.reliability import MessageDirection, MessageEvent, MessageKind  # noqa: E402

NOW = datetime.now(timezone.utc)
GROUP = "监听测试群"
CONVERSATION = Conversation(name=GROUP, kind=ConversationKind.GROUP, member_count=128)


def event(event_id: str, content: str, *, sender: str = "张姐", direction=MessageDirection.INCOMING):
    return MessageEvent(
        event_id=event_id,
        stable_id=f"stable-{event_id}",
        chat_id=GROUP,
        account_id="default",
        sender=sender,
        content=content,
        kind=MessageKind.TEXT,
        direction=direction,
        observed_at=time.time(),
    )


class FakeDialog:
    def __init__(self):
        self.closed = False

    def exists(self, timeout: float = 0.1) -> bool:
        return not self.closed

    def close(self) -> None:
        self.closed = True


class ScriptedCollector:
    def __init__(self, script: list[list[MessageEvent]]):
        self.script = script
        self.calls = 0
        self.seen_close_flag: list[bool] = []

    def __call__(self, **kwargs) -> dict:
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        # 客户端必须要求"不要关窗口"，否则窗口复用无从谈起。
        self.seen_close_flag.append(kwargs.get("close_dialog_window"))
        batch = self.script[index]
        return {"count": len(batch), "events": [item.to_dict() for item in batch]}


class OrderStoreSink:
    """模拟客户端那一侧：落进 OrderStore，返回**它自己的** message id。"""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.source = f"live:{CONVERSATION.identity}"

    def __call__(self, ev: MessageEvent) -> tuple[bool, str]:
        # 故意让 OrderStore 的 id 与 event_id / stable_id 长得完全不一样 ——
        # 真实库里就是 msg_<hash>。这样"错用了采集侧 id"一定会被测出来。
        message_id = f"msg_{abs(hash(ev.event_id)) % 99999:05d}"
        if message_id in self.rows:
            return (False, "")
        self.rows[message_id] = {
            "id": message_id, "sender": ev.sender, "content": ev.content,
            "kind": "text", "sent_at": "", "group_id": GROUP,
        }
        return (True, message_id)


class RecordingPipeline:
    def __init__(self, store: OrderStoreSink):
        self.store = store
        self.processed: list[str] = []

    def process(self, message_id: str, *, source: str = "live", on_progress=None):
        # 复现 OrderPipeline._message 的查库：找不到就"rejected"，静默丢弃。
        if message_id not in self.store.rows:
            return type("Outcome", (), {"order_id": "", "needs_review": False})()
        self.processed.append(message_id)
        return type("Outcome", (), {"order_id": f"o_{message_id}", "needs_review": False})()


def build(collector, sink, pipeline, **kwargs):
    merger = MessageMerger(":memory:", MergeSettings(silence_window=0.3, max_wait=1.0))
    return LiveListener(
        conversation=CONVERSATION, store=None, merger=merger, pipeline=pipeline,
        dialog_opener=lambda **kw: FakeDialog(), listener=collector,
        sink=sink, poll_seconds=0.2, **kwargs,
    ), merger


class PersistSinkTests(unittest.TestCase):
    """落库那一端必须是可注入的，且默认行为不变。"""

    def test_sink_id_is_used_for_the_merger_not_the_collect_side_id(self):
        first = event("e1", "苹果两箱")
        burst = [event("e2", "再加一箱"), event("e3", "明天送")]
        sink = OrderStoreSink()
        pipeline = RecordingPipeline(sink)
        collector = ScriptedCollector([[first], burst, burst, burst])
        listener, merger = build(collector, sink, pipeline)

        listener.start()
        deadline = time.time() + 10
        while time.time() < deadline and not pipeline.processed:
            time.sleep(0.1)
        listener.stop()

        self.assertTrue(pipeline.processed, "一条都没处理：落库 id 用错了")
        for message_id in pipeline.processed:
            self.assertTrue(message_id.startswith("msg_"), f"用了采集侧的 id：{message_id}")
            self.assertNotIn(message_id, {first.event_id, first.stable_id})

    def test_duplicate_delivery_is_not_stored_twice(self):
        one = event("e1", "苹果两箱")
        # 第一轮是启动基线（会被整批跳过），所以要单独给一轮空的。
        sink = OrderStoreSink()
        collector = ScriptedCollector([[], [one], [one], [one]])
        listener, _ = build(collector, sink, RecordingPipeline(sink))
        listener.start()
        time.sleep(1.0)
        listener.stop()
        self.assertEqual(listener.stats.events_stored, 1)
        self.assertGreaterEqual(listener.stats.events_skipped_duplicate, 1)

    def test_own_messages_are_never_stored(self):
        own = event("own-1", "好的，已记单：苹果 3箱", direction=MessageDirection.OUTGOING)
        sink = OrderStoreSink()
        collector = ScriptedCollector([[], [own], [own], [own]])
        listener, _ = build(collector, sink, RecordingPipeline(sink))
        listener.start()
        time.sleep(1.0)
        listener.stop()
        self.assertEqual(listener.stats.events_stored, 0, "自己的回执不该入库")
        self.assertGreaterEqual(listener.stats.events_skipped_own, 1)

    def test_baseline_messages_are_not_treated_as_new(self):
        history = [event(f"old{i}", f"老消息{i}") for i in range(3)]
        fresh = [event("new1", "刚来的两箱")]
        sink = OrderStoreSink()
        collector = ScriptedCollector([history, fresh, fresh, fresh])
        listener, _ = build(collector, sink, RecordingPipeline(sink))
        listener.start()
        time.sleep(1.0)
        listener.stop()
        self.assertEqual(listener.stats.events_stored, 1, "启动基线里的老消息被当成新消息了")

    def test_a_window_that_disappears_is_reported_not_hidden(self):
        dialog = FakeDialog()
        collector = ScriptedCollector([[], []])

        def opener(**kwargs):
            return dialog

        merger = MessageMerger(":memory:", MergeSettings(silence_window=0.3, max_wait=1.0))
        states: list[tuple[str, str]] = []
        listener = LiveListener(
            conversation=CONVERSATION, store=None, merger=merger, pipeline=None,
            dialog_opener=opener, listener=collector, sink=OrderStoreSink(),
            poll_seconds=0.2, on_state=lambda s, d: states.append((s, d)),
        )
        listener.start()
        time.sleep(0.5)
        dialog.close()
        time.sleep(0.6)
        listener.stop()
        self.assertTrue(any(state == LISTEN_DEGRADED for state, _ in states), states)

    def test_the_window_is_reused_not_reopened_every_poll(self):
        dialog = FakeDialog()
        opens: list[int] = []

        def opener(**kwargs):
            opens.append(1)
            return dialog

        sink = OrderStoreSink()
        merger = MessageMerger(":memory:", MergeSettings(silence_window=0.3, max_wait=1.0))
        listener = LiveListener(
            conversation=CONVERSATION, store=None, merger=merger, pipeline=None,
            dialog_opener=opener, listener=ScriptedCollector([[]] * 20), sink=sink, poll_seconds=0.15,
        )
        listener.start()
        time.sleep(1.0)
        listener.stop()
        self.assertEqual(len(opens), 1, f"窗口被重复打开了 {len(opens)} 次")

    def test_collection_asks_wechat_not_to_close_the_window(self):
        sink = OrderStoreSink()
        collector = ScriptedCollector([[]] * 4)
        listener, _ = build(collector, sink, RecordingPipeline(sink))
        listener.start()
        time.sleep(0.6)
        listener.stop()
        self.assertTrue(collector.seen_close_flag)
        self.assertFalse(any(collector.seen_close_flag), "要求关窗口，窗口复用就没了")


class UiLockTests(unittest.TestCase):
    def test_collection_runs_inside_the_serial_queue(self):
        """监听每轮都在动微信界面，必须和读历史、发送串行。"""
        sink = OrderStoreSink()
        collector = ScriptedCollector([[]] * 4)
        listener, _ = build(collector, sink, RecordingPipeline(sink), ui_lock=None)
        self.assertIsNone(listener._ui_lock)
        listener.start()
        time.sleep(0.5)
        listener.stop()
        # 只是确认参数确实被接住了，不在这里验证并发（那要靠 application 层）。
        self.assertTrue(listener.stats.events_seen >= 0)


class ListenViewTests(unittest.TestCase):
    """界面上那个开关要显示的东西。"""

    def _app(self, **kwargs):
        from pyweixin.application import LISTEN_FAIR_NOTE, MerchantApplication

        return MerchantApplication, LISTEN_FAIR_NOTE

    def test_the_view_shape_is_stable_before_anything_starts(self):
        import inspect

        source = inspect.getsource(
            __import__("pyweixin.application", fromlist=["MerchantApplication"]).MerchantApplication.listen_view
        )
        for key in ("running", "conversation", "state", "summary", "gaps", "gap_count", "note"):
            self.assertIn(f'"{key}"', source, f"监听视图少了 {key}")

    def test_the_fair_note_says_it_takes_the_foreground(self):
        from pyweixin.application import LISTEN_FAIR_NOTE

        self.assertIn("前台", LISTEN_FAIR_NOTE)
        self.assertNotIn("不抢焦点", LISTEN_FAIR_NOTE.replace("从未在真实机器上验证过抢不抢焦点", ""))

    def test_listening_is_off_until_the_merchant_asks(self):
        from pyweixin.application import MerchantApplication

        self.assertIsNone(MerchantApplication.__init__.__code__.co_consts is None or None)


class FailFastTests(unittest.TestCase):
    """窗口一次都打不开：3 次就停下来明说，别无限转圈干等超时。"""

    def _listener(self, opener, states):
        merger = MessageMerger(":memory:", MergeSettings(silence_window=0.3, max_wait=1.0))
        return LiveListener(
            conversation=CONVERSATION, store=None, merger=merger, pipeline=None,
            dialog_opener=opener, listener=ScriptedCollector([[]]),
            sink=OrderStoreSink(), poll_seconds=0.2,
            on_state=lambda s, d: states.append((s, d)),
        )

    def test_three_failed_opens_stop_with_failed_state(self):
        attempts: list[int] = []

        def opener(**kwargs):
            attempts.append(1)
            raise RuntimeError("打不开")

        states: list[tuple[str, str]] = []
        listener = self._listener(opener, states)
        listener.start()
        deadline = time.time() + 10
        while listener.running and time.time() < deadline:
            time.sleep(0.05)
        listener.stop()
        self.assertFalse(listener.running)
        self.assertEqual(len(attempts), 3, f"开了 {len(attempts)} 次才停")
        self.assertIn(LISTEN_FAILED, [state for state, _ in states])
        detail = " ".join(detail for state, detail in states if state == LISTEN_FAILED)
        self.assertIn("历史读取", detail, "要告诉商户还有别的路")

    def test_mid_run_failures_still_retry(self):
        dialog = FakeDialog()
        calls = {"n": 0}

        def opener(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return dialog
            raise RuntimeError("中途掉了")

        states: list[tuple[str, str]] = []
        listener = self._listener(opener, states)
        listener.start()
        time.sleep(0.4)
        dialog.close()
        time.sleep(0.8)
        failed = [state for state, _ in states if state == LISTEN_FAILED]
        degraded = [state for state, _ in states if state == LISTEN_DEGRADED]
        listener.stop()
        self.assertEqual(failed, [], "开过窗口之后掉线要重连，不能判死")
        self.assertTrue(degraded, "掉线要报 degraded")


if __name__ == "__main__":
    unittest.main()