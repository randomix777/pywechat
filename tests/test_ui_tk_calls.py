"""``MerchantWindow`` 不是 Tk 控件 —— 定时器必须挂在 ``self.root`` 上。

## 出过什么事

摘要的「已等待 N 秒」停在 0 秒不动。根因不是计时逻辑，是**调用了一个不存在的方法**：

``MerchantWindow`` 是个普通 Python 类，它自己持有 ``self.root = tk.Tk()``，
**本身没有 ``after``**。全文件原本 4 处都写 ``self.root.after(...)``，
只有我新加的摘要代码写成了 ``self.after(...)``：

* ``self.after(500, self._summary_tick)`` → ``AttributeError``
  → 计时器设完「0 秒」就死掉，之后再不更新。
* ``self.after(0, lambda: self._summary_finished(...))`` → ``AttributeError``
  → **``_summary_finished`` 永远不执行**：界面卡在「正在整理……」，
  ``_summary_running`` 永远为 True，按钮从此再也点不动。

第二个比第一个严重得多：商户点了按钮，界面一直显示在处理，
既没有结果也没有报错，而且**再也点不了第二次**。

## 为什么单测没抓到

``after`` 是 Tk 的方法，单测里没有真窗口，那一行根本不会被执行到。
只有真窗口 + 真点击才会走到。

所以这里做**静态检查**：本文件里所有 ``after(`` 调用都必须是 ``self.root.after(``。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

MERCHANT_UI = ROOT / "src/pyweixin/merchant_ui.py"


class TkCallsAreAnchoredTests(unittest.TestCase):
    def setUp(self):
        self.source = MERCHANT_UI.read_text(encoding="utf-8")
        self.lines = self.source.splitlines()

    def test_the_class_is_not_a_widget(self):
        """前提：这个类不是 Tk 控件。哪天它改成继承 tk.Tk，这条会提醒复核。"""
        tree = ast.parse(self.source)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "MerchantWindow")
        self.assertEqual([ast.unparse(b) for b in cls.bases], [],
                         "MerchantWindow 变成 Tk 子类了 —— 那么 self.after 可用，"
                         "但请先确认所有定时器都仍然挂在同一个对象上")
        self.assertIn("self.root = tk.Tk()", self.source)

    def test_every_after_call_goes_through_root(self):
        """本文件里不许出现裸的 ``self.after(``。"""
        offenders = []
        for index, line in enumerate(self.lines, 1):
            if re.search(r"(?<!root)self\.after\(", line):
                offenders.append(f"L{index}: {line.strip()[:70]}")
        self.assertEqual(offenders, [],
                         f"\u8fd9\u4e9b\u5b9a\u65f6\u5668\u6302\u9519\u4e86\u5bf9\u8c61"
                         f"\uff08\u5e94\u8be5\u662f self.root.after\uff09\uff1a{offenders}")

    def test_the_summary_timer_reschedules_through_root(self):
        """具体盯住摘要计时器 —— 它就是出问题的那一处。"""
        tick = next(n for n in ast.walk(ast.parse(self.source))
                    if isinstance(n, ast.FunctionDef) and n.name == "_summary_tick")
        self.assertIn("self.root.after", ast.unparse(tick))

    def test_finishing_goes_through_the_main_thread(self):
        """结果必须由主线程接手。

        这条以前断言的是「_on_summarize 里有 self.root.after」——
        而那正是错的写法：它让工作线程直接碰了 Tk，
        抛出 RuntimeError 而且**静默**。现在改成断言走 _post。
        """
        on_summarize = next(n for n in ast.walk(ast.parse(self.source))
                            if isinstance(n, ast.FunctionDef) and n.name == "_on_summarize")
        body = ast.unparse(on_summarize)
        self.assertIn("self._post", body,
                      "后台线程只能把结果交给 _post")
        # _post 送出的 kind 必须有主线程处理器，否则事件被丢掉
        handlers = {n.name for n in ast.walk(ast.parse(self.source))
                    if isinstance(n, ast.FunctionDef)}
        self.assertIn("_on_summary", handlers)
        self.assertIn("_summary_finished", handlers)


class NoTkCallsFromWorkerThreadsTests(unittest.TestCase):
    """**后台线程绝不能碰 Tk。**

    ## 出过什么事

    摘要的工作线程里写的是 ``self.root.after(0, lambda: ...)``。它抛：

        RuntimeError: main thread is not in main loop

    但这个异常抛在**工作线程**里，不是 Tk 回调里 —— 所以
    ``report_callback_exception`` 抓不到，stderr 一片安静。

    后果是所有点击都没反应：

    1. 第一次点 → 显示「正在整理……」
    2. 线程结束 → ``after`` 抛错 → 静默吞掉
    3. ``_summary_finished`` 永远不执行 → ``_summary_running`` 永远是 True
    4. 之后每次点击都被 ``if self._summary_running`` 挡掉

    ## 正确的做法

    这个文件本来就有跨线程机制：``_post()`` 往队列放事件，``_drain()``
    在**主线程**取出来派发。后台线程只许用 ``_post``。
    """

    def setUp(self):
        self.source = MERCHANT_UI.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def _thread_targets(self):
        """找出所有被 Thread 当作 target 的嵌套函数。"""
        targets = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and "Thread" in ast.unparse(node.func):
                for kw in node.keywords:
                    if kw.arg == "target" and isinstance(kw.value, ast.Name):
                        name = kw.value.id
                        for inner in ast.walk(self.tree):
                            if isinstance(inner, ast.FunctionDef) and inner.name == name:
                                targets.append(inner)
        return targets

    def test_thread_targets_do_not_touch_tk(self):
        offenders = []
        for target in self._thread_targets():
            body = ast.unparse(target)
            if "self.root.after" in body or "self.after(" in body:
                offenders.append(target.name)
        self.assertEqual(offenders, [],
                         f"这些后台线程直接碰了 Tk（应该用 self._post）：{offenders}")

    def test_the_summary_worker_posts_instead_of_calling_after(self):
        """具体盯住摘要那个线程 —— 它就是出问题的那一处。"""
        target = next(t for t in self._thread_targets() if t.name == "work")
        body = ast.unparse(target)
        self.assertIn("self._post", body,
                      "后台线程必须把结果交给 _post，由主线程派发")
        self.assertNotIn("after", body)

    def test_there_is_a_main_thread_handler(self):
        """_post 送出的 kind 必须有对应的 _on_<kind>，否则事件被丢掉。"""
        posted = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("._post"):
                if node.args and isinstance(node.args[0], ast.Constant):
                    posted.add(node.args[0].value)
        handlers = {n.name for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)}
        missing = [k for k in posted if f"_on_{k}" not in handlers]
        self.assertEqual(missing, [],
                         f"这些事件没有主线程处理器，会被丢掉：{missing}")






class FieldsAreInitialisedTests(unittest.TestCase):
    """摘要用到的字段必须在 __init__ 里先建好。

    页面会因导航重建，重建时如果字段不存在，回调一进来就 AttributeError。
    """

    def setUp(self):
        self.source = MERCHANT_UI.read_text(encoding="utf-8")
        tree = ast.parse(self.source)
        init = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        self.init_source = ast.unparse(init)

    def test_every_summary_field_has_a_default(self):
        for field in ("_summary_running", "_summary_text", "_summary_status",
                      "_summary_bar", "_summary_elapsed", "_summary_started",
                      "_summary_elapsed_label"):
            self.assertIn(field, self.init_source,
                          f"{field} \u6ca1\u6709\u521d\u59cb\u503c\uff0c\u9875\u9762\u91cd\u5efa\u540e\u4f1a AttributeError")


class AnalysisCancelTests(unittest.TestCase):
    """取消分析：按钮只在运行时出现，置位后线程在批次间隙停下。

    出过什么事：分析一旦开始只能干等，reduce 合并不上时只能等超时。
    取消键置位一个 Event，后台线程每个批次/轮询前检查 —— 不 join、
    不碰 Tk（还是走 _post 回主线程）。
    """

    def setUp(self):
        self.source = MERCHANT_UI.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def _function(self, name):
        return next(n for n in ast.walk(self.tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    def test_the_worker_receives_the_cancel_flag(self):
        body = ast.unparse(self._function("_on_analyze"))
        self.assertIn("is_cancelled", body,
                      "取消标记必须交到 run_summary 手里，否则按钮摆设")

    def test_the_results_page_has_a_cancel_button(self):
        body = ast.unparse(self._function("_page_analysis_results"))
        self.assertIn("analysis_cancel_button", body)

    def test_cancel_only_sets_a_flag(self):
        """取消处理器只许置位：join 会卡住主线程，碰 Tk 会跨线程。"""
        body = ast.unparse(self._function("_on_analysis_cancel"))
        self.assertIn(".set()", body)
        self.assertNotIn("join", body)
        self.assertNotIn("Thread", body)

    def test_finishing_clears_the_cancel_flag(self):
        body = ast.unparse(self._function("_on_analysis_finished"))
        self.assertIn("_analysis_cancel", body,
                      "不清理的话，下一次分析会带着上次的取消标记直接停掉")

    def test_the_cancel_button_hides_when_idle(self):
        body = ast.unparse(self._function("_render_analysis_results"))
        self.assertIn("analysis_cancel_button", body)
        self.assertIn("pack_forget", body,
                      "平时摆着会让人以为随时能停，没跑时点它又没反应 —— 等于骗人")


if __name__ == "__main__":
    unittest.main()