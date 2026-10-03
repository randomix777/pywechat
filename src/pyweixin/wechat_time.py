"""微信时间戳的**唯一**解析实现。

真实微信导出的 ``消息发送时间`` 长这样::

    2026-10-2 12:21

两个地方和 ISO 8601 不一样，而且都会静默出错：

1. **日的位数不固定** —— ``10-2`` 而不是 ``10-02``。
   ``datetime.fromisoformat`` 直接抛 :class:`ValueError`。
2. **没有时区** —— 界面上显示的就是本机时间。

这两个问题在阅读历史（:mod:`pyweixin.history_read`）和入库
（:mod:`pyweixin.orders`）两处都踩过，且都表现为**静默失败**而不是报错：

* 阅读历史侧解析失败 → ``_filter`` 对解析不出的时间**无条件保留**消息，
  于是「最近一天」这个时间范围在真实数据上完全不生效，窗口外的消息照样进库，
  界面只能显示 ``? 至 ?``。
* 入库侧解析失败 → ``sent_at_epoch`` 存成 ``NULL``，消息之间无法按时间排序。

所以必须只有这一份实现，两边都调它 —— 分两份就等于把这个 bug 复制一遍。

**为什么无时区按本机时区解释**：这个时间戳是微信**界面上显示给用户看**的时间，
用户看到「12:21」就是本机时钟的 12:21。按 UTC 解释会让商户看到的时间范围整体
偏移一个时区。注意这与免费额度「每天」的口径**故意不同**：计费的换日必须可配置
（见 :func:`pyweixin.product.day_key`），而聊天时间只有一个正确答案。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

#: 微信实际会给出的几种写法。按可能性从高到低试。
#: 注意前两个允许日不补零，``%m``/``%d`` 会自动接受 1～2 位。
_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y年%m月%d日 %H:%M",
)


def local_timezone() -> timezone:
    """本机时区。取不到就回落 UTC，不让时间解析整个崩掉。"""
    try:
        tz = datetime.now().astimezone().tzinfo
    except (OSError, ValueError):  # pragma: no cover - 平台相关
        return timezone.utc
    return tz or timezone.utc


def _parse_loose(text: str) -> datetime | None:
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_timestamp(value: Any) -> datetime | None:
    """把微信的时间戳解析成**带时区**的时间；认不出来返回 ``None``。

    无时区的时间按**本机时区**解释（见模块文档）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=local_timezone())
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = _parse_loose(text)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=local_timezone())


def sent_epoch(value: Any) -> float | None:
    """消息发送时刻的 UNIX 时间戳。认不出来返回 ``None``。

    以前这里用 ``datetime.fromisoformat`` 并且**显式拒绝**无时区输入，于是真实
    微信导出的每一条消息都是 ``NULL``，消息之间没法按时间排序。
    """
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):  # pragma: no cover - 极端输入
        return None