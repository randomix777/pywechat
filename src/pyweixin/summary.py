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
import re
import urllib.error
import urllib.request
from typing import Any, Iterable, Mapping, Sequence

#: 默认模型。测过真实调用：**DeepSeek v4.1 flash 在「说不清」这件
#: 事上比 gpt-4o-mini 做得好** —— 它会把「要什么 / 多少 / 什么时候」
#: 三个维度都标出来，而且会主动注明「牧牛」是原话照抄。
#: 成本 $0.0005（gpt-4o-mini 约 $0.0002）。
#:
#: 模型标识取自 OpenRouter 的实际列表，不是凭印象拼的。
DEFAULT_SUMMARY_MODEL = "deepseek/deepseek-v4.1-flash"

SUMMARY_SYSTEM_PROMPT = """\
你会看微信群里的订货消息，然后把一个群这段时间的订货情况整理成一段简报，
给**店主本人**看。店主就在群里，他知道「老张」是谁，也知道自家卖什么。

## 谁说的，比说了什么更重要

每一条都要写清**是谁说的**。同一个客户说了几句就并到他那一段，但**绝不能
把两个客户的话写在一起**，也不能只写内容不写人 —— 店主看到「要 3 箱苹果」
而不知道是谁要的，这条简报就是废的。

分组只看**发送人**，不要按话题分组。

## 不是文字的消息

消息里可能带一个「这条不是文字消息」的字段，值是「图片」「语音」「文件」
「视频」「链接」之类。

遇到这种消息：

* **照实写出来**。写「李叔发了一张图片」，不要写图片里可能是什么。
* **不要猜内容**。你看不到图片里是什么、听不到语音说了什么。客户发图片很可能
  就是在下单（比如拍一张货架照片），但那要店主自己去看。
* 这类消息要**单独列一段提醒**，放在最后，标题写「需要你自己看一眼的」，
  让店主知道有几条他必须亲自过目。

## 判断要求

1. **谁要什么、多少**。数量和单位要照抄原文，没写就写「没说要多少」。
2. **客户原话**。客户说「那个黄的」就写「那个黄的」，不要替换成你猜的商品。
   你不知道他指的是什么，店主知道。
3. **时间**。什么时候要、什么时候送到。没写就写「没说时间」。
4. **说不清的地方直接标出来**。有客户说的话你判断不出要什么，就写
   「这句没说清要什么」，**不要猜一个填上**。
5. **只写订货相关的**。闲聊、表情、跟订货无关的消息一句话带过就行，不要展开。

## 不要做的事

* 不要编造商品名称、品牌、规格。客户没说的就是没说。
* 不要猜测图片、语音、文件里是什么内容。你没看到就是没看到。
* 不要判断这是新单还是追加是老客户 —— 那是店主自己的记忆，你没有。
* 不要写「建议」「推荐」「可以考虑」这类意见。你只负责转述。

## 输出

纯文本，不要 JSON，不要 markdown 标题，不要代码块。
按时间顺序分段写，**每段开头必须是发送人的称呼，后面接冒号或换行**。
最后列两段：「还没说清的」和「需要你自己看一眼的（非文字消息）」——
后者没有内容就写「无」。

"""



def _as_message(item: Any) -> dict[str, Any]:
    """把一条消息统一成字典。

    ## 为什么不能直接 ``dict(m)``

    读取结果里的消息**不一定是映射**。它可能是 ``sqlite3.Row``（能 ``dict``），
    也可能是 dataclass 或普通对象（**不能** ``dict``）—— 后者会直接
    ``TypeError: cannot convert dictionary update sequence``。

    这个错误在单测里抓不到，因为单测喂的是字典；只有接到真实的读取结果上
    才会炸。所以这里两种都收：先试映射，再按属性取。

    **只取需要的字段**：发送人、内容、时间、类型。读取结果里可能还带着消息 id、
    群 id、原始游标之类的东西 —— 那些既不该送去模型，也不该出这台机器。

    ## ``kind`` 必须一起带上

    图片消息的 ``content`` 是占位符「图片」，**语音消息的 content 直接是空的**。
    不带 kind 的话，模型看到的就是「王姨：（空）」，完全不知道发生了什么，
    要么忽略这条，要么自己瞎猜一个内容出来。

    所以这里把 kind 译成中文一起送过去，让模型能照实说「客户发了一张图片」。
    """
    fields = ("sender", "content", "sent_at", "observed_at", "kind")
    # **幂等**：已经是转换后的形状就直接用。
    #
    # 不设这条，调用方先自己转一次、summarize 内部再转一次，第二次就找不到
    # sender/content/kind 了（它们已经变成长中文键名），于是**静默清空**
    # 全部内容，模型回一句「这批消息全是空的」。这个坑真实踩到过。
    if isinstance(item, Mapping) and "发送人" in item:
        return {"发送人": item.get("发送人") or "",
                "内容": item.get("内容") or "",
                "时间": item.get("时间") or "",
                **({"这条不是文字消息": item["这条不是文字消息"]}
                   if item.get("这条不是文字消息") else {})}
    raw: dict[str, Any] = {}
    if isinstance(item, Mapping):
        raw = {k: item.get(k) for k in fields if k in item}
    elif hasattr(item, "keys"):
        # ``sqlite3.Row``：**不是 Mapping**，也不支持属性访问，但支持按列名取值。
        # 少了这条分支它会掉进下面的 getattr，然后**静默返回全空字典** ——
        # 模型收到「发送人：'　内容：'」，回一句「这批消息全是空的」，
        # 而商户手里明明有五条。这个坑真实发生过。
        available = set(item.keys())
        raw = {k: item[k] for k in fields if k in available}
    else:
        raw = {k: getattr(item, k) for k in fields if hasattr(item, k)}
        if not raw:
            # 认不出来的东西**必须吵**。
            #
            # 静默返回空字典的后果是模型说「这批消息全是空的」，而商户手里
            # 明明有一堆 —— 排查的人会去怀疑模型、怀疑提示词，不会想到
            # 是这里把一个 tuple 当成了对象。这个坑真实踩到过两次
            # （sqlite3.Row 一次、未设 row_factory 的 tuple 一次）。
            raise TypeError(
                f"认不出这种消息:{type(item).__name__}。"
                "需要映射、sqlite3.Row，或带 sender/content 属性的对象。"
                "（若来自 sqlite3，请确认连接设了 row_factory = sqlite3.Row）"
            )
    message: dict[str, Any] = {
        "发送人": raw.get("sender") or "",
        "内容": raw.get("content") or "",
        "时间": raw.get("sent_at") or raw.get("observed_at") or "",
    }
    kind = str(raw.get("kind") or "text").strip().lower()
    if kind and kind != "text":
        message["这条不是文字消息"] = KIND_LABELS.get(kind, kind)
    return message


