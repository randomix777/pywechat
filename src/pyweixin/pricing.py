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

## 不知道单价时怎么办

**不猜。** :func:`merchant_units_for` 在单价未知时返回 ``None``，调用方必须
自己决定怎么办 —— 通常是退回运营配置的 ``cost_units_per_job``，而不是拿
一个编出来的价格去扣钱。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

#: 1 美元上游成本 = 10 个分析额度。
#:
#: 商家定的：商户买 1 美元额度，平台实际花 0.1 美元上游。
#: 这是**商业决定**，写在代码里是因为它必须有一个唯一的、可查的出处。
MERCHANT_UNITS_PER_VENDOR_USD = 10.0

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


def merchant_units_for(vendor_usd: float, rate: float = MERCHANT_UNITS_PER_VENDOR_USD) -> int:
    """上游成本（美元）→ 商户该扣的额度。

    **向上取整**：平台已经花了这么多钱，不该因为小数部分而少收。
    但**最低 1 个额度** —— 一次真实的分析不应该算出 0 额度，那等于免费。
    """
    units = math.ceil(max(0.0, float(vendor_usd)) * float(rate))
    return max(1, int(units))


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


def estimate_units_for_request(
    prompt_text: str,
    price: ModelPrice | None,
    output_tokens: int = DEFAULT_OUTPUT_TOKENS,
    rate: float = MERCHANT_UNITS_PER_VENDOR_USD,
) -> int | None:
    """提交前：按预计成本算出该冻结多少额度。

    ``price`` 未知时返回 ``None``，调用方必须自己处理 —— 通常是退回运营
    配置的 ``cost_units_per_job``。这里**不返回 0**，因为 0 会悄悄变成免费。
    """
    if price is None:
        return None
    expected_usd = price.cost_for(estimate_input_tokens(prompt_text), int(output_tokens))
    return merchant_units_for(expected_usd, rate)


__all__ = [
    "DEFAULT_OUTPUT_TOKENS",
    "MERCHANT_UNITS_PER_VENDOR_USD",
    "ModelPrice",
    "estimate_input_tokens",
    "estimate_units_for_request",
    "merchant_units_for",
    "price_from_mapping",
    "vendor_cost_usd",
]