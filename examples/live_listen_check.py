"""实时监听的**持续运行**验证：证明合并器会按时自己触发。

与单元测试的区别：这里不起线程跑 `flush()`，而是让 :class:`LiveListener`
在自己的循环里跑，用真实时钟等静默窗口真的过去，然后断言它自己结算了。

    python examples\live_listen_check.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _name in ("business_agent", "order_agent"):
    _src = ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from wechat_ai_assistant.agent import AgentStore  # noqa: E402
from wechat_ai_assistant.conversations import Conversation, ConversationKind  # noqa: E402
from wechat_ai_assistant.merging import MergeSettings, MessageMerger  # noqa: E402
from wechat_ai_assistant.live_listen import LISTEN_RUNNING, LiveListener  # noqa: E402
from wechat_ai_assistant.reliability import MessageDirection, MessageEvent, MessageKind  # noqa: E402

NOW = datetime.now(timezone.utc)
GROUP = "监听验证群"


class FakeDialog:
    """假装窗口一直可用，用来验证「不重开窗口」。"""

    def __init__(self):
        self.closed = False

    def exists(self, timeout: float = 0.1) -> bool:
        return not self.closed

    def close(self) -> None:
        self.closed = True


class ScriptedCollector:
    """按脚本产出消息，可重复调用（模拟持续监听）。"""

    def __init__(self, script: list[list[dict]]):
        self.script = script
        self.calls = 0

    def __call__(self, **kwargs) -> dict:
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        events = [
            MessageEvent(
                event_id=item["event_id"],
                stable_id=item["event_id"],
                chat_id=GROUP,
                account_id="default",
                sender=item.get("sender", "张姐"),
                content=item["content"],
                direction=item.get("direction", MessageDirection.INCOMING),
                kind=MessageKind.TEXT,
                observed_at=item["at"],
            )
            for item in self.script[index]
        ]
        return {"events": [event.to_dict() for event in events], "count": len(events)}


class FakePipeline:
    """记录被解析的代表消息。"""

    def __init__(self):
        self.processed: list[str] = []

    def process(self, message_id, source="live", on_progress=None):
        self.processed.append(message_id)
        kind = type("O", (), {"order_id": "o_" + uuid.uuid4().hex[:8], "needs_review": False})()
        return kind


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    workdir = Path(tempfile.mkdtemp(prefix="listen-"))
    store = AgentStore(str(workdir / "agent.db"))
    merger = MessageMerger(str(workdir / "merge.db"), MergeSettings(silence_window=1.0, max_wait=3.0))
    pipeline = FakePipeline()

    base = time.time()
    # 第一轮：启动基线（窗口里已有的消息，不能当成新消息）
    baseline = [{"event_id": "old-1", "content": "昨天的旧消息", "at": base}]
    # 第二轮：客户连发三条
    burst = [
        {"event_id": "e1", "content": "苹果两箱", "at": base + 1},
        {"event_id": "e2", "content": "再加一箱", "at": base + 1.2},
        {"event_id": "e3", "content": "明天送", "at": base + 1.4},
    ]
    # 第三轮：空
    empty: list[dict] = []
    # 第四轮：自己发的回执，必须被跳过
    own = [{"event_id": "own-1", "content": "好的，已记单：苹果 3箱（直接回复即可修改）",
            "at": base + 5, "direction": MessageDirection.OUTGOING, "sender": "我"}]

    collector = ScriptedCollector([baseline, burst, empty, burst, own, empty, empty, empty])
    dialog = FakeDialog()
    opens: list[int] = []

    def opener(**kwargs):
        opens.append(1)
        return dialog

    listener = LiveListener(
        conversation=Conversation(name=GROUP, kind=ConversationKind.GROUP, member_count=128),
        store=store, merger=merger, pipeline=pipeline,
        dialog_opener=opener, listener=collector, poll_seconds=0.3,
    )

    print("① 启动持续监听（静默窗口 1.0s，最大等待 3.0s）")
    listener.start()
    deadline = time.time() + 12
    while time.time() < deadline and not pipeline.processed:
        time.sleep(0.2)
    time.sleep(1.5)   # 再多跑几轮，确认不是只触发一次
    listener.stop()

    stats = listener.stats
    print(f"② 窗口打开次数：{stats.window_opens}（复用 {stats.window_reuses} 次）")
    print(f"   采集调用 {collector.calls} 轮")
    print(f"③ 统计：{stats.describe()}")
    print(f"④ 解析的代表消息：{pipeline.processed}")
    print(f"⑤ 合并组：{merger.stats()}")
    if listener.gaps:
        print(f"⑥ 缺口：{listener.gap_report()}")

    checks = [
        ("窗口只打开一次（不周期性重开）", stats.window_opens == 1),
        ("窗口被复用而不是重开", stats.window_reuses > 0),
        ("启动基线里的旧消息没有被当成新消息", stats.events_stored == 3),
        ("合并器在持续运行中自己结算了", len(pipeline.processed) >= 1),
        ("只解析代表消息，不逐条解析", len(pipeline.processed) == 1),
        ("解析的是第一条（代表）", pipeline.processed and pipeline.processed[0] == "e1"),
        ("自己的回执被跳过", stats.events_skipped_own >= 1),
        ("重复投递被去重", stats.events_skipped_duplicate >= 1),
        ("缺口被显式记录", bool(listener.gaps)),
    ]
    print()
    failed = []
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            failed.append(name)
    print()
    print(f"{len(checks) - len(failed)}/{len(checks)} 项通过")
    merger.close()
    store.close()
    shutil.rmtree(workdir, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
