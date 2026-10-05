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

import os
import pathlib
from typing import Any, Mapping, Sequence

from .summary import DEFAULT_SUMMARY_MODEL, summarize_messages

#: 交给模型的最多消息条数。上下文和钱都有限。
SUMMARY_MESSAGE_LIMIT = 200

#: 商户本地配置上游密钥用的环境变量名。
API_KEY_ENV = "PYWEIXIN_OPENROUTER_API_KEY"

#: 本地开发时放密钥的文件。跟着商户数据目录，不进仓库。
KEY_FILE = pathlib.Path.home() / ".pyweixin_dev" / "openrouter.key"


def local_api_key() -> str:
    """本地配置的上游密钥。读不到返回空串，**不抛异常**。

    **不从一个「会话」对象上取。** 会话里只有商户的 access_token，没有
    上游密钥 —— 上游密钥是运营配置，不该跟着商户的登录 token 进界面。
    """
    key = os.environ.get(API_KEY_ENV, "")
    if key.strip():
        return key.strip()
    try:
        if KEY_FILE.is_file():
            return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        from .openrouter_adapter import api_key_from_env
        return api_key_from_env()
    except Exception:
        return ""


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
    """把这次读到的**会话**的消息取出来。

    ## 为什么按会话取而不是按「最后一批导入」取

    读取分两种：「读取全部」导入一大批，「更新历史」只导入新增的几条。
    只按最后一次导入的 ``import_ids`` 取，于是「更新」之后摘要的范围就
    塌缩成那几条新的 —— 商户刚明明读到了 12 条，一点摘要只剩 2 条，
    界面和他刚做的事对不上。

    商户口径是**「这个群我读过什么」**，所以按 ``result.coverage`` 里的
    会话取（对应 ``messages.group_id``）。

    ## 拿不到会话时

    退回按导入批次取 —— 旧的结果对象可能没有 coverage。两条路径都在，
    但主路径是会话。
    """
    target = _app(app)
    store = getattr(target, "orders", None)
    tenant = getattr(target, "tenant", "")
    if store is None:
        return []
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
            [tenant, *groups, SUMMARY_MESSAGE_LIMIT]).fetchall()
        # 上面按时间**倒序**取最近的，交给模型时要正过来。
        return [_row_to_message(r) for r in reversed(rows)]

    import_ids = [str(x) for x in (getattr(result, "import_ids", None) or []) if x]
    if not import_ids:
        return []
    marks = ",".join("?" for _ in import_ids)
    rows = store.db.execute(
        "SELECT sender, content, kind, COALESCE(sent_at, observed_at) AS sent_at"
        " FROM messages WHERE merchant_id=? AND import_id IN (" + marks + ")"
        " ORDER BY COALESCE(sent_at_epoch, strftime('%s', observed_at)), observed_at, id",
        [tenant, *import_ids]).fetchall()
    return [_row_to_message(r) for r in rows]


def _row_to_message(row: Any) -> dict[str, Any]:
    """一行消息 → 给模型的形状。**必须带上 kind**。

    图片消息的 ``content`` 是占位符「图片」，**语音消息的 content 直接是空的**。
    不带 kind，模型看到的是「王姨：（空）」，完全不知道发生了什么。
    """
    return {"sender": row["sender"], "content": row["content"],
            "kind": row["kind"], "sent_at": row["sent_at"]}


def run_summary(app: Any, result: Any) -> dict[str, Any]:
    """真正去调模型。**在后台线程里跑，调用方不许碰界面。**

    返回 ``{"summary", "error", "cost", "message_count"}``，从不抛异常 ——
    网络不可预期，不该让调用方的线程死掉。
    """
    messages = messages_for_summary(app, result)
    if not messages:
        return {"summary": "", "error": "没读到消息", "cost": None, "message_count": 0}
    api_key = local_api_key()
    if not api_key:
        return {"summary": "", "error": "还没配置模型密钥，联系不上模型",
                "cost": None, "message_count": len(messages)}
    try:
        return summarize_messages(
            messages,
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model=DEFAULT_SUMMARY_MODEL,
        )
    except Exception as exc:
        return {"summary": "", "error": type(exc).__name__, "cost": None,
                "message_count": len(messages)}


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
    "accumulated_coverage",
    "has_messages_for",
    "import_reference",
    "local_api_key",
    "messages_for_summary",
    "run_summary",
]