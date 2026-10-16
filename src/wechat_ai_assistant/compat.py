"""Compatibility helpers for installations that used the ``pyweixin`` name."""
from __future__ import annotations

import os


def import_legacy_environment() -> None:
    """Map old PYWEIXIN_* variables to WECHAT_AI_* without overriding new ones."""
    for name, value in tuple(os.environ.items()):
        if name.startswith("PYWEIXIN_"):
            os.environ.setdefault("WECHAT_AI_" + name[len("PYWEIXIN_"):], value)
