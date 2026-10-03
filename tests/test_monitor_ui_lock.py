"""监视器不能在读取期间去戳微信窗口。

## 动机

`WeChatMonitor` 每 2 秒调一次 `probe.probe()`，而探测要驱动微信窗口。
它**没有**和读历史共用那把 UI 锁 —— 两个线程可能同时驱动同一个窗口。

## ⚠️ 这不是任何已知故障的修复

我一度以为这就是「同一��群有时读到 7 条、有时读到 0 条」的原因，并把它
当结论汇报了。**那是错的。** 对照实验见 `examples/real_race_repro.py`：

    基线（无并发）              : 7 条
    阶段一 监视器不共用锁（并发）: 7,7,7,7   ← 修复前的行为，也没坏
    阶段二 监视器共用锁（并发）  : 7,7,7,7

**没能复现空读。** 所以这组测试盯的是「不该有的竞争确实去掉了」，
而不是「空读修好了」。真正让读取返回 0 的原因**仍未查明**。

把未验证的因果写进注释，会让下一个���"确认过了"而不再查 —— 所以这里明确写出来。
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.reliability import UiOperationQueue  # noqa: E402
from pyweixin.wechat_state import RealWeChatProbe, WeChatMonitor  # noqa: E402


class BusyFlagTests(unittest.TestCase):
    def setUp(self):
        self.queue = UiOperationQueue(thread_name="test-busy")

    def tearDown(self):
        self.queue.close(wait=False)

    def test_idle_queue_is_not_busy(self):
        self.assertFalse(self.queue.busy)

    def test_a_running_operation_makes_it_busy(self):
        """正在跑的那一个**已经出队**了，只看 pending 会漏掉它。"""
        seen = []
        started = threading.Event()
        release = threading.Event()

        def long_task():
            started.set()
            release.wait(5)
            return "done"

        future = self.queue.submit(long_task)
        started.wait(5)
        seen.append(self.queue.busy)
        release.set()
        future.result(timeout=5)
        # 跑完之后要恢复成不忙，否则监视器会永远不检测。
        time.sleep(0.1)
        seen.append(self.queue.busy)
        self.assertEqual(seen, [True, False], f"busy 标志不对：{seen}")

    def test_queued_work_also_counts_as_busy(self):
        release = threading.Event()

        def blocker():
            release.wait(5)

        first = self.queue.submit(blocker)
        self.queue.submit(lambda: None)      # 排在后面
        time.sleep(0.15)
        self.assertTrue(self.queue.busy)
        release.set()
        first.result(timeout=5)


class MonitorSkipsWhileBusyTests(unittest.TestCase):
    """锁忙时监视器这一轮直接跳过，不碰微信窗口。"""

    def test_the_probe_is_not_called_while_the_ui_lock_is_busy(self):
        calls = []

        class SpyProbe:
            def probe(self):
                calls.append(1)
                from pyweixin.wechat_state import WeChatState, WeChatStatus

                return WeChatStatus(WeChatState.LOGGED_IN, "已登录", checked_at=0.0)

        class FakeLock:
            busy = True

        monitor = WeChatMonitor(SpyProbe(), ui_lock=FakeLock())
        status = monitor.check_once()
        self.assertEqual(calls, [], "锁正忙时仍然去驱动了微信窗口")
        # 跳过时返回上一次的结果，界面不该闪一下。
        self.assertIsNotNone(status)

    def test_it_probes_normally_when_not_busy(self):
        calls = []

        class SpyProbe:
            def probe(self):
                calls.append(1)
                from pyweixin.wechat_state import WeChatState, WeChatStatus

                return WeChatStatus(WeChatState.LOGGED_IN, "已登录", checked_at=0.0)

        class IdleLock:
            busy = False

        monitor = WeChatMonitor(SpyProbe(), ui_lock=IdleLock())
        monitor.check_once()
        self.assertEqual(len(calls), 1)

    def test_a_broken_lock_does_not_stop_the_monitor(self):
        """锁接口变了也不能让状态检测彻底停摆 —— 那比偶发空读更糟。"""

        class BrokenLock:
            @property
            def busy(self):
                raise RuntimeError("no such attribute")

        class SpyProbe:
            def probe(self):
                return RealWeChatProbe().probe()

        monitor = WeChatMonitor(SpyProbe(), ui_lock=BrokenLock())
        self.assertIsNotNone(monitor.check_once())


class WiringTests(unittest.TestCase):
    """应用必须真的把锁交给监视器 —— 忘了接就等于没修。"""

    def test_the_application_passes_the_ui_lock_to_the_monitor(self):
        source = (ROOT / "src" / "pyweixin" / "application.py").read_text(encoding="utf-8")
        start = source.index("self.monitor = WeChatMonitor(")
        block = source[start:start + 400]
        self.assertIn("ui_lock=self.ui_lock", block,
                      "监视器没拿到 UI 锁 —— 它仍会抢读历史正在用的微信窗口")

    def test_the_monitor_still_works_without_a_lock(self):
        """不传锁的老调用方不能被这次改动弄坏。"""
        calls = []

        class SpyProbe:
            def probe(self):
                calls.append(1)
                from pyweixin.wechat_state import WeChatState, WeChatStatus

                return WeChatStatus(WeChatState.LOGGED_IN, "已登录", checked_at=0.0)

        monitor = WeChatMonitor(SpyProbe())
        monitor.check_once()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()