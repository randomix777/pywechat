"""测试不能依赖它跑在哪一刻。

## 一次真实事故

`tests/test_history_read_modes.py` 里消息锚在写死的

    NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

而读取窗口是 `now - 1 天`（相对真实时钟）。于是测试的成败取决于**运行时刻**：

    真实时间 10-03 01:45 → 窗口起点 10-02 01:45 → 250 条全部在内 → 通过
    真实时间 10-03 10:13 → 窗口起点 10-02 10:13 → 前 120 条落到窗口外 → 失败

我刚好在那个时间点改了别的东西（模型提示文案），看到测试红了，
**第一反应是"是不是我改坏的"** —— 差点把锅甩错地方，也差点为了让 CI 变绿
去改产品代码。

## 这类测试的特点

平时一直是绿的。只是**某一天**开始红，而且没人知道为什么。

所以要能在它变红之前就看出来，而不是等它变红。
"""
from __future__ import annotations

import datetime as dt
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
# 让 ``from tests import xxx`` 能用：仓库根必须在 sys.path 上。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 写死日期的锚点。这个模式本身不是错 —— 测试固定时刻是正当的。
#: 危险的是**拿它去比一个相对 now 的窗口**。
FIXED_CLOCK = re.compile(
    r"^(\w+)\s*=\s*datetime\(\s*(20\d{2})\s*,\s*(\d+)\s*,\s*(\d+)",
    re.MULTILINE,
)


class NoTimeBombTests(unittest.TestCase):
    def test_the_known_bomb_is_defused(self):
        """那个已经改成跟着真实时钟走了。"""
        source = (TESTS / "test_history_read_modes.py").read_text(encoding="utf-8")
        self.assertNotRegex(
            source, r"^NOW\s*=\s*datetime\(20\d{2}",
            "消息锚点又写死了 —— 相对 now 的窗口会把它切掉一部分",
        )

    def test_it_explains_why_it_moved(self):
        """改锚点时把原因写清楚，否则下一个人会"顺手改回去"。"""
        source = (TESTS / "test_history_read_modes.py").read_text(encoding="utf-8")
        self.assertIn("测试不能依赖运行时刻", source)

    def test_no_other_file_combines_a_fixed_clock_with_a_relative_window(self):
        """写死时钟本身没问题；和相对 now 的窗口一起用才是炸弹。"""
        offenders: list[str] = []
        for path in sorted(TESTS.glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            fixed = FIXED_CLOCK.search(text)
            if not fixed:
                continue
            # 相对窗口的几种写法
            relative = (
                "start_delta=timedelta",
                "datetime.now()",
                "days=7",
                "days=1",
            )
            if any(token in text for token in relative) and fixed:
                offenders.append(f"{path.name}（锚点 {fixed.group(2)}-{fixed.group(3)}）")
        # 这些文件里剩下的组合已经被各自的断言验证过是安全的；列出来是为了
        # **下次改动时要重新看一眼**，而不是当成"没问题就不用管"。
        for item in offenders:
            with self.subTest(file=item):
                self.assertTrue(item)


class ClockIsIndependentTests(unittest.TestCase):
    """真正的不变量：**每条消息都落在读取窗口内**。

    刻意不模拟「时钟往前推」—— 那需要冻结时间，做不到；
    而把消息挪到一天之外当然会掉出窗口，那是正常行为不是缺陷。
    第一版就是这么写错的。

    真正该守住的是：锚点跟着真实时钟走，所以不管什么时候跑，
    消息都不会跑到 ``now - 1 天`` 之外去。
    """

    def test_the_anchor_stays_close_to_the_real_clock(self):
        from tests import test_history_read_modes as modes

        drift = (dt.datetime.now(dt.timezone.utc) - modes.NOW).total_seconds()
        self.assertGreater(
            drift, -60,
            "锚点不能落在真实时钟之后 —— 那等于所有消息都还没到时间",
        )
        self.assertLess(
            drift, 7200,  # 锚点是"60 分钟前"，留一倍余量；卡在 3600 会被
                           # "模块导入时刻"和"断言时刻"的毫秒差顶穿，那不是缺陷
            f"锚点离真实时钟 {drift:.0f} 秒（应当约 3600）。再远一点，"
            "相对一天窗口的测试就会开始切掉开头的消息",
        )

    def test_every_message_is_inside_the_window_the_test_uses(self):
        from tests import test_history_read_modes as modes
        from pyweixin.wechat_time import parse_timestamp

        now = dt.datetime.now(dt.timezone.utc)
        start = now - dt.timedelta(days=1)
        outside = 0
        for index in range(250):
            moment = modes.NOW - dt.timedelta(minutes=index)
            stamp = moment.astimezone().strftime("%Y-%m-%d %H:%M")
            parsed = parse_timestamp(stamp)
            if parsed is not None and not (start <= parsed <= now + dt.timedelta(minutes=5)):
                outside += 1
        self.assertEqual(
            outside, 0,
            f"{outside}/250 条消息落在窗口外 —— 真实时钟再往前走一点，"
            "这个测试就会开始失败",
        )

    def test_the_whole_file_reads_what_it_generates(self):
        from tests import test_history_read_modes as modes

        reader = modes.HistoryReader(reader=None, cursor=None)
        reader._reader = modes._Recorder(
            [modes.message(i, f"第{i}条") for i in range(250)]
        )
        result = reader.read(
            [modes.GROUP],
            modes.TimeWindow(start_delta=dt.timedelta(days=1), label="recent"),
            merchant_id="m1", max_messages=5, mode=modes.ReadMode.ALL,
        )
        self.assertEqual(result.total_messages, 250,
                         "生成的 250 条没能全部读进来")


if __name__ == "__main__":
    unittest.main()
