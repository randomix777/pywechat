"""未查明的故障：读取历史偶尔返回 0 条。

## 现象

同一个 6 人群，同样的操作，不同时候读到：

    7 条  →  7 条  →  7 条  →  7 条（连跑 5 轮，稳定）
    7 条  →  35 条  →  0 条  →  0 条

「电子订货系统」实际有 7 条消息。返回 35 条的是另一个群（424 人）。
**返回 0 的那两次，原因至今没有查明。**

## 已经排除的假设

| 假设 | 实验 | 结果 |
| --- | --- | --- |
| 微信窗口收在托盘 | 恢复窗口后连跑 5 轮 | 5/5 稳定，**排除** |
| 订单助手占着前台 | 前台状态下连跑 5 轮 | 5/5 稳定，**排除** |
| 监视器与读取抢窗口 | 并发对照实验 `real_race_repro.py` | 修复前后都是 7/7/7/7，**排除** |

共享 UI 锁那个改动**本身是对的**（两个线程驱动同一个窗口本就危险），
但它**不是**这个故障的原因 —— 我一度把它当结论汇报，那是错的。

## 为什么留一个"未查明"的记录

不知道原因，就没法保证它不会再发生。更糟的是：一旦写成"已修复"，
下一个人就会停止追查，而它随时可能再出现。

商户看到的症状是：**进度条在动，最后告诉他什么都没读到**。界面已经把
"确实读到 0 条"和"读到 N 条却入库 0 条"分开说了（后者目前还没抓到实例），
但根因仍然悬着。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class UnresolvedReadIssueTests(unittest.TestCase):
    """未查明 ≠ 已修复。这条记录必须在，否则会被当成解决。"""

    def test_the_reproduction_experiment_exists(self):
        script = ROOT / "examples" / "real_race_repro.py"
        self.assertTrue(script.is_file(),
                        "对照实验脚本不见了，后来的人会失去判断依据")
        text = script.read_text(encoding="utf-8")
        self.assertIn("没能复现", text,
                      "实验脚本应当如实说明它没能复现，而不是悄悄改成绿的")

    def test_the_stability_experiment_exists(self):
        self.assertTrue((ROOT / "examples" / "real_read_stability.py").is_file())

    def test_the_ui_still_distinguishes_the_two_empty_cases(self):
        """读到 0 条 ≠ 读到 N 条却入库 0 条。商户看到的必须不一样。"""
        source = (ROOT / "src" / "pyweixin" / "merchant_ui.py").read_text(encoding="utf-8")
        self.assertIn("刚才读到了", source)
        self.assertIn("本地订单里还是空的", source)

    def test_no_comment_claims_the_lock_fixed_the_empty_reads(self):
        """把未验证的因果写进注释，是最容易骗过后人的一种谎。"""
        source = (ROOT / "src" / "pyweixin" / "wechat_state.py").read_text(encoding="utf-8")
        start = source.index("def check_once")
        block = source[start:start + 900]
        self.assertNotIn("有时读到 7 条", block,
                         "注释仍在断言'并发导致空读'——对照实验没能复现")
        self.assertIn("不是", block, "应当明确写出这不是已知故障的修复")


class HonestReportingTests(unittest.TestCase):
    """文档和注释里不能有把猜测写成结论的句子。"""

    def test_the_monitor_lock_is_not_sold_as_the_bug_fix(self):
        source = (ROOT / "src" / "pyweixin" / "application.py").read_text(encoding="utf-8")
        start = source.index("self.monitor = WeChatMonitor(")
        block = source[start:start + 500]
        self.assertIn("原因仍未查明", block)


if __name__ == "__main__":
    unittest.main()