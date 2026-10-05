"""把群里的消息汇总成一张给人看的摘要卡。

## 为什么有这个东西

这一整轮我们给模型建了一套很重的契约：意图分类、客户别名、目标订单、
每件商品的 ``said``/``brand``/``category``/``sku``/``quantity``/``unit``/``evidence``、
缺字段、歧义标记。

但真实场景里客户下单很随便——「上次那种」「那个黄的」——大部分条目**对不上
目录里的任何商品**。契约要求填满字段，模型只能编，而编出来的 sku 会让商户
照着发错货。

商户真正要的东西其实很简单：**一眼看清这个群里在说什么、要什么**。
他自己知道哪个是苹果哪个是酱油，**不需要软件替他判断**。

所以这里只做一件事：把消息整理成一段人话。不建订单、不定价、不匹配商品。

## 为什么不复用那套契约

刻意不用。契约要求「每条商品都有 sku、有 quantity」，而这份简报的价值恰恰
在于**照实说**：这个客户说的是「那个黄的」，那就原样写「那个黄的」，
不要翻译成一个目录里的编码。

模型在这里是**转述者**，不是分类器。

## 摘要里刻意保留的东西

* **原话**。摘要里写「客户说：那个上次买的」，而不是「客户要买上次那件商品」。
  商户可能自己就知道那是什么。
* **说不清的地方**。摘要里直接标「这句没说清」���而不是猜一个填上。
* **时间与数量**。这两样最容易在整理时丢掉，而它们恰恰决定要不要送货、送多少。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterable, Mapping, Sequence

SUMMARY_SYSTEM_PROMPT = """\
你会看微信群里的订货消息，然后把一个群这段时间的订货情况整理成一段简报，
给**店主本人**看。店主就在群里，他知道「老张」是谁，也知道自家卖什么。

## 你要做的

把下面的消息整理成一份简报。要点：

1. **谁要什么、多少**。数量和单位要照抄原文，没写就写「没说要多少」。
2. **客户原话**。客户说「那个黄的」就写「那个黄的」，不要替换成你猜的商品。
   你不知道他指的是什么，店主知道。
3. **时间**。什么时候要、什么时候送到。没写就写「没说时间」。
4. **说不清的地方直接标出来**。有客户说的话你判断不出要什么，就写
   「这句没说清要什么」，**不要猜一个填上**。
5. **只写订货相关的**。闲聊、表情、跟订货无关的消息一句话带过就行，不要展开。

## 不要做的事

* 不要编造商品名称、品牌、规格。客户没说的就是没说。
* 不要判断这是新单还是追加是老客户 —— 那是店主自己的记忆，你没有。
* 不要写「建议」「推荐」「可以考虑」这类意见。你只负责转述。

## 输出

纯文本，不要 JSON，不要 markdown 标题，不要代码块。
按时间顺序分段写，每段一个客户。最后一段列出「还没说清的」。

"""


class Summarizer:
    """把消息汇总成一段可读文本。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60,
                 max_tokens: int = 2000):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        #: **必须设**。OpenRouter 上的模型默认要 65536 的 max_tokens，
        #: 账户余额不够会直接 402 —— 这不是配额问题，是请求本身就超了。
        self.max_tokens = max_tokens

    def summarize(self, messages: Sequence[Mapping[str, Any]],
                  catalog: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        """返回 ``{"summary": 文本, "error": None|str, "cost": float|None}``。

        ``messages`` 至少要有 ``sender`` / ``content`` / ``sent_at``。
        ``catalog`` 传空即可 —— **不要把它当匹配用的**，这里只作参考，
        让模型知道店里大致卖什么，而不是让它去挑编码。
        """
        if not messages:
            return {"summary": "", "error": None, "cost": None, "message_count": 0}
        context: dict[str, Any] = {"messages": [dict(m) for m in messages]}
        if catalog:
            context["店里的商品（仅供参考，不是让你去匹配编码）"] = [
                {"名称": p.get("name"), "单位": p.get("unit")} for p in catalog]
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)},
            ],
        }
        request = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {"summary": "", "error": f"HTTP {exc.code}", "cost": None,
                    "message_count": len(messages)}
        except Exception as exc:  # pragma: no cover - network
            return {"summary": "", "error": type(exc).__name__, "cost": None,
                    "message_count": len(messages)}
        message = (data.get("choices") or [{}])[0].get("message", {})
        return {"summary": (message.get("content") or "").strip(),
                "error": None,
                "cost": (data.get("usage") or {}).get("cost"),
                "message_count": len(messages)}


def summarize_messages(messages: Iterable[Mapping[str, Any]], *,
                       base_url: str, api_key: str, model: str,
                       catalog: Sequence[Mapping[str, Any]] = (),
                       timeout: float = 60,
                       max_tokens: int = 2000) -> dict[str, Any]:
    return Summarizer(base_url, api_key, model, timeout, max_tokens).summarize(list(messages), catalog)