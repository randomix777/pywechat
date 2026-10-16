"""OpenRouter 适配器 —— 第二个正式上游。

## 为什么不直接复用 DeepSeek 适配器

传输层能复用：OpenRouter 也是 OpenAI 兼容的 ``POST /chat/completions``，
认证同样是 ``Authorization: Bearer <key>``。所以底层直接用
:class:`business_agent.backends.deepseek.DeepSeekBackend`。

但**下面这些必须换掉**，否则就是在说假话：

* ``provider_id``：沿用 ``deepseek`` 会让计费与统计记到错的供应商名下。
* ``models_documented``：那是 DeepSeek 官方的模型清单。拿它冒充 OpenRouter
  可用模型，界面上会出现**根本没在 OpenRouter 开过**的模型名。
* ``model_warning``：DeepSeek 的下线判断只认 DeepSeek 的 ``RETIRED_MODELS``，
  拿它判断 OpenRouter 的模型，结论必然是错的。
* ``list_providers``：同理，必须不编造模型清单。

这些都做成覆盖，而不是在网关里写 ``if provider == ...`` —— 供应商差异应该
留在各自的适配器里，而不是散进调度逻辑。

## 计价：额度与美元是两件事，不能混

商户侧扣的是**冻结的分析额度**（``cost_micro_usd_per_job``，提交时写进快照，
商户确认过价格就不该变）。这是产品刻意的设计。

OpenRouter 按**美元**结算，而且**每次调用的真实成本都不同** —— 消息越长花得
越多。所以：

* **商户扣多少额度** —— 冻结值。这是承诺，不按实时价变。
* **平台实际花了多少美元** —— 从响应里的真实 token 用量折算，记在
  :attr:`ParseOutcome.input_tokens` / ``output_tokens`` 里，由
  :mod:`wechat_ai_assistant.product` 的内部账留存，供运营对账。

把两者混为一谈，就会变成「按实时价扣商户的钱」—— 而商户确认过的不是这个价。

## 归因请求头

OpenRouter 用 ``HTTP-Referer`` 和 ``X-Title`` 把调用归因到某个应用，只影响
他们那边的排行统计，不发也能调用成功。所以**没配置就不发** —— 发一个编造的
域名上去，比不发更糟：那是往对方的数据里写假东西。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Mapping

from business_agent.backends.deepseek import DeepSeekBackend, UpstreamBalanceExhausted

from .deepseek_adapter import _build_agent_request, _build_prompt, _parse_json
from .opencode import (
    MERCHANT_MESSAGES,
    AdapterRejected,
    AdapterTimeout,
    AdapterUnavailable,
    ParseOutcome,
    _CORE_TO_ADAPTER,
    validate_parse_result,
)

LOGGER = logging.getLogger(__name__)

#: OpenRouter 的 OpenAI 兼容端点。
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

#: 环境变量。与 DeepSeek 的分开命名，这样两套上游可以并存。
API_KEY_ENV = "WECHAT_AI_OPENROUTER_API_KEY"
MODEL_ENV = "WECHAT_AI_OPENROUTER_MODEL"
BASE_URL_ENV = "WECHAT_AI_OPENROUTER_BASE_URL"
TIMEOUT_ENV = "WECHAT_AI_OPENROUTER_TIMEOUT"
REFERER_ENV = "WECHAT_AI_OPENROUTER_REFERER"
TITLE_ENV = "WECHAT_AI_OPENROUTER_TITLE"


def api_key_from_env(env: Mapping[str, str] | None = None) -> str:
    """从环境变量读 OpenRouter 密钥；读不到就返回空串，不抛异常。

    故意不抛：装配阶段要依次试多个上游，缺一个密钥不该让整个程序起不来。
    """
    source = os.environ if env is None else env
    return (source.get(API_KEY_ENV) or "").strip()


def build_from_env(
    response_schema: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> "OpenRouterAdapter | None":
    """有密钥才建适配器；没有就返回 ``None``，让装配流程继续试下一个上游。"""
    source = os.environ if env is None else env
    key = api_key_from_env(source)
    if not key:
        return None
    return OpenRouterAdapter(
        api_key=key,
        model=(source.get(MODEL_ENV) or "").strip(),
        base_url=(source.get(BASE_URL_ENV) or "").strip() or DEFAULT_BASE_URL,
        timeout=float(source.get(TIMEOUT_ENV) or 60),
        response_schema=response_schema,
        referer=(source.get(REFERER_ENV) or "").strip(),
        title=(source.get(TITLE_ENV) or "").strip(),
    )


class OpenRouterAdapter:
    """直连 OpenRouter。无会话、无进程、无端口依赖。"""

    #: 计费与统计认这个名字，不是 "deepseek"。
    provider_id = "openrouter"

    #: 归因请求头。没配就是空 —— 见模块开头的说明。
    referer = ""
    title = ""

    def __init__(
        self,
        api_key: str,
        model: str = "",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        temperature: float = 0.0,
        system: str = "",
        response_schema: Mapping[str, Any] | None = None,
        referer: str = "",
        title: str = "",
        backend: DeepSeekBackend | None = None,
    ):
        if not str(api_key).strip():
            raise ValueError("OpenRouter 需要密钥")
        self.model = model or "anthropic/claude-3.5-sonnet"
        self.referer = str(referer or "").strip()
        self.title = str(title or "").strip()
        self._backend = backend or DeepSeekBackend(
            api_key=str(api_key).strip(),
            model=self.model,
            base_url=(base_url or DEFAULT_BASE_URL).rstrip("/"),
            timeout=timeout,
            temperature=temperature,
            system=system,
            response_schema=response_schema,
        )

    # ------------------------------------------------------------- accessors

    @property
    def base_url(self) -> str:
        return self._backend.base_url

    def health(self) -> dict[str, Any]:
        return self.probe()

    def probe(self) -> dict[str, Any]:
        report = dict(self._backend.probe())
        report["provider"] = self.provider_id
        # 刻意**不**回 models_documented：OpenRouter 能用哪些模型取决于账户
        # 开通情况，我们没有权威清单。编一份出来只会让人误以为那就是全部。
        report.pop("models_documented", None)
        report.pop("retired_models", None)
        report["model_configured"] = self._backend.model
        report["pricing_note"] = (
            "OpenRouter 按美元结算；每次调用的真实成本随消息长度变化，"
            "商户侧扣的仍是提交时冻结的分析额度"
        )
        return report

    @property
    def model_warning(self) -> str:
        """OpenRouter 这边**不做** DeepSeek 的下线判断。

        模型是否可用取决于账户开通情况，本地猜不出来。宁可不说，也别套用
        DeepSeek 的下线名单 —— 那会让运营去处理一个不存在的问题。
        """
        return ""

    # --------------------------------------------------- session compatibility
    #
    # OpenRouter 是无状态 HTTP 调用，没有会话可建可删。下面三个方法满足契约
    # 但不做实质动作 —— 网关照常调用，生命周期记账保持统一，换后端不必改调度。

    def create_session(self, job_id: str) -> str:
        """返回的"会话"只是本任务的一次性句柄，供崩溃后对账用。"""
        return f"orr_{job_id}"

    def abort(self, session_id: str) -> bool:
        del session_id
        # 单次 HTTP 调用没有可中断的句柄；取消由提交前检查与任务状态承担。
        return True

    def delete_session(self, session_id: str) -> bool:
        del session_id
        return True

    def reconcile(self, session_id: str) -> dict[str, Any] | None:
        """返回 ``None`` 表示"无法从上游确定结果"。

        网关据此走人工对账流程 —— 这比编造一个"没跑过"的结论强：结果未知时
        宁可保留预留等核实，也不要先退钱再发现其实成功了。
        """
        del session_id
        return None

    def list_providers(self) -> list[dict[str, Any]]:
        """只报一个供应商，**不给模型清单**。

        OpenRouter 的模型目录按账户开通，本地没有权威事实。返回空列表加一句
        说明，比列一堆没法验证的名字诚实 —— 界面上"没有模型"是可解释的，
        "列了一堆但都是猜的"不是。
        """
        return [
            {
                "id": self.provider_id,
                "name": "OpenRouter",
                "connected": True,
                "warning": "",
                "models": [],
                "models_note": "OpenRouter 的可用模型取决于账户开通情况，客户端未内置清单",
            }
        ]

    # -------------------------------------------------------------- execution

    def run_parse(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        job_id: str,
        deadline: float,
        cancel_check: Callable[[], bool] | None = None,
        provider_id: str = "",
        model_id: str = "",
    ) -> ParseOutcome:
        if cancel_check is not None and cancel_check():
            raise AdapterRejected("任务已取消")
        if str(provider_id) and str(provider_id) != self.provider_id:
            # 任务冻结的路由指向别的供应商 —— 不切换。这条路径不静默改用别的模型。
            raise AdapterRejected(MERCHANT_MESSAGES["no_agent"])

        prompt = _build_prompt(payload, job_id)
        request = _build_agent_request(payload, job_id, deadline)
        run_model = str(model_id or self._backend.model)
        try:
            result = self._backend.run(prompt, request)
        except UpstreamBalanceExhausted as exc:
            # 平台侧欠费 —— 不是商户点数的问题，文案要分开。
            LOGGER.error("openrouter account balance exhausted: %s", exc)
            raise AdapterRejected("解析服务账户余额不足，请联系客服") from exc
        except TimeoutError as exc:
            raise AdapterTimeout("分析超时，还没拿到结果。请稍后重试") from exc
        except Exception as exc:
            category = getattr(exc, "category", None)
            if category is not None:
                raise _CORE_TO_ADAPTER.get(category, AdapterUnavailable)() from exc
            raise AdapterUnavailable() from exc

        return ParseOutcome(
            result=validate_parse_result(result.raw or _parse_json(result.text)),
            session_id=session_id,
            message_id="",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model_requested=f"{self.provider_id}/{run_model}",
            # OpenRouter 同样不回显实际执行的模型。**没回标识不等于确认一致**。
            model_reported="",
            model_confirmed=False,
            model_warning="上游未返回模型标识，未能独立确认实际执行的模型",
            request_evidence={
                "method": "POST",
                "path_template": "/chat/completions",
                "provider": self.provider_id,
                "model": run_model,
                "structured_output": "json_object",
                "payload_keys": sorted(str(k) for k in payload) if isinstance(payload, Mapping) else [],
            },
        )


__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "DEFAULT_BASE_URL",
    "MODEL_ENV",
    "OpenRouterAdapter",
    "REFERER_ENV",
    "TIMEOUT_ENV",
    "TITLE_ENV",
    "api_key_from_env",
    "build_from_env",
]