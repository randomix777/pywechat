"""JYI 文字的解析与摘要执行：提示词与校验复用仓库现货，直调网关 chat 接口。

## 和产品网关任务的区别

产品网关（`gateway.py`）是"提交任务 → 轮询 → 结算"：提示词在服务端，
输出在服务端校验，计费按任务冻结。JYI 网关只有透传的 chat 接口，
所以三件事挪到客户端做：

* 提示词：订单用 `order_agent` 的 SYSTEM_PROMPT，摘要用 `summary` 的
  SUMMARY_SYSTEM_PROMPT —— **和服务端用的是同一份，不另写**；
* 校验：订单用 `validate_order_parse_result`，摘要只收非空文本；
* 落地：校验通过走同一套 `apply_cloud_suggestion`，失败进人工核对。

钥匙用 JYI 登录态，不读本地任何密钥。服务接口和
:class:`LocalParseService` 对齐（`submit` / `settle`），管线不用改。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from .local_parse import (
    LOCAL_BLOCKED,
    LOCAL_DONE,
    LOCAL_NEEDS_RETRY,
    LOCAL_PENDING,
    STATUS_NETWORK_DOWN,
    LocalParseService,
    ParseProgress,
)

SUMMARY_MAX_OUTPUT_TOKENS = 2000
ORDER_MAX_OUTPUT_TOKENS = 2000


def build_order_messages(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """订单解析的提示包：系统提示词 + 最小化上下文 JSON。"""
    from order_agent.plugin import SYSTEM_PROMPT

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(dict(request), ensure_ascii=False, default=str)},
    ]


def build_summary_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """摘要的提示包：系统提示词 + 消息列表。"""
    from .summary import SUMMARY_SYSTEM_PROMPT

    lines = []
    for item in messages or []:
        if not isinstance(item, Mapping):
            continue
        sender = str(item.get("sender", "") or "")
        content = str(item.get("content", "") or "")
        sent_at = str(item.get("sent_at", "") or "")
        kind = str(item.get("kind", "text") or "text")
        line = f"{sender}：{content}"
        if sent_at:
            line += f"（{sent_at}）"
        if kind not in ("text", "text_message", ""):
            line += f"［{kind}］"
        lines.append(line)
    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def build_role_analysis_messages(messages: list[dict[str, Any]], role_id: str) -> list[dict[str, Any]]:
    """Build role-directed analysis instructions and the current batch."""
    from .roles import analysis_instruction

    lines = []
    for item in messages or []:
        if not isinstance(item, Mapping):
            continue
        sender = str(item.get("sender", "") or "")
        content = str(item.get("content", "") or "")
        sent_at = str(item.get("sent_at", "") or "")
        kind = str(item.get("kind", "text") or "text")
        line = f"{sender}：{content}"
        if sent_at:
            line += f"（{sent_at}）"
        if kind not in ("text", "text_message", ""):
            line += f"［{kind}］"
        lines.append(line)
    return [
        {"role": "system", "content": analysis_instruction(role_id)},
        {"role": "user", "content": "请分析以下本次读取的消息：\n" + "\n".join(lines)},
    ]


def extract_text(body: Any) -> str:
    """从 chat 响应里拿正文。拿不到返回空串，调用方判空处理。"""
    try:
        choices = (body or {}).get("choices") or []
        message = (choices[0] or {}).get("message") or {}
        return str(message.get("content") or "").strip()
    except (AttributeError, IndexError, TypeError):
        return ""


def extract_usage(body: Any) -> dict[str, int]:
    """用量（token 数）。没有就空 dict —— 不编造。"""
    try:
        usage = (body or {}).get("usage") or {}
        return {
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        }
    except (AttributeError, TypeError, ValueError):
        return {}


def parse_order_json(text: str) -> dict[str, Any]:
    """模型输出 → 订单建议。去 markdown 包裹，结构走同一套校验。"""
    from .opencode import InvalidParseOutput, validate_parse_result

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
    return validate_parse_result(value)


class JYIParseService(LocalParseService):
    """走 JYI chat 接口的解析服务。接口与 LocalParseService 对齐。

    同步执行：submit 里调完模型、校验完、落完库；settle 直接返回结果。
    计费不在本地算 —— JYI 侧按账号计量，客户端只透传用量。
    """

    def submit(self, message_id: str, on_progress: Callable[[ParseProgress], None] | None = None) -> ParseProgress:
        emit = on_progress or (lambda _progress: None)
        try:
            request = self.build_request(message_id)
        except ValueError as exc:
            emit(ParseProgress(LOCAL_BLOCKED, str(exc), message_id=message_id))
            return ParseProgress(LOCAL_BLOCKED, str(exc), message_id=message_id)
        emit(ParseProgress(LOCAL_PENDING, "正在分析", message_id=message_id))
        try:
            model = str(getattr(self, "model_alias", "") or self.client.text_model())
            body = self.client.chat_completions(
                model, build_order_messages(request),
                response_format={"type": "json_object"},
                max_tokens=ORDER_MAX_OUTPUT_TOKENS)
            text = extract_text(body)
            result = parse_order_json(text)
        except Exception as exc:
            from .client_login import GatewayError

            if isinstance(exc, GatewayError) and exc.code in {
                    "network_unavailable", "backend_unavailable"}:
                emit(ParseProgress(LOCAL_NEEDS_RETRY, STATUS_NETWORK_DOWN, message_id=message_id))
                return ParseProgress(LOCAL_NEEDS_RETRY, STATUS_NETWORK_DOWN, message_id=message_id)
            message = getattr(exc, "merchant_message", "") or str(exc) or "解析失败"
            emit(ParseProgress(LOCAL_NEEDS_RETRY, message, message_id=message_id, needs_review=True))
            return ParseProgress(LOCAL_NEEDS_RETRY, message, message_id=message_id, needs_review=True)
        usage = extract_usage(body)
        applied = self.orders.apply_cloud_suggestion(
            self.tenant, self.group, message_id, result, job_id="")
        needs_review = applied["outcome"] in {"review", "rejected", "clarify", "payment_claim"}
        labels = {
            "ignored": "无需处理",
            "duplicate": "已处理过，未重复建单",
            "clarify": "需要向客户问一句，已记录",
            "payment_claim": "已记录客户声称付款，请核对收款",
        }
        if applied["outcome"] in labels:
            label = labels[applied["outcome"]]
        elif needs_review:
            label = "需要您核对"
        else:
            label = "已生成待确认订单"
        progress = ParseProgress(
            LOCAL_DONE, label, message_id=message_id, job_id="",
            order_id=applied.get("order_id") or "",
            clarification_id=applied.get("clarification_id") or "",
            needs_review=needs_review,
        )
        self._last_progress = progress
        self._last_usage = dict(usage)
        emit(progress)
        return progress

    def settle(self, on_progress: Callable[[ParseProgress], None] | None = None) -> ParseProgress:
        """同步服务没有待轮询的：返回 submit 的结果。"""
        emit = on_progress or (lambda _progress: None)
        progress = getattr(self, "_last_progress", None)
        if progress is None:
            progress = ParseProgress(LOCAL_PENDING, "没有待处理的解析")
        emit(progress)
        return progress

    def last_usage(self) -> dict[str, int]:
        """最近一次调用的 token 用量。界面展示用，不做结算依据。"""
        return dict(getattr(self, "_last_usage", None) or {})


__all__ = [
    "JYIParseService",
    "ORDER_MAX_OUTPUT_TOKENS",
    "SUMMARY_MAX_OUTPUT_TOKENS",
    "build_order_messages",
    "build_summary_messages",
    "build_role_analysis_messages",
    "extract_text",
    "extract_usage",
    "parse_order_json",
]
