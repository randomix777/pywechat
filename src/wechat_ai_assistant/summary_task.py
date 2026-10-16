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
import inspect
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

#: 旧服务端单次任务的兼容上限。客户端会按会话拆成更小的请求批次。
SUMMARY_MESSAGE_LIMIT = 200
# Leave ample headroom in the published 1M context while keeping a whole
# conversation (including its images) in one request in ordinary cases.
SUMMARY_CHUNK_TARGET_TOKENS = 100_000
# No upstream context metadata: keep the existing conservative fallback scale.
SUMMARY_CHUNK_FALLBACK_TOKENS = 30_000
SUMMARY_CHUNK_MAX_IMAGES = 5
# The active JYI gateway enforces five image parts per request (HTTP 422).
SUMMARY_MULTIMODAL_MAX_IMAGES = 5
SUMMARY_CHUNK_MAX_DOCUMENT_CHARS = 12_000
SUMMARY_CHUNK_MAX_IMAGE_BYTES = 5 * 1024 * 1024
SUMMARY_MULTIMODAL_MAX_IMAGE_BYTES = 32 * 1024 * 1024
SUMMARY_MULTIMODAL_IMAGE_ESTIMATE_BYTES = 1_400_000
# A reducer should make a compact digest of already analyzed batches. Letting it
# use the model's full output budget can expand a 10k-character report into tens
# of thousands of characters, making both the request and the UI stall.
SUMMARY_REDUCE_MAX_OUTPUT_TOKENS = 4096

LOGGER = logging.getLogger("wechat_ai_assistant.summary")
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


