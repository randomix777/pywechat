"""DeepSeek 适配器 —— 网关执行后端的**正式默认实现**。

它实现 :class:`wechat_ai_assistant.opencode.OrderParseAdapter` 契约，因此网关不需要知道
上游是谁。契约里那些「会话」方法在这里是**空实现**：DeepSeek 是无状态的
HTTP 调用，没有会话可建可删。网关仍然会照常调用它们——保留统一的生命周期
记账，也避免为了换后端去改网关的调度逻辑。

## 与遗留 OpenCode 路径的关系

OpenCode 适配器**仍然可以装配**（历史记录与既有测试都还在用），但它不再是
产品启动的前提：没有 OpenCode 地址、没有 OpenCode 进程、没有 Basic 密码，
本适配器照样能把产品跑起来。

事实来源见 :mod:`business_agent.backends.deepseek`。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from business_agent.backends.deepseek import (
    DEFAULT_BASE_URL,
    MODELS,
    RETIRED_MODELS,
    DeepSeekBackend,
    UpstreamBalanceExhausted,
)

from .opencode import (
    MERCHANT_MESSAGES,
    AdapterError,
    AdapterRejected,
    AdapterTimeout,
    AdapterUnavailable,
    InvalidParseOutput,
    OrderParseAdapter,
    OutcomeUnknown,
    ParseOutcome,
    _CORE_TO_ADAPTER,
    build_agent_prompt,
    validate_parse_result,
)

LOGGER = logging.getLogger(__name__)
SUMMARY_OUTPUT_TOKEN_FALLBACK = 16_000


class DeepSeekAdapter:
    """直连 DeepSeek。无会话、无进程、无端口依赖。"""

    #: 上游是 OpenAI 兼容接口，模型标识按官方文档取值。
    provider_id = "deepseek"

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-flash",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        temperature: float = 0.0,
        system: str = "",
        response_schema: Mapping[str, Any] | None = None,
        backend: DeepSeekBackend | None = None,
    ):
        self._backend = backend or DeepSeekBackend(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            temperature=temperature,
            system=system,
            response_schema=response_schema,
        )

    # ------------------------------------------------------------- accessors

    @property
    def model(self) -> str:
        return self._backend.model

    @property
    def base_url(self) -> str:
        return self._backend.base_url

    def health(self) -> dict[str, Any]:
        return self.probe()

    def probe(self) -> dict[str, Any]:
        report = dict(self._backend.probe())
        report["provider"] = self.provider_id
        report["models_documented"] = sorted(MODELS)
        report["retired_models"] = list(RETIRED_MODELS)
        return report

    # ------------------------------------------------- session compatibility
    #
    # DeepSeek 无状态。下面三个方法满足契约但不做事 —— 网关照常调用，
    # 生命周期记账保持统一，换后端不必改调度。

    def create_session(self, job_id: str) -> str:
        """返回的"会话"只是本任务的一次性句柄，供崩溃后对账用。"""
        return f"ds_{job_id}"

    def abort(self, session_id: str) -> bool:
        del session_id
        # 单次 HTTP 调用没有可以中断的句柄；取消由提交前的检查与任务状态承担。
        return True

    def delete_session(self, session_id: str) -> bool:
        del session_id
        return True

    def reconcile(self, session_id: str) -> dict[str, Any] | None:
        """DeepSeek 没有可查询的上游会话。

        返回 ``None`` 表示"无法从上游确定结果"，由网关走人工对账流程 ——
        这比编造一个"没跑过"的结论强：结果未知时宁可保留预留等核实。
        """
        del session_id
        return None

    def list_providers(self) -> list[dict[str, Any]]:
        """DeepSeek 的模型是固定文档，不需要像 OpenCode 那样拉目录。

        返回固定的一条，携带文档事实。**不含任何凭据字段。**
        """
        warning = self._backend.model_warning
        return [
            {
                "id": self.provider_id,
                "name": "DeepSeek",
                "connected": True,
                "warning": warning,
                "models": [
                    {
                        "id": name,
                        "name": label,
                        "cost": {"input": None, "output": None},
                        "capabilities": {"json_object": True},
                    }
                    for name, label in sorted(MODELS.items())
                ],
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

        prompt = self._build_prompt(payload, job_id)
        request = _build_agent_request(payload, job_id, deadline)
        run_model = str(model_id or self._backend.model)
        try:
            result = self._backend.run(prompt, request)
        except UpstreamBalanceExhausted as exc:
            # 平台侧欠费。不是用户的点数问题，文案要分开。
            LOGGER.error("deepseek account balance exhausted: %s", exc)
            raise AdapterRejected("解析服务账户余额不足，请联系客服") from exc
        except Exception as exc:
            raise self._translate(exc) from exc

        return ParseOutcome(
            result=validate_parse_result(result.raw or _parse_json(result.text)),
            session_id=session_id,
            message_id="",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model_requested=f"{self.provider_id}/{run_model}",
            # DeepSeek 不回显实际执行的模型。**没回标识不等于确认一致**。
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

    def run_summary(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        job_id: str,
        deadline: float,
        cancel_check: Callable[[], bool] | None = None,
        provider_id: str = "",
        model_id: str = "",
    ) -> ParseOutcome:
        """摘要任务执行：提示词复用 :mod:`wechat_ai_assistant.summary` 那一份（单源），
        输出是纯文本简报（不是订单 JSON），用摘要自己的校验。"""
        from .summary import SUMMARY_SYSTEM_PROMPT
        from .roles import ROLE_ORDER, analysis_instruction, order_report_merge_instruction
        from business_agent.contracts.protocols import PromptPackage

        if cancel_check is not None and cancel_check():
            raise AdapterRejected("任务已取消")
        if str(provider_id) and str(provider_id) != self.provider_id:
            raise AdapterRejected(MERCHANT_MESSAGES["no_agent"])
        role_id = str(payload.get("role_id") or "")
        analysis_stage = str(payload.get("analysis_stage") or "map")
        try:
            configured_output_limit = int(payload.get("max_output_tokens") or 0)
        except (TypeError, ValueError):
            configured_output_limit = 0
        output_budget = (configured_output_limit if configured_output_limit > 0
                         else SUMMARY_OUTPUT_TOKEN_FALLBACK)
        raw_messages = payload.get("messages", [])
        if role_id == ROLE_ORDER and analysis_stage == "reduce":
            prompt_system = order_report_merge_instruction()
            prompt_payload = {
                "reports": [str(item.get("content") or "")
                            for item in raw_messages if isinstance(item, Mapping)]
            }
        else:
            prompt_system = (analysis_instruction(role_id) if role_id
                             else SUMMARY_SYSTEM_PROMPT)
            prompt_payload = {"messages": raw_messages}
        prompt = PromptPackage(
            system=prompt_system,
            payload=prompt_payload,
            run_label=job_id,
            max_output_tokens=output_budget,
        )
        request = _build_agent_request(
            payload, job_id, deadline, task_type="summary",
            max_output_tokens=output_budget)
        run_model = str(model_id or self._backend.model)
        try:
            result = self._backend.run(prompt, request)
        except UpstreamBalanceExhausted as exc:
            LOGGER.error("deepseek account balance exhausted: %s", exc)
            raise AdapterRejected("解析服务账户余额不足，请联系客服") from exc
        except Exception as exc:
            raise self._translate(exc) from exc
        text = validate_summary_result(result.text)
        return ParseOutcome(
            result={"summary": text},
            session_id=session_id,
            message_id="",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model_requested=f"{self.provider_id}/{run_model}",
            model_reported="",
            model_confirmed=False,
            model_warning="上游未返回模型标识，未能独立确认实际执行的模型",
            request_evidence={
                "method": "POST",
                "path_template": "/chat/completions",
                "provider": self.provider_id,
            },
        )

    def _translate(self, exc: Exception) -> AdapterError:
        """core 的错误分类 → 网关认识的适配器异常。"""
        category = getattr(exc, "category", None)
        if category is not None:
            return _CORE_TO_ADAPTER.get(category, AdapterUnavailable)()
        return AdapterUnavailable()


def _parse_json(text: str) -> dict[str, Any]:
    import json

    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        newline = stripped.find("\n")
        if newline >= 0:
            stripped = stripped[newline + 1:]
        stripped = stripped.rsplit("```", 1)[0]
    try:
        value = json.loads(stripped)
    except ValueError as exc:
        raise InvalidParseOutput("模型没有返回可解析的 JSON") from exc
    if not isinstance(value, dict):
        raise InvalidParseOutput("模型返回的不是 JSON 对象")
    return value


def _build_prompt(payload: Mapping[str, Any], job_id: str):
    """构造送进 DeepSeek 的提示包。

    提示词**复用订单插件那一份**，不另写：它已经写清了防注入（群消息是数据
    不是指令）、证据必须逐字、商品只能来自目录、不编造到账状态这些硬要求。
    另写一份必然与它漂移，而漂移的那份就是会在生产上出错的那份。
    """
    from order_agent.plugin import SYSTEM_PROMPT
    from business_agent.contracts.protocols import PromptPackage

    body = payload.get("messages") if isinstance(payload, Mapping) else None
    data = {
        "messages": [str(item) for item in (body or [])],
        "context": payload.get("context") if isinstance(payload, Mapping) else None,
    }
    return PromptPackage(
        system=SYSTEM_PROMPT,
        payload=data,
        run_label=job_id,
        max_output_tokens=2000,
    )


def _build_agent_request(payload: Mapping[str, Any], job_id: str, deadline: float,
                         *, task_type: str = "order_parse",
                         max_output_tokens: int = 2000) -> Any:
    """构造 core 的 :class:`AgentRequest`。

    这里刻意**只带必要字段**：租户标识用来做上游隔离，事件就是待解析的正文。
    不把订单、客户或支付信息塞进请求体。
    """
    from datetime import datetime, timezone

    from business_agent.contracts.request import AgentRequest, AsOf, Budget, Event, Scope

    now = datetime.now(timezone.utc)
    body = payload.get("messages") if isinstance(payload, Mapping) else None
    lines = [str(item) for item in (body or []) if str(item).strip()] or [str(payload)]
    events = tuple(
        Event(event_id=f"{job_id}-e{index}", body=line, occurred_at=now)
        for index, line in enumerate(lines[:50])
    )
    remaining = max(5.0, float(deadline) - datetime.now(timezone.utc).timestamp()) if deadline else 30.0
    return AgentRequest(
        request_id=job_id,
        idempotency_key=job_id,
        task_type=task_type,
        scope=Scope(tenant_id=str((payload or {}).get("merchant_id") or "default")),
        events=events,
        as_of=AsOf(instant=now),
        budget=Budget(max_steps=1, max_output_tokens=max_output_tokens,
                      timeout_seconds=remaining),
    )


__all__ = [
    "DeepSeekAdapter",
    "AdapterRejected",
    "AdapterTimeout",
    "AdapterUnavailable",
    "InvalidParseOutput",
    "OrderParseAdapter",
    "OutcomeUnknown",
    "ParseOutcome",
]