#: 非文字消息在简报里的说法。模型照这个写，**不要自己去解释内容**。
KIND_LABELS = {
    "image": "图片",
    "voice": "语音",
    "video": "视频",
    "file": "文件",
    "link": "链接",
    "quote": "引用",
    "other": "其他类型",
    "unknown": "类型不明",
}


class Summarizer:
    """把消息汇总成一段可读文本。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60,
                 max_tokens: int = 800):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        #: **必须设**，而且**不能设大**。
        #:
        #: 不设会被上游默认成 65536 —— 请求本身就超了余额，直接 402。
        #:
        #: 设**太大**同样 402：上游按「你最多可能用多少」预扣，要得比需要的多
        #: 就白白抬高门槛。一份群消息简报通常几百个字，800 够用；
        #: 原来设 2000，在余额紧的账号上必然失败（实测提示
        #: 「你需要 2000 tokens，但只付得起 473」）。
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
        context: dict[str, Any] = {"messages": [_as_message(m) for m in messages]}
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
        result = self._call(body, len(messages))
        # 402 且上游告诉了「你只付得起多少」时，**按它说的重试一次**。
        #
        # 这个错误里带着能用的数字，白白扔掉不如用上 —— 余额紧的账号上
        # 这决定了「能出一份短一点的简报」还是「什么都出不来」。
        affordable = result.pop("_affordable", None)
        if affordable and result.get("error"):
            body["max_tokens"] = affordable
            retry = self._call(body, len(messages))
            if not retry.get("error"):
                return retry
            result = retry
        return result

    def _call(self, body: dict[str, Any], count: int) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return self._http_error(exc, count, int(body.get("max_tokens") or 0))
        except Exception as exc:  # pragma: no cover - network
            return {"summary": "", "error": f"连接模型失败（{type(exc).__name__}）",
                    "cost": None, "message_count": count}
        message = (data.get("choices") or [{}])[0].get("message", {})
        return {"summary": (message.get("content") or "").strip(),
                "error": None,
                "cost": (data.get("usage") or {}).get("cost"),
                "message_count": count}

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError, count: int, asked: int) -> dict[str, Any]:
        """把上游的错误翻成**商户能照着做**的一句话。

        原来是 ``f"HTTP {exc.code}"`` —— 「整理失败：HTTP 402」。
        商户看到这个学不到任何东西：不知道是自己余额不够、还是软件坏了、
        还是该去交钱。而真实原因就写在响应体里。

        余额不足这条尤其要说清：它**不是**软件故障，降低一次要的量就能出结果，
        所以连可用的额度一起带回去让调用方重试。
        """
        detail = ""
        affordable = None
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            error = payload.get("error") or {}
            detail = str(error.get("message") or "")
            # 上游会说「You requested up to 2000 tokens, but can only afford 473」。
            match = re.search(r"can only afford\s+(\d+)", detail)
            if match:
                affordable = int(match.group(1))
            # 真正的首条原因可能被埋在 previous_errors 的末尾（上游重试链）。
            for previous in (error.get("metadata") or {}).get("previous_errors") or []:
                if previous.get("message"):
                    detail = str(previous["message"])
                    match = re.search(r"can only afford\s+(\d+)", detail)
                    if match:
                        affordable = int(match.group(1))
                    break
        except Exception:
            pass

        if exc.code == 402 or affordable is not None:
            if affordable:
                return {"summary": "", "cost": None, "message_count": count,
                        "error": f"模型账户余额不足：这次最多只能用 {affordable} 个 token，"
                                 f"而请求要 {asked} 个。正在按可用额度重试一次。",
                        "_affordable": max(64, affordable)}
            return {"summary": "", "cost": None, "message_count": count,
                    "error": "模型账户余额不足，需要充值后才能继续。"
                             "这不是软件问题 —— 已经读到的东西一条都没少。"}
        if exc.code == 429:
            return {"summary": "", "cost": None, "message_count": count,
                    "error": "模型这边临时限流，等一会儿再点一次就好。"}
        if exc.code in (401, 403):
            return {"summary": "", "cost": None, "message_count": count,
                    "error": "模型密钥无效或已失效，需要重新配置。"}
        tail = f"（{detail[:120]}）" if detail else ""
        return {"summary": "", "cost": None, "message_count": count,
                "error": f"模型调用失败，错误码 {exc.code}{tail}"}


def summarize_messages(messages: Iterable[Mapping[str, Any]], *,
                       base_url: str, api_key: str, model: str,
                       catalog: Sequence[Mapping[str, Any]] = (),
                       timeout: float = 60,
                       max_tokens: int = 800) -> dict[str, Any]:
    return Summarizer(base_url, api_key, model, timeout, max_tokens).summarize(list(messages), catalog)