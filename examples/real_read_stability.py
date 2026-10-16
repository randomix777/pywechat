"""同一个群连续读 N 次，找出返回 0 的规律。

## 为什么值得查

同一个「电子订货系统」（6 人）这���读出来的结果分别是：

    3 条  →  35 条  →  0 条  →  0 条

商户点一次「开始读取」，有时拿到 35 条、有时拿到 0 条。**没查清原因就没有
任何保证**。这是核心流程，不能靠"重试一下"蒙混过去。

只读，不发送，不删除任何东西。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.application import MerchantApplication  # noqa: E402
from wechat_ai_assistant.conversations import RealConversationSource  # noqa: E402
from wechat_ai_assistant.history_read import HistoryRange, ReadMode  # noqa: E402
from wechat_ai_assistant.merchant_app import _force_utf8_stdout  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402
from wechat_ai_assistant.WeChatAuto import Messages  # noqa: E402
from wechat_ai_assistant.wechat_state import RealWeChatProbe  # noqa: E402
from wechat_ai_assistant import diagnostics  # noqa: E402

TARGET = "电子订货系统"
ROUNDS = 5
LIMIT = 50


def restore_wechat() -> bool:
    """把微信窗口从托盘恢复出来。收在托盘就读不到内容。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    GetWindowTextW = user32.GetWindowTextW
    GetClassNameW = user32.GetClassNameW
    IsWindowVisible = user32.IsWindowVisible
    ShowWindow = user32.ShowWindow
    GetWindowThreadProcessId = user32.GetWindowThreadProcessId
    SW_RESTORE = 9

    hit: list = []

    def proc(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(256)
        GetClassNameW(hwnd, cls, 256)
        title = ctypes.create_unicode_buffer(512)
        GetWindowTextW(hwnd, title, 512)
        if cls.value.startswith("Qt") and title.value in ("WeChat", "微信", "Weixin"):
            hit.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(proc), 0)
    for hwnd in hit:
        ShowWindow(hwnd, SW_RESTORE)
    return bool(hit)


def main() -> int:
    _force_utf8_stdout()
    print("① 尝试把微信窗口从托盘恢复")
    if restore_wechat():
        print("   已请求恢复，等 2 秒")
        time.sleep(2)

    status = RealWeChatProbe().probe()
    print(f"② 微信：{status.state}　{status.detail}")
    if status.state != "logged_in":
        print("   微信窗口还拿不到，无法做稳定性实验。请把微信窗口打开再跑。")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pyw-stab-"))
    diagnostics.install(tmp)
    diagnostics.quiet_noisy_modules()
    store = OrderStore(str(tmp / "orders.db"))
    tenant = store.create_merchant("读取稳定性")["merchant_id"]
    app = MerchantApplication(
        data_dir=tmp, orders=store, tenant=tenant, probe=None, launcher=None,
        history_reader_fn=Messages.dump_chat_history, simulated=False,
    )
    app.conversation_source = RealConversationSource(ui_lock=app.ui_lock)
    app.wizard.conversation_source = app.conversation_source

    found = app.wizard.refresh_conversations()
    target = next((c for c in found if c.name == TARGET), None)
    if target is None:
        print(f"   没找到「{TARGET}」")
        app.close(); store.close(); shutil.rmtree(tmp, ignore_errors=True)
        return 1
    app.wizard.choose_history_conversations([target])
    print(f"③ 目标：{target.disambiguation}\n")

    print(f"{'轮次':<6}{'读��':<8}{'入库':<8}{'耗时':<9}{'进度次数':<10}{'时间范围'}")
    rows = []
    for index in range(1, ROUNDS + 1):
        # 每轮用全新的库，才知道这一轮**真的**读到了什么，而不是上一轮的残留。
        round_store = OrderStore(str(tmp / f"round{index}.db"))
        round_tenant = round_store.create_merchant(f"第{index}轮")["merchant_id"]
        round_app = MerchantApplication(
            data_dir=tmp, orders=round_store, tenant=round_tenant,
            probe=None, launcher=None,
            conversation_source=app.conversation_source,
            history_reader_fn=Messages.dump_chat_history, simulated=False,
        )
        round_app.wizard.choose_history_conversations([target])

        ticks = {"count": 0, "max": 0}

        def on_progress(progress, _ticks=ticks):
            _ticks["count"] += 1
            _ticks["max"] = max(_ticks["max"], int(getattr(progress, "messages_read", 0)))

        started = time.time()
        result = round_app.wizard.read_history(
            range_kind=HistoryRange.LAST_WEEK, max_messages=LIMIT,
            mode=ReadMode.RECENT, on_progress=on_progress,
        )
        elapsed = time.time() - started
        imported = round_store.db.execute(
            "SELECT COUNT(*) c FROM messages WHERE merchant_id=?", (round_tenant,)
        ).fetchone()["c"]
        read = sum(int(getattr(i, "message_count", 0) or 0) for i in result.coverage)
        rows.append((index, read, imported, elapsed, ticks["max"], result.actual_range_text()))
        print(f"{index:<6}{read:<8}{imported:<8}{elapsed:<8.1f}s"
              f"{ticks['max']:<10}{result.actual_range_text()}")
        for closer in (round_app.merger, round_app.cursor, round_app.agent_store):
            try:
                closer.close()
            except Exception:
                pass
        round_app.close()
        round_store.close()
        time.sleep(1.5)

    print("\n④ 规律")
    counts = [r[1] for r in rows]
    if len(set(counts)) == 1:
        print(f"   稳定：每次都是 {counts[0]} 条")
    else:
        print(f"   不稳定：读到 {counts}")
        zeros = [r[0] for r in rows if r[1] == 0]
        if zeros:
            print(f"   返回 0 的轮次：{zeros}")
            for index, read, _imp, elapsed, peak, _range in rows:
                if index in zeros:
                    print(f"     第{index}轮：耗时 {elapsed:.1f}s，但进度最高只到 {peak} 条")
            for index, read, _imp, elapsed, peak, _range in rows:
                if index not in zeros:
                    print(f"     第{index}轮：读到 {read} 条，耗时 {elapsed:.1f}s，"
                          f"进度最高 {peak} 条")

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