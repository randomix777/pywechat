"""额度与供应商成本之间的换算。

## 产品定价（由商家确认）

    商户买 1 美元额度  ==  平台实际花 0.1 美元上游成本

也就是 **1 美元上游成本 = 10 个分析额度**。这个 10 倍是商业决定，不是技术
常数 —— 它写在下面这个具名常量里，要改就改这一处，不要散进业务代码。

## 冻结价与实时价的关系

商户在提交前确认过价格，之后就不该变。这是产品刻意的设计，也是商户能信任
这个界面的前提。所以**不能按调用结束后的真实成本扣商户的钱** —— 那等于扣了
商户没确认过的价。

于是分成两件事，各管各的：

* **商户扣多少额度** —— 提交时按**预计**成本算出来，冻结进快照，之后不变。
* **平台实际花了多少美元** —— 调用结束后按**真实** token 用量记在内部账里，
  用来算毛利、发现某个模型其实比预期贵。

两者的差额就是这个 10 倍。如果它们长期偏离，说明预计估错了，要回去调估算，
而不是偷偷改商户的账单。

## 记账单位：微美元，不是"额度"

商户侧按**美元**计价，不是按抽象的"分析额度"。金额一律以整数微美元
（1 美元 = 1,000,000 µ$）存储 —— **绝不用浮点记钱**，浮点在累加几万次之后
会出现分位误差，而商户的余额不该因为他解析了 3000 条消息就少一分钱。

微美元还有一个副作用是好的：它足够细，所以**不需要"最低 1 额度"那条地板**。
以额度为单位时，一次真实解析的成本折算后常常不足 1，向上取整全变成 1，
于是价格差 2000 倍的模型扣的额度一模一样 —— 额度成了定价开关。换成微美元
之后差异回来了：

    $0.000012 上游 ->      120 \u00b5$
    $0.001100 \u4e0a\u6e38 -> 11,001 \u00b5$
    $0.066600 \u4e0a\u6e38 -> 666,000 \u00b5$

## \u4e0d\u77e5\u9053\u5355\u4ef7\u65f6\u600e\u4e48\u529e

**\u4e0d\u731c\u3002** :func:`merchant_micro_usd_for` \u5728\u5355\u4ef7\u672a\u77e5\u65f6\u8fd4\u56de ``None``\uff0c\u8c03\u7528\u65b9\u5fc5\u987b
\u81ea\u5df1\u51b3\u5b9a\u600e\u4e48\u529e \u2014\u2014 \u901a\u5e38\u662f\u9000\u56de\u8fd0\u8425\u914d\u7f6e\u7684\u56fa\u5b9a\u5355\u4ef7\uff0c\u800c\u4e0d\u662f\u62ff
\u4e00\u4e2a\u7f16\u51fa\u6765\u7684\u4ef7\u683c\u53bb\u6263\u94b1\u3002
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

#: 1 美元上游成本 = 10 美元商户账单（乘以 10）。
#:
#: 商家定的：商户买 1 美元额度，平台实际花 0.1 美元上游。
#: 这是**商业决定**，写在代码里是因为它必须有一个唯一的、可查的出处。
MERCHANT_USD_PER_VENDOR_USD = 10.0

#: 1 美元 = 100 万微美元。金额一律用它存。
#:
#: 整数微美元而不是浮点：浮点在累加几万次之后会出现分位误差，而商户的余额
#: 不该因为他解析了 3000 条消息就少一分钱。
US_PER_USD = 1_000_000

#: 一次解析的输出 token 上限估计。模型一般在这个量级内收尾；
#: 估高了商户多付，估低了平台贴钱，所以偏保守。
DEFAULT_OUTPUT_TOKENS = 800

#: 中英文混排时，一个字符大约对应多少 token。
#: 中文约 1 字 1 token，英文约 4 字符 1 token，取一个偏保守的下界。
_TOKENS_PER_CHAR = 0.6
_ENGLISH_CHARS_PER_TOKEN = 4.0


def estimate_input_tokens(text: str) -> int:
    """估算输入 token 数。

    **是估算。** 不是精确值，也不假装是：真实 token 数只有上游知道。
    这里要的是"提交前给商户一个能确认的价格"，不是事后对账的凭据。

    中文按每字 0.6 token 估（中文通常 1 字 1-2 token，但提示词里英文和
    标点会拉低均值）；纯 ASCII 按每 4 字符 1 token。
    """
    raw = str(text or "")
    if not raw:
        return 0
    cjk = sum(1 for ch in raw if "\u4e00" <= ch <= "\u9fff")
    other = len(raw) - cjk
    return int(math.ceil(cjk * _TOKENS_PER_CHAR + other / _ENGLISH_CHARS_PER_TOKEN))


def vendor_cost_usd(
    input_tokens: int,
    output_tokens: int,
    input_price_per_million: float,
    output_price_per_million: float,
) -> float:
    """真实的上游成本（美元）。

    OpenRouter 的模型单价是"每百万 token 多少美元"。单价必须由调用方传入 ——
    本模块**不内置任何价格表**，因为价格会变，而一个过期的价格表会让人
    按错误的价扣钱。
    """
    return (
        max(0, int(input_tokens)) * float(input_price_per_million)
        + max(0, int(output_tokens)) * float(output_price_per_million)
    ) / 1_000_000.0


def merchant_micro_usd_for(
    vendor_usd: float,
    rate: float = MERCHANT_USD_PER_VENDOR_USD,
) -> int:
    """上游成本（美元）→ 商户该扣多少**微美元**。

    **向上取整**：平台已经花了这么多钱，不该因为小数部分而少收。微美元已经
    足够细，所以这里**不需要**"最低 1 额度"的地板 —— 加了反而会把价格差异
    压平。
    """
    return int(math.ceil(max(0.0, float(vendor_usd)) * float(rate) * US_PER_USD))


def format_usd(micro: int | float) -> str:
    """微美元 → 给商户看的金额。

    小于 1 美分时显示到 6 位小数（0.000012 美元），因为再粗就显示成 0.00，
    而商户会以为自己没被扣钱、或者被系统吞了。再细就没意义了 —— 那全是噪声。
    """
    value = max(0, int(micro)) / US_PER_USD
    if value == 0:
        return "$0.00"
    if value < 0.01:
        return f"${value:.6f}"
    return f"${value:.2f}"


@dataclass(frozen=True)
class ModelPrice:
    """某个模型在 OpenRouter 上的单价。单位是"每百万 token 美元"。"""

    model: str
    input_per_million: float
    output_per_million: float

    def cost_for(self, input_tokens: int, output_tokens: int) -> float:
        return vendor_cost_usd(
            input_tokens, output_tokens, self.input_per_million, self.output_per_million
        )


def price_from_mapping(raw: Mapping[str, Any] | None) -> ModelPrice | None:
    """从 OpenRouter 的模型条目里取出单价。

    读不到就返回 ``None`` —— **不猜价格**。宁可用运营配置的固定额度，
    也不要拿一个编出来的单价去扣商户的钱。
    """
    if not raw:
        return None
    pricing = raw.get("pricing")
    if not isinstance(pricing, Mapping):
        return None
    try:
        in_price = float(pricing.get("prompt") or pricing.get("input") or 0)
        out_price = float(pricing.get("completion") or pricing.get("output") or 0)
    except (TypeError, ValueError):
        return None
    if in_price <= 0 and out_price <= 0:
        return None
    return ModelPrice(
        model=str(raw.get("id") or raw.get("name") or ""),
        input_per_million=in_price,
        output_per_million=out_price,
    )


def estimate_micro_usd_for_request(
    prompt_text: str,
    price: ModelPrice | None,
    output_tokens: int = DEFAULT_OUTPUT_TOKENS,
    rate: float = MERCHANT_USD_PER_VENDOR_USD,
) -> int | None:
    """提交前：按预计成本算出该冻结扣多少微美元。

    ``price`` 未知时返回 ``None``，调用方必须自己处理 —— 通常是退回运营
    配置的固定单价。这里**不返回 0**，因为 0 会悄悄变成免费。
    """
    if price is None:
        return None
    expected_usd = price.cost_for(estimate_input_tokens(prompt_text), int(output_tokens))
    return merchant_micro_usd_for(expected_usd, rate)


def reserve_micro_usd_for_request(
    prompt_text: str,
    price: ModelPrice | None,
    output_tokens: int = DEFAULT_OUTPUT_TOKENS,
    rate: float = MERCHANT_USD_PER_VENDOR_USD,
) -> int | None:
    """提交时**预留**多少微美元。

    用**最大可能成本**，不是估计值：输入能估，输出估不准。先把上限占住，
    结算时按真实用量扣、多占的部分自动退回（见 :func:`settle_micro_usd_for`）。

    为什么不多留少扣，而是先留上限：留少了任务跑到一半才发现余额不够，
    那时候钱已经花了；留多了只是商户暂时看到额度少一块，结算完就回来。

    **比上限再多留 1 微美元。** 预留和实扣各自向上取整，误差方向相反，
    边界上会出现"预留 121、实扣 122"—— 实扣超过预留，商户余额会变成负数。
    那 1 微美元是给取整误差留的余量，不���预留不覆盖实扣。

    ``price`` 未知时返回 ``None`` —— 调用方退回运营配置的固定预留，
    **不要**拿一个编出来的数去占商户的额度。
    """
    if price is None:
        return None
    worst_case = price.cost_for(
        estimate_input_tokens(prompt_text), max(0, int(output_tokens))
    )
    return merchant_micro_usd_for(worst_case, rate) + 1


def settle_micro_usd_for(
    input_tokens: int,
    output_tokens: int,
    price: ModelPrice | None,
    rate: float = MERCHANT_USD_PER_VENDOR_USD,
) -> int | None:
    """调用结束后**实扣**多少微美元。

    用上游回的真实 token 用量，不再是估计。这是商户最终被扣的钱。

    返回值可能**小于**预留值 —— 差额由结算时的预留释放自动退回商户，
    所以商户看到的余额只会变多不会变少。

    ``price`` 未知时返回 ``None``：实扣金额必须能算出来，算不出来就不能
    悄悄按 0 结算（那等于白送一次解析），也不能随便估一个。
    """
    if price is None:
        return None
    actual = price.cost_for(int(input_tokens), int(output_tokens))
    return merchant_micro_usd_for(actual, rate)


__all__ = [
    "DEFAULT_OUTPUT_TOKENS",
    "MERCHANT_USD_PER_VENDOR_USD",
    "US_PER_USD",
    "ModelPrice",
    "estimate_input_tokens",
    "estimate_micro_usd_for_request",
    "format_usd",
    "merchant_micro_usd_for",
    "price_from_mapping",
    "reserve_micro_usd_for_request",
    "settle_micro_usd_for",
    "vendor_cost_usd",
]