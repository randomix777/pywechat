"""复现「监视器和读取抢微信窗口」，并验证修复。

上一轮锁定了根因（监视器没共用 UI 锁），但**没能复现出坏结果** ——
我自己的探针不启动监视器，所以复现不了。那样只能说"中高置信"。

这里做闭环：

    阶段一：不传锁（= 修复前的行为），监视器和读取并发 → 看会不会读坏
    阶段二：传锁（= 修复后），同样的并发 → 看还坏不坏

真实微信、同一个群、多轮。**只读，不发送。**
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.application import MerchantApplication  # noqa: E402
from wechat_ai_assistant.conversations import RealConversationSource  # noqa: E402
from wechat_ai_assistant.history_read import HistoryRange, ReadMode  # noqa: E402
from wechat_ai_assistant.merchant_app import _force_utf8_stdout  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402
from wechat_ai_assistant.WeChatAuto import Messages  # noqa: E402
from wechat_ai_assistant.wechat_state import RealWeChatProbe, WeChatMonitor  # noqa: E402

TARGET = "电子订货系统"
ROUNDS = 4
LIMIT = 50


def restore_wechat() -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    proc_t = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    GetWindowTextW = user32.GetWindowTextW
    GetClassNameW = user32.GetClassNameW
    ShowWindow = user32.ShowWindow
    hit: list = []

    def proc(hwnd, _l):
        cls = ctypes.create_unicode_buffer(256)
        GetClassNameW(hwnd, cls, 256)
        title = ctypes.create_unicode_buffer(512)
        GetWindowTextW(hwnd, title, 512)
        if cls.value.startswith("Qt") and title.value in ("WeChat", "微信", "Weixin"):
            hit.append(hwnd)
        return True

    user32.EnumWindows(proc_t(proc), 0)
    for hwnd in hit:
        ShowWindow(hwnd, 9)


def one_phase(label: str, use_lock: bool, target, probe, tmp: Path) -> list[int]:
    """跑若干轮，返回每轮读到的条数。"""
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    results: list[int] = []
    for index in range(1, ROUNDS + 1):
        round_store = OrderStore(str(tmp / f"{label[:2]}{index}.db"))
        tenant = round_store.create_merchant(f"第{index}轮")["merchant_id"]
        app = MerchantApplication(
            data_dir=tmp, orders=round_store, tenant=tenant, probe=None, launcher=None,
            history_reader_fn=Messages.dump_chat_history, simulated=False,
        )
        app.conversation_source = RealConversationSource(ui_lock=app.ui_queue)
        app.wizard.choose_history_conversations([target])

        # 关键：监视器**没有**启动界面队列那一侧（阶段一），
        # 或者用同一把锁（阶段二）。
        monitor = WeChatMonitor(probe, ui_lock=app.ui_queue if use_lock else None)
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                monitor.check_once()
                stop.wait(2.0)

        worker = threading.Thread(target=poll, daemon=True)
        worker.start()
        time.sleep(0.3)

        started = time.time()
        result = app.wizard.read_history(
            range_kind=HistoryRange.LAST_WEEK, max_messages=LIMIT, mode=ReadMode.RECENT,
        )
        elapsed = time.time() - started
        read = sum(int(getattr(i, "message_count", 0) or 0) for i in result.coverage)
        imported = round_store.db.execute(
            "SELECT COUNT(*) c FROM messages WHERE merchant_id=?", (tenant,)
        ).fetchone()["c"]
        results.append(read)
        print(f"  第{index}轮：读到 {read:3d} 条，入库 {imported:3d} 条，"
              f"耗时 {elapsed:5.1f}s")

        stop.set()
        worker.join(timeout=3)
        for closer in (app.merger, app.cursor, app.agent_store):
            try:
                closer.close()
            except Exception:
                pass
        app.close()
        round_store.close()
        time.sleep(1.0)
    return results


def main() -> int:
    _force_utf8_stdout()
    restore_wechat()
    time.sleep(2)
    probe = RealWeChatProbe()
    status = probe.probe()
    print(f"① 微信：{status.state}")
    if status.state != "logged_in":
        print("   拿不到微信窗口，无法做本实验。")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pyw-race-"))
    store = OrderStore(str(tmp / "base.db"))
    tenant = store.create_merchant("竞争实验")["merchant_id"]
    app = MerchantApplication(
        data_dir=tmp, orders=store, tenant=tenant, probe=None, launcher=None,
        history_reader_fn=Messages.dump_chat_history, simulated=False,
    )
    app.conversation_source = RealConversationSource(ui_lock=app.ui_queue)
    app.wizard.conversation_source = app.conversation_source
    found = app.wizard.refresh_conversations()
    target = next((c for c in found if c.name == TARGET), None)
    if target is None:
        print(f"   没找到「{TARGET}」")
        app.close(); store.close(); shutil.rmtree(tmp, ignore_errors=True)
        return 1
    print(f"② 目标：{target.disambiguation}")

    # 先单独读一次，拿到"正常应该是多少条"的基线。
    base_store = OrderStore(str(tmp / "base_only.db"))
    base_tenant = base_store.create_merchant("基线")["merchant_id"]
    base_app = MerchantApplication(
        data_dir=tmp, orders=base_store, tenant=base_tenant, probe=None, launcher=None,
        history_reader_fn=Messages.dump_chat_history, simulated=False,
    )
    base_app.wizard.choose_history_conversations([target])
    base_result = base_app.wizard.read_history(
        range_kind=HistoryRange.LAST_WEEK, max_messages=LIMIT, mode=ReadMode.RECENT,
    )
    baseline = sum(int(getattr(i, "message_count", 0) or 0) for i in base_result.coverage)
    print(f"③ 基线（无并发）：{baseline} 条\n")
    for closer in (base_app.merger, base_app.cursor, base_app.agent_store):
        try:
            closer.close()
        except Exception:
            pass
    base_app.close()
    base_store.close()

    bad = one_phase("阶段一：监视器不共用锁（修复前的行为）", False, target, probe, tmp)
    good = one_phase("阶段二：监视器共用锁（修复后）", True, target, probe, tmp)

    print(f"\n{'=' * 60}\n④ 结论\n{'=' * 60}")
    print(f"  基线（无并发）      : {baseline}")
    print(f"  阶段一 抢窗口       : {bad}")
    print(f"  阶段二 共用锁       : {good}")
    degraded_one = [x for x in bad if x != baseline]
    degraded_two = [x for x in good if x != baseline]
    print(f"\n  阶段一偏离基线的轮次: {len(degraded_one)}/{len(bad)}")
    print(f"  阶段二偏离基线的轮次: {len(degraded_two)}/{len(good)}")
    if degraded_one and not degraded_two:
        print("\n  → 复现成功：不共用锁会读坏，共用锁后不再发生。")
    elif not degraded_one:
        print("\n  → **没能复现出坏结果。** 阶段一也没偏离基线，")
        print("    所以这轮不能证明修复有效，只能说没有反证。")
    else:
        print("\n  → 两个阶段都有偏离。根因可能不止锁这一处，需要继续查。")

    for closer in (app.merger, app.cursor, app.agent_store):
        try:
            closer.close()
        except Exception:
            pass
    app.close()
    store.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())