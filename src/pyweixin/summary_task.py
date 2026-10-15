"""摘要与参考信息：**纯逻辑**，界面无关。

## 为什么要把它从界面里搬出来

摘要的「取哪些消息」「能不能放行」「密钥从哪来」这几件事，最早写在
``merchant_ui.py`` 里。后来又要加一个 Web 界面 —— 如果照抄一份，就会有
两份判据。而两份判据必然在某次改动后分叉：

    第一处（按钮显不显示）改成「库里还有就放行」
    第二处（点了之后放不放行）还是老判据
    → 按钮出现了，一点却弹「还没有读到消息」

这个 bug 本轮真实发生过。所以：**判据只有一份，两个界面都从这里取。**

## 界面层只负责三件事

* 决定什么时候调（用户点了）
* 决定怎么显示（文字、表格、进度条）
* 保证不阻塞（后台线程）

界面**不许**自己判断「能不能放行」「取哪些消息」—— 那会分叉。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

#: 旧服务端单次任务的兼容上限。客户端会按会话拆成更小的请求批次。
SUMMARY_MESSAGE_LIMIT = 200
# When a model publishes its limits, batches target at most this many estimated
# input tokens to avoid routinely sending giant, slow, expensive requests.
SUMMARY_CHUNK_TARGET_TOKENS = 20_000
# Large upstream ceilings are model limits, not useful per-report targets.
SUMMARY_REPORT_OUTPUT_TARGET_TOKENS = 8_000
# No upstream context metadata: keep the existing conservative fallback scale.
SUMMARY_CHUNK_FALLBACK_TOKENS = 30_000
SUMMARY_CHUNK_MAX_IMAGES = 5
SUMMARY_CHUNK_MAX_DOCUMENT_CHARS = 12_000
SUMMARY_CHUNK_MAX_IMAGE_BYTES = 5 * 1024 * 1024

LOGGER = logging.getLogger("pyweixin.summary")
_SUMMARY_RUN_LOCK = threading.Lock()


def _app(target: Any) -> Any:
    """把传进来的东西解析成「应用对象」，认不出来就返回 ``None``。

    调用方可能传**界面**（里面有 ``.app``），也可能直接传应用对象。
    界面逻辑还会被单测用**桩对象**直接驱动 —— 桩上可能既没有 ``orders``
    也没有 ``app``。

    所以这里三层都试：认不出来就返回 None，调用方自然退回旧写法。
    绝不能在这儿抛 AttributeError：那是「界面只是少个字段」的问题，
    不该让整页渲染失败。
    """
    if target is None:
        return None
    if hasattr(target, "orders"):
        return target
    inner = getattr(target, "app", None)
    if inner is not None and hasattr(inner, "orders"):
        return inner
    return None


def accumulated_coverage(app: Any, result: Any) -> dict[str, tuple[int, str, str]]:
    """这些会话**积累下来**的总量：条数、最早、最晚。

    「读取全部」报的是总量，「更新历史」报的是增量 —— 两者写在界面上同一个
    位置时，商户看到的是两个不同的量却难以分辨。查一下库里到底有多少，
    让那一列始终代表**总量**。

    查不到就返回空字典，界面退回原来的写法 —— 一次查询失败不该让读取
    结果看不见。
    """
    target = _app(app)
    store = getattr(target, "orders", None)
    tenant = getattr(target, "tenant", "")
    groups: list[str] = []
    for item in getattr(result, "coverage", None) or []:
        conversation = getattr(item, "conversation", None)
        name = getattr(conversation, "name", "") if conversation is not None else ""
        if name and name not in groups:
            groups.append(str(name))
    if store is None or not groups:
        return {}
    marks = ",".join("?" for _ in groups)
    try:
        rows = store.db.execute(
            "SELECT group_id, COUNT(*) AS n,"
            " MIN(COALESCE(sent_at, observed_at)) AS lo,"
            " MAX(COALESCE(sent_at, observed_at)) AS hi"
            " FROM messages WHERE merchant_id=? AND group_id IN (" + marks + ")"
            " GROUP BY group_id", [tenant, *groups]).fetchall()
    except Exception:
        return {}
    return {str(r["group_id"]): (int(r["n"]), str(r["lo"] or ""), str(r["hi"] or ""))
            for r in rows}


def has_messages_for(app: Any, result: Any) -> bool:
    """这个群到底有没有可用的消息 —— **放行的唯一判据**。

    **不能只看 ``result.total_messages``。** 那是**本次**读到的条数：更新时
    发现没有新消息就是 0，可库里可能已经积累了几百条。

    只看它就会出现这种自相矛盾的界面：按钮放出来了，一点却弹「还没有读到
    消息，请先点开始读取」，而商户刚刚才读完一大群。
    """
    if result is None:
        return False
    if getattr(result, "total_messages", 0):
        return True
    return any(n for n, _, _ in accumulated_coverage(app, result).values())


def messages_for_summary(app: Any, result: Any) -> list[dict[str, Any]]:
    """按会话取全量，不按「最后一批导入」取。

    「更新」之后本次可能只有 1 条新增，但库里已经有几百条 —— 如果只取
    本次导入批次，摘要范围会塌缩成那 1 条，之前的内容等于白读了。
    所以：覆盖到的会话一律取库里全部已存消息（按时间正序，LIMIT 兜底）；
    只有 coverage 为空的老结果对象，才回退到按 import_ids 取当批。
    """
    target = _app(app)
    store = getattr(target, "orders", None)
    tenant = getattr(target, "tenant", "")
    if store is None:
        return []
    labels = _conversation_labels(result)
    groups: list[str] = []
    for item in getattr(result, "coverage", None) or []:
        conversation = getattr(item, "conversation", None)
        name = str(getattr(conversation, "name", "") or "") if conversation is not None else ""
        if name and not getattr(item, "error", "") and name not in groups:
            groups.append(name)
    if groups:
        marks = ",".join("?" for _ in groups)
        rows = store.db.execute(
            "SELECT group_id, sender, content, kind, media_json, COALESCE(sent_at, observed_at) AS sent_at"
            " FROM messages WHERE merchant_id=? AND group_id IN (" + marks + ")"
            " ORDER BY group_id, COALESCE(sent_at_epoch, strftime('%s', observed_at)), observed_at, id"
            " LIMIT " + str(int(SUMMARY_MESSAGE_LIMIT)),
            [tenant, *groups],
        ).fetchall()
        return _attach_media({"group_id": str(row["group_id"] or ""),
                              "group_label": labels.get(str(row["group_id"] or ""), str(row["group_id"] or "")),
                              **_row_to_message(row)}
                             for row in rows)
    import_ids = [str(x) for x in (getattr(result, "import_ids", None) or []) if x]
    if import_ids:
        marks = ",".join("?" for _ in import_ids)
        rows = store.db.execute(
            "SELECT group_id, sender, content, kind, media_json, COALESCE(sent_at, observed_at) AS sent_at"
            " FROM messages WHERE merchant_id=? AND import_id IN (" + marks + ")"
            " ORDER BY group_id, COALESCE(sent_at_epoch, strftime('%s', observed_at)), observed_at, id"
            " LIMIT " + str(int(SUMMARY_MESSAGE_LIMIT)),
            [tenant, *import_ids],
        ).fetchall()
        return _attach_media({"group_id": str(row["group_id"] or ""),
                              "group_label": labels.get(str(row["group_id"] or ""), str(row["group_id"] or "")),
                              **_row_to_message(row)}
                             for row in rows)
    return []


def history_message_counts(app: Any, result: Any) -> list[tuple[str, int]]:
    """Return stored message counts for successfully read selected conversations."""
    target = _app(app)
    store = getattr(target, "orders", None)
    tenant = getattr(target, "tenant", "")
    counts: list[tuple[str, int]] = []
    if store is None:
        return counts
    names = []
    for item in getattr(result, "coverage", None) or ():
        conversation = getattr(item, "conversation", None)
        name = str(getattr(conversation, "name", "") or "")
        if name and not getattr(item, "error", "") and name not in names:
            names.append(name)
    if not names:
        return counts
    marks = ",".join("?" for _ in names)
    rows = store.db.execute(
        "SELECT group_id, COUNT(*) AS n FROM messages WHERE merchant_id=? AND group_id IN ("
        + marks + ") GROUP BY group_id", [tenant, *names]).fetchall()
    found = {str(row["group_id"]): int(row["n"]) for row in rows}
    return [(name, found.get(name, 0)) for name in names]


def _conversation_labels(result: Any) -> dict[str, str]:
    labels: dict[str, str] = {}
    for item in getattr(result, "coverage", None) or ():
        conversation = getattr(item, "conversation", None)
        key = str(getattr(conversation, "name", "") or "")
        label = str(getattr(conversation, "disambiguation", "") or key)
        if key:
            labels[key] = label
    return labels


def messages_for_history(app: Any, result: Any) -> list[dict[str, Any]]:
    """Fetch all stored messages from successful selected conversations."""
    target = _app(app)
    store = getattr(target, "orders", None)
    tenant = getattr(target, "tenant", "")
    names = []
    for item in getattr(result, "coverage", None) or ():
        conversation = getattr(item, "conversation", None)
        name = str(getattr(conversation, "name", "") or "")
        if name and not getattr(item, "error", "") and name not in names:
            names.append(name)
    if store is None or not names:
        return []
    labels = _conversation_labels(result)
    marks = ",".join("?" for _ in names)
    rows = store.db.execute(
        "SELECT group_id, sender, content, kind, media_json, COALESCE(sent_at, observed_at) AS sent_at"
        " FROM messages WHERE merchant_id=? AND group_id IN (" + marks + ")"
        " ORDER BY group_id, COALESCE(sent_at_epoch, strftime('%s', observed_at)), id",
        [tenant, *names]).fetchall()
    return _attach_media({"group_id": str(row["group_id"] or ""),
                          "group_label": labels.get(str(row["group_id"] or ""), str(row["group_id"] or "")),
                          **_row_to_message(row)}
                         for row in rows)


def _row_to_message(row: Any) -> dict[str, Any]:
    """一行消息 → 给模型的形状。**必须带上 kind**。

    图片消息的 ``content`` 是占位符「图片」，**语音消息的 content 直接是空的**。
    不带 kind，模型看到的是「王姨：（空）」，完全不知道发生了什么。
    """
    try:
        media = json.loads(row["media_json"] or "{}")
    except (KeyError, TypeError, ValueError):
        media = {}
    return {"sender": row["sender"], "content": row["content"],
            "kind": row["kind"], "sent_at": row["sent_at"],
            "media": media if isinstance(media, dict) else {}}


def _attach_media(messages: Any) -> list[dict[str, Any]]:
    rows = [dict(message) for message in messages]
    try:
        from .media_attachments import attach_cached_files
        return attach_cached_files(rows)
    except Exception:
        return rows


#: 云摘要任务轮询间隔（秒）与总超时。网关任务排队+模型执行，
#: 比直调慢，给足时间；超时不代表扣费（以服务端结算为准）。
SUMMARY_POLL_INTERVAL = 2.0
SUMMARY_TIMEOUT = 180.0


def _gateway_client(app: Any) -> Any | None:
    """拿网关客户端：没登录（没 client/没 token）返回 None。"""
    target = _app(app)
    client = getattr(target, "client", None)
    if client is None:
        return None
    session = getattr(client, "session", None)
    if session is None or not getattr(session, "access_token", ""):
        return None
    return client


def _is_jyi_client(client: Any) -> bool:
    try:
        from .jyi_client import JYIClient

        return isinstance(client, JYIClient)
    except Exception:
        return False


def _estimate_tokens(text: str) -> int:
    """Conservative multilingual text estimate when the provider has no counter."""
    cjk = 0
    other_non_ascii = 0
    ascii_chars = 0
    for char in text:
        code = ord(char)
        if (0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF
                or 0x20000 <= code <= 0x3134F):
            cjk += 1
        elif code < 128:
            ascii_chars += 1
        else:
            other_non_ascii += 1
    # Chinese ideographs tend to consume about one token each; use 1.5 for
    # margin. Count other Unicode characters whole and ASCII roughly by word.
    return int(cjk * 1.5 + other_non_ascii + ascii_chars / 3.2 + 0.999)


def _summary_chunks(messages: list[dict[str, Any]],
                    token_budget: int = SUMMARY_CHUNK_FALLBACK_TOKENS,
                    max_document_chars: int = 20000) -> list[list[dict[str, Any]]]:
    """Split with separate text, image-count, and extracted-document budgets."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    tokens = 0
    images = 0
    image_bytes = 0
    document_chars = 0
    for message in messages:
        kind = str(message.get("kind", "")).lower()
        size = (_estimate_tokens(str(message.get("content", "") or ""))
                + _estimate_tokens(str(message.get("sender", "") or "")) + 32)
        document_text = str(message.get("_analysis_document_text") or "")
        document_size = len(document_text)
        image_size = 0
        if kind == "image":
            # High-detail image tokenization is resolution/model dependent.
            size += 3000
            paths = message.get("attachments") or []
            if paths:
                try:
                    image_size = min(2 * 1024 * 1024, Path(str(paths[0])).stat().st_size)
                except OSError:
                    image_size = 1024 * 1024
            else:
                image_size = 1024 * 1024
        elif kind == "file" and not document_text:
            # For unprepared callers, count a bounded extraction once here.
            from .media_attachments import document_text
            for path in message.get("attachments") or []:
                try:
                    extracted = document_text(str(path), max_chars=max_document_chars)
                    document_size += len(extracted)
                    size += _estimate_tokens(extracted)
                except (OSError, ValueError, RuntimeError):
                    pass
        size += _estimate_tokens(document_text)
        is_image = kind == "image"
        document_budget = min(SUMMARY_CHUNK_MAX_DOCUMENT_CHARS,
                              max_document_chars if max_document_chars > 0 else SUMMARY_CHUNK_MAX_DOCUMENT_CHARS)
        if current and (tokens + size > token_budget
                        or (is_image and images >= SUMMARY_CHUNK_MAX_IMAGES)
                        or (image_size and image_bytes + image_size > SUMMARY_CHUNK_MAX_IMAGE_BYTES)
                        or (document_size and document_chars + document_size > document_budget)):
            chunks.append(current)
            current, tokens, images, image_bytes, document_chars = [], 0, 0, 0, 0
        current.append(message)
        tokens += size
        images += int(is_image)
        image_bytes += image_size
        document_chars += document_size
    if current:
        chunks.append(current)
    return chunks


def _split_document_text(text: str, max_chars: int) -> list[str]:
    """Split extracted document text near paragraph boundaries without dropping text."""
    if not text:
        return []
    limit = max(256, int(max_chars))
    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + int(limit * 0.75), end)
            if boundary > start:
                end = boundary + 1
        parts.append(text[start:end])
        start = end
    return parts


def _prepare_document_segments(message: Mapping[str, Any],
                                 max_document_chars: int) -> tuple[str, list[str]]:
    """Extract one file message into bounded text segments.

    有正文就按段拆出来进模型；取不到正文就返回空 —— 附件本身仍在结果页
    展示，模型不许猜文件里是什么。调用方把每段包成独立的
    ``DOCX/PDF 文档`` 批次，``_analysis_document_text`` 预置好给
    ``add_document_text`` 复用（不再读第二次盘）。
    """
    from .media_attachments import document_text

    prepared = str(message.get("_analysis_document_text") or "")
    name = str(message.get("_analysis_document_name") or "")
    if prepared:
        limit = max(256, int(max_document_chars or SUMMARY_CHUNK_MAX_DOCUMENT_CHARS))
        return name, _split_document_text(prepared, limit)
    per_file = max(256, min(int(max_document_chars or SUMMARY_CHUNK_MAX_DOCUMENT_CHARS),
                             SUMMARY_CHUNK_MAX_DOCUMENT_CHARS))
    for path in message.get("attachments") or []:
        try:
            extracted = document_text(str(path), max_chars=per_file)
        except (OSError, ValueError, RuntimeError):
            continue
        if extracted:
            return Path(str(path)).name, _split_document_text(extracted, per_file)
    return "", []


def _summary_work_batches(messages: list[dict[str, Any]], *, token_budget: int,
                           max_document_chars: int) -> list[tuple[str, list[dict[str, Any]]]]:
    """按媒资类别分批：纯文字 / 图片 / DOCX-PDF 文档。

    音频视频不进模型（只在结果页当附件展示，提示词里也这么约束模型）。
    图片按张数切批，文档按提取正文切段 —— 大群里几十张图、几万字文档
    不会挤进同一个请求里。
    """
    texts: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        kind = str(message.get("kind", "text") or "text").lower()
        if kind in ("text", "text_message", ""):
            if str(message.get("content", "") or "").strip():
                texts.append(dict(message))
        elif kind == "image":
            images.append(dict(message))
        elif kind == "file":
            files.append(dict(message))
    batches: list[tuple[str, list[dict[str, Any]]]] = [
        ("纯文字", chunk) for chunk in _summary_chunks(
            texts, token_budget=token_budget,
            max_document_chars=max_document_chars)]
    for start in range(0, len(images), SUMMARY_CHUNK_MAX_IMAGES):
        batches.append(("图片", images[start:start + SUMMARY_CHUNK_MAX_IMAGES]))
    for item in files:
        name, segments = _prepare_document_segments(item, max_document_chars)
        for index, segment in enumerate(segments):
            part = dict(item)
            part["_analysis_document_text"] = segment
            part["_analysis_document_name"] = name
            if len(segments) > 1:
                part["_analysis_document_part"] = f"{index + 1}/{len(segments)}"
            batches.append(("DOCX/PDF 文档", [part]))
    return batches


def _text_analysis_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only messages whose textual body is sent to the model."""
    excluded = {"image", "audio", "voice", "video", "file"}
    return [dict(message) for message in messages
            if str(message.get("kind", "text") or "text").lower() not in excluded
            and str(message.get("content", "") or "").strip()]


def _summary_cache_key(stage: str, model: str, role_id: str,
                       group: str, value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                     separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{stage}:{model}:{role_id}:{group}:{digest}"


def _summary_cache(app: Any) -> dict[str, str]:
    cache = getattr(app, "_analysis_batch_cache", None)
    if not isinstance(cache, dict):
        cache_path = Path(getattr(app, "data_dir", "")) / "analysis_batches.json"
        try:
            stored = json.loads(cache_path.read_text(encoding="utf-8"))
            cache = {str(key): str(value) for key, value in stored.items()
                     if isinstance(stored, dict) and isinstance(value, str)} if isinstance(stored, dict) else {}
        except (OSError, ValueError, TypeError):
            cache = {}
        setattr(app, "_analysis_batch_cache", cache)
    return cache


def _persist_summary_cache(app: Any, cache: dict[str, str]) -> None:
    """Atomically persist completed reports locally so restart/retry can reuse them."""
    data_dir = getattr(app, "data_dir", None)
    if not data_dir:
        return
    path = Path(data_dir) / "analysis_batches.json"
    temporary = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _analysis_error(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc) or "模型未能完整生成分析结果。"
    return _gateway_error_text(exc, max_len=300)


def _jyi_model_limits(app: Any, model: str) -> tuple[int, int | None]:
    """Return a bounded output request and optional safe input-text budget."""
    from .jyi_parse import SUMMARY_MAX_OUTPUT_TOKENS

    catalog = getattr(app, "model_catalog", None) or {}
    entry = next((item for item in catalog.get("models", [])
                  if str(item.get("alias") or "") == model), {})
    try:
        context = int(entry.get("context_window") or 0)
    except (TypeError, ValueError):
        context = 0
    try:
        model_output = int(entry.get("max_output_tokens") or 0)
    except (TypeError, ValueError):
        model_output = 0
    # Respect upstream availability while keeping each individual report
    # bounded; long conversations are summarized in multiple cached chunks.
    output = min(model_output, SUMMARY_REPORT_OUTPUT_TARGET_TOKENS) if model_output > 0 else SUMMARY_MAX_OUTPUT_TOKENS
    if context <= 0:
        return output, None
    # Keep input room and a safety margin for system instructions, multimodal
    # payload accounting and tokenizer differences. The text batcher applies
    # a practical token target in addition to this context-derived budget.
    reserve = min(4096, max(512, context // 20))
    minimum_input = min(1024, max(256, context // 10))
    output = min(output, max(512, context - reserve - minimum_input))
    input_tokens = max(minimum_input, context - reserve - output)
    return output, min(input_tokens, SUMMARY_CHUNK_TARGET_TOKENS)


def _emit_stream(on_stream: Any, text: str, chunk: int = 1000) -> None:
    """Replay a complete report through the streaming callback in slices.

    缓存命中和网关逐批完成时，手里已经是一整段文字。直接一次塞给界面，
    Tk 要一次 insert 几万字；而切成小片，界面看起来是"正在一段段出来"，
    和实时 token 流观感一致。``on_stream`` 为空时什么都不做。
    """
    if not on_stream or not text:
        return
    step = max(256, int(chunk))
    for start in range(0, len(text), step):
        on_stream(text[start:start + step])


def _complete_summary_prompt(client: Any, model: str,
                             prompt_messages: list[dict[str, Any]],
                             max_output_tokens: int | None = None,
                             on_progress: Any = None,
                             on_stream: Any = None) -> str:
    """Complete one bounded report, continuing once the model hits its output cap."""
    from .jyi_parse import (
        SUMMARY_MAX_OUTPUT_TOKENS, empty_text_detail, extract_text,
        output_was_truncated,
    )
    from .summary import validate_summary_result

    output_limit = max_output_tokens or SUMMARY_MAX_OUTPUT_TOKENS
    streamed_chars = 0
    first_fragment = True

    def progress_delta(fragment: str) -> None:
        nonlocal streamed_chars, first_fragment
        if on_stream and fragment:
            on_stream(fragment)
        streamed_chars += len(fragment)
        if not on_progress:
            return
        if first_fragment:
            # 首个 token 到达即报一声：TTFT 阶段界面不再只有转圈，
            # 商户第一时间知道"模型已经开始写了"。
            first_fragment = False
            streamed_chars = 0
            on_progress("AI 正在生成分析内容…")
        elif streamed_chars >= 2000:
            on_progress("AI 正在生成分析内容…")
            streamed_chars = 0
    complete = getattr(client, "chat_completions_stream", None)
    # Preserve explicitly injected/test completion adapters that replace the
    # synchronous method but do not implement streaming.
    if "chat_completions" in getattr(client, "__dict__", {}):
        complete = None
    body = (complete(model, prompt_messages, max_tokens=output_limit,
                     on_chunk=progress_delta) if callable(complete)
            else client.chat_completions(model, prompt_messages, max_tokens=output_limit))
    text = extract_text(body)
    if not text:
        raise ValueError(empty_text_detail(body))
    continuations = 0
    while output_was_truncated(body) and continuations < 2:
        original_user = prompt_messages[-1].get("content", "")
        if isinstance(original_user, list):
            original_user = "\n".join(
                str(part.get("text", "")) for part in original_user
                if isinstance(part, Mapping) and part.get("type") == "text")
        continuation_messages = [prompt_messages[0]]
        if len(prompt_messages) > 1:
            continuation_messages.append({"role": "user", "content": str(original_user)})
        continuation_messages.extend([
            {"role": "assistant", "content": text},
            {"role": "user", "content": "请从上次停止的位置继续，只补充尚未写完的内容，不要重复已写部分，并完成这部分分析。"},
        ])
        body = (complete(model, continuation_messages, max_tokens=output_limit,
                         on_chunk=progress_delta) if callable(complete)
                else client.chat_completions(
                    model, continuation_messages, max_tokens=output_limit))
        fragment = extract_text(body)
        if not fragment:
            raise ValueError(empty_text_detail(body))
        text += "\n" + fragment
        continuations += 1
    if output_was_truncated(body):
        raise ValueError("单批分析仍超过模型输出上限。")
    return validate_summary_result(text)


def _complete_jyi_chunk(client: Any, model: str, role_id: str,
                        chunk: list[dict[str, Any]], on_progress: Any = None,
                        max_output_tokens: int | None = None,
                        max_document_chars: int = 20000,
                        split_depth: int = 0,
                        on_stream: Any = None) -> str:
    """Analyze a chunk, splitting it further only if the model hits its output cap."""
    from .jyi_parse import (
        add_document_text,
        add_image_parts,
        build_role_analysis_messages,
        build_summary_messages,
    )

    prompt = (build_role_analysis_messages(chunk, role_id) if role_id
              else build_summary_messages(chunk))
    kinds = {str(item.get("kind", "text") or "text").lower()
             for item in chunk if isinstance(item, Mapping)}
    if "image" in kinds:
        prompt = add_image_parts(prompt, chunk)
    if "file" in kinds:
        prompt = add_document_text(prompt, chunk,
                                   max_chars_per_document=max_document_chars)
    try:
        return _complete_summary_prompt(client, model, prompt, max_output_tokens,
                                        on_progress, on_stream)
    except ValueError as exc:
        reason = str(exc)
        hit_output_cap = "长度上限" in reason or "输出上限" in reason
        # One bounded split is enough to reduce a large prompt. Recursively
        # fanning out into dozens of tiny calls can hit upstream unsettled-call
        # limits, especially when a previous request timed out ambiguously.
        if not hit_output_cap or len(chunk) <= 1 or split_depth >= 1:
            raise
        middle = len(chunk) // 2
        if on_progress:
            on_progress(f"本批内容较长，自动拆分为 {middle} 条和 {len(chunk) - middle} 条继续分析")
        first = _complete_jyi_chunk(client, model, role_id, chunk[:middle], on_progress,
                                    max_output_tokens, max_document_chars, split_depth + 1,
                                    on_stream)
        second = _complete_jyi_chunk(client, model, role_id, chunk[middle:], on_progress,
                                     max_output_tokens, max_document_chars, split_depth + 1,
                                     on_stream)
        return first + "\n\n" + second


def _reduce_summary_reports(client: Any, model: str, role_id: str, group: str,
                            reports: list[str], cache: dict[str, str], app: Any,
                            max_output_tokens: int, on_progress: Any = None,
                            on_stream: Any = None) -> str:
    """Merge analysis batches pairwise so document-heavy chats never make one huge reducer prompt."""
    from .jyi_parse import build_role_analysis_messages, build_summary_messages

    level = 1
    current = list(reports)
    while len(current) > 1:
        merged: list[str] = []
        pairs = (len(current) + 1) // 2
        for index in range(0, len(current), 2):
            pair = current[index:index + 2]
            if len(pair) == 1:
                merged.append(pair[0])
                continue
            key = _summary_cache_key(f"reduce-{level}", model, role_id,
                                     group, {"index": index // 2, "reports": pair})
            result = cache.get(key, "")
            if not result:
                if on_progress:
                    on_progress(f"正在合并 {group} 的分析结果（第 {level} 层 {index // 2 + 1}/{pairs}）")
                inputs = [
                    {"sender": f"分析分段 {index + offset + 1}", "content": report,
                     "kind": "text", "sent_at": ""}
                    for offset, report in enumerate(pair)
                ]
                prompt = (build_role_analysis_messages(inputs, role_id) if role_id
                          else build_summary_messages(inputs))
                prompt[-1] = dict(prompt[-1])
                prompt[-1]["content"] += (
                    "\n以上是同一会话不同类别或批次的分析结果。请合并为一份报告，"
                    "去重并保留原有事实、发送人、数量、时间和不确定项；"
                    "保留每部分的来源类别，不得添加原分析中没有的信息。")
                result = _complete_summary_prompt(
                    client, model, prompt, max_output_tokens, on_progress,
                    on_stream)
                cache[key] = result
                if app is not None:
                    _persist_summary_cache(app, cache)
            merged.append(result)
        current = merged
        level += 1
    return current[0] if current else ""


def _run_summary_jyi(client: Any, messages: list[dict[str, Any]], role_id: str = "",
                     model_alias: str = "", app: Any = None,
                     on_progress: Any = None,
                     on_stream: Any = None) -> dict[str, Any]:
    """JYI 直调摘要：提示词同样复用 `summary` 那一份，不另写。

    计费不在本地算（JYI 按账号计量），cost 恒为 None —— 别让界面
    把它读成"这次没花钱"。
    """
    model = model_alias or client.text_model()
    max_output_tokens, input_chars = _jyi_model_limits(app, model)
    chunk_tokens = input_chars or SUMMARY_CHUNK_FALLBACK_TOKENS
    max_document_chars = max(512, min(20000, chunk_tokens // 2))
    cache = _summary_cache(app) if app is not None else {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    group_labels: dict[str, str] = {}
    for message in messages:
        group = str(message.get("group_id") or message.get("group") or "所选会话")
        grouped.setdefault(group, []).append(message)
        group_labels[group] = str(message.get("group_label") or group)

    reports: list[str] = []
    completed_groups: list[str] = []
    failures: list[str] = []
    processed = 0
    group_total = len(grouped)
    # 会话组头是给多会话分隔用的：只有一个会话时不加，
    # 单群分析的结果就是干干净净的一份报告。
    show_group = len(grouped) > 1
    streamed_groups: set[str] = set()
    for group_index, (group, group_messages) in enumerate(grouped.items(), start=1):
        if on_progress:
            on_progress(f"正在分析第 {group_index}/{group_total} 个会话：{group_labels[group]}")
        chunk_reports: list[str] = []
        group_error = ""
        work_batches = _summary_work_batches(
            group_messages, token_budget=chunk_tokens,
            max_document_chars=max_document_chars)
        if not work_batches:
            failures.append(f"{group_labels[group]}：没有可供模型分析的文字内容")
            continue
        category_totals = {category: sum(item_category == category
                                         for item_category, _ in work_batches)
                           for category, _ in work_batches}
        category_seen: dict[str, int] = {}
        for _chunk_index, (category, chunk) in enumerate(work_batches, start=1):
            category_seen[category] = category_seen.get(category, 0) + 1
            if on_progress:
                on_progress(f"{group_labels[group]}：分析{category}第 "
                            f"{category_seen[category]}/{category_totals[category]} 批")
            cache_key = _summary_cache_key("text-only-map-v1", model, role_id, group, chunk)
            report = cache.pop(cache_key, None)
            if not report:
                try:
                    batch_stream = None
                    if on_stream:
                        prefix = (f"【{group_labels[group]}】\n"
                                  if show_group and group not in streamed_groups
                                  else ("\n\n" if show_group else ""))
                        streamed_groups.add(group)
                        if prefix:
                            on_stream(prefix)
                        batch_stream = on_stream
                    report = _complete_jyi_chunk(
                        client, model, role_id, chunk, on_progress,
                        max_output_tokens, max_document_chars,
                        on_stream=batch_stream)
                    if app is not None:
                        cache[cache_key] = report
                        _persist_summary_cache(app, cache)
                except Exception as exc:
                    LOGGER.exception("AI analysis batch failed (stage=map, group=%s, code=%s, status=%s)",
                                     group, getattr(exc, "code", ""), getattr(exc, "status", ""))
                    group_error = _analysis_error(exc)
                    break
            else:
                cache[cache_key] = report
                if on_stream:
                    # 缓存命中以前是静默的：界面一直转圈，最后全文突然出现，
                    # 看起来和"卡死后突然恢复"一模一样。回放出来。
                    prefix = (f"【{group_labels[group]}】\n"
                              if show_group and group not in streamed_groups
                              else ("\n\n" if show_group else ""))
                    streamed_groups.add(group)
                    if prefix:
                        on_stream(prefix)
                    _emit_stream(on_stream, report)
            chunk_reports.append(f"【{category}】\n{report}")
        if group_error:
            failures.append(f"{group_labels[group]}：{group_error}")
            continue

        if len(chunk_reports) == 1:
            final_report = chunk_reports[0]
        else:
            try:
                if on_stream:
                    header = (f"\n\n【{group_labels[group]}·合并分段】\n" if show_group
                              else "\n\n【合并分段】\n")
                    on_stream(header)
                final_report = _reduce_summary_reports(
                    client, model, role_id, group, chunk_reports, cache, app,
                    max_output_tokens, on_progress, on_stream)
            except Exception as exc:
                LOGGER.exception("AI analysis batch failed (stage=reduce, group=%s, code=%s, status=%s)",
                                 group, getattr(exc, "code", ""), getattr(exc, "status", ""))
                failures.append(f"{group_labels[group]}（合并分批结果）：{_analysis_error(exc)}")
                continue
        reports.append(f"【{group_labels[group]}】\n{final_report}" if show_group
                       else final_report)
        completed_groups.append(group)
        processed += sum(len(chunk) for _, chunk in work_batches)

    # Bound process-local reuse while preserving the most recent completed batches.
    while len(cache) > 4096:
        cache.pop(next(iter(cache)))
    if app is not None:
        _persist_summary_cache(app, cache)
    combined = "\n\n".join(reports)
    partial_error = ("部分会话未完成：" + "；".join(failures)
                     + "。返回第六步重试时，已完成的分批结果会复用。" if failures and reports else "")
    error = ("分析未完成：" + "；".join(failures)) if failures and not reports else ""
    if on_progress:
        on_progress("会话分析完成" if not failures else "部分会话分析完成")
    return {"summary": combined, "error": error, "partial_error": partial_error,
            "completed_groups": completed_groups, "cost": None,
            "message_count": processed}


def _run_summary_gateway_job(client: Any, messages: list[dict[str, Any]],
                              role_id: str, idempotency_key: str) -> tuple[str, int | None, str]:
    """Submit and poll one bounded, billable gateway batch."""
    import time

    payload_messages = [
        {"sender": str(item.get("sender", ""))[:128],
         "content": str(item.get("content", ""))[:4000],
         "sent_at": str(item.get("sent_at", ""))[:64],
         "kind": str(item.get("kind", "text"))[:32]}
        for item in messages
    ]
    try:
        submitted = client.submit_summary(
            {"task": "summarize", "messages": payload_messages,
             **({"role_id": role_id} if role_id else {})}, idempotency_key)
        job_id = str((submitted.get("job") or {}).get("job_id", ""))
        if not job_id:
            return "", 0, "提交失败，请稍后重试。"
    except Exception as exc:
        return "", 0, _gateway_error_text(exc, max_len=300)
    deadline = time.time() + SUMMARY_TIMEOUT
    last_billing: dict = {}
    while time.time() < deadline:
        time.sleep(SUMMARY_POLL_INTERVAL)
        try:
            fetched = client.summary_job(job_id)
        except Exception:
            continue
        job = fetched.get("job") or {}
        billing = fetched.get("billing") or {}
        if isinstance(billing, dict) and billing.get("available", True):
            last_billing = billing
        state = str(job.get("state", ""))
        if state == "succeeded":
            text = _summary_text(job.get("result"))
            if text:
                return text, _billed_cost(last_billing), ""
            return "", _billed_cost(last_billing), "模型未生成可展示的分析内容。"
        if state in ("failed", "cancelled"):
            error = job.get("error") or {}
            from .client_login import merchant_safe_error
            code = str(error.get("code") or "")
            server_message = str(error.get("message") or "").strip()
            text = merchant_safe_error(code, server_message,
                                       fallback="摘要失败，请稍后重试")
            if code == "backend_unavailable" and server_message:
                # 网关自己的失败已经是商户话术（网关只说人话），别再用
                # 客户端的罐头把它盖掉 —— 罐头还可能指错方向（比如明明是
                # 服务端故障，却说成"网络暂不可用"）。
                text = server_message
            return "", _billed_cost(last_billing), text
    return "", _billed_cost(last_billing), "分析超时，结果未知（以服务端结算为准）。"


def _run_summary_gateway(app: Any, client: Any, messages: list[dict[str, Any]],
                          role_id: str, on_progress: Any = None,
                          on_stream: Any = None) -> dict[str, Any]:
    """Run the same per-conversation map/reduce flow through asynchronous jobs.

    网关任务是"提交 → 轮询"的异步 job，服务端在模型写完之前不会吐任何东西，
    所以真 token 流做不到。这里做**逐批流式**：每一批 job 完成就立刻把整段
    推给 ``on_stream``（分片），商户看到的是"一个会话一段往外冒"，而不是
   180 秒转圈后全文突然出现。
    """
    model = str(getattr(getattr(app, "settings", None), "model_alias", "") or "gateway")
    cache = _summary_cache(app)
    grouped: dict[str, list[dict[str, Any]]] = {}
    group_labels: dict[str, str] = {}
    for message in messages:
        group = str(message.get("group_id") or message.get("group") or "所选会话")
        grouped.setdefault(group, []).append(message)
        group_labels[group] = str(message.get("group_label") or group)
    reports: list[str] = []
    failures: list[str] = []
    completed_groups: list[str] = []
    processed = 0
    total_cost = 0
    cost_known = True
    submitted_batches = 0

    def run_batch(stage: str, group: str, batch: list[dict[str, Any]]) -> tuple[str, str]:
        nonlocal total_cost, cost_known, submitted_batches
        key = _summary_cache_key(f"text-only-{stage}-v1", model, role_id, group, batch)
        cached = cache.pop(key, None)
        if cached:
            cache[key] = cached
            return cached, ""
        submitted_batches += 1
        idempotency = "sum-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        report, cost, error = _run_summary_gateway_job(client, batch, role_id, idempotency)
        if cost is None:
            cost_known = False
        else:
            total_cost += cost
        if report:
            cache[key] = report
        return report, error

    for group_index, (group, group_messages) in enumerate(grouped.items(), start=1):
        if on_progress:
            on_progress(f"正在分析第 {group_index}/{len(grouped)} 个会话：{group_labels[group]}")
        show_group = len(grouped) > 1
        if on_stream and show_group:
            prefix = (f"【{group_labels[group]}】\n" if group_index == 1 else f"\n\n【{group_labels[group]}】\n")
            on_stream(prefix)
        chunks = [chunk for _, chunk in _summary_work_batches(
            group_messages, token_budget=SUMMARY_CHUNK_FALLBACK_TOKENS,
            max_document_chars=SUMMARY_CHUNK_MAX_DOCUMENT_CHARS)]
        if not chunks:
            failures.append(f"{group_labels[group]}：没有可供模型分析的文字内容")
            continue
        chunk_reports: list[str] = []
        failed = ""
        for index, chunk in enumerate(chunks, start=1):
            if on_progress:
                on_progress(f"{group_labels[group]}：分析第 {index}/{len(chunks)} 批消息")
            report, error = run_batch("map", group, chunk)
            if error:
                failed = error
                break
            chunk_reports.append(report)
            # 每一批完成就推一段：网关轮询要等很久，这是这条链路上唯一的增量输出。
            _emit_stream(on_stream, report)
        if failed:
            failures.append(f"{group_labels[group]}：{failed}")
            continue
        if len(chunk_reports) == 1:
            final_report = chunk_reports[0]
        else:
            reduced = [
                {"sender": f"第 {index} 批分析结果", "content": report,
                 "sent_at": "", "kind": "text"}
                for index, report in enumerate(chunk_reports, start=1)
            ]
            reduced[0]["content"] += (
                "\n请将以下同一会话的分批分析结果合并，去重并保留事实、发送人、数量、时间和不确定项；"
                "不得添加原分析中没有的信息。")
            final_report, error = run_batch("reduce", group, reduced)
            if error:
                failures.append(f"{group_labels[group]}（合并分批结果）：{error}")
                continue
            merge_header = (f"\n\n【{group_labels[group]}·合并分段】\n" if show_group
                            else "\n\n【合并分段】\n")
            _emit_stream(on_stream, f"{merge_header}{final_report}")
        reports.append(f"【{group_labels[group]}】\n{final_report}" if show_group
                       else final_report)
        completed_groups.append(group)
        processed += sum(len(chunk) for chunk in chunks)
    while len(cache) > 4096:
        cache.pop(next(iter(cache)))
    combined = "\n\n".join(reports)
    partial_error = ("部分会话未完成：" + "；".join(failures)
                     + "。返回第六步重试时，已完成的分批结果会复用。" if failures and reports else "")
    error = ("分析未完成：" + "；".join(failures)) if failures and not reports else ""
    if on_progress:
        on_progress("会话分析完成" if not failures else "部分会话分析完成")
    return {"summary": combined, "error": error, "partial_error": partial_error,
            "completed_groups": completed_groups,
            "cost": (total_cost / 1_000_000 if submitted_batches and cost_known else None),
            "message_count": processed}


def run_summary(app: Any, result: Any, role_id: str = "",
                messages_override: list[dict[str, Any]] | None = None,
                on_progress: Any = None,
                on_stream: Any = None) -> dict[str, Any]:
    """经云网关跑摘要。**在后台线程里跑，调用方不许碰界面。**

    LLM 只走云：这里不再读任何本地密钥。没登录返回"需要登录产品账号"。
    返回 ``{"summary", "error", "cost", "message_count"}``，从不抛异常 ——
    网络不可预期，不该让调用方的线程死掉。
    """
    messages = messages_override if messages_override is not None else messages_for_summary(app, result)
    if not messages:
        return {"summary": "", "error": "没读到消息", "cost": None, "message_count": 0}
    target = _app(app)
    client = _gateway_client(app)
    if client is None:
        return {"summary": "", "error": "需要登录产品账号",
                "cost": None, "message_count": len(messages)}
    if not _SUMMARY_RUN_LOCK.acquire(blocking=False):
        return {"summary": "", "error": "已有智能分析任务正在运行，请等待当前任务结束。",
                "cost": None, "message_count": len(messages)}
    try:
        if _is_jyi_client(client):
            settings = getattr(target, "settings", None)
            model_alias = str(getattr(settings, "model_alias", "") or "")
            return _run_summary_jyi(client, messages, role_id, model_alias, target,
                                    on_progress, on_stream)
        return _run_summary_gateway(target, client, messages, role_id,
                                    on_progress, on_stream)
    finally:
        _SUMMARY_RUN_LOCK.release()


def _summary_text(result: Any) -> str:
    if isinstance(result, Mapping):
        return str(result.get("summary", "") or "").strip()
    return ""


def _billed_cost(billing: Mapping) -> Any:
    if not isinstance(billing, Mapping):
        return None
    charged = billing.get("charged_micro_usd")
    try:
        return int(charged) if charged is not None else None
    except (TypeError, ValueError):
        return None


def _gateway_error_text(exc: Exception, max_len: int = 60) -> str:
    """网关异常 → 商户能看懂的一句话。原文透传技术细节的一律改写。

    ``max_len`` 是透传商户话术的长度上限：状态行地方小用 60，
    有换行显示的地方可以放宽（比如 JYI 的配额说明近 80 字，
    截了反而看不懂）。
    """
    code = str(getattr(exc, "code", "") or "")
    from .client_login import merchant_safe_error
    # GatewayError 把商户话术放 merchant_message（不是 message）。
    # 非网关异常没有商户话术，直接兜底，不透传技术原文。
    message = str(getattr(exc, "merchant_message", "")
                  or getattr(exc, "message", ""))
    lowered = message.casefold()
    if any(term in lowered for term in (
            "vision not supported", "image input is not supported",
            "does not support image", "model does not support vision",
            "unsupported image", "vision capability")):
        return "当前模型不支持图片分析。请在模型设置中选择支持图片的模型后重试。"
    if "no published price" in lowered and "bill" in lowered:
        return "所选分析模型暂不可用，请更换模型或稍后重试。"
    if code in ("network_unavailable", "backend_unavailable"):
        return "网络暂不可用"
    if code == "too_many_concurrent":
        try:
            wait_seconds = int(float(getattr(exc, "retry_after", 0) or 0) + 0.99)
        except (TypeError, ValueError):
            wait_seconds = 0
        wait_copy = f"系统已暂停新请求约 {wait_seconds} 秒。" if wait_seconds else "请稍等片刻再试。"
        return (f"当前请求较多，{wait_copy}"
                "已完成的分析分段会保留并在重试时复用。")
    if code == "request_timeout_unknown":
        return ("本批请求等待超时，服务端结果状态未知。已完成的分段分析已保存在本机，"
                "稍后重试会复用；超时的这一段若未返回，可能需要重新提交。")
    if code in ("unauthenticated",):
        return "登录已过期，请重新登录。"
    if code == "business_api_unavailable":
        return "智能分析暂时不可用，请稍后重试。"
    if code in ("quota_exhausted", "free_quota_exhausted", "subscription_inactive"):
        return merchant_safe_error(code, message, fallback="分析额度不足")
    message = merchant_safe_error(code, message, fallback="提交失败，请稍后重试")
    if message and len(message) <= max_len:
        return message
    return "提交失败，请稍后重试"


def import_reference(app: Any, kind: str, text: str) -> dict[str, Any]:
    """导入商品或客户名单。``kind`` 取 ``products`` / ``customers``。

    表头认中文常用名，详见 ``OrderStore.import_products_csv`` 与
    ``import_customers_csv``。重复导入不会让别名膨胀。
    """
    target = _app(app)
    store = getattr(target, "orders", None)
    tenant = getattr(target, "tenant", "")
    if store is None:
        return {"error": "本地数据还没准备好"}
    if kind == "products":
        return store.import_products_csv(tenant, text)
    if kind == "customers":
        return store.import_customers_csv(tenant, text)
    return {"error": f"不认识的名单类型：{kind}"}


__all__ = [
    "SUMMARY_MESSAGE_LIMIT",
    "SUMMARY_POLL_INTERVAL",
    "SUMMARY_TIMEOUT",
    "accumulated_coverage",
    "has_messages_for",
    "import_reference",
    "messages_for_summary",
    "history_message_counts",
    "messages_for_history",
    "run_summary",
]
