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
from pathlib import Path
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

# Output budget is independent from the model's input context window. JYI's
# DEFAULT alias does not publish per-model limits, so use a practical larger
# request and keep adaptive chunk splitting as the fallback.
SUMMARY_MAX_OUTPUT_TOKENS = 16000
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


def add_image_parts(prompt_messages: list[dict[str, Any]],
                    messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add cached images as OpenAI-compatible image_url parts; leave AV out."""
    from .media_attachments import image_data_url

    parts: list[dict[str, Any]] = []
    added = 0
    missing = 0
    for item in messages:
        if str(item.get("kind", "")).lower() != "image":
            continue
        paths = item.get("attachments") or []
        if not paths:
            missing += 1
        for path in paths:
            if added >= 20:
                break
            try:
                data_url = image_data_url(str(path))
            except (OSError, ValueError):
                missing += 1
                item["media_status"] = "图片缓存无法解码，未纳入视觉分析"
                continue
            parts.append({"type": "text", "text":
                          f"图片消息：{item.get('sender', '')} {item.get('sent_at', '')}"})
            parts.append({"type": "image_url", "image_url":
                          {"url": data_url, "detail": "high"}})
            added += 1
    if not added and not missing:
        return prompt_messages
    if len(prompt_messages) < 2:
        return prompt_messages
    result = [dict(message) for message in prompt_messages]
    user = dict(result[-1])
    text = str(user.get("content") or "")
    guidance = ("\n说明：图片附件会参与分析；音频和视频仅作为附件保留，不分析其内容。"
                "没有成功附加的图片不可推测。")
    if missing:
        guidance += f"另有 {missing} 条图片消息未能从本地缓存取得图片内容。"
    if added >= 20:
        guidance += "本次最多发送 20 张图片，其余图片不参与视觉分析。"
    user["content"] = [{"type": "text", "text": text + guidance}, *parts]
    result[-1] = user
    return result


def add_document_text(prompt_messages: list[dict[str, Any]],
                      messages: list[dict[str, Any]], *,
                      max_chars_per_document: int = 20000) -> list[dict[str, Any]]:
    """Extract supported document attachments locally and send their text."""
    from .media_attachments import document_text

    parts: list[dict[str, str]] = []
    unavailable: list[str] = []
    for item in messages:
        if str(item.get("kind", "")).lower() != "file":
            continue
        prepared_text = str(item.get("_analysis_document_text") or "")
        prepared_name = str(item.get("_analysis_document_name") or "")
        prepared_part = str(item.get("_analysis_document_part") or "")
        if prepared_text:
            label = prepared_name or "附件"
            if prepared_part:
                label += f"（第 {prepared_part} 段）"
            parts.append({"type": "text", "text":
                          f"\n\n文件附件正文（{label}）：\n{prepared_text}"})
            continue
        extracted_any = False
        for path in item.get("attachments") or []:
            try:
                extracted = document_text(str(path), max_chars=max_chars_per_document)
            except (OSError, ValueError, RuntimeError):
                extracted = ""
            if extracted:
                name = Path(str(path)).name
                parts.append({"type": "text", "text":
                              f"\n\n文件附件正文（{name}）：\n{extracted}"})
                extracted_any = True
                break
        if not extracted_any:
            unavailable.append(str(item.get("_analysis_document_name")
                                   or ", ".join(Path(str(path)).name
                                                 for path in item.get("attachments") or [])
                                   or "未读取的文档附件"))
    if (not parts and not unavailable) or len(prompt_messages) < 2:
        return prompt_messages
    result = [dict(message) for message in prompt_messages]
    user = dict(result[-1])
    content = user.get("content") or ""
    if unavailable:
        note = ("\n\n说明：以下文档未能提取到正文，请勿推测文件内容："
                + "、".join(unavailable))
        if isinstance(content, list):
            content = [*content, {"type": "text", "text": note}]
        else:
            content = str(content) + note
    if isinstance(content, list):
        user["content"] = [*content, *parts]
    else:
        user["content"] = [{"type": "text", "text": str(content)}, *parts]
    result[-1] = user
    return result


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
    """从常见 OpenAI 兼容响应中提取可展示正文。"""
    if not isinstance(body, Mapping):
        return ""

    def text_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, Mapping):
                    # Chat Completions multimodal/text content parts and
                    # Responses API output_text parts.
                    if item.get("type") in {"text", "output_text", None}:
                        part = item.get("text", item.get("output_text"))
                        if isinstance(part, str):
                            parts.append(part)
            return "".join(parts)
        return ""

    direct = text_value(body.get("output_text"))
    if direct.strip():
        return direct.strip()

    choices = body.get("choices") or []
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        choice = choices[0]
        message = choice.get("message") or {}
        if isinstance(message, Mapping):
            content = text_value(message.get("content"))
            if content.strip():
                return content.strip()
        # Some compatible gateways return a text completion or streaming-like
        # delta object even for a non-streaming request.
        content = text_value(choice.get("text"))
        if not content:
            delta = choice.get("delta") or {}
            if isinstance(delta, Mapping):
                content = text_value(delta.get("content"))
        if content.strip():
            return content.strip()

    # Responses API style: output[].content[].text
    output = body.get("output") or []
    if isinstance(output, list):
        parts = []
        for item in output:
            if not isinstance(item, Mapping):
                continue
            parts.append(text_value(item.get("content")))
            parts.append(text_value(item.get("text")))
        combined = "".join(parts).strip()
        if combined:
            return combined
    return ""


def empty_text_detail(body: Any) -> str:
    """Return provider-neutral metadata explaining an empty model response."""
    if not isinstance(body, Mapping):
        return "模型未生成可展示的分析内容。"
    choices = body.get("choices") or []
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        reason = str(choices[0].get("finish_reason") or "").strip().lower()
        if reason == "length":
            return "模型输出达到长度上限，未生成可展示的分析内容；请缩短消息范围后重试。"
        if reason in {"content_filter", "safety"}:
            return "模型未能生成可展示的分析内容，请调整消息范围后重试。"
    status = str(body.get("status") or "").strip().lower()
    if status in {"incomplete", "failed", "cancelled"}:
        return "模型未能完成本次分析，请稍后重试。"
    return "模型未生成可展示的分析内容。"


def output_was_truncated(body: Any) -> bool:
    """Detect common OpenAI-compatible output-token limit indicators."""
    if not isinstance(body, Mapping):
        return False
    choices = body.get("choices") or []
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        if str(choices[0].get("finish_reason") or "").strip().lower() == "length":
            return True
    details = body.get("incomplete_details") or {}
    return (str(body.get("status") or "").strip().lower() == "incomplete"
            and isinstance(details, Mapping)
            and str(details.get("reason") or "").strip().lower()
            in {"max_output_tokens", "length"})


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
            from .client_login import GatewayError, merchant_safe_error

            if isinstance(exc, GatewayError) and exc.code in {
                    "network_unavailable", "backend_unavailable"}:
                emit(ParseProgress(LOCAL_NEEDS_RETRY, STATUS_NETWORK_DOWN, message_id=message_id))
                return ParseProgress(LOCAL_NEEDS_RETRY, STATUS_NETWORK_DOWN, message_id=message_id)
            message = merchant_safe_error(
                getattr(exc, "code", ""),
                getattr(exc, "merchant_message", "") or str(exc),
                fallback="解析暂时未能完成，请稍后重试。",
            )
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
    "add_image_parts",
    "add_document_text",
    "build_role_analysis_messages",
    "extract_text",
    "output_was_truncated",
    "extract_usage",
    "parse_order_json",
]
