"""出问题时能拿出证据。

## 为什么必须有

这是一个**驱动另一个程序**（微信）的桌面应用：读消息要动微信窗口、发回执要
动微信窗口。任何一个环节出问题，商户看到的都只是「读取失败」四个字 ——
他自己查不了，也不知道该跟客服说什么。

而在这个模块之前，`merchant_app.py` 里 `logging` 出现 0 次：**整个应用没
有落盘日志**。所有 ``LOGGER.warning`` / ``LOGGER.exception`` 全部落空，
客户端又跑在 ``pythonw.exe``（没有控制台），连 stderr 也丢。

也就是说：**产品报不出自己的故障**。

## 三条硬规矩

1. **不写完整聊天内容。** 日志可能被商户转发、也可能落在别人能翻到的目录。
   消息正文一律不记；长度和条数可以记 —— 那才是排障要的东西。
2. **不写凭据。** 密钥、令牌、口令一律不记。已有的脱敏见 :func:`_redact`。
3. **有上限。** 单文件 1 MB，最多留 3 个。日志不是垃圾堆。
"""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "merchant.log"
MAX_BYTES = 1024 * 1024      # 单个文件 1 MB
BACKUP_COUNT = 3             # 留 3 个历史，合计不超过 4 MB

#: 默认不记的模块。这些每时每刻都在刷 INFO，记下来只会把真问题淹掉。
QUIET_BY_DEFAULT = (
    "pyweixin.wechat_state",     # 每 2 秒探测一次微信
    "pywinauto",                 # 控件查找的 DEBUG 噪音极大
    "pyautogui",
    "urllib3",
)


def _redact(text: str) -> str:
    """把明显的凭据形态替换掉。

    宁可误伤：日志是用来排障的，不是用来归档的。少几个字符不致命，
    泄一个密钥是致命的。
    """
    patterns = (
        (re.compile(r"(sk-[A-Za-z0-9_\-]{8})[A-Za-z0-9_\-]+"), r"\1***"),
        (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***"),
        (re.compile(r"((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)\S+", re.I),
         r"\1***"),
    )
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    return text


class _RedactingFormatter(logging.Formatter):
    """每条记录都过一遍脱敏。模块级脱敏会被后来加的日志绕过。"""

    def format(self, record: logging.LogRecord) -> str:
        record.msg = _redact(str(record.getMessage()))
        record.args = ()
        return super().format(record)


def install(data_dir: Path, level: int = logging.INFO) -> Path | None:
    """把日志接到 ``data_dir``。返回日志路径；接不上就返回 None。

    接不上**不能**让程序起不来 —— 日志是手段，程序是目的。
    """
    try:
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / LOG_FILE_NAME
        handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8",
        )
        handler.setFormatter(_RedactingFormatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
        ))
        handler.setLevel(level)

        root = logging.getLogger()
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(level)
        # 客户端跑在 pythonw 下，没有控制台。不接管 stderr ——
        # 开发时 `python -m pyweixin.merchant_app` 的输出还得看得见。
        logging.getLogger(__name__).info(
            "日志已开启：%s（单文件上限 %d KB，保留 %d 个）",
            path, MAX_BYTES // 1024, BACKUP_COUNT,
        )
        return path
    except Exception:  # pragma: no cover - 磁盘满、目录无权限
        logging.getLogger(__name__).debug("无法启用文件日志", exc_info=True)
        return None


def quiet_noisy_modules() -> None:
    """把高频模块调到 WARNING 以上。"""
    for name in QUIET_BY_DEFAULT:
        logging.getLogger(name).setLevel(logging.WARNING)


def path_of(data_dir: Path) -> Path:
    return Path(data_dir) / LOG_FILE_NAME