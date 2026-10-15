"""摘要的进度显示必须诚实。

## 为什么不能用百分比进度条

网关任务是"提交 → 轮询"的异步 job：服务端在让模型写完之前不会送出任何
东西。所以商户手里**没有**可换算成百分比的信息（JYI 直调链路是 SSE
token 流式，会一段段冒出来；网关链路是逐批完成后一段段冒出来 —— 都是
增量，但都不是百分比）。

常见但错误的做法：画一条会自己往前走的进度条，日志里写着「模型正在思考」。
它给出的是**结束时间**，不是进度。商户盯着它走到 80% 而请求其实才刚开始，
或者已经失败而条子还在走。界面在骗人。

这里用的是：

* **不确定进度条**（Tk 的 indeterminate）—— 它左右往返，明确表示「不知道还剩多少」
* **已等待 N 秒** —— 真实秒数，让商户知道程序没卡死

## 秒数是唯一可验证的部分

进度条不可验证，秒数可以。所以测试盯的是秒数：它必须**真的在走**，
而且必须在结束时**停下来**——一个还在走的进度条会被理解成「还在处理」。

另外几项盯的是「不许退回假进度」和「不许忘了停」。
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

MERCHANT_UI = ROOT / "src/pyweixin/merchant_ui.py"


class NoFakeProgressTests(unittest.TestCase):
    """不许出现会自己往前走的确定性进度条。"""

    def setUp(self):
        self.tree = ast.parse(MERCHANT_UI.read_text(encoding="utf-8"))

    def _summary_function(self):
        # 进度条现在画在第 7 步分析结果页里（以前叫 _build_summary_card，
        # 页面流程改版时改名了，断言的属性还在同一处）。
        return next(n for n in ast.walk(self.tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_page_analysis_results")

    def test_the_bar_is_indeterminate(self):
        """determinate + maximum=100 就是假的百分比。"""
        card = ast.dump(self._summary_function())
        self.assertIn("indeterminate", card,
                      "摘要进度条必须是不确定模式 —— 单次请求没有可换算的百分比")
        self.assertNotIn("maximum=100", card,
                         "摘要没有真进度，给 maximum=100 就是编造进度")

    def test_it_shows_elapsed_seconds(self):
        """秒数是这段等待里唯一可验证的信息。"""
        card = ast.dump(self._summary_function())
        self.assertIn("_summary_elapsed", card)

    def test_the_timer_uses_the_real_clock(self):
        """必须用真实时钟，不能是自己累加的计数。"""
        source = MERCHANT_UI.read_text(encoding="utf-8")
        tick = next(n for n in self.tree.body if isinstance(n, ast.ClassDef)
                    for n in n.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_summary_tick")
        body = ast.dump(tick)
        self.assertIn("monotonic", body,
                      "已等待秒数必须来自真实时钟，自己累加会与真实耗时脱节")

    def test_the_tick_is_rescheduled(self):
        """不重新调度，秒数只会停在 0。"""
        tick = next(n for n in self.tree.body if isinstance(n, ast.ClassDef)
                    for n in n.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_summary_tick")
        self.assertIn("after", ast.dump(tick))


class TheBarAlwaysStopsTests(unittest.TestCase):
    """进度条必须停下来。还在转的进度条会被读成「还在处理」。"""

    def setUp(self):
        self.tree = ast.parse(MERCHANT_UI.read_text(encoding="utf-8"))
        self.finished = next(
            n for c in self.tree.body if isinstance(c, ast.ClassDef) for n in c.body
            if isinstance(n, ast.FunctionDef) and n.name == "_summary_finished")

    def test_finishing_stops_the_bar(self):
        self.assertIn("_summary_bar", ast.dump(self.finished))
        self.assertIn("stop", ast.dump(self.finished),
                      "结束后必须 stop()，否则进度条永远在转")

    def test_finishing_clears_the_elapsed_label(self):
        self.assertIn("_summary_elapsed", ast.dump(self.finished),
                      "结束后要清掉秒数，否则商户会以为还在等")

    def test_running_is_reset_so_the_button_works_again(self):
        """否则商户第二次点按钮没反应，且他不知道原因。"""
        self.assertIn("_summary_running", ast.dump(self.finished))


class OffThreadTests(unittest.TestCase):
    """请求绝不能在主线程跑 —— 那会把整个界面冻住。"""

    def setUp(self):
        self.tree = ast.parse(MERCHANT_UI.read_text(encoding="utf-8"))
        self.on_summarize = next(
            n for c in self.tree.body if isinstance(c, ast.ClassDef) for n in c.body
            if isinstance(n, ast.FunctionDef) and n.name == "_on_summarize")

    def test_the_call_runs_in_a_thread(self):
        body = ast.dump(self.on_summarize)
        self.assertIn("Thread", body)
        self.assertIn("daemon", body)

    def test_the_ui_is_updated_back_on_the_main_thread(self):
        """后台线程不能碰 Tk。

        这条以前断言的是「必须 after(0, ...)」—— 而那正是错的：
        工作线程调 self.root.after 会抛 RuntimeError，而且因为
        它不是 Tk 回调，**静默无输出**。现在改成断言走 _post。
        """
        body = ast.dump(self.on_summarize)
        self.assertIn("_post", body,
                      "必须用 _post 把结果交给主线程，不能在后台线程里碰 Tk")
        self.assertNotIn("self.root.after", body)


if __name__ == "__main__":
    unittest.main()