"""商户看得见的症状 → 背后的缺陷 → 哪条断言钉住它。

## 为什么是脚本，不是文档

这十八轮里挖到的问题，绝大多数不是功能错，而是**界面在说谎或装死**：

* 说假话 —— 进度条写「准备中」（其实已完成）、「剩余分析额度：剩余分析额度」
* 装死 —— 点了没反应、失败消息消失、控件被挤到窗口外
* 假装 —— 按钮亮着但按了没用、数据来源标错、拖小窗口功能直接消失

商户看得见这些，但**说不出口**。他只会觉得"这软件不好用"。

写成文档，两轮之后就过期了。写成脚本，它每次都跟着代码一起变 ——
而且能把"**哪些还没护住**"直接列出来，那才是下一步该做的地方。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 症状 →（说明，用来核对这条护栏还对不对）。断言关键字是"或"关系。
GUARDS: list[tuple[str, str, tuple[str, ...]]] = [
    ("说假话", "读完了进度条还写「准备中」",
     ("读完后进度区写「已完成」", "test_after_reading_the_page_does_not_say_ready")),
    ("说假话", "「剩余分析额度：剩余分析额度：128」",
     ("额度行不重复标签", "test_the_headline_is_not_wrapped_in_its_own_label")),
    ("说假话", "「微信：微信正常」标签重复",
     ("微信状态行不写", "test_the_wechat_status_line_is_not_prefixed_twice")),
    ("说假话", "同一屏重复提示同一件事",
     ("模型不可用只提示一次", "test_read_but_not_stored_says_so")),
    ("说假话", "把真实消息说成演示数据",
     ("模拟数据源时会明说", "test_a_demo_source_says_so_explicitly")),
    ("装死", "后台任务失败后界面一言不发",
     ("test_a_failing_background_task_reaches_the_interface",
      "test_every_background_task_has_a_message_of_its_own")),
    ("装死", "第 4 步整理失败，消息无处可放",
     ("test_the_analyze_page_has_a_place_to_show_failures",
      "test_when_there_is_nowhere_it_still_tells_the_user")),
    ("装死", "点了「开始智能整理」没反应",
     ("未登录时「开始智能整理」不假装可用",
      "test_the_analyze_button_reflects_whether_it_can_be_used")),
    ("装死", "读完没有下一步", ("读完出现「下一步」", "test_the_next_button_only_appears")),
    ("装死", "缩窗口后控件直接消失",
     ("首页所有控件都在窗口内",
      "test_the_listen_button_survives_the_smallest_window")),
    ("装死", "读了 35 条却只入库 0 条，商户不知道",
     ("test_read_but_not_stored_says_so", "test_the_two_are_not_the_same_message")),
    ("假装", "维护按钮和主按钮一样重", ("维护按钮都比主按钮弱",)),
    ("假装", "不会发送时摆着发送开关",
     ("不会发送时不显示发送开关", "暂停后「恢复发送」仍在")),
    ("假装", "退出登录后看不出数据还在",
     ("退出后首页说清楚本地订单还在", "test_logout_says_the_local_data_is_kept")),
    ("假装", "退出后顶栏仍写「账号：未登录」",
     ("登录后账号条写明是谁", "test_the_window_never_assigns_its_own_session")),
    ("假装", "能登录但永远登不出去",
     ("退出前会先确认", "test_detach_notifies_the_server")),
    ("假装", "订单超量却不提示",
     ("test_it_states_how_many_orders_there_are",
      "test_it_no_longer_silently_drops_orders_past_a_limit")),
    ("假装", "长内容被截断却看不出来",
     ("过长时带省略号", "test_long_text_is_marked_as_truncated")),
    ("假装", "两遍遍历结果不一致却没人知道",
     ("test_two_passes_over_one_list_agree",)),
]


def scan() -> dict[str, list[tuple[Path, int]]]:
    """在测试与检查脚本里找断言关键字。"""
    hits: dict[str, list[tuple[Path, int]]] = {}
    sources = list((ROOT / "tests").glob("test_*.py"))
    sources.append(ROOT / "examples" / "merchant_ui_check.py")
    for path in sources:
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for needle in ALL_NEEDLES:
                if needle in line:
                    hits.setdefault(needle, []).append((path, number))
    return hits


ALL_NEEDLES = tuple({n for _cat, _desc, needles in GUARDS for n in needles})


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    hits = scan()
    print("=" * 74)
    print("商户看得见的症状 → 缺陷 → 哪条断言钉住它")
    print("=" * 74)
    by_category: dict[str, list] = {}
    for category, symptom, needles in GUARDS:
        by_category.setdefault(category, []).append((symptom, needles))

    uncovered: list[str] = []
    total = 0
    for category, rows in by_category.items():
        print(f"\n【{category}】")
        for symptom, needles in rows:
            total += 1
            places = []
            for needle in needles:
                for path, number in hits.get(needle, []):
                    places.append(f"{path.name}:{number}")
            uniq = sorted(set(places))
            mark = "已护住" if uniq else "★ 没有断言"
            if not uniq:
                uncovered.append(f"{category}／{symptom}")
            print(f"  {mark}  {symptom}")
            for place in uniq[:3]:
                print(f"            {place}")
            if len(uniq) > 3:
                print(f"            ……另有 {len(uniq) - 3} 处")

    print("\n" + "=" * 74)
    print(f"共 {total} 条症状，已护住 {total - len(uncovered)} 条")
    if uncovered:
        print("\n还没有断言护住的：")
        for item in uncovered:
            print(f"  · {item}")
        print("\n这些是下一步该做的地方 —— 商户看得见，但一改就可能悄悄坏掉。")
    else:
        print("每一条症状都有断言护着。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())