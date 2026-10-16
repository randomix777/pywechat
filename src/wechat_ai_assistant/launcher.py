"""双击启动入口：不依赖 PYTHONPATH、环境变量或开发环境。

``python -m wechat_ai_assistant.merchant_app`` 需要 ``PYTHONPATH=src``，那是**开发**用的。
商户双击时不会设这个变量，所以这里自己把路径准备好。

同时负责本地数据目录：打包便携版使用 EXE 同级的 ``data`` 目录；
源码运行使用 ``%LOCALAPPDATA%\\wechat-ai-assistant``，并兼容旧 ``pyweixin`` 数据。
所有依赖缺失都会给出**中文**说明。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    """定位仓库根目录：``<root>/src/wechat_ai_assistant/launcher.py``。"""
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
    # This function is also called by users who import launcher directly.
    bootstrap_paths()
    from wechat_ai_assistant.paths import default_data_dir

    return default_data_dir()


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


def _show_update_splash(version: str) -> None:
    """更新闪屏：告诉商户正在换版、完成后自动打开，不要重复双击。

    打包后没有控制台，print 等于没说。Tk 顶 3 秒，更新脚本接管后
    本进程退出，闪屏跟着消失。任何异常都静默跳过 —— 闪屏不能挡更新。
    """
    try:
        import tkinter as tk
    except Exception:
        return
    root = None
    try:
        root = tk.Tk()
        root.title("微信AI智能助理 - 正在更新")
        root.resizable(False, False)
        label = f"正在更新到 v{version or '新版本'}，完成后自动打开，请稍候…\n请不要重复双击。"
        tk.Label(root, text=label, font=("Microsoft YaHei", 10),
                 padx=28, pady=22).pack()
        root.update_idletasks()
        root.update()
        import time as _time
        deadline = _time.time() + 3.0
        while _time.time() < deadline:
            try:
                root.update()
            except Exception:
                break
            _time.sleep(0.05)
    except Exception:
        pass
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass


def _check_and_apply_update() -> bool:
    """启动前检查新版；发现新版时下载、校验并交给替换脚本重启。

    返回 True 表示更新脚本已接管当前进程，调用方应结束旧进程；
    False 表示无更新或更新不可用，继续启动当前客户端。
    """
    try:
        from wechat_ai_assistant.updater import (
            apply_pending_update,
            check_for_update,
            download_and_stage,
            is_frozen_app,
            pending_update,
        )
        if not is_frozen_app():
            return False
        # 之前下载完但尚未来得及替换：先应用它，避免重复下载。
        pending = pending_update()
        if pending is not None:
            _show_update_splash(str(pending.get("version") or ""))
            return apply_pending_update()
    except Exception:
        return False

    # 下载在工作线程执行，启动提示窗口保持响应。检查/下载失败不影响旧版启动。
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

    def work() -> None:
        try:
            info = check_for_update()
            if info is None:
                result_queue.put(("current", None))
                return
            staged = download_and_stage(info)
            result_queue.put(("staged" if staged is not None else "failed", info))
        except Exception:
            result_queue.put(("failed", None))

    try:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title("微信AI智能助理")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        body = ttk.Frame(root, padding=(28, 22))
        body.pack(fill="both", expand=True)
        status = ttk.Label(body, text="正在检查客户端更新…", font=("Microsoft YaHei", 10))
        status.pack(anchor="w")
        progress = ttk.Progressbar(body, mode="indeterminate", length=310)
        progress.pack(fill="x", pady=(14, 0))
        progress.start(12)
        root.update_idletasks()
        root.geometry(f"{root.winfo_reqwidth()}x{root.winfo_reqheight()}")
        cancelled = {"value": False}

        def close_window() -> None:
            cancelled["value"] = True
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", close_window)
        threading.Thread(target=work, name="startup-update-check", daemon=True).start()
        outcome: tuple[str, Any] | None = None

        def poll() -> None:
            nonlocal outcome
            if cancelled["value"]:
                return
            try:
                outcome = result_queue.get_nowait()
            except queue.Empty:
                root.after(100, poll)
                return
            kind, info = outcome
            if kind == "staged":
                status.configure(text=f"正在更新到 v{info.get('version', '新版本')}，完成后自动打开…")
                progress.configure(mode="determinate", value=100)
                root.after(500, root.destroy)
            else:
                root.destroy()

        root.after(100, poll)
        root.mainloop()
        if cancelled["value"]:
            return False
        if outcome is not None and outcome[0] == "staged":
            return apply_pending_update()
        return False
    except Exception:
        # Tk 初始化失败时仍可自动更新；这一条路径只在真实发现新版时才下载。
        try:
            info = check_for_update()
            if info is None or download_and_stage(info) is None:
                return False
            return apply_pending_update()
        except Exception:
            return False


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
    # 打包客户端每次启动前检查上游。新版会先下载并校验，再由独立脚本
    # 替换目录并自动重启；无新版/网络失败时继续用当前版本启动。
    try:
        args = list(argv if argv is not None else sys.argv[1:])
        if "--check" not in args and _check_and_apply_update():
            return 0
    except Exception:
        pass
    try:
        import wechat_ai_assistant  # noqa: F401
    except Exception as exc:  # pragma: no cover - defensive
        return _fail(
            "软件文件不完整",
            f"没有找到本软件的核心文件：\n{exc}\n\n请重新解压便携版文件夹后重试。",
        )

    missing = check_dependencies()
    if missing:
        lines = ["这台电脑上缺少以下组件，无法运行：", ""]
        for name, description in missing:
            lines.append(f"· {description}（{name}）")
        lines += [
            "",
            "请重新下载并解压完整的便携版软件包。",
        ]
        return _fail("缺少运行组件", "\n".join(lines))

    try:
        # 按绝对模块名导入：本文件既可能被 ``python -m wechat_ai_assistant.launcher``
        # 启动，也可能被 ``python path\\to\\launcher.py`` 直接执行。后者没有
        # 包上下文，相对导入会失败。
        from wechat_ai_assistant.merchant_app import main as app_main
    except Exception as exc:  # pragma: no cover - defensive
        return _fail("软件启动失败", f"{exc}\n\n请把这段信息发给客服。")

    if "--data-dir" not in args:
        try:
            args = ["--data-dir", str(data_dir())] + args
        except RuntimeError as exc:
            return _fail("便携数据迁移失败", str(exc))
    return app_main(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
