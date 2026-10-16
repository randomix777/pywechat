"""多个线程同时读写同一个 store 时，连接不能被打乱。

## 出过什么事

`sqlite3.InterfaceError: bad parameter or other API misuse`

原因很具体：写路径都经过 `_transaction`，它会 `BEGIN IMMEDIATE`；
**读路径原本不拿锁**，而连接是**共享的一个**。于是线程 A 正在事务里
（连接处于"事务中"状态），线程 B 毫不知情地 `execute()` 一下，
sqlite3 模块的内部状态就乱了。

这不是测试造出来的问题 —— 多线程访问同一个 store 就会遇到。

## 为什么用装饰器而不是在方法体里加锁

产品代码里已经因为重新缩进出过事故。加一个 `@_guarded` 装饰器，
不改动任何方法体的缩进，就能把锁加到每一个碰连接的方法上。

`_lock` 是 RLock，所以在 `_transaction` 里再加一层包装不会自锁。
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant.product import (  # noqa: E402
    ProductStore, Conflict, IdempotencyConflict,
    QuotaExhausted, SubscriptionInactive,
)


def _run_concurrently(fns, workers=8):
    """和 test_concurrency_budget 里一样，但故意用更多线程。"""
    out: list = [None] * len(fns)
    barrier = threading.Barrier(len(fns))

    def run(i):
        barrier.wait()
        try:
            out[i] = ("ok", fns[i]())
        except Exception as exc:  # noqa: BLE001 - 这里就是要看异常类型
            out[i] = ("err", exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(fns))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return out


class SharedConnectionGuardTests(unittest.TestCase):
    def setUp(self):
        self.store = ProductStore(":memory:")
        self.account = self.store.create_account("guard-shop", "password-1234", quota_micro_usd=1000)
        self.device_id, _created = self.store._upsert_device(
            self.account["account_id"], self.account["merchant_id"], "电脑")

    def tearDown(self):
        self.store.close()

    def test_reads_and_writes_interleave_without_corrupting_the_connection(self):
        """读写交错时不能出现 sqlite 的 API 误用。"""
        jobs = [f"job-{i}" for i in range(24)]

        def submit(key):
            return self.store.create_job(
                self.account["account_id"], self.device_id, self.account["merchant_id"],
                key, key, 1, 300.0,
            )

        def read(_i=0):
            return len(self.store.list_jobs(self.account["account_id"]))

        fns = [(lambda k=k: submit(k)) if i % 2 == 0 else read
               for i, k in enumerate(jobs)]
        results = _run_concurrently(fns)

        # 业务拒绝（并发超上限、额度不足）是**正常行为**，
        # 不是数据库出错。这里只要求不出现 sqlite 自己的错误。
        expected = (Conflict, QuotaExhausted, SubscriptionInactive, IdempotencyConflict)
        bad = [r[1] for r in results
               if r[0] == "err" and not isinstance(r[1], expected)]
        self.assertEqual(bad, [], f"并发读写报错：{bad[:3]}")
        # 并且确认它确实拒绝过并发上限的请求，
        # 否则这个测试可能只是流水过，没真压到并发。
        refused = [r[1] for r in results if r[0] == "err" and isinstance(r[1], expected)]
        self.assertTrue(refused, "没有一个请求被并发上限拒绝，测试可能没真压到并发")

    def test_a_held_transaction_is_not_corrupted_by_a_concurrent_read(self):
        """最危险的一种交错：别人**正处在事务里**时来读。"""
        started = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []

        def writer():
            # 无意地穿过 ProductStore 的事务留下一个 BEGIN。
            import sqlite3
            store = self.store
            store._connection.execute("BEGIN IMMEDIATE")
            try:
                started.set()
                release.wait(timeout=5)
                store._connection.execute("COMMIT")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                try:
                    store._connection.execute("ROLLBACK")
                except Exception:
                    pass

        t = threading.Thread(target=writer)
        t.start()
        started.wait(timeout=5)

        # 这里的读必须被锁串行化，不能直接碰连接。
        readers = [lambda: self.store.get_subscription(self.account["account_id"]) for _ in range(8)]
        read_results = _run_concurrently(readers, workers=8)

        release.set()
        t.join(timeout=10)

        read_errors = [r[1] for r in read_results if r[0] == "err"]
        self.assertEqual(errors, [], f"写侧被读受打乱：{errors}")
        self.assertEqual(read_errors, [], f"读受打乱：{read_errors[:3]}")

    def test_the_guard_does_not_deadlock_inside_a_transaction(self):
        """_lock 是 RLock：在 _transaction 里再加一层包装不能自锁。"""
        done = threading.Event()
        result: list = []

        def work():
            with self.store._transaction(immediate=True):
                # 这一行内部会再次拿锁（超过 5 秒就算自锁了）
                result.append(self.store.get_subscription(self.account["account_id"]))
            done.set()

        t = threading.Thread(target=work, daemon=True)
        t.start()
        self.assertTrue(done.wait(timeout=5), "RLock 应该允许重入，否则就是自锁")
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()