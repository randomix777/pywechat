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

from typing import Any, Mapping, Sequence

#: 交给模型的最多消息条数。上下文和钱都有限。
SUMMARY_MESSAGE_LIMIT = 200


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
    """只取本次读取批次的消息，避免把同一会话旧批次的记录算进来。"""
    target = _app(app)
    store = getattr(target, "orders", None)
    tenant = getattr(target, "tenant", "")
    if store is None:
        return []
    import_ids = [str(x) for x in (getattr(result, "import_ids", None) or []) if x]
    if import_ids:
        marks = ",".join("?" for _ in import_ids)
        rows = store.db.execute(
            "SELECT sender, content, kind, COALESCE(sent_at, observed_at) AS sent_at"
            " FROM messages WHERE merchant_id=? AND import_id IN (" + marks + ")"
            " ORDER BY COALESCE(sent_at_epoch, strftime('%s', observed_at)), observed_at, id"
            " LIMIT ?",
            [tenant, *import_ids, SUMMARY_MESSAGE_LIMIT],
        ).fetchall()
        return [_row_to_message(r) for r in rows]

    # 新版 HistoryResult 即使本次 0 条也会带 import_ids=[]；这时不能回退
    # 到会话全量，否则刚读了 30 条却把库里的几百条旧消息也送去整理。
    if hasattr(result, "import_ids"):
        return []

    # 仅兼容没有 import_ids 字段的旧结果对象，按其本次读取条数限制范围。
    groups: list[str] = []
    for item in getattr(result, "coverage", None) or []:
        conversation = getattr(item, "conversation", None)
        name = getattr(conversation, "name", "") if conversation is not None else ""
        if name and name not in groups:
            groups.append(str(name))

    if groups:
        marks = ",".join("?" for _ in groups)
        rows = store.db.execute(
            "SELECT sender, content, kind, COALESCE(sent_at, observed_at) AS sent_at"
            " FROM messages WHERE merchant_id=? AND group_id IN (" + marks + ")"
            " ORDER BY COALESCE(sent_at_epoch, strftime('%s', observed_at)) DESC, id DESC"
            " LIMIT ?",
            [tenant, *groups, min(SUMMARY_MESSAGE_LIMIT,
                                  max(0, int(getattr(result, "total_messages", 0) or 0)))],
        ).fetchall()
        return [_row_to_message(r) for r in reversed(rows)]
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


def messages_for_history(app: Any, result: Any) -> list[dict[str, Any]]:
    """Fetch up to the analysis limit from successful selected conversations."""
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
    marks = ",".join("?" for _ in names)
    rows = store.db.execute(
        "SELECT group_id, sender, content, kind, COALESCE(sent_at, observed_at) AS sent_at"
        " FROM messages WHERE merchant_id=? AND group_id IN (" + marks + ")"
        " ORDER BY COALESCE(sent_at_epoch, strftime('%s', observed_at)) DESC, id DESC LIMIT ?",
        [tenant, *names, SUMMARY_MESSAGE_LIMIT]).fetchall()
    return [{"group": str(row["group_id"] or ""), "sender": row["sender"],
             "content": row["content"], "kind": row["kind"], "sent_at": row["sent_at"]}
            for row in reversed(rows)]


def _row_to_message(row: Any) -> dict[str, Any]:
    """一行消息 → 给模型的形状。**必须带上 kind**。

    图片消息的 ``content`` 是占位符「图片」，**语音消息的 content 直接是空的**。
    不带 kind，模型看到的是「王姨：（空）」，完全不知道发生了什么。
    """
    return {"sender": row["sender"], "content": row["content"],
            "kind": row["kind"], "sent_at": row["sent_at"]}


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


def _run_summary_jyi(client: Any, messages: list[dict[str, Any]], role_id: str = "",
                     model_alias: str = "") -> dict[str, Any]:
    """JYI 直调摘要：提示词同样复用 `summary` 那一份，不另写。

    计费不在本地算（JYI 按账号计量），cost 恒为 None —— 别让界面
    把它读成"这次没花钱"。
    """
    from .jyi_parse import (
        SUMMARY_MAX_OUTPUT_TOKENS,
        build_summary_messages, build_role_analysis_messages,
        extract_text,
    )
    from .summary import validate_summary_result

    count = len(messages)
    try:
        model = model_alias or client.text_model()
        body = client.chat_completions(
            model, (build_role_analysis_messages(messages, role_id) if role_id
                    else build_summary_messages(messages)),
            max_tokens=SUMMARY_MAX_OUTPUT_TOKENS)
        text = validate_summary_result(extract_text(body))
    except ValueError as exc:
        # 校验挂的是我们自己的话（空/超长），直接说，不进网关错误改写。
        return {"summary": "", "error": str(exc) or "摘要结果异常",
                "cost": None, "message_count": count}
    except Exception as exc:
        # 状态行能换行显示，透传上限放宽：配额这类长说明截了看不懂。
        return {"summary": "", "error": _gateway_error_text(exc, max_len=300),
                "cost": None, "message_count": count}
    if not text:
        return {"summary": "", "error": "摘要结果异常，已转人工核对",
                "cost": None, "message_count": count}
    return {"summary": text, "error": "", "cost": None,
            "message_count": count}


def run_summary(app: Any, result: Any, role_id: str = "",
                messages_override: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """经云网关跑摘要。**在后台线程里跑，调用方不许碰界面。**

    LLM 只走云：这里不再读任何本地密钥。没登录返回"需要登录产品账号"。
    返回 ``{"summary", "error", "cost", "message_count"}``，从不抛异常 ——
    网络不可预期，不该让调用方的线程死掉。
    """
    messages = messages_override if messages_override is not None else messages_for_summary(app, result)
    if not messages:
        return {"summary": "", "error": "没读到消息", "cost": None, "message_count": 0}
    client = _gateway_client(app)
    if client is None:
        return {"summary": "", "error": "需要登录产品账号",
                "cost": None, "message_count": len(messages)}
    if _is_jyi_client(client):
        target = _app(app)
        settings = getattr(target, "settings", None)
        model_alias = str(getattr(settings, "model_alias", "") or "")
        return _run_summary_jyi(client, messages, role_id, model_alias)
    import hashlib as _hashlib
    import json as _json
    import time as _time

    payload_messages = [
        {"sender": str(m.get("sender", ""))[:128],
         "content": str(m.get("content", ""))[:4000],
         "sent_at": str(m.get("sent_at", ""))[:64],
         "kind": str(m.get("kind", "text"))[:32]}
        for m in messages
    ]
    fingerprint = _hashlib.sha256(
        _json.dumps(payload_messages, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]
    idempotency_key = f"sum-{fingerprint}"
    try:
        submitted = client.submit_summary(
            {"task": "summarize", "messages": payload_messages,
             **({"role_id": role_id} if role_id else {})},
            idempotency_key if not role_id else f"role-{idempotency_key}")
        job_id = str((submitted.get("job") or {}).get("job_id", ""))
        if not job_id:
            return {"summary": "", "error": "提交失败，请稍后重试",
                    "cost": None, "message_count": len(messages)}
    except Exception as exc:
        return {"summary": "", "error": _gateway_error_text(exc),
                "cost": None, "message_count": len(messages)}
    deadline = _time.time() + SUMMARY_TIMEOUT
    last_billing: dict = {}
    while _time.time() < deadline:
        _time.sleep(SUMMARY_POLL_INTERVAL)
        try:
            fetched = client.summary_job(job_id)
        except Exception:
            continue  # 轮询失败就下轮再问，不判死刑
        job = fetched.get("job") or {}
        billing = fetched.get("billing") or {}
        if isinstance(billing, dict) and billing.get("available", True):
            last_billing = billing
        state = str(job.get("state", ""))
        if state == "succeeded":
            text = _summary_text(job.get("result"))
            if text:
                return {"summary": text, "error": "",
                        "cost": _billed_cost(last_billing),
                        "message_count": len(messages)}
            return {"summary": "", "error": "摘要结果异常，已转人工核对",
                    "cost": _billed_cost(last_billing),
                    "message_count": len(messages)}
        if state in ("failed", "cancelled"):
            error = job.get("error") or {}
            return {"summary": "",
                    "error": str(error.get("message") or "摘要失败，请稍后重试"),
                    "cost": _billed_cost(last_billing),
                    "message_count": len(messages)}
    return {"summary": "", "error": "摘要超时，结果未知（以服务端结算为准）",
            "cost": _billed_cost(last_billing), "message_count": len(messages)}


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
    # GatewayError 把商户话术放 merchant_message（不是 message）。
    # 非网关异常没有商户话术，直接兜底，不透传技术原文。
    message = str(getattr(exc, "merchant_message", "")
                  or getattr(exc, "message", ""))
    lowered = message.casefold()
    if "no published price" in lowered and "bill" in lowered:
        return "当前模型尚未发布计费价格，暂时无法分析。请稍后重试或联系 JYI 客服。"
    if code in ("network_unavailable", "backend_unavailable"):
        return "网络暂不可用"
    if code in ("unauthenticated",):
        return "需要登录产品账号"
    if code == "business_api_unavailable":
        return str(getattr(exc, "merchant_message", "智能整理接口尚未接入"))
    if code in ("quota_exhausted", "free_quota_exhausted", "subscription_inactive"):
        return message or "分析额度不足"
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
