"""双击启动入口：不依赖 PYTHONPATH、环境变量或开发环境。

``python -m pyweixin.merchant_app`` 需要 ``PYTHONPATH=src``，那是**开发**用的。
商户双击时不会设这个变量，所以这里自己把路径准备好。

同时负责本地数据目录：优先用 ``%LOCALAPPDATA%\\pyweixin``，
找不到时退回用户目录。所有依赖缺失都会给出**中文**说明，而不是 traceback。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def repo_root() -> Path:
    """定位仓库根目录：``<root>/src/pyweixin/launcher.py``。"""
    return Path(__file__).resolve().parents[2]


def bootstrap_paths() -> list[str]:
    """把 ``src`` 和 ``packages/*/src`` 加进 ``sys.path``。幂等。"""
    root = repo_root()
    candidates = [root / "src"]
    packages = root / "packages"
    if packages.is_dir():
        candidates.extend(sorted(child / "src" for child in packages.iterdir() if child.is_dir()))
    added: list[str] = []
    for path in candidates:
        text = str(path)
        if path.is_dir() and text not in sys.path:
            sys.path.insert(0, text)
            added.append(text)
    return added


def data_dir() -> Path:
    """商户的本地数据目录。断电或换用户也不会丢。"""
    override = os.environ.get("PYWEIXIN_CLIENT_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".pyweixin"
    return root / "pyweixin"


def check_dependencies() -> list[tuple[str, str]]:
    """返回缺失依赖的 ``(包名, 中文说明)``。空列表表示齐全。"""
    required = [
        ("tkinter", "图形界面组件"),
        ("sqlite3", "本地数据库"),
        ("pywinauto", "微信窗口读取"),
        ("win32gui", "微信窗口操作"),
        ("psutil", "进程检测"),
    ]
    missing: list[tuple[str, str]] = []
    for name, description in required:
        try:
            __import__(name)
        except ImportError:
            missing.append((name, description))
    return missing


def _fail(title: str, detail: str) -> int:
    print("=" * 56)
    print(f"  {title}")
    print()
    for line in detail.strip().splitlines():
        print(f"  {line}")
    print()
    print("=" * 56)
    try:
        input("按回车键关闭…")
    except (EOFError, KeyboardInterrupt):
        pass
    return 1


def main(argv: list[str] | None = None) -> int:
    """启动商户软件。``--check`` 只做自检。"""
    if sys.version_info < (3, 10):
        return _fail(
            "需要更新 Python",
            "本软件需要 Python 3.10 或更高版本。\n请到 python.org 下载安装后重试。",
        )

    bootstrap_paths()
    try:
        import pyweixin  # noqa: F401
    except Exception as exc:  # pragma: no cover - defensive
        return _fail(
            "软件文件不完整",
            f"没有找到本软件的核心文件：\n{exc}\n\n请重新解压或重新安装本软件。",
        )

    missing = check_dependencies()
    if missing:
        lines = ["这台电脑上缺少以下组件，无法运行：", ""]
        for name, description in missing:
            lines.append(f"· {description}（{name}）")
        lines += [
            "",
            "请重新运行安装程序，选择「完整安装」以补齐这些组件。",
        ]
        return _fail("缺少运行组件", "\n".join(lines))

    try:
        # 按绝对模块名导入：本文件既可能被 ``python -m pyweixin.launcher``
        # 启动，也可能被 ``python path\\to\\launcher.py`` 直接执行。后者没有
        # 包上下文，相对导入会失败。
        from pyweixin.merchant_app import main as app_main
    except Exception as exc:  # pragma: no cover - defensive
        return _fail("软件启动失败", f"{exc}\n\n请把这段信息发给客服。")

    args = list(argv if argv is not None else sys.argv[1:])
    if "--data-dir" not in args:
        args = ["--data-dir", str(data_dir())] + args
    return app_main(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
