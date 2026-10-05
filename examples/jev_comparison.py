"""JEV 与现有模型的真实对比实验。

## 为什么做这个

JEV 是 TypeSafe 的第一个 System One **决策模型**。它宣称的核心能力是
**calibrated probabilities** —— 知道自己有多确定。

而本项目这一整轮的核心问题恰恰是：**不确定就别编一个 sku 出来**。
现在这条约束只靠提示词的一句话撑着，没有验证过它真的守得住。

所以用真实场景比一比，而不是靠文档判断：

1. 能不能稳定给出 ``said`` / ``brand`` / ``category``
2. 遇到「那个上次买的」这种模糊话时，是**说不知道**还是**编一个**
3. 实际成本差多少

## 三条已经踩到的坑

* ``/api/v1/models`` 里 ``typesafe/jev-router`` 的 ``pricing`` 是 ``-1`` ——
  按实际路由计费。**真实单价只能从响应的 ``usage.cost`` 读**，
  按目录单价预估会算错。
* 不显式设置 ``max_tokens`` 时它默认 65536，余额不够直接 **402**。
  本项目现在的调用**没有设 max_tokens**，接上去第一天就会撞墙。
* 它返回的是**自然语言**（「订单信息：张姐，明天下午要 3 箱红富士苹果」），
  不是 JSON。契约要的字段一个都没有。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

#: 真实场景的消息。前 6 条是清楚的，后 4 条是**故意模糊的** ——
#: 模糊的那些才是分辨「会说不知道」和「会编一个」的关键。
CASES = [
    ("张姐：明天下午要 3 箱红富士苹果，约 300 元，运到山东市场", "清楚", "苹果"),
    ("李叔：李记酱油 2 瓶，下午送过来", "清楚", "酱油"),
    ("王阿姨：上次那种大瓶的牛奶再来 4 箱", "靠历史", "牛奶"),
    ("老赵：来两箱苹果，还是老规矩", "清楚", "苹果"),
    ("刘姐：红富士 3 箱，加两瓶酱油", "清楚", "两样"),
    ("陈师傅：明天早上 6 点前送到，要 5 箱苹果", "清楚", "苹果"),
    ("小周：那个上次买的", "模糊", "**不该编**"),
    ("孙经理：就老样子", "模糊", "**不该编**"),
    ("吴女士：那个黄的，还行", "模糊", "**不该编**"),
    ("郑总：有货吗？先问问", "模糊", "**不该编**"),
]


def _key() -> str:
    text = pathlib.Path(os.path.expanduser("~/.dsh/.credentials.yaml")).read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("OPENROUTER_API_KEY:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    raise RuntimeError("no OPENROUTER_API_KEY in credentials")


def call(model: str, content: str, max_tokens: int = 500, system: str = "") -> dict:
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": content}]
    body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    request = urllib.request.Request(
        OPENROUTER,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + _key(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # pragma: no cover - network
        return {"ok": False, "error": type(exc).__name__}
    message = (data.get("choices") or [{}])[0].get("message", {})
    return {"ok": True, "content": message.get("content") or "",
            "usage": data.get("usage") or {}}


def _hedged(text: str) -> bool:
    """说「不知道」的样子。"""
    return any(word in text for word in
               ("不确定", "没把握", "不知道", "说不清", "哪个", "需要确认", "请问",
                "没有指明", "未指明", "无法确定", "是不是", "？", "?"))


def main() -> int:
    from order_agent.plugin import SYSTEM_PROMPT

    print("=" * 78)
    print("JEV 决策模型 vs gpt-4o-mini —— 真实场景对比")
    print("=" * 78)

    totals = {}
    for label, model, system in (("jev", "typesafe/jev-router", ""),
                                 ("gpt", "openai/gpt-4o-mini", SYSTEM_PROMPT)):
        print(f"\n### {label}  ({model})")
        costs = []
        for message, kind, note in CASES:
            result = call(model, message, system=system)
            if not result["ok"]:
                print(f"  [{kind}] {message[:22]:<24} \u2192 {result['error']}")
                continue
            usage = result["usage"]
            cost = usage.get("cost")
            if cost is not None:
                costs.append(cost)
            content = result["content"].strip().replace("\n", " ")
            if kind == "模糊":
                mark = "\u2713\u8bf4\u4e0d\u77e5\u9053" if _hedged(content) else "\u2717\u7f16\u4e86"
            else:
                mark = "\u2713"
            print(f"  [{kind}] {message[:22]:<24} {mark:<8} \u00a5{cost if cost is not None else '?':<9}")
            print(f"       \u2192 {content[:110]}")
        totals[label] = costs
        if costs:
            print(f"  -- 单次成本 中位 \u00a5{sorted(costs)[len(costs)//2]:.6f}  合计 \u00a5{sum(costs):.6f}")

    print("\n" + "=" * 78)
    print("结论要点：")
    print("  1. 模糊消息上，JEV 是否会说「不知道」—— 上表 \u2717 处即为它编了的地方")
    print("  2. 两边成本差多少")
    print("  3. JEV 输出是自然语言，契约要的 said/brand/category 一个都没有")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())