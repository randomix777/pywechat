import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Thread


MODULE_PATH = Path(__file__).parents[1] / "src" / "wechat_ai_assistant" / "reliability.py"
SPEC = importlib.util.spec_from_file_location("wechat_ai_assistant_reliability_test_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ReliabilityTests(unittest.TestCase):
    def test_message_event_normalizes_and_serializes(self):
        event = MODULE.MessageEvent(
            chat_id="chat",
            content="  hello\n world ",
            sender="alice",
            direction="incoming",
            kind="text",
            stable_id="message-1",
            metadata={"source": "test"},
        )
        self.assertEqual(event.normalized_content, "hello world")
        self.assertEqual(event.identity_quality, "stable")
        restored = MODULE.MessageEvent.from_dict(event.to_dict())
        self.assertEqual(restored.to_dict(), event.to_dict())

    def test_ledger_deduplicates_stable_ids(self):
        now = [100.0]
        ledger = MODULE.MessageLedger(clock=lambda: now[0])
        first = MODULE.MessageEvent("chat", "same", stable_id="m-1")
        second = MODULE.MessageEvent("chat", "same", stable_id="m-1")
        self.assertTrue(ledger.accept(first))
        self.assertFalse(ledger.accept(second))
        self.assertEqual(len(ledger), 1)

    def test_ledger_does_not_drop_unidentified_repeated_messages(self):
        ledger = MODULE.MessageLedger()
        first = MODULE.MessageEvent("chat", "再来一箱")
        second = MODULE.MessageEvent("chat", "再来一箱")
        self.assertEqual(ledger.filter([first, second]), [first, second])

    def test_ledger_expires_entries(self):
        now = [100.0]
        ledger = MODULE.MessageLedger(ttl_seconds=10, clock=lambda: now[0])
        event = MODULE.MessageEvent("chat", "hello", stable_id="m-1")
        self.assertTrue(ledger.accept(event))
        now[0] = 111.0
        self.assertTrue(ledger.accept(event))
        self.assertEqual(len(ledger), 1)

    def test_ledger_round_trips_state(self):
        now = [100.0]
        ledger = MODULE.MessageLedger(clock=lambda: now[0])
        ledger.accept(MODULE.MessageEvent("chat", "hello", stable_id="m-1"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger.save(path)
            restored = MODULE.MessageLedger.load(path, clock=lambda: 1000.0)
            self.assertFalse(restored.accept(MODULE.MessageEvent("chat", "hello", stable_id="m-1")))

    def test_ledger_round_trips_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger = MODULE.MessageLedger(ttl_seconds=10, clock=lambda: 100.0)
            ledger.accept(MODULE.MessageEvent("chat", "hello", stable_id="m-1"))
            ledger.save(path)
            now = [1000.0]
            restored = MODULE.MessageLedger.load(path, clock=lambda: now[0])
            self.assertEqual(restored.ttl_seconds, 10)
            self.assertFalse(restored.accept(MODULE.MessageEvent("chat", "hello", stable_id="m-1")))
            now[0] = 1011.0
            self.assertTrue(restored.accept(MODULE.MessageEvent("chat", "hello", stable_id="m-1")))

    def test_ui_queue_serializes_operations(self):
        active = []
        maximum = []
        gate = Event()

        def operation(value):
            active.append(value)
            maximum.append(len(active))
            if value == 0:
                gate.wait(1)
            time.sleep(0.01)
            active.remove(value)
            return value * 2

        with MODULE.UiOperationQueue(max_pending=10) as queue:
            futures = [queue.submit(operation, value) for value in range(4)]
            gate.set()
            self.assertEqual([future.result(timeout=2) for future in futures], [0, 2, 4, 6])
            self.assertEqual(max(maximum), 1)

    def test_ui_queue_rejects_work_after_close(self):
        queue = MODULE.UiOperationQueue()
        queue.close()
        with self.assertRaises(MODULE.OperationQueueClosed):
            queue.submit(lambda: None)

    def test_retry_call_stops_after_retryable_failures(self):
        calls = []
        delays = []

        def operation():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("temporary")
            return "ok"

        result = MODULE.retry_call(operation, attempts=3, delay=0.1, sleep=delays.append)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [0.1, 0.2])
    def test_ledger_uses_stable_id_independent_of_content(self):
        ledger = MODULE.MessageLedger()
        first = MODULE.MessageEvent("chat", "before", stable_id="m-1")
        second = MODULE.MessageEvent("chat", "after", stable_id="m-1")
        self.assertTrue(ledger.accept(first))
        self.assertFalse(ledger.accept(second))

    def test_sequence_identity_requires_window_generation(self):
        ledger = MODULE.MessageLedger()
        first = MODULE.MessageEvent("chat", "same", sequence=1)
        second = MODULE.MessageEvent("chat", "same", sequence=1)
        self.assertTrue(ledger.accept(first))
        self.assertTrue(ledger.accept(second))
        ledger.clear()
        generated_first = MODULE.MessageEvent("chat", "same", sequence=1, window_generation="g-1")
        generated_second = MODULE.MessageEvent("chat", "same", sequence=1, window_generation="g-1")
        self.assertTrue(ledger.accept(generated_first))
        self.assertFalse(ledger.accept(generated_second))

    def test_runtime_identity_is_not_used_for_durable_deduplication(self):
        ledger = MODULE.MessageLedger()
        first = MODULE.MessageEvent("chat", "first", runtime_id=(1,), window_generation="g-1")
        duplicate = MODULE.MessageEvent("chat", "first", runtime_id=(1,), window_generation="g-1")
        second = MODULE.MessageEvent("chat", "second", runtime_id=(2,), window_generation="g-1")
        self.assertTrue(ledger.accept(first))
        self.assertTrue(ledger.accept(duplicate))
        self.assertTrue(ledger.accept(second))

    def test_runtime_id_round_trip_preserves_metadata(self):
        event = MODULE.MessageEvent("chat", "hello", runtime_id=(1, 2), window_generation="g-1")
        restored = MODULE.MessageEvent.from_dict(event.to_dict())
        self.assertEqual(restored.runtime_id, (1, 2))
        self.assertIsNone(event.dedup_key())
        self.assertIsNone(restored.dedup_key())

    def test_ui_queue_later_close_waits_for_requested_shutdown(self):
        queue = MODULE.UiOperationQueue()
        gate = Event()
        future = queue.submit(gate.wait)
        queue.close(wait=False)
        errors = []

        def close_queue():
            try:
                queue.close(wait=True, timeout=2)
            except BaseException as exc:
                errors.append(exc)

        closer = Thread(target=close_queue)
        closer.start()
        self.assertTrue(closer.is_alive())
        gate.set()
        future.result(timeout=2)
        closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])

    def test_ui_queue_survives_base_exception_from_operation(self):
        queue = MODULE.UiOperationQueue()

        def raises_system_exit():
            raise SystemExit("stop")

        failed = queue.submit(raises_system_exit)
        with self.assertRaises(SystemExit):
            failed.result(timeout=2)
        completed = queue.submit(lambda: "ok")
        self.assertEqual(completed.result(timeout=2), "ok")
        queue.close()


if __name__ == "__main__":
    unittest.main()