def messages_for_summary(app: Any, result: Any, role_id: str = "",
                         full_history: bool = False) -> list[dict[str, Any]]:
    """按会话取消息供模型分析。

    有 ``role_id`` 时默认走**增量路径**：取该会话水位线（上次分析到的消息）
    之后的新增消息，无水位线（首次或角色切换后）取全量。``full_history``
    用于用户明确选择全量重分析时，忽略水位线并读取所有已保存消息。

    返回的消息已带 ``id`` 字段，供水位线回写；``group_label`` 用于结果页
    展示；``_analysis_document_text`` 等字段由 ``_attach_media`` 回填。
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
    if not groups:
        import_ids = [str(x) for x in (getattr(result, "import_ids", None) or []) if x]
        if import_ids:
            marks = ",".join("?" for _ in import_ids)
            limit_clause = "" if full_history else " LIMIT " + str(int(SUMMARY_MESSAGE_LIMIT))
            rows = store.db.execute(
                "SELECT group_id, sender, content, kind, media_json, COALESCE(sent_at, observed_at) AS sent_at, id, source_id"
                " FROM messages WHERE merchant_id=? AND import_id IN (" + marks + ")"
                " ORDER BY group_id, COALESCE(sent_at_epoch, strftime('%s', observed_at)), observed_at, id"
                + limit_clause,
                [tenant, *import_ids]).fetchall()
            messages = [{"group_id": str(row["group_id"] or ""),
                         "group_label": labels.get(str(row["group_id"] or ""), str(row["group_id"] or "")),
                         **_row_to_message(row)}
                        for row in rows]
            _enrich_image_metadata_from_wcdb(app, result, messages)
            return _attach_media(messages)
        return []

    _ensure_analysis_tables(store)
    messages: list[dict[str, Any]] = []
    last_per_group: dict[str, tuple[str, str]] = {}
    for group in groups:
        wm = _get_watermark(store, tenant, group, role_id) if role_id and not full_history else None
        limit_clause = "" if full_history else " LIMIT " + str(int(SUMMARY_MESSAGE_LIMIT))
        if wm is not None:
            last_msg_id, last_sent_at = wm
            rows = store.db.execute(
                "SELECT group_id, sender, content, kind, media_json, COALESCE(sent_at, observed_at) AS sent_at, id, source_id"
                " FROM messages WHERE merchant_id=? AND group_id=? AND "
                "(COALESCE(sent_at, observed_at) > ? OR (COALESCE(sent_at, observed_at) = ? AND id > ?))"
                " ORDER BY COALESCE(sent_at_epoch, strftime('%s', observed_at)), observed_at, id"
                + limit_clause,
                [tenant, group, last_sent_at, last_sent_at, last_msg_id]).fetchall()
        else:
            rows = store.db.execute(
                "SELECT group_id, sender, content, kind, media_json, COALESCE(sent_at, observed_at) AS sent_at, id, source_id"
                " FROM messages WHERE merchant_id=? AND group_id=? "
                " ORDER BY COALESCE(sent_at_epoch, strftime('%s', observed_at)), observed_at, id"
                + limit_clause,
                [tenant, group]).fetchall()
        for row in rows:
            messages.append({"group_id": str(row["group_id"] or ""),
                             "group_label": labels.get(str(row["group_id"] or ""), str(row["group_id"] or "")),
                             **_row_to_message(row)})
            last_per_group[group] = (str(row["id"]), str(row["sent_at"]))
    # 写回水位线：每个会话只记住最后一条（已按时间正序，最后一条最大）。
    for group, (msg_id, sent_at) in last_per_group.items():
        _set_watermark(store, tenant, group, role_id, msg_id, sent_at)
    _enrich_image_metadata_from_wcdb(app, result, messages)
    return _attach_media(messages)


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


def analyzed_group_ids(app: Any) -> set[str]:
    """已经进入过智能整理的会话（有分析水位线，任一角色）。

    导出放行用：只要商户对某个会话跑过一次智能整理，这个会话就算
    「过了一遍 AI」。按消息逐条记的 ``parse_runs`` 只在成功那一跑写，
    老数据、或增量跑（会话已到水位线、本次没有新增）都可能没有记录 ——
    水位线更可靠，否则会话一旦有水印就永远导不出。
    """
    target = _app(app)
    store = getattr(target, "orders", None)
    tenant = getattr(target, "tenant", "")
    if store is None:
        return set()
    _ensure_analysis_tables(store)
    try:
        rows = store.db.execute(
            "SELECT DISTINCT group_id FROM analysis_watermarks WHERE merchant_id=?",
            [tenant]).fetchall()
    except Exception:
        LOGGER.debug("read analyzed groups failed", exc_info=True)
        return set()
    return {str(row["group_id"]) for row in rows if str(row["group_id"] or "")}


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
        "SELECT group_id, sender, content, kind, media_json, COALESCE(sent_at, observed_at) AS sent_at, id"
        " FROM messages WHERE merchant_id=? AND group_id IN (" + marks + ")"
        " ORDER BY group_id, COALESCE(sent_at_epoch, strftime('%s', observed_at)), id",
        [tenant, *names]).fetchall()
    return _attach_media({"group_id": str(row["group_id"] or ""),
                          "group_label": labels.get(str(row["group_id"] or ""), str(row["group_id"] or "")),
                          **_row_to_message(row)}
                         for row in rows)


def message_time_sources(app: Any, result: Any) -> list[dict[str, str]]:
    """Return source text and actual send times for matching analysis evidence.

    Unlike ``messages_for_history``, this deliberately does not fall back to
    ``observed_at``: a receive/collection time must not be presented as the
    original message time.
    """
    target = _app(app)
    store = getattr(target, "orders", None)
    tenant = getattr(target, "tenant", "")
    if store is None:
        return []
    labels = _conversation_labels(result)
    groups: list[str] = []
    for item in getattr(result, "coverage", None) or ():
        conversation = getattr(item, "conversation", None)
        name = str(getattr(conversation, "name", "") or "") if conversation is not None else ""
        if name and not getattr(item, "error", "") and name not in groups:
            groups.append(name)
    if groups:
        marks = ",".join("?" for _ in groups)
        rows = store.db.execute(
            "SELECT group_id, sender, content, kind, sent_at FROM messages WHERE merchant_id=? AND group_id IN ("
            + marks + ") ORDER BY group_id, COALESCE(sent_at_epoch,strftime('%s',observed_at)), id",
            [tenant, *groups]).fetchall()
    else:
        import_ids = [str(value) for value in (getattr(result, "import_ids", None) or []) if value]
        if not import_ids:
            return []
        marks = ",".join("?" for _ in import_ids)
        rows = store.db.execute(
            "SELECT group_id, sender, content, kind, sent_at FROM messages WHERE merchant_id=? AND import_id IN ("
            + marks + ") ORDER BY group_id, COALESCE(sent_at_epoch,strftime('%s',observed_at)), id",
            [tenant, *import_ids]).fetchall()
    return [{"group_id": str(row["group_id"] or ""),
             "group_label": labels.get(str(row["group_id"] or ""), str(row["group_id"] or "")),
             "sender": str(row["sender"] or ""),
             "content": str(row["content"] or ""),
             "kind": str(row["kind"] or "text"),
             "sent_at": str(row["sent_at"] or "")}
            for row in rows]


def _row_to_message(row: Any) -> dict[str, Any]:
    """一行消息 → 给模型的形状。**必须带上 kind**。

    图片消息的 ``content`` 是占位符「图片」，**语音消息的 content 直接是空的**。
    不带 kind，模型看到的是「王姨：（空）」，完全不知道发生了什么。
    """
    try:
        media = json.loads(row["media_json"] or "{}")
    except (KeyError, TypeError, ValueError):
        media = {}
    try:
        source_id = row["source_id"]
    except (KeyError, IndexError):
        source_id = ""
    return {"id": row["id"], "source_id": str(source_id or ""),
            "sender": row["sender"], "content": row["content"],
            "kind": row["kind"], "sent_at": row["sent_at"],
            "media": media if isinstance(media, dict) else {}}


def _enrich_image_metadata_from_wcdb(app: Any, result: Any,
                                     messages: list[dict[str, Any]]) -> None:
    """Recover missing image locators from WCDB for older imported messages.

    Earlier client versions stored only the message MD5. When the original
    history read already supplied a stable WCDB message ID, use that ID to
    recover the encrypted image's AES/CDN metadata directly from the local
    decrypted database. This stays on-device and does not open a WeChat window.
    """
    missing = [message for message in messages
               if str(message.get("kind") or "").lower() in {"image", "图片", "图像"}
               and not any((message.get("media") or {}).get(key)
                           for key in ("cdnthumburl", "cdnmidimgurl",
                                       "cdnhdimgurl", "cdnbigimgurl"))
               and str(message.get("source_id") or "").startswith("wcdb-")]
    if not missing:
        return
    target = _app(app)
    history = getattr(target, "history", None)
    reader_factory = getattr(history, "_wcdb_reader_fn", None)
    if not callable(reader_factory):
        return

    group_usernames: dict[str, str] = {}
    for item in getattr(result, "coverage", None) or ():
        conversation = getattr(item, "conversation", None)
        if conversation is None or getattr(item, "error", ""):
            continue
        label = str(getattr(conversation, "name", "") or "")
        username = str(getattr(conversation, "wxid", "") or "")
        if label and username:
            group_usernames[label] = username
    for message in missing:
        group = str(message.get("group_id") or "")
        if group.endswith("@chatroom"):
            group_usernames.setdefault(group, group)
    if not group_usernames:
        return

    by_group: dict[str, list[dict[str, Any]]] = {}
    group_limits: dict[str, int] = {}
    for message in messages:
        group = str(message.get("group_id") or "")
        group_limits[group] = group_limits.get(group, 0) + 1
    for message in missing:
        group = str(message.get("group_id") or "")
        if group in group_usernames:
            by_group.setdefault(group, []).append(message)
    if not by_group:
        return

    recovered = 0
    try:
        reader = reader_factory()
        if reader is None:
            return
        # HistoryReader uses a shared scratch folder. Give this concurrent,
        # read-only enrichment pass its own short-lived directory.
        with tempfile.TemporaryDirectory(prefix="pyw-image-meta-") as workdir:
            reader.workdir = Path(workdir)
            for group, targets in by_group.items():
                try:
                    source_messages = reader.read_conversation_messages(
                        group_usernames[group],
                        limit=max(200, group_limits.get(group, len(targets)) + 32))
                except Exception:
                    LOGGER.debug("Local WCDB image metadata refresh failed", exc_info=True)
                    continue
                source_by_id = {
                    str(item.source_id): item.media
                    for item in source_messages
                    if str(item.source_id or "").startswith("wcdb-")
                    and item.kind == "图片" and isinstance(item.media, dict)
                }
                for message in targets:
                    fresh = source_by_id.get(str(message.get("source_id") or ""))
                    if not fresh:
                        continue
                    current = message.get("media") or {}
                    merged = {**current, **{
                        key: value for key, value in fresh.items()
                        if value not in (None, "")
                    }}
                    if any(merged.get(key) for key in (
                            "aeskey", "cdnthumburl", "cdnmidimgurl",
                            "cdnhdimgurl", "cdnbigimgurl")):
                        message["media"] = merged
                        recovered += 1
    except Exception:
        LOGGER.debug("Local WCDB image metadata refresh unavailable", exc_info=True)
        return
    if recovered:
        LOGGER.info("Recovered image locators from local WCDB: image_messages=%d", recovered)


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

#: 增量分析用的水位线表与报告表。按 (商户, 会话, 角色) 隔离，换角色
#: 重新分析互不影响；撤回的消息水位线已过，不再重新纳入（已生成的
#: 报告保留，不追溯）。
ANALYSIS_WATERMARK_SQL = (
    "CREATE TABLE IF NOT EXISTS analysis_watermarks("
    "merchant_id TEXT NOT NULL, group_id TEXT NOT NULL, role_id TEXT NOT NULL,"
    " last_msg_id TEXT NOT NULL, last_sent_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL,"
    " PRIMARY KEY(merchant_id, group_id, role_id))"
)
ANALYSIS_REPORT_SQL = (
    "CREATE TABLE IF NOT EXISTS analysis_reports("
    "merchant_id TEXT NOT NULL, report_key TEXT NOT NULL, role_id TEXT NOT NULL,"
    " report_text TEXT NOT NULL DEFAULT '', message_count INTEGER NOT NULL DEFAULT 0,"
    " updated_at TEXT NOT NULL,"
    " PRIMARY KEY(merchant_id, report_key, role_id))"
)


def _ensure_analysis_tables(store: Any) -> None:
    """建水位线与报告表（幂等）。"""
    try:
        store.db.execute(ANALYSIS_WATERMARK_SQL)
        store.db.execute(ANALYSIS_REPORT_SQL)
        store.db.commit()
    except Exception:
        # 表建不了不该阻断分析：退回全量取消息，走老路径。
        LOGGER.debug("analysis tables not created", exc_info=True)


def _get_watermark(store: Any, tenant: str, group_id: str, role_id: str) -> tuple[str, str] | None:
    """返回 (last_msg_id, last_sent_at)；没有水位线返回 None。"""
    try:
        row = store.db.execute(
            "SELECT last_msg_id, last_sent_at FROM analysis_watermarks"
            " WHERE merchant_id=? AND group_id=? AND role_id=?",
            [tenant, group_id, role_id]).fetchone()
    except Exception:
        LOGGER.debug("read watermark failed", exc_info=True)
        return None
    if row is None:
        return None
    return str(row["last_msg_id"]), str(row["last_sent_at"])


def _set_watermark(store: Any, tenant: str, group_id: str, role_id: str,
                   msg_id: str, sent_at: str) -> None:
    """写入/更新水位线。"""
    _ensure_analysis_tables(store)
    try:
        store.db.execute(
            "INSERT INTO analysis_watermarks(merchant_id, group_id, role_id,"
            " last_msg_id, last_sent_at, updated_at) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(merchant_id, group_id, role_id) DO UPDATE SET"
            " last_msg_id=excluded.last_msg_id, last_sent_at=excluded.last_sent_at,"
            " updated_at=excluded.updated_at",
            [tenant, group_id, role_id, msg_id, sent_at,
             __import__("datetime").datetime.now().isoformat()])
        store.db.commit()
    except Exception:
        LOGGER.debug("write watermark failed", exc_info=True)


def _get_report(store: Any, tenant: str, report_key: str, role_id: str) -> str:
    """读已持久化的完整报告；没有返回空字符串。"""
    _ensure_analysis_tables(store)
    try:
        row = store.db.execute(
            "SELECT report_text FROM analysis_reports"
            " WHERE merchant_id=? AND report_key=? AND role_id=?",
            [tenant, report_key, role_id]).fetchone()
    except Exception:
        LOGGER.debug("read report failed", exc_info=True)
        return ""
    return str(row["report_text"] or "") if row else ""


def _set_report(store: Any, tenant: str, report_key: str, role_id: str,
                report_text: str, message_count: int) -> None:
    """持久化合并后的完整报告。"""
    _ensure_analysis_tables(store)
    try:
        store.db.execute(
            "INSERT INTO analysis_reports(merchant_id, report_key, role_id,"
            " report_text, message_count, updated_at) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(merchant_id, report_key, role_id) DO UPDATE SET"
            " report_text=excluded.report_text, message_count=excluded.message_count,"
            " updated_at=excluded.updated_at",
            [tenant, report_key, role_id, report_text, message_count,
             __import__("datetime").datetime.now().isoformat()])
        store.db.commit()
    except Exception:
        LOGGER.debug("write report failed", exc_info=True)


def _analysis_report_key(group_ids: list[str] | tuple[str, ...], role_id: str) -> str:
    """Version persisted reports when the role's output contract changes."""
    from .roles import ROLE_ORDER

    joined = "_".join(sorted(str(group or "") for group in group_ids if group))
    if str(role_id or "") == ROLE_ORDER:
        return f"order-intent-table-v1:{joined}"
    return joined


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


def _jyi_model_supports_images(app: Any, model: str) -> bool | None:
    """Return published JYI image capability, or ``None`` when unknown."""
    catalog = getattr(_app(app), "model_catalog", None) or {}
    models = catalog.get("models", []) if isinstance(catalog, Mapping) else []
    for item in models:
        if not isinstance(item, Mapping) or str(item.get("alias") or "") != model:
            continue
        modalities = item.get("input_modalities")
        if isinstance(modalities, (list, tuple, set)):
            return "image" in {str(value).lower() for value in modalities}
        return None
    return None


def can_analyze_images(app: Any, messages: Sequence[Mapping[str, Any]]) -> bool:
    """Whether this configured JYI model can receive local image attachments."""
    target = _app(app)
    if not _is_jyi_client(_gateway_client(target)):
        return False
    if not any(str(item.get("kind") or "").lower() == "image" for item in messages):
        return False
    model = str(getattr(getattr(target, "settings", None), "model_alias", "") or "")
    if not model:
        client = _gateway_client(target)
        model = str(client.text_model()) if client is not None else ""
    return _jyi_model_supports_images(target, model) is not False


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
                    max_document_chars: int = 20000,
                    max_images: int = SUMMARY_CHUNK_MAX_IMAGES,
                    max_image_bytes: int = SUMMARY_CHUNK_MAX_IMAGE_BYTES,
                    image_size_estimate: int | None = None) -> list[list[dict[str, Any]]]:
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
        image_count = 0
        if kind == "image":
            # High-detail image tokenization is resolution/model dependent.
            paths = message.get("attachments") or []
            image_count = max(1, len(paths))
            size += 3000 * image_count
            if paths:
                for path in paths:
                    if image_size_estimate is not None:
                        image_size += image_size_estimate
                        continue
                    try:
                        image_size += min(2 * 1024 * 1024, Path(str(path)).stat().st_size)
                    except OSError:
                        image_size += 1024 * 1024
            else:
                image_size = image_size_estimate or 1024 * 1024
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
                        or (image_count and images + image_count > max_images)
                        or (image_size and image_bytes + image_size > max_image_bytes)
                        or (document_size and document_chars + document_size > document_budget)):
            chunks.append(current)
            current, tokens, images, image_bytes, document_chars = [], 0, 0, 0, 0
        current.append(message)
        tokens += size
        images += image_count
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
                           max_document_chars: int,
                           include_images: bool = True,
                           combine_modalities: bool = False) -> list[tuple[str, list[dict[str, Any]]]]:
    """Prepare bounded batches, optionally keeping text and images in timeline order."""
    if include_images and combine_modalities:
        mixed = []
        for message in messages:
            kind = str(message.get("kind") or "text").lower()
            if kind == "image":
                if message.get("attachments"):
                    mixed.append(dict(message))
            elif kind not in {"audio", "voice", "video", "file"} and str(
                    message.get("content") or "").strip():
                mixed.append(dict(message))
        return [("多模态", chunk) for chunk in _summary_chunks(
            mixed, token_budget=token_budget,
            max_document_chars=max_document_chars,
            max_images=SUMMARY_MULTIMODAL_MAX_IMAGES,
            max_image_bytes=SUMMARY_MULTIMODAL_MAX_IMAGE_BYTES,
            image_size_estimate=SUMMARY_MULTIMODAL_IMAGE_ESTIMATE_BYTES)]

    texts = _text_analysis_messages(messages)
    batches = [("纯文字", chunk) for chunk in _summary_chunks(
        texts, token_budget=token_budget, max_document_chars=max_document_chars)]
    if include_images:
        images = [message for message in messages
                  if str(message.get("kind") or "").lower() == "image"
                  and message.get("attachments")]
        batches.extend(("图片", chunk) for chunk in _summary_chunks(
            images, token_budget=token_budget,
            max_document_chars=max_document_chars))
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


def _is_vision_unsupported_error(exc: Exception) -> bool:
    message = _analysis_error(exc).casefold()
    return ("不支持图片分析" in message
            or any(term in message for term in (
                "vision not supported", "image input is not supported",
                "does not support image", "model does not support vision",
                "unsupported image", "vision capability")))


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
    # Reserve output for both maximum-effort reasoning and final content.
    # The gateway bounds the request against its published model capacity.
    output = (min(model_output, SUMMARY_MAX_OUTPUT_TOKENS)
              if model_output > 0 else SUMMARY_MAX_OUTPUT_TOKENS)
    if context <= 0:
        return output, None
    # Reserve room for the prompt/system instructions and keep enough input
    # budget for useful batches. The output ceiling is clipped to the remainder
    # of the actual context window, so a large upstream cap cannot crowd out
    # the messages being analyzed.
    reserve = min(4096, max(512, context // 20))
    minimum_input = min(1024, max(256, context // 10))
    preferred_input = min(SUMMARY_CHUNK_TARGET_TOKENS,
                          max(minimum_input, context // 2))
    output = min(output, max(1, context - reserve - preferred_input))
    input_tokens = min(preferred_input,
                       max(minimum_input, context - reserve - output))
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


class AnalysisCancelled(Exception):
    """用户取消了分析。

    不是失败：已完成的分段已经入缓存、调用方按部分完成组装结果，
    商户重试时会复用它们。只在工作线程内部流转，不出这个模块。
    """


class _NoUsableImageParts(Exception):
    """本地缓存没有产生可发给模型的图片内容。"""


def _raise_if_cancelled(is_cancelled: Any) -> None:
    """检查取消标记。界面在工作线程外置位，这里只读。"""
    if callable(is_cancelled) and is_cancelled():
        raise AnalysisCancelled()


def _complete_summary_prompt(client: Any, model: str,
                             prompt_messages: list[dict[str, Any]],
                             max_output_tokens: int | None = None,
                             on_progress: Any = None,
                             on_stream: Any = None,
                             is_cancelled: Any = None,
                             diagnostic_context: str = "",
                             max_continuations: int = 2) -> str:
    """Complete one bounded report, continuing once the model hits its output cap."""
    from .jyi_parse import (
        SUMMARY_MAX_OUTPUT_TOKENS, empty_text_detail, extract_text,
        output_was_truncated,
    )
    from .summary import validate_summary_result

    output_limit = max_output_tokens or SUMMARY_MAX_OUTPUT_TOKENS
    request_options: dict[str, Any] = {"max_tokens": output_limit}
    from .jyi_client import DEFAULT_TEXT_MODEL

    if model == DEFAULT_TEXT_MODEL or model.rsplit("/", 1)[-1] in {
            "deepseek-flash", "deepseek-v4.1-flash"}:
        # Default to direct answers. The user can still enable reasoning in
        # explicit request options, but analysis should not wait on a long
        # hidden-reasoning phase before producing its first visible token.
        request_options.update(thinking={"type": "disabled"}, temperature=1.0)
    LOGGER.info(
        "AI completion request: context=%s model=%s max_tokens=%d "
        "thinking=%s reasoning_effort=%s temperature=%s",
        diagnostic_context or "unspecified", model, output_limit,
        request_options.get("thinking"), request_options.get("reasoning_effort"),
        request_options.get("temperature"))
    streamed_chars = 0
    first_fragment = True
    first_content_logged = False
    first_reasoning_logged = False
    last_reasoning_progress = 0.0
    request_started = time.monotonic()
    stream_stats: dict[str, Any] = {}

    def progress_delta(fragment: str) -> None:
        nonlocal streamed_chars, first_fragment, first_content_logged
        _raise_if_cancelled(is_cancelled)
        if on_stream and fragment:
            on_stream(fragment)
        streamed_chars += len(fragment)
        if fragment and not first_content_logged:
            first_content_logged = True
            LOGGER.info(
                "AI stream first visible content: context=%s model=%s ttft_ms=%d fragment_chars=%d",
                diagnostic_context or "unspecified", model,
                round((time.monotonic() - request_started) * 1000), len(fragment))
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

    def progress_reasoning(_fragment_chars: int) -> None:
        nonlocal first_reasoning_logged, last_reasoning_progress
        _raise_if_cancelled(is_cancelled)
        if not on_progress:
            return
        now = time.monotonic()
        if not first_reasoning_logged or now - last_reasoning_progress >= 8:
            first_reasoning_logged = True
            last_reasoning_progress = now
            on_progress("模型正在推理，尚未生成可展示正文……")
    complete = getattr(client, "chat_completions_stream", None)
    # Preserve explicitly injected/test completion adapters that replace the
    # synchronous method but do not implement streaming.
    if "chat_completions" in getattr(client, "__dict__", {}):
        complete = None

    def log_stream_diagnostics(stats: Mapping[str, Any]) -> None:
        stream_stats.update(stats)
        LOGGER.info(
            "AI stream diagnostics: context=%s model=%s outcome=%s elapsed_ms=%s "
            "first_line_ms=%s first_data_ms=%s first_content_ms=%s lines=%s "
            "response_bytes=%s heartbeat_lines=%s data_events=%s reasoning_chars=%s "
            "content_chars=%s finish_reason=%s usage_prompt_tokens=%s "
            "usage_completion_tokens=%s usage_total_tokens=%s http_status=%s error_type=%s "
            "request_max_tokens=%s request_thinking=%s request_reasoning_effort=%s "
            "response_model=%s usage_reasoning_tokens=%s gateway_request_id=%s",
            diagnostic_context or "unspecified", model, stats.get("outcome", ""),
            stats.get("elapsed_ms"), stats.get("first_line_ms"),
            stats.get("first_data_ms"), stats.get("first_content_ms"),
            stats.get("line_count"), stats.get("response_bytes"),
            stats.get("heartbeat_lines"), stats.get("data_events"),
            stats.get("reasoning_chars"), stats.get("content_chars"),
            stats.get("finish_reason"), stats.get("usage_prompt_tokens"),
            stats.get("usage_completion_tokens"), stats.get("usage_total_tokens"),
            stats.get("http_status"), stats.get("error_type"),
            stats.get("request_max_tokens"), stats.get("request_thinking"),
            stats.get("request_reasoning_effort"), stats.get("response_model"),
            stats.get("usage_reasoning_tokens"), stats.get("gateway_request_id"))

    if callable(complete):
        diagnostic_kwargs: dict[str, Any] = {}
        try:
            if "on_diagnostics" in inspect.signature(complete).parameters:
                diagnostic_kwargs["on_diagnostics"] = log_stream_diagnostics
            if "on_reasoning" in inspect.signature(complete).parameters:
                diagnostic_kwargs["on_reasoning"] = progress_reasoning
        except (TypeError, ValueError):
            pass
        body = complete(model, prompt_messages, **request_options,
                        on_chunk=progress_delta, **diagnostic_kwargs)
    else:
        body = client.chat_completions(model, prompt_messages, **request_options)
    text = extract_text(body)
    choices = body.get("choices") if isinstance(body, Mapping) else None
    finish_reason = (choices[0].get("finish_reason")
                     if choices and isinstance(choices[0], Mapping) else None)
    usage = body.get("usage") if isinstance(body, Mapping) else None
    usage_completion = (usage.get("completion_tokens")
                        if isinstance(usage, Mapping) else None)
    LOGGER.info(
        "AI completion result: context=%s model=%s elapsed_ms=%d output_limit=%d "
        "finish_reason=%s visible_chars=%d usage_completion_tokens=%s",
        diagnostic_context or "unspecified", model,
        round((time.monotonic() - request_started) * 1000), output_limit,
        finish_reason, len(text), usage_completion)
    if not text and callable(complete):
        # Some OpenAI-compatible gateways finish their SSE response normally
        # but put no user-visible text in delta.content. Retry as one ordinary
        # completion only for a completed, non-length-limited stream; never
        # retry an ambiguous timeout or a capped response automatically.
        finish_reason = (choices[0].get("finish_reason")
                         if choices and isinstance(choices[0], Mapping) else None)
        if str(finish_reason or "").lower() not in {"length", "max_tokens"}:
            if on_progress:
                on_progress("正在获取完整分析内容……")
            body = client.chat_completions(
                model, prompt_messages, **request_options)
            text = extract_text(body)
    if not text:
        if (str(finish_reason or "").lower() in {"length", "max_tokens"}
                and stream_stats.get("reasoning_chars")
                and not stream_stats.get("content_chars")):
            raise ValueError(
                "模型持续输出推理过程，但达到输出上限前没有生成分析正文；请切换模型或稍后重试。")
        raise ValueError(empty_text_detail(body))
    continuations = 0
    while output_was_truncated(body) and continuations < max(0, int(max_continuations)):
        # 续写是一次新的模型调用：取消就停，别再花一次钱。
        _raise_if_cancelled(is_cancelled)
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
        if callable(complete):
            body = complete(model, continuation_messages, **request_options,
                            on_chunk=progress_delta, **diagnostic_kwargs)
        else:
            body = client.chat_completions(
                model, continuation_messages, **request_options)
        fragment = extract_text(body)
        if not fragment:
            raise ValueError(empty_text_detail(body))
        text += "\n" + fragment
        continuations += 1
    if output_was_truncated(body):
        raise ValueError("单批分析仍超过模型输出上限。")
    # 网关未返回任何流式 token（不支持 SSE 或返回非 SSE 格式），
    # 将完整结果分片回放，让用户看到内容正在生成，而不是干等。
    if streamed_chars == 0 and text and on_stream:
        _emit_stream(on_stream, text, chunk=200)
    return validate_summary_result(text)


def _complete_jyi_chunk(client: Any, model: str, role_id: str,
                        chunk: list[dict[str, Any]], on_progress: Any = None,
                        max_output_tokens: int | None = None,
                        max_document_chars: int = 20000,
                        split_depth: int = 0,
                        on_stream: Any = None,
                        is_cancelled: Any = None,
                        include_images: bool = False,
                        diagnostic_context: str = "") -> str:
    """Analyze a chunk, splitting it further only if the model hits its output cap."""
    from .jyi_parse import build_role_analysis_messages, build_summary_messages

    prompt = (build_role_analysis_messages(chunk, role_id, include_images=include_images)
              if role_id else build_summary_messages(chunk, include_images=include_images))
    prompt_text_chars = 0
    image_parts = 0
    image_data_chars = 0
    image_mime_counts: dict[str, int] = {}
    for message in prompt:
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str):
            prompt_text_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                if part.get("type") == "text":
                    prompt_text_chars += len(str(part.get("text") or ""))
                elif part.get("type") == "image_url":
                    image_parts += 1
                    image_url = part.get("image_url")
                    if isinstance(image_url, Mapping):
                        data_url = str(image_url.get("url") or "")
                        image_data_chars += len(data_url)
                        mime = data_url.partition(";")[0].removeprefix("data:") or "unknown"
                        image_mime_counts[mime] = image_mime_counts.get(mime, 0) + 1
    context = diagnostic_context or "unspecified"
    LOGGER.info(
        "AI analysis prompt prepared: context=%s split_depth=%d items=%d "
        "prompt_text_chars=%d image_parts=%d image_data_chars=%d image_mime_counts=%s",
        context, split_depth, len(chunk), prompt_text_chars, image_parts,
        image_data_chars, image_mime_counts)
    has_text_messages = bool(_text_analysis_messages(chunk))
    if include_images and image_parts == 0 and not has_text_messages:
        raise _NoUsableImageParts("图片缓存没有生成可提交的图片内容")
    try:
        return _complete_summary_prompt(client, model, prompt, max_output_tokens,
                                        on_progress, on_stream, is_cancelled,
                                        diagnostic_context=(
                                            f"{context} split_depth={split_depth}"))
    except ValueError as exc:
        reason = str(exc)
        hit_output_cap = "长度上限" in reason or "输出上限" in reason
        # One bounded split is enough to reduce a large prompt. Recursively
        # fanning out into dozens of tiny calls can hit upstream unsettled-call
        # limits, especially when a previous request timed out ambiguously.
        if (not hit_output_cap or "推理过程" in reason
                or len(chunk) <= 1 or split_depth >= 1):
            raise
        middle = len(chunk) // 2
        if on_progress:
            on_progress(f"本批内容较长，自动拆分为 {middle} 条和 {len(chunk) - middle} 条继续分析")
        _raise_if_cancelled(is_cancelled)
        first = _complete_jyi_chunk(client, model, role_id, chunk[:middle], on_progress,
                                    max_output_tokens, max_document_chars, split_depth + 1,
                                    on_stream, is_cancelled, include_images,
                                    diagnostic_context + "/left")
        _raise_if_cancelled(is_cancelled)
        second = _complete_jyi_chunk(client, model, role_id, chunk[middle:], on_progress,
                                     max_output_tokens, max_document_chars, split_depth + 1,
                                     on_stream, is_cancelled, include_images,
                                     diagnostic_context + "/right")
        return first + "\n\n" + second


def _reduce_summary_reports(client: Any, model: str, role_id: str, group: str,
                            reports: list[str], cache: dict[str, str], app: Any,
                            max_output_tokens: int, on_progress: Any = None,
                            on_stream: Any = None,
                            is_cancelled: Any = None,
                            force_refresh: bool = False) -> str:
    """Merge analysis batches pairwise so document-heavy chats never make one huge reducer prompt."""
    from .jyi_parse import (
        build_order_report_merge_messages,
        build_role_analysis_messages,
        build_summary_messages,
    )
    from .roles import ROLE_ORDER

    level = 1
    current = list(reports)
    while len(current) > 1:
        _raise_if_cancelled(is_cancelled)
        merged: list[str] = []
        pairs = (len(current) + 1) // 2
        for index in range(0, len(current), 2):
            pair = current[index:index + 2]
            if len(pair) == 1:
                merged.append(pair[0])
                continue
            # Version the reducer cache: old entries may contain unbounded expanded
            # reports produced before the reducer output cap was introduced.
            key = _summary_cache_key(f"order-reduce-v4-{level}", model, role_id,
                                     group, {"index": index // 2, "reports": pair})
            result = "" if force_refresh else cache.get(key, "")
            if not result:
                if on_progress:
                    on_progress(f"正在合并 {group} 的分析结果（第 {level} 层 {index // 2 + 1}/{pairs}）")
                if role_id == ROLE_ORDER:
                    # These are completed reports, not chat lines. Don't feed the
                    # artificial chunk label through the customer attribution prompt.
                    prompt = build_order_report_merge_messages(pair)
                else:
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
                diagnostic_context = (
                    f"stage=reduce group={group} level={level} pair={index // 2 + 1}/{pairs}"
                )
                try:
                    reduce_output_limit = min(
                        max(256, int(max_output_tokens or SUMMARY_REDUCE_MAX_OUTPUT_TOKENS)),
                        SUMMARY_REDUCE_MAX_OUTPUT_TOKENS,
                    )
                    result = _complete_summary_prompt(
                        client, model, prompt, reduce_output_limit, on_progress,
                        on_stream, is_cancelled, diagnostic_context=diagnostic_context,
                        max_continuations=0)
                except ValueError as exc:
                    if not any(marker in str(exc)
                               for marker in ("过长", "长度上限", "输出上限")):
                        raise
                    # 合并超出小型 reducer 预算时保留原分段。每份 map 报告已经
                    # 完整可读；继续续写只会拖长等待，并可能扩写出远大于输入的报告。
                    LOGGER.warning("AI analysis merge too long, keeping segments "
                                   "(stage=reduce, group=%s, level=%s, pair=%s/%s, "
                                   "model=%s, output_limit=%d)",
                                   group, level, index // 2 + 1, pairs, model,
                                   reduce_output_limit)
                    merged.extend(pair)
                    continue
                cache[key] = result
                if app is not None:
                    _persist_summary_cache(app, cache)
            merged.append(result)
        if merged == current:
            # 这一层一个都没合上（全超长兜底）：再循环只会原地打转。
            # 直接返回并列分段 —— 每份各自合规，只是没能压成一份。
            break
        current = merged
        level += 1
    return "\n\n".join(current) if len(current) > 1 else (current[0] if current else "")


def _run_summary_jyi(client: Any, messages: list[dict[str, Any]], role_id: str = "",
                     model_alias: str = "", app: Any = None,
                     on_progress: Any = None,
                     on_stream: Any = None,
                     is_cancelled: Any = None,
                     force_refresh: bool = False) -> dict[str, Any]:
    """JYI 直调摘要：提示词同样复用 `summary` 那一份，不另写。

    计费不在本地算（JYI 按账号计量），cost 恒为 None —— 别让界面
    把它读成"这次没花钱"。
    """
    model = model_alias or client.text_model()
    max_output_tokens, input_chars = _jyi_model_limits(app, model)
    image_messages = [message for message in messages
                      if str(message.get("kind") or "").lower() == "image"]
    image_support = _jyi_model_supports_images(app, model)
    include_images = any(item.get("attachments") for item in image_messages) and image_support is not False
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
    group_reports: list[dict[str, str]] = []
    completed_groups: list[str] = []
    failures: list[str] = []
    processed = 0
    group_total = len(grouped)
    # 会话组头是给多会话分隔用的：只有一个会话时不加，
    # 单群分析的结果就是干干净净的一份报告。
    show_group = len(grouped) > 1
    streamed_groups: set[str] = set()
    cancelled = False
    image_gateway_rejected = False
    if image_support is False:
        for item in image_messages:
            item["media_status"] = "当前模型不支持图片输入，未提交给 AI"
    else:
        for item in image_messages:
            if not item.get("attachments") and not item.get("media_status"):
                item["media_status"] = "本地没有可用图片缓存，未提交图片内容"
    try:
        for group_index, (group, group_messages) in enumerate(grouped.items(), start=1):
            _raise_if_cancelled(is_cancelled)
            if on_progress:
                on_progress(f"正在分析第 {group_index}/{group_total} 个会话：{group_labels[group]}")
            chunk_reports: list[str] = []
            group_processed = 0
            group_error = ""
            work_batches = _summary_work_batches(
                group_messages, token_budget=chunk_tokens,
                max_document_chars=max_document_chars,
                include_images=include_images,
                combine_modalities=include_images)
            if not work_batches:
                reason = ("所选模型未声明支持图片分析" if image_messages and image_support is False
                          else "没有可供模型分析的文字或图片内容")
                failures.append(f"{group_labels[group]}：{reason}")
                continue
            category_totals = {category: sum(item_category == category
                                             for item_category, _ in work_batches)
                               for category, _ in work_batches}
            category_seen: dict[str, int] = {}
            for _chunk_index, (category, chunk) in enumerate(work_batches, start=1):
                _raise_if_cancelled(is_cancelled)
                if category == "图片" and image_support is False:
                    for item in chunk:
                        item["media_status"] = "当前模型不支持图片输入，未提交给 AI"
                    continue
                batch_chunk = chunk
                send_images = include_images and category in {"图片", "多模态"} and image_support is not False
                if category == "多模态" and image_support is False:
                    batch_chunk = _text_analysis_messages(chunk)
                    for item in chunk:
                        if str(item.get("kind") or "").lower() == "image":
                            item["media_status"] = "当前模型或网关不支持图片输入，已仅分析文字"
                    if not batch_chunk:
                        continue
                category_seen[category] = category_seen.get(category, 0) + 1
                batch_index = category_seen[category]
                if on_progress:
                    on_progress(f"{group_labels[group]}：分析{category}第 "
                                f"{category_seen[category]}/{category_totals[category]} 批")
                # Text batches contain the same prompt regardless of whether this
                # run also has image batches. Keep their cache key text-only so a
                # vision-enabled run can reuse a completed text analysis instead
                # of submitting the same slow request again.
                cache_stage = ("vision-map-v2" if category == "图片" else
                               "multimodal-map-v1" if category == "多模态" and send_images else
                               "text-only-map-v4")
                cache_key = _summary_cache_key(cache_stage, model, role_id, group, batch_chunk)
                report = None if force_refresh else cache.pop(cache_key, None)
                if not report:
                    batch_started = time.monotonic()
                    diagnostic_context = (
                        f"stage=map group={group} category={category} "
                        f"batch={batch_index}/{category_totals[category]}"
                    )
                    text_chars = sum(len(str(item.get("content") or "")) for item in batch_chunk)
                    image_messages_in_chunk = sum(
                        str(item.get("kind") or "").lower() == "image" for item in batch_chunk)
                    image_attachments_in_chunk = sum(
                        len(item.get("attachments") or [])
                        for item in batch_chunk
                        if str(item.get("kind") or "").lower() == "image")
                    LOGGER.info(
                        "AI analysis batch started: group=%s category=%s batch=%d/%d "
                        "model=%s items=%d text_chars=%d image_messages=%d "
                        "image_attachments=%d output_limit=%s image_input=%s",
                        group, category, batch_index, category_totals[category], model,
                        len(batch_chunk), text_chars, image_messages_in_chunk,
                        image_attachments_in_chunk, max_output_tokens,
                        send_images)
                    try:
                        batch_stream = None
                        if on_stream:
                            prefix = (f"【{group_labels[group]}】\n"
                                      if show_group and group not in streamed_groups
                                      else ("\n\n" if show_group else ""))
                            streamed_groups.add(group)
                            if len(work_batches) > 1:
                                prefix += f"【分段 {_chunk_index}/{len(work_batches)} · {category}】\n"
                            if prefix:
                                on_stream(prefix)
                            batch_stream = on_stream
                        report = _complete_jyi_chunk(
                            client, model, role_id, batch_chunk, on_progress,
                            max_output_tokens, max_document_chars,
                            on_stream=batch_stream, is_cancelled=is_cancelled,
                            include_images=send_images,
                            diagnostic_context=diagnostic_context)
                        LOGGER.info(
                            "AI analysis batch completed: group=%s category=%s batch=%d/%d "
                            "model=%s elapsed_ms=%d report_chars=%d",
                            group, category, batch_index, category_totals[category], model,
                            round((time.monotonic() - batch_started) * 1000), len(report))
                        if app is not None:
                            cache[cache_key] = report
                            _persist_summary_cache(app, cache)
                    except AnalysisCancelled:
                        raise
                    except _NoUsableImageParts:
                        LOGGER.warning(
                            "AI image batch skipped: group=%s batch=%d/%d reason=no_decodable_image_parts",
                            group, batch_index, category_totals[category])
                        for item in chunk:
                            if str(item.get("kind") or "").lower() == "image" and item.get("attachments"):
                                item["media_status"] = "图片缓存无法解码，未纳入视觉分析"
                        continue
                    except Exception as exc:
                        if callable(is_cancelled) and is_cancelled():
                            raise AnalysisCancelled() from exc
                        LOGGER.exception(
                            "AI analysis batch failed (stage=map, group=%s, category=%s, "
                            "batch=%d/%d, model=%s, elapsed_ms=%d, code=%s, status=%s)",
                            group, category, batch_index, category_totals[category], model,
                            round((time.monotonic() - batch_started) * 1000),
                            getattr(exc, "code", ""), getattr(exc, "status", ""))
                        if category in {"图片", "多模态"}:
                            gateway_rejected = getattr(exc, "code", "") == "unsupported_content"
                            vision_rejected = gateway_rejected or _is_vision_unsupported_error(exc)
                            image_gateway_rejected = image_gateway_rejected or gateway_rejected
                            if vision_rejected:
                                image_support = False
                            for item in chunk:
                                if str(item.get("kind") or "").lower() == "image":
                                    item["media_status"] = (
                                        "图片分析请求失败：本地图片已解密，但 AI 网关未开放图片输入"
                                        if gateway_rejected else "图片分析请求失败，未将图片内容交给 AI")
                            if category == "多模态" and vision_rejected:
                                text_chunk = _text_analysis_messages(chunk)
                                if text_chunk:
                                    try:
                                        if on_progress:
                                            on_progress(f"{group_labels[group]}：图片入口不可用，改为分析本批文字")
                                        text_report = _complete_jyi_chunk(
                                            client, model, role_id, text_chunk, on_progress,
                                            max_output_tokens, max_document_chars,
                                            on_stream=batch_stream, is_cancelled=is_cancelled,
                                            include_images=False,
                                            diagnostic_context=diagnostic_context + "/text-fallback")
                                        text_cache_key = _summary_cache_key(
                                            "text-only-map-v4", model, role_id, group, text_chunk)
                                        cache[text_cache_key] = text_report
                                        if app is not None:
                                            _persist_summary_cache(app, cache)
                                        chunk_reports.append(text_report)
                                        group_processed += len(text_chunk)
                                        continue
                                    except AnalysisCancelled:
                                        raise
                                    except Exception as fallback_exc:
                                        group_error = _analysis_error(fallback_exc)
                                        break
                            LOGGER.info("JYI image batch failed; retaining completed text batches")
                            if chunk_reports:
                                continue
                            group_error = _analysis_error(exc)
                            break
                        group_error = _analysis_error(exc)
                        break
                else:
                    LOGGER.info(
                        "AI analysis batch cache hit: group=%s category=%s batch=%d/%d model=%s",
                        group, category, batch_index, category_totals[category], model)
                    cache[cache_key] = report
                    if send_images:
                        for item in chunk:
                            if (str(item.get("kind") or "").lower() == "image"
                                    and not str(item.get("media_status") or "").startswith((
                                    "本地没有可用图片缓存", "图片缓存无法",
                                    "图片分析请求失败"))):
                                item["media_status"] = "已复用之前的图片分析结果，本次未重新上传"
                    if on_stream:
                        # 缓存命中以前是静默的：界面一直转圈，最后全文突然出现，
                        # 看起来和"卡死后突然恢复"一模一样。回放出来。
                        prefix = (f"【{group_labels[group]}】\n"
                                  if show_group and group not in streamed_groups
                                  else ("\n\n" if show_group else ""))
                        streamed_groups.add(group)
                        if len(work_batches) > 1:
                            prefix += f"【分段 {_chunk_index}/{len(work_batches)} · {category}】\n"
                        if prefix:
                            on_stream(prefix)
                        _emit_stream(on_stream, report)
                chunk_reports.append(report)
                group_processed += len(batch_chunk)
            if group_error:
                failures.append(f"{group_labels[group]}：{group_error}")
                continue
            if not chunk_reports:
                reason = ("当前模型不支持图片输入" if image_support is False
                          else "模型没有生成可展示的分析内容")
                failures.append(f"{group_labels[group]}：{reason}")
                continue

            if len(chunk_reports) == 1:
                final_report = chunk_reports[0]
            else:
                final_report = "\n\n".join(
                    f"【分段 {index}/{len(chunk_reports)}】\n{report}"
                    for index, report in enumerate(chunk_reports, start=1))
            reports.append(f"【{group_labels[group]}】\n{final_report}" if show_group
                           else final_report)
            group_reports.append({
                "group_id": group,
                "group_label": group_labels[group],
                "report": final_report,
            })
            completed_groups.append(group)
            processed += group_processed
    except AnalysisCancelled:
        # 已完成的分段和缓存都在，尾巴照常组装成部分结果。
        cancelled = True

    # Bound process-local reuse while preserving the most recent completed batches.
    while len(cache) > 4096:
        cache.pop(next(iter(cache)))
    if app is not None:
        _persist_summary_cache(app, cache)
    combined = "\n\n".join(reports)
    partial_error = ("部分会话未完成：" + "；".join(failures)
                     + "。返回第六步重试时，已完成的分批结果会复用。" if failures and reports else "")
    if image_messages and image_support is False:
        skipped = f"当前所选模型不支持图片输入，本次有 {len(image_messages)} 条图片消息未发送给 AI。"
        partial_error = (partial_error + "；" + skipped) if partial_error else skipped
    elif image_messages:
        unavailable = sum(
            not (message.get("attachments") or [])
            or str(message.get("media_status") or "").startswith((
                "本地没有可用图片缓存", "图片缓存无法",
                "当前模型不支持图片输入", "当前模型或网关不支持图片输入",
                "图片分析请求失败", "已提交部分图片"))
            for message in image_messages
        )
        if unavailable:
            warning = ("本地图片已解密，但 AI 网关未开放图片输入，图片内容未参与本次分析。"
                       if image_gateway_rejected else
                       f"有 {unavailable} 条图片消息未能准备并提交给 AI，详情见附件列表。")
            partial_error = (partial_error + "；" + warning) if partial_error else warning
    if cancelled:
        note = "已取消分析：已完成的分段已保留，未完成的会话下次重试会继续。"
        partial_error = (partial_error + "；" + note) if partial_error else (note if reports else "")
    error = ("分析未完成：" + "；".join(failures)) if failures and not reports else ""
    if on_progress:
        on_progress("已取消分析" if cancelled else (
            "会话分析完成" if not failures and not partial_error else "部分会话分析完成"))
    return {"summary": combined, "error": error, "partial_error": partial_error,
            "group_reports": group_reports,
            "completed_groups": completed_groups, "cost": None,
            "message_count": processed, "cancelled": cancelled,
            "is_incremental": bool(role_id)}


def _run_summary_gateway_job(client: Any, messages: list[dict[str, Any]],
                              role_id: str, idempotency_key: str,
                              is_cancelled: Any = None, *,
                              analysis_stage: str = "map") -> tuple[str, int | None, str]:
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
             **({"role_id": role_id, "analysis_stage": analysis_stage} if role_id else {})},
            idempotency_key)
        job_id = str((submitted.get("job") or {}).get("job_id", ""))
        if not job_id:
            return "", 0, "提交失败，请稍后重试。"
    except Exception as exc:
        return "", 0, _gateway_error_text(exc, max_len=300)
    deadline = time.time() + SUMMARY_TIMEOUT
    last_billing: dict = {}
    while time.time() < deadline:
        if callable(is_cancelled) and is_cancelled():
            # 服务端 job 一并撤掉，别让它在那边空转扣费。
            try:
                cancel = getattr(client, "cancel_summary", None)
                if callable(cancel):
                    cancel(job_id)
            except Exception:
                pass
            raise AnalysisCancelled()
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
                          on_stream: Any = None,
                          is_cancelled: Any = None,
                          force_refresh: bool = False) -> dict[str, Any]:
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
    group_reports: list[dict[str, str]] = []
    failures: list[str] = []
    completed_groups: list[str] = []
    processed = 0
    total_cost = 0
    cost_known = True
    submitted_batches = 0
    run_nonce = uuid.uuid4().hex if force_refresh else ""

    def run_batch(stage: str, group: str, batch: list[dict[str, Any]]) -> tuple[str, str]:
        nonlocal total_cost, cost_known, submitted_batches
        key = _summary_cache_key(f"text-only-{stage}-v4", model, role_id, group, batch)
        cached = None if force_refresh else cache.pop(key, None)
        if cached:
            cache[key] = cached
            return cached, ""
        submitted_batches += 1
        idempotency_seed = f"{key}:{run_nonce}" if run_nonce else key
        idempotency = "sum-" + hashlib.sha256(idempotency_seed.encode("utf-8")).hexdigest()[:32]
        report, cost, error = _run_summary_gateway_job(
            client, batch, role_id, idempotency, analysis_stage=stage,
            is_cancelled=is_cancelled)
        if cost is None:
            cost_known = False
        else:
            total_cost += cost
        if report:
            cache[key] = report
        return report, error

    cancelled = False
    try:
        for group_index, (group, group_messages) in enumerate(grouped.items(), start=1):
            _raise_if_cancelled(is_cancelled)
            if on_progress:
                on_progress(f"正在分析第 {group_index}/{len(grouped)} 个会话：{group_labels[group]}")
            show_group = len(grouped) > 1
            if on_stream and show_group:
                prefix = (f"【{group_labels[group]}】\n" if group_index == 1 else f"\n\n【{group_labels[group]}】\n")
                on_stream(prefix)
            chunks = [chunk for _, chunk in _summary_work_batches(
                group_messages, token_budget=SUMMARY_CHUNK_FALLBACK_TOKENS,
                max_document_chars=SUMMARY_CHUNK_MAX_DOCUMENT_CHARS,
                include_images=False)]
            if not chunks:
                failures.append(f"{group_labels[group]}：没有可供模型分析的文字内容")
                continue
            chunk_reports: list[str] = []
            failed = ""
            for index, chunk in enumerate(chunks, start=1):
                _raise_if_cancelled(is_cancelled)
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
            group_reports.append({
                "group_id": group,
                "group_label": group_labels[group],
                "report": final_report,
            })
            completed_groups.append(group)
            processed += sum(len(chunk) for chunk in chunks)
    except AnalysisCancelled:
        cancelled = True
    while len(cache) > 4096:
        cache.pop(next(iter(cache)))
    combined = "\n\n".join(reports)
    partial_error = ("部分会话未完成：" + "；".join(failures)
                     + "。返回第六步重试时，已完成的分批结果会复用。" if failures and reports else "")
    if cancelled:
        note = "已取消分析：已完成的分段已保留，未完成的会话下次重试会继续。"
        partial_error = (partial_error + "；" + note) if partial_error else (note if reports else "")
    error = ("分析未完成：" + "；".join(failures)) if failures and not reports else ""
    if on_progress:
        on_progress("已取消分析" if cancelled else ("会话分析完成" if not failures else "部分会话分析完成"))
    return {"summary": combined, "error": error, "partial_error": partial_error,
            "group_reports": group_reports,
            "completed_groups": completed_groups,
            "cost": (total_cost / 1_000_000 if submitted_batches and cost_known else None),
            "message_count": processed, "cancelled": cancelled,
            "is_incremental": bool(role_id)}


def run_summary(app: Any, result: Any, role_id: str = "",
                messages_override: list[dict[str, Any]] | None = None,
                on_progress: Any = None,
                on_stream: Any = None,
                is_cancelled: Any = None,
                incremental: bool | None = None,
                force_refresh: bool = False) -> dict[str, Any]:
    """经云网关跑摘要。**在后台线程里跑，调用方不许碰界面。**

    LLM 只走云：这里不再读任何本地密钥。没登录返回"需要登录产品账号"。
    返回 ``{"summary", "error", "cost", "message_count"}``，从不抛异常 ——
    网络不可预期，不该让调用方的线程死掉。
    ``is_cancelled`` 为可调用对象时，每个批次/轮询间隙检查一次；
    取消后返回已完成分段与 ``cancelled=True``。
    ``role_id`` 非空时默认走按角色增量路径；调用方可用 ``incremental=False``
    声明全量报告应覆盖旧报告。``force_refresh`` 跳过本地已完成分段缓存，
    用于用户明确要求重新分析全部历史的场景。
    """
    messages = messages_override if messages_override is not None else messages_for_summary(app, result, role_id=role_id)
    is_incremental = bool(role_id) if incremental is None else bool(incremental)
    if not messages:
        return {"summary": "", "error": "没读到消息", "cost": None, "message_count": 0,
                "is_incremental": is_incremental}
    target = _app(app)
    client = _gateway_client(app)
    if client is None:
        return {"summary": "", "error": "需要登录产品账号",
                "cost": None, "message_count": len(messages),
                "is_incremental": is_incremental}
    if not _SUMMARY_RUN_LOCK.acquire(blocking=False):
        return {"summary": "", "error": "已有智能分析任务正在运行，请等待当前任务结束。",
                "cost": None, "message_count": len(messages),
                "is_incremental": is_incremental}
    try:
        if _is_jyi_client(client):
            settings = getattr(target, "settings", None)
            model_alias = str(getattr(settings, "model_alias", "") or "")
            outcome = _run_summary_jyi(client, messages, role_id, model_alias, target,
                                        on_progress, on_stream, is_cancelled, force_refresh)
        else:
            outcome = _run_summary_gateway(target, client, messages, role_id,
                                           on_progress, on_stream, is_cancelled,
                                           force_refresh)
            image_count = sum(str(item.get("kind") or "").lower() == "image"
                              for item in messages)
            if image_count:
                warning = f"当前连接只接收文字，本次有 {image_count} 条图片消息未发送给 AI。"
                existing = str(outcome.get("partial_error") or "")
                outcome["partial_error"] = (existing + "；" + warning) if existing else warning
        outcome["is_incremental"] = is_incremental
        return outcome
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
    "AnalysisCancelled",
    "SUMMARY_MESSAGE_LIMIT",
    "SUMMARY_POLL_INTERVAL",
    "SUMMARY_TIMEOUT",
    "accumulated_coverage",
    "has_messages_for",
    "import_reference",
    "messages_for_summary",
    "history_message_counts",
    "messages_for_history",
    "analyzed_group_ids",
    "run_summary",
]
