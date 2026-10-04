"""模型目录：云端统一管理，客户只选别名。

    OpenCode /provider
        → 网关拉取与规范化（去掉全部凭据）
        → 运营允许列表与能力策略
        → 客户可选目录（只有别名）
        → 客户端展示

## 三件事必须分清

1. **目录里有 ≠ 能用。** 上游目录列出一个模型，不代表调用能成功。
   :attr:`CatalogStatus.DISCOVERED` 与 :attr:`CatalogStatus.SELECTABLE`
   是两个状态，中间隔着一次真实调用验证。
2. **上游价格 ≠ 客户免费。** ``free_for_customers`` 只由运营配置决定。
   某个模型包含在订阅里、或名字带 free、或上游价格为 0，都**不**构成免费。
3. **已连接 ≠ 可用。** OpenCode 的 ``connected`` 只说明凭据存在。

## 客户端看不到什么

:func:`ModelCatalog.for_client` 只输出别名、显示名、说明、是否免费、状态与限制。
供应商 id、模型 id、上游价格、地址、凭据一律不出现。客户提交任务时也只提交
别名 —— :func:`resolve` 由服务端把别名还原成真正的 provider/model。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from ._packages import ensure_packages_on_path

ensure_packages_on_path()


class CatalogStatus:
    """模型当前处于哪个状态。客户看到的中文由此映射。"""

    #: 上游目录里有，但本产品还没验证过。**不可选。**
    DISCOVERED = "discovered"
    #: 已发过验证请求，结果未知。**不可选。**
    PENDING_VERIFICATION = "pending_verification"
    #: 通过订单能力验证，可供客户选择。
    SELECTABLE = "selectable"
    #: 暂时限流，可稍后再试。
    RATE_LIMITED = "rate_limited"
    #: 调用失败或不可用。
    UNAVAILABLE = "unavailable"
    #: 运营主动停用。
    DISABLED = "disabled"

    ALL = (DISCOVERED, PENDING_VERIFICATION, SELECTABLE, RATE_LIMITED, UNAVAILABLE, DISABLED)
    #: 客户可以主动选择的。
    CHOOSABLE = (SELECTABLE, RATE_LIMITED)

    COPY = {
        DISCOVERED: "尚未验证",
        PENDING_VERIFICATION: "正在验证",
        SELECTABLE: "可用",
        RATE_LIMITED: "当前繁忙，请稍后重试",
        UNAVAILABLE: "暂不可用",
        DISABLED: "已停用",
    }


#: 支持的业务能力。免费模型也必须通过这些能力验证。
CAP_ORDER_PARSE = "order_parse"

REQUIRED_CAPABILITIES = (CAP_ORDER_PARSE,)

#: 文案里"扣 N 次"的样子。文案与数字对不上时以策略为准。
_CHARGE_UNITS_PATTERN = re.compile(r"扣\s*(\d+)\s*(?:次|个|笔)")

#: 文案里出现这些词，就是在声称这次分析不收钱。
#:
#: 免费档位已经焊死（产品规则：免费模型也扣分析额度），所以收费模型自称
#: 「免费」「不扣额度」**一定是假的** —— 那不是文案跑偏，是在骗商户。
#: 以前只在文案里数「扣 N 次」，这类谎话根本抓不住。
#:
#: 注意**否定语境不算撒谎**：「不是免费模型，但会扣费」是在如实说明，
#: 不该被丢掉。所以下面几个词只在**没有被否定**时才算谎话。
_FALSE_CHARGE_CLAIMS = ("免费", "不扣", "不收费", "无需付费", "不用付费", "不消耗额度")
_NEGATIONS = ("不是", "不会", "不再", "并非", "不是免费的")


def _claims_to_be_free(note: str) -> bool:
    """这段文案是否在声称「不收钱」。

    带否定的说法（"不是免费模型"）返回 ``False`` —— 那是在如实说明，
    不该因为出现了"免费"两个字就把运营的话丢掉。
    """
    for word in _FALSE_CHARGE_CLAIMS:
        start = 0
        while True:
            index = note.find(word, start)
            if index < 0:
                break
            prefix = note[max(0, index - 4):index]
            if not any(neg in prefix for neg in _NEGATIONS):
                return True
            start = index + len(word)
    return False


def _note_contradicts(note: str, units: int) -> bool:
    """``charge_note`` 是否与结构化策略矛盾。

    两类要拦：

    * 声称**免费** —— 免费档位已焊死，收费模型自称免费一定是在骗商户。
    * 次数对不上 —— 文案写「扣 1 次」而策略是 3 次时，以策略为准。

    纯描述性的一句话（"分析较慢，会消耗额度"）不参与判断，不会被丢掉。
    """
    text = str(note or "")
    if _claims_to_be_free(text):
        return True
    match = _CHARGE_UNITS_PATTERN.search(text)
    return bool(match) and int(match.group(1)) != int(units)


class PricingClass:
    """客户侧的收费类别。**不是**上游 token 价格。"""

    FREE = "free"          # 本产品不扣该任务的分析额度
    METERED = "metered"    # 扣分析额度

    COPY = {
        FREE: "免费模型",
        METERED: "该模型会消耗分析额度",
    }


@dataclass(frozen=True)
class OperatorModelPolicy:
    """运营对一个模型的配置。

    只有这里能决定「对客户免费」以及各项上限。发现阶段拿到的上游价格
    不会、也不应该影响这里的任何字段。

    ``alias`` 由运营**自己命名**（例如 ``free-standard``），并在这里声明它
    指向哪个上游模型。别名不从上游 id 派生 —— 派生出来的别名会把模型名
    直接告诉客户，等于绕过了"客户端只选别名"这条边界。
    """

    alias: str
    #: 运营指定的上游目标。客户看不到这两个字段。
    provider_id: str = ""
    model_id: str = ""
    pricing_class: str = PricingClass.METERED
    #: 每天免费分析次数。0 表示该模型不提供免费额度。
    #:
    #: 这是**每天的次数上限**（例：50 表示一天 50 条消息），
    #: 和 :attr:`cost_units_per_job` 是两回事：后者是**每条任务扣多少分析额度**。
    #: 免费模型不扣分析额度，所以它的 ``cost_units_per_job`` 恒为 0。
    daily_free_limit: int = 0
    max_input_tokens: int = 200_000
    max_output_tokens: int = 8_000
    max_task_seconds: float = 300.0
    max_concurrent_per_tenant: int = 2
    requests_per_minute: int = 30
    capabilities: tuple[str, ...] = (CAP_ORDER_PARSE,)
    is_default: bool = False
    #: 仅用于**没有上游目录可拉**的部署（离线/演练）。为真时运营直接声明该
    #: 模型可用，**跳过**真实调用验证 —— 启动时会打警告，因为真实验证正是
    #: 准入检查的目的。有上游目录时这个字段被忽略。
    assumed_verified: bool = False
    enabled: bool = True
    #: 客户看到的一句话说明。
    summary: str = ""
    #: 收费模型给客户看的扣费说明。**纯展示文案**，不参与任何扣费计算 ——
    #: 真实扣多少看 :attr:`cost_units_per_job`。两者由 :meth:`charge_line` 同源生成，
    #: 避免"显示一套、扣另一套"。
    charge_note: str = ""
    #: 每个任务扣多少分析额度。免费模型忽略这个值（恒按 0 处理）。
    #:
    #: 单位是"分析额度"，**不是**每天的次数。次数上限看 :attr:`daily_free_limit`。
    #: 收费模型配 3 表示"每条消息扣 3 次额度"；免费模型配 0。
    cost_units_per_job: int = 1
    #: 免费额度按哪个时区的日期重置。默认 UTC；商户所在时区要显式配置。
    free_quota_timezone: str = "UTC"
    #: 策略版本。目录版本变化时用于追溯"当时按哪套规则算的"。
    policy_version: str = "1"
    #: **保留字段，当前不实现自动换模型。** 运营的旧配置里可能有它，解析出来
    #: 只是为了不静默丢弃配置；网关**不会**因为它在上游失败时改用别的模型。
    #: 自动换模型会把"模型不可用"变成"悄悄多扣一次收费额度"，与本产品的原则
    #: 相反。真要换模型，必须由商户显式改选并新建任务。
    failover_to: str = ""

    def __post_init__(self) -> None:
        if self.pricing_class not in (PricingClass.FREE, PricingClass.METERED):
            raise ValueError(f"unknown pricing class: {self.pricing_class}")
        if self.pricing_class == PricingClass.FREE:
            # **产品规则：免费模型也扣分析额度。**
            #
            # 「对客户免费」这个档位曾经是一条完整的计费旁路：免费任务预留
            # 每日免费额度、不碰订阅额度（见 ``ProductStore.create_job`` 的
            # ``charged = 0 if free``）。那条旁路等于"分析不花钱"，与
            # 「初始额度为 0，必须先充值才能真实分析」直接冲突。
            #
            # 这里**焊死**它：直接构造一个免费策略会失败，口子永远打不开。
            # 存量配置走 :meth:`from_dict` 自动降级，不会因为旧数据而启动失败。
            raise ValueError(
                "免费模型也扣分析额度：pricing_class 不允许是 free"
            )
        if self.daily_free_limit < 0:
            raise ValueError("daily_free_limit must not be negative")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if self.max_task_seconds <= 0:
            raise ValueError("max_task_seconds must be positive")
        if self.cost_units_per_job < 0:
            raise ValueError("cost_units_per_job must not be negative")
        if self.max_concurrent_per_tenant < 1:
            raise ValueError("max_concurrent_per_tenant must be at least 1")
        if self.requests_per_minute < 1:
            raise ValueError("requests_per_minute must be at least 1")

    @property
    def is_free(self) -> bool:
        return self.pricing_class == PricingClass.FREE

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["capabilities"] = list(self.capabilities)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperatorModelPolicy":
        pricing_class = str(data.get("pricing_class", PricingClass.METERED))
        cost_units = int(data.get("cost_units_per_job", 1))
        if pricing_class == PricingClass.FREE:
            # 存量配置里可能还有免费档位。**不丢数据，也不让它继续免费**：
            # 直接按收费加载，单价取配置里的 ``cost_units_per_job``；
            # 它当年若是 0（免费模型按定义就是 0），补成 1 ——
            # 宁可按最低单价扣，也不能出现"这次不扣"的旁路。
            pricing_class = PricingClass.METERED
            cost_units = max(1, cost_units)
        return cls(
            alias=str(data["alias"]),
            provider_id=str(data.get("provider_id", "")),
            model_id=str(data.get("model_id", "")),
            pricing_class=pricing_class,
            daily_free_limit=0,
            max_input_tokens=int(data.get("max_input_tokens", 200_000)),
            max_output_tokens=int(data.get("max_output_tokens", 8_000)),
            max_task_seconds=float(data.get("max_task_seconds", 300.0)),
            max_concurrent_per_tenant=int(data.get("max_concurrent_per_tenant", 2)),
            requests_per_minute=int(data.get("requests_per_minute", 30)),
            capabilities=tuple(data.get("capabilities") or (CAP_ORDER_PARSE,)),
            is_default=bool(data.get("is_default", False)),
            assumed_verified=bool(data.get("assumed_verified", False)),
            enabled=bool(data.get("enabled", True)),
            summary=str(data.get("summary", "")),
            charge_note=str(data.get("charge_note", "")),
            cost_units_per_job=cost_units,
            free_quota_timezone=str(data.get("free_quota_timezone", "UTC")),
            policy_version=str(data.get("policy_version", "1")),
            failover_to=str(data.get("failover_to", "")),
        )


@dataclass
class ModelEntry:
    """目录里的一个模型。客户侧只暴露 :meth:`for_client`。"""

    alias: str
    display_name: str
    provider_id: str
    model_id: str
    policy: OperatorModelPolicy
    status: str = CatalogStatus.DISCOVERED
    status_reason: str = ""
    context_limit: int = 0
    output_limit: int = 0
    #: 上游价格，只给运营看，**绝不下发**。
    upstream_cost: Mapping[str, Any] = field(default_factory=dict)
    discovered_at: float = 0.0
    verified_at: float = 0.0
    #: 验证通过的检查项。
    verification: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in CatalogStatus.ALL:
            raise ValueError(f"unknown catalog status: {self.status}")

    @property
    def choosable(self) -> bool:
        return (
            self.status in CatalogStatus.CHOOSABLE
            and self.policy.enabled
            and all(self.policy.supports(cap) for cap in REQUIRED_CAPABILITIES)
        )

    @property
    def usable(self) -> bool:
        """能当默认模型：可选 + 通过了能力验证。

        以前这里另外要求 ``is_free``（函数名字就叫 ``free_usable``），
        传递参数是“免费”。免费档位已被焊死（见
        :meth:`OperatorModelPolicy.__post_init__`），这里的条件永远成立不起。
        """
        return self.choosable and bool(self.verified_at)

    def summary_line(self) -> str:
        return self.policy.summary or "分析模型"

    def charge_line(self) -> str:
        """收费模型的扣费说明。

        **从 :attr:`cost_units_per_job` 生成**，不是在文案里另写一套 ——
        显示和扣费必须同源，否则会出现"显示扣 1、实际扣 3"。

        运营确实可以自己写 ``charge_note``；但只要它写的次数与结构化策略里的
        数字对不上，就**以策略为准**并丢掉这句文案。展示文案可以改，扣费依据
        不能被一段中文悄悄改掉。

        **计价单位是美元，不是"次数"。** 下面那句"扣 N 次额度"是历史口径：
        商户当时买的是一个抽象额度，用掉一部分而已。现在的实际扣费是按
        供应商真实用量折算的金额（见 :mod:`pyweixin.pricing`），**同一个模型
        每条消息的成本并不相同** —— 消息越长，模型读的越多，扣得越多。

        所以这里只说清两件商户真正需要知道的事：**会扣费**，以及**扣多少取决于
        这条消息有多长**。不写死一个数字，因为那个数字本来就是假的。
        """
        note = str(self.policy.charge_note or "").strip()
        if note and not _note_contradicts(note, self.policy.cost_units_per_job):
            return note
        return (
            "该模型会消耗分析额度，按这条消息的实际长度计费"
            "（消息越长，模型读取的内容越多，扣费越多）"
        )

    def free_quota_day(self, now: float | None = None) -> str:
        """免费额度记在哪一天。跨午夜的任务按**预留**那天算，不是结算那天。

        用的是 :func:`pyweixin.product.day_key` —— 存储层算日期用的是同一个，
        两边口径必须一致，否则同一个任务会被记到两个日子里。
        """
        from .product import day_key

        return day_key(now, str(self.policy.free_quota_timezone or "UTC"))

    def billing_snapshot(self) -> dict[str, Any]:
        """冻结用的计费事实。展示文案不在里面 —— 它不是计费依据。

        免费档位已被 :meth:`OperatorModelPolicy.__post_init__` 焊死，
        所以这里**不再**有"免费就不扣"的分支：每条任务都按
        ``cost_units_per_job`` 扣分析额度。留着那个分支等于给将来留后门。
        """
        return {
            "pricing_class": self.policy.pricing_class,
            "cost_units_per_job": max(1, int(self.policy.cost_units_per_job)),
            "daily_free_limit": 0,
            "free_quota_timezone": self.policy.free_quota_timezone,
            "max_concurrent_per_tenant": self.policy.max_concurrent_per_tenant,
            "requests_per_minute": self.policy.requests_per_minute,
            "max_task_seconds": self.policy.max_task_seconds,
            "max_output_tokens": self.policy.max_output_tokens,
            "policy_version": self.policy.policy_version,
        }

    def for_client(self) -> dict[str, Any]:
        """客户可见的视图。

        只有别名、显示名、说明、收费类别、状态与限制。**没有** provider_id、
        model_id、上游价格、地址或凭据。
        """
        return {
            "alias": self.alias,
            "display_name": self.display_name,
            "summary": self.summary_line(),
            # 不再下发 free / free_label / daily_free_limit / free_quota_text：
            # 免费档位已被焊死，这些字段永远只能是 false/0，
            # 下发给客户端只会让界面多出现无意义的“免费”。
            "charge_line": self.charge_line(),
            "status": self.status,
            "status_label": CatalogStatus.COPY.get(self.status, self.status),
            "status_reason": self.status_reason if not self.choosable else "",
            "is_default": self.policy.is_default and self.choosable,
            "capabilities": [c for c in self.policy.capabilities if c in REQUIRED_CAPABILITIES],
            "context_limit": self.context_limit or self.policy.max_input_tokens,
            "slow": self.output_limit > 0 and False,   # 由真实延迟测量填充，暂不猜
        }

    def resolve_target(self) -> dict[str, str]:
        """服务端内部使用：别名 → 真正的 provider/model。"""
        return {"provider_id": self.provider_id, "model_id": self.model_id}

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "display_name": self.display_name,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "policy": self.policy.to_dict(),
            "status": self.status,
            "status_reason": self.status_reason,
            "context_limit": self.context_limit,
            "output_limit": self.output_limit,
            "upstream_cost": dict(self.upstream_cost),
            "discovered_at": self.discovered_at,
            "verified_at": self.verified_at,
            "verification": dict(self.verification),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelEntry":
        return cls(
            alias=str(data["alias"]),
            display_name=str(data.get("display_name", "")),
            provider_id=str(data.get("provider_id", "")),
            model_id=str(data.get("model_id", "")),
            policy=OperatorModelPolicy.from_dict(data["policy"]),
            status=str(data.get("status", CatalogStatus.DISCOVERED)),
            status_reason=str(data.get("status_reason", "")),
            context_limit=int(data.get("context_limit", 0)),
            output_limit=int(data.get("output_limit", 0)),
            upstream_cost=dict(data.get("upstream_cost") or {}),
            discovered_at=float(data.get("discovered_at", 0.0)),
            verified_at=float(data.get("verified_at", 0.0)),
            verification=dict(data.get("verification") or {}),
        )


def stable_alias(provider_id: str, model_id: str) -> str:
    """由真实 id 派生出**稳定**的产品别名。

    别名是客户看到和保存的东西，所以它必须在同一对 (provider, model) 下
    永远相同；反过来，上游下架一个模型时，别名不会漂移到别的模型上。
    """
    slug = model_id.split("/")[-1]
    safe = "".join(ch if ch.isalnum() else "-" for ch in slug.lower()).strip("-")
    digest = hashlib.sha256(f"{provider_id}/{model_id}".encode("utf-8")).hexdigest()[:8]
    return f"{safe[:40] or 'model'}-{digest}"


def build_catalog(
    providers: Sequence[Mapping[str, Any]],
    policies: Mapping[str, OperatorModelPolicy],
    *,
    now: float = 0.0,
    previous: "ModelCatalog | None" = None,
) -> "ModelCatalog":
    """把上游目录与运营策略合成客户目录。

    匹配方式是**策略里声明的上游目标** (provider_id, model_id)，别名由运营
    命名。这样客户拿到的别名里不含任何上游信息。

    上游消失的模型不会被删除，只会标成 ``unavailable``：既有运行记录和历史
    结果不能因为目录变化而对不上。
    """
    entries: dict[str, ModelEntry] = {}
    seen: set[str] = set()
    for provider in providers:
        provider_id = str(provider.get("provider_id") or "")
        connected = bool(provider.get("connected"))
        for model in provider.get("models") or ():
            model_id = str(model.get("model_id") or "")
            if not model_id or not provider_id:
                # 没有真实上游 id 的条目（FakeBackend 之类）不进正式目录。
                continue
            # 只认运营显式声明指向这个上游模型的策略。
            alias = next(
                (
                    policy.alias
                    for policy in policies.values()
                    if policy.enabled
                    and policy.provider_id == provider_id
                    and policy.model_id == model_id
                ),
                "",
            )
            if not alias:
                continue
            policy = policies[alias]
            seen.add(alias)
            old = previous.entry(alias) if previous else None
            # 凭据已经在上游载荷里剔除，这里只搬模型本身的事实。
            entries[alias] = ModelEntry(
                alias=alias,
                display_name=str(model.get("display_name") or model_id),
                provider_id=provider_id,
                model_id=model_id,
                policy=policy,
                status=(
                    CatalogStatus.SELECTABLE
                    if connected and old and old.status == CatalogStatus.SELECTABLE
                    else CatalogStatus.DISCOVERED
                ),
                status_reason="" if connected else "暂时无法使用该模型，请稍后重试",
                context_limit=int(model.get("context_limit") or 0),
                output_limit=int(model.get("output_limit") or 0),
                upstream_cost=dict(model.get("upstream_cost") or {}),
                discovered_at=float(model.get("discovered_at") or now),
                verified_at=old.verified_at if old else 0.0,
                verification=dict(old.verification) if old else {},
            )
    # 上游已消失但运营仍保留的条目：标为不可用，保留历史痕迹。
    if previous is not None:
        for alias, old in previous.entries.items():
            if alias in seen:
                continue
            policy = policies.get(alias)
            if policy is None or not policy.enabled:
                continue
            old.status = CatalogStatus.UNAVAILABLE
            old.status_reason = "该模型暂时无法使用，请稍后重试"
            entries[alias] = old
    return ModelCatalog(entries=entries, updated_at=now)


class ModelCatalog:
    """一份目录，带版本、缓存时间与逐条状态。"""

    def __init__(self, entries: Mapping[str, ModelEntry] | None = None, updated_at: float = 0.0,
                 fetched_ok: bool = True, last_success_at: float = 0.0, version: str = ""):
        self.entries: dict[str, ModelEntry] = dict(entries or {})
        self.updated_at = float(updated_at)
        #: 上一次拉取是否成功。失败时用旧数据但**必须**标成陈旧。
        self.fetched_ok = bool(fetched_ok)
        self.last_success_at = float(last_success_at or updated_at)
        self.version = version or self.compute_version()

    def compute_version(self) -> str:
        """目录版本。改一个别名或一条策略，版本就变。"""
        payload = json.dumps(
            {
                alias: {
                    "status": entry.status,
                    "provider": entry.provider_id,
                    "model": entry.model_id,
                    "pricing": entry.policy.pricing_class,
                    "free_limit": entry.policy.daily_free_limit,
                    "default": entry.policy.is_default,
                    "enabled": entry.policy.enabled,
                }
                for alias, entry in sorted(self.entries.items())
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return "cat_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def entry(self, alias: str) -> ModelEntry | None:
        return self.entries.get(str(alias))

    def mark_status(
        self, alias: str, status: str, reason: str = "", verified: bool | None = None,
        verification: Mapping[str, Any] | None = None, now: float | None = None,
    ) -> ModelEntry:
        """更新单个模型的状态。

        ``verified=True`` 只能由**真实调用**的结果触发；"已连接"和"目录里
        有"都不算。
        """
        entry = self.entry(alias)
        if entry is None:
            raise KeyError(f"unknown model alias: {alias}")
        entry.status = status
        entry.status_reason = reason
        if verified is True:
            entry.verified_at = float(now or 0.0)
        if verification is not None:
            entry.verification = dict(verification)
        self.version = self.compute_version()
        return entry

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def stale(self) -> bool:
        return not self.fetched_ok

    def choosable(self) -> list[ModelEntry]:
        return sorted(
            (entry for entry in self.entries.values() if entry.choosable),
            key=lambda e: (not e.policy.is_default, e.alias),
        )

    def default_entry(self) -> ModelEntry | None:
        """服务端默认模型。

        只看运营明确标记的默认项（且已通过验证）。

        以前这里优先选**合格免费**模型，没有才回落到其它。免费档位已被焊死
        （见 :meth:`OperatorModelPolicy.__post_init__`），所以那条路径永远不会被选中 ——
        留着只会让以后看这里的人误以为还有免费模型。
        """
        for entry in self.choosable():
            if entry.policy.is_default and entry.usable:
                return entry
        for entry in self.choosable():
            if entry.policy.is_default and entry.verified_at:
                return entry
        return None

    def resolve(self, alias: str) -> ModelEntry:
        """别名 → 条目。不存在或不可选时抛错，**不做静默替换**。"""
        from .client_login import GatewayError

        entry = self.entry(alias)
        if entry is None:
            raise GatewayError("model_not_allowed", "所选模型不可用，请重新选择")
        if not entry.choosable:
            reason = CatalogStatus.COPY.get(entry.status, entry.status)
            raise GatewayError("model_unavailable", f"该模型{reason}，请选择其它模型或稍后重试")
        return entry

    def for_client(self) -> dict[str, Any]:
        """客户目录。只有别名，没有任何上游标识。"""
        default = self.default_entry()
        choosable = list(self.choosable())
        return {
            "version": self.version,
            "default_alias": default.alias if default else "",
            # 这个字段的**含义**是"有没有可用的模型"，不是"有没有免费模型"。
            #
            # 以前写成 ``default.policy.is_free``，于是免费档位焊死之后它恒为
            # False —— 客户端拿它判断"目录里有没有模型"，商户就会看到
            # 「暂时没有可用于分析消息的模型」挂在每一屏上。
            #
            # 一条永远为真的假警报，比没有提示更糟。
            "free_available": bool(choosable),
            "stale": self.stale,
            "updated_at": self.updated_at,
            "last_success_at": self.last_success_at,
            "notice": (
                "暂时无法获取最新模型列表，下面是最近一次成功获取的结果。"
                if self.stale
                else ""
            ),
            "models": [entry.for_client() for entry in choosable],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "last_success_at": self.last_success_at,
            "fetched_ok": self.fetched_ok,
            "entries": {alias: entry.to_dict() for alias, entry in self.entries.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelCatalog":
        return cls(
            entries={alias: ModelEntry.from_dict(item) for alias, item in (data.get("entries") or {}).items()},
            updated_at=float(data.get("updated_at", 0.0)),
            fetched_ok=bool(data.get("fetched_ok", True)),
            last_success_at=float(data.get("last_success_at", 0.0)),
            version=str(data.get("version", "")),
        )


class CatalogStore:
    """目录缓存：拉一次，所有客户读缓存；后台定期刷新。

    拉取失败时**保留上一次成功的数据**并标 ``stale``，界面上如实写明
    「最近一次成功获取」的时间和「不是最新」。不伪装成最新状态。
    """

    def __init__(self, path: str = "", clock=time.time, refresh_interval: float = 3600.0):
        self._clock = clock
        self.refresh_interval = float(refresh_interval)
        self.catalog = ModelCatalog()
        self._lock = threading.RLock()
        self._source: Callable[[], list[dict[str, Any]]] | None = None
        self._policies: dict[str, OperatorModelPolicy] = {}
        self._path = path
        if path:
            self._load(path)

    def _load(self, path: str) -> None:
        from pathlib import Path

        file = Path(path)
        if file.is_file():
            try:
                self.catalog = ModelCatalog.from_dict(json.loads(file.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                self.catalog = ModelCatalog()

    def _save(self, path: str) -> None:
        from pathlib import Path

        try:
            file = Path(path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps(self.catalog.to_dict(), ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def bind(self, source: Callable[[], list[dict[str, Any]]], policies: Mapping[str, OperatorModelPolicy]) -> None:
        self._source = source
        self._policies = dict(policies)

    def load_policies(self, policies: Sequence[OperatorModelPolicy | Mapping[str, Any]]) -> None:
        """接受策略对象或它的字典形式，两种都常见。"""
        parsed: dict[str, OperatorModelPolicy] = {}
        for item in policies or ():
            policy = item if isinstance(item, OperatorModelPolicy) else OperatorModelPolicy.from_dict(item)
            parsed[policy.alias] = policy
        self._policies = parsed

    def policies(self) -> list[OperatorModelPolicy]:
        return list(self._policies.values())

    # ------------------------------------------------- read-only delegation
    # 网关和客户端读的是"当前目录"，拿到的应该是 ModelCatalog 本身。
    # 这里把只读访问转发过去，调用方不必记住外面套了一层 store。

    @property
    def version(self) -> str:
        return self.catalog.version

    def default_entry(self) -> ModelEntry | None:
        return self.catalog.default_entry()

    def resolve(self, alias: str) -> ModelEntry:
        return self.catalog.resolve(alias)

    def for_client(self) -> dict[str, Any]:
        return self.catalog.for_client()

    def __len__(self) -> int:
        return len(self.catalog)

    def __iter__(self):
        return iter(self.catalog.entries.values())

    def refresh(self, *, force: bool = False) -> ModelCatalog:
        """拉取上游并重建目录。失败时保留旧数据并标陈旧。"""
        with self._lock:
            now = self._clock()
            if not force and self._source is not None and self.catalog.updated_at:
                if now - self.catalog.last_success_at < self.refresh_interval:
                    return self.catalog
            if self._source is None:
                return self.catalog
            try:
                providers = self._source()
            except Exception as exc:
                # 保留旧数据。不把失败伪装成"没有模型"。
                self.catalog.fetched_ok = False
                self.catalog.version = self.catalog.compute_version()
                self._log_failure(str(exc))
                return self.catalog
            for provider in providers:
                for model in provider.get("models") or ():
                    if model.get("discovered_at") is None:
                        model["discovered_at"] = now
            self.catalog = build_catalog(providers, self._policies, now=now, previous=self.catalog)
            self.catalog.last_success_at = now
            self.catalog.fetched_ok = True
            if self._path:
                self._save(self._path)
            return self.catalog

    def _log_failure(self, reason: str) -> None:
        import logging

        logging.getLogger("pyweixin.catalog").warning("model catalog refresh failed: %s", reason)

    def entry(self, alias: str) -> ModelEntry | None:
        return self.catalog.entry(alias)

    def choosable(self) -> list[ModelEntry]:
        return self.catalog.choosable()

    def mark_status(self, alias: str, status: str, reason: str = "", verified: bool | None = None,
                    verification: Mapping[str, Any] | None = None, now: float | None = None) -> ModelEntry:
        """更新状态并落盘。转交给 :class:`ModelCatalog` 后保存。"""
        with self._lock:
            entry = self.catalog.mark_status(
                alias, status, reason, verified, verification, now if now is not None else self._clock()
            )
            if self._path:
                self._save(self._path)
            return entry


#: 客户端**不得**提交的字段。出现即拒绝。
FORBIDDEN_MODEL_KEYS = frozenset(
    {
        "provider",
        "provider_id",
        "providerID",
        "model",
        "model_id",
        "modelID",
        "upstream",
        "upstream_url",
        "base_url",
        "baseURL",
        "api_key",
        "apiKey",
        "key",
        "token",
        "endpoint",
        "url",
        "path",
        "system_prompt",
        "prompt",
        "tools",
    }
)


def reject_client_model_override(payload: Mapping[str, Any]) -> None:
    """客户端只能提交**产品别名**。

    任何试图直接指定供应商、模型 id、地址或密钥的字段都在这里被挡下。
    """
    from .client_login import GatewayError

    present = {str(key) for key in payload} & FORBIDDEN_MODEL_KEYS
    # ``model`` 是唯一允许的键，但值必须是别名，由网关解析。
    present.discard("model")
    if present:
        raise GatewayError("model_not_allowed", f"请求包含不支持的字段：{', '.join(sorted(present))}")


__all__ = [
    "CAP_ORDER_PARSE",
    "CatalogStatus",
    "CatalogStore",
    "ModelCatalog",
    "ModelEntry",
    "OperatorModelPolicy",
    "PricingClass",
    "REQUIRED_CAPABILITIES",
    "build_catalog",
    "reject_client_model_override",
    "stable_alias",
]
