"""检查更新与静默更新：启动前检查 GitHub Releases，有新版就替换并重启。

## 流程（全自动，不弹窗）

1. 启动入口查 ``releases/latest``，有新版就下载；
2. 压缩包 + `.sha256` 双文件校验，验不过直接扔掉，绝不安装；
3. 解压到版本目录并写 ``pending.json``；
4. 起一个分离的更新脚本，
   等本进程退出后换目录、重启，旧版留备份，起不来自动回滚。

## 为什么用分离脚本替换

Windows 下运行中的 exe 和 DLL 全被锁死，原地覆盖必失败。
换目录 + 重启是唯一不拼运气的做法。更新在启动阶段完成，不会中断
已经打开并正在使用的客户端。

## 约定（发版的人必须遵守，见 docs/release.md）

* 版本号就是 Git tag 去掉开头的 ``v``，和 :mod:`wechat_ai_assistant.version` 一致；
* 安装包叫 ``微信AI智能助理-win64-<版本>.zip``，内容是整个 onedir 目录；
* 同名加 ``.sha256`` 放十六进制哈希（单个文件哈希，不是 checksum 列表）。

这里任何一步失败都只记日志、不抛异常 —— 更新永远不能挡住启动。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping

LOGGER = logging.getLogger(__name__)

#: 默认查这个仓库的 releases。换地方发版就改这里（或调用时传 repo）。
DEFAULT_REPO = "randomix777/wechat-ai-assistant"
API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"

#: 安装包名前缀。完整文件名形如 ``微信AI智能助理-win64-1.9.9.zip``。
ASSET_PREFIX = "微信AI智能助理-win64-"
PENDING_NAME = "pending.json"
UPDATES_DIR_NAME = "_updates"
BACKUP_PREFIX = "_backup-"

HTTP_TIMEOUT = 15.0
DOWNLOAD_TIMEOUT = 300.0


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.9.8' / '1.9.8' → (1, 9, 8)。解析不出返回空元组（当没有）。"""
    cleaned = str(text or "").strip().lstrip("vV")
    if not cleaned:
        return ()
    parts: list[int] = []
    for piece in cleaned.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits and piece:
            return ()
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else ()


def is_newer(remote: str, current: str) -> bool:
    """远端版本是否比当前新。任一解析不出都算"没有"。"""
    want, have = parse_version(remote), parse_version(current)
    return bool(want) and bool(have) and want > have


def current_version() -> str:
    """本程序的版本号。拿不到也不崩（更新跳过）。"""
    try:
        from .version import __version__
        return str(__version__ or "")
    except Exception:
        return ""


def app_dir() -> Path:
    """程序所在目录：exe 和 _internal 的上一级。

    打包后是 exe 所在目录；源码跑是仓库根（更新只在打包形态下生效，
    源码跑直接 git pull，不走这套）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def is_frozen_app() -> bool:
    """是不是打好包的 exe。源码开发模式不做自我更新。"""
    return bool(getattr(sys, "frozen", False))


def _http_get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "wechat-ai-assistant-updater/1",
    })
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))


def _http_get_bytes(url: str, timeout: float = HTTP_TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "wechat-ai-assistant-updater/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(1024 * 1024 * 1024 + 1)


def check_for_update(repo: str = DEFAULT_REPO, current: str = "") -> dict[str, Any] | None:
    """查最新 release。有新版返回 ``{version, notes, zip_url, sha_url}``，
    没有/查不到/任何异常都返回 None（调用方直接跳过）。"""
    current = current or current_version()
    if not parse_version(current):
        return None
    try:
        release = _http_get_json(API_LATEST.format(repo=repo))
    except Exception as exc:
        LOGGER.debug("检查更新失败：%s", exc)
        return None
    if not isinstance(release, dict):
        return None
    version = str(release.get("tag_name") or release.get("name") or "")
    if not is_newer(version, current):
        return None
    assets = release.get("assets") or []
    zip_url, sha_url = "", ""
    want_zip = f"{ASSET_PREFIX}{version.lstrip('vV')}.zip"
    for asset in assets:
        if not isinstance(asset, Mapping):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name == want_zip:
            zip_url = url
        elif name == want_zip + ".sha256":
            sha_url = url
    # 文件名对不上就退而求其次：第一个 win64 zip + 同名 sha256。
    if not zip_url:
        for asset in assets:
            if not isinstance(asset, Mapping):
                continue
            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            if name.endswith(".zip") and "win64" in name.lower():
                zip_url = url
                sha_url = url + ".sha256"
                break
    if not zip_url:
        LOGGER.debug("release %s 里没有安装包", version)
        return None
    return {"version": version.lstrip("vV"), "notes": str(release.get("body") or ""),
            "zip_url": zip_url, "sha_url": sha_url}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_and_stage(info: Mapping[str, Any], base: Path | None = None,
                       on_progress: Callable[[int, int], None] | None = None) -> Path | None:
    """下载 → 验签 → 解压到版本目录。成功返回版本目录，失败返回 None。

    验签不过/解压失败一律删干净，不留半截文件让下次启动误用。
    """
    base = base or app_dir()
    version = str(info.get("version") or "")
    zip_url = str(info.get("zip_url") or "")
    sha_url = str(info.get("sha_url") or "")
    if not version or not zip_url or not parse_version(version):
        return None
    staged = base / UPDATES_DIR_NAME / version
    tmp_dir = Path(tempfile.mkdtemp(prefix="pyw-dl-"))
    try:
        zip_path = tmp_dir / "app.zip"
        try:
            data = _http_get_bytes(zip_url, DOWNLOAD_TIMEOUT)
        except Exception as exc:
            LOGGER.debug("下载更新失败：%s", exc)
            return None
        zip_path.write_bytes(data)
        if on_progress is not None:
            try:
                on_progress(len(data), len(data))
            except Exception:
                pass
        # 验签：有 sha 文件就必须对上；没有 sha 文件就拒绝安装。
        # 无签名的包不装 —— 省这一步等于把商户电脑交给任意下载。
        expected = ""
        if sha_url:
            try:
                expected = _http_get_bytes(sha_url).decode("utf-8", errors="replace").split()[0].strip().lower()
            except Exception as exc:
                LOGGER.debug("下载校验文件失败：%s", exc)
        if not expected or _sha256_file(zip_path) != expected:
            LOGGER.warning("更新包校验失败，已丢弃")
            return None
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(staged)
        except Exception as exc:
            LOGGER.debug("更新包解压失败：%s", exc)
            shutil.rmtree(staged, ignore_errors=True)
            return None
        _flatten_single_top_dir(staged)
        if not _looks_like_app(staged):
            LOGGER.warning("更新包内容不对（找不到主程序），已丢弃")
            shutil.rmtree(staged, ignore_errors=True)
            return None
        pending = {"version": version, "dir": staged.name,
                   "notes": str(info.get("notes") or "")}
        (base / PENDING_NAME).write_text(
            json.dumps(pending, ensure_ascii=False), encoding="utf-8")
        return staged
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _flatten_single_top_dir(staged: Path) -> None:
    """压缩包里多套了一层目录就扒掉（只扒一层，里面还套就不管了）。"""
    try:
        children = [p for p in staged.iterdir()]
    except OSError:
        return
    if len(children) == 1 and children[0].is_dir():
        inner = children[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(staged / item.name))
        try:
            inner.rmdir()
        except OSError:
            pass


def _looks_like_app(directory: Path) -> bool:
    """解压出来像不像我们的程序：有 exe + _internal。"""
    try:
        names = {p.name for p in directory.iterdir()}
    except OSError:
        return False
    has_exe = any(name.lower().endswith(".exe") for name in names)
    return has_exe and "_internal" in names


def pending_update(base: Path | None = None) -> dict[str, Any] | None:
    """读 pending.json。没有/坏了/目录不在了都返回 None。"""
    base = base or app_dir()
    path = base / PENDING_NAME
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(info, dict) or not info.get("version"):
        return None
    staged = base / UPDATES_DIR_NAME / str(info.get("dir") or "")
    if not staged.is_dir() or not _looks_like_app(staged):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    info["staged_dir"] = str(staged)
    return info


def build_updater_script(base: Path, info: Mapping[str, Any], exe_name: str,
                         self_pid: int) -> Path:
    """生成更新脚本并返回路径。脚本自己住在系统临时目录（搬目录时不受影响）。

    流程：等本进程退出 → 把新版目录挪到外面 → 旧目录整个改名备份 →
    新版目录搬进来 → 起新版（15 秒还活着就算成，删备份）→
    起不来就删了换回旧版再起。最后自删。

    用 .bat 而不用再打一个 exe：少一个构建产物，逻辑全摆在明面上。
    """
    staged = base / UPDATES_DIR_NAME / str(info.get("dir"))
    backup = base.parent / (base.name + BACKUP_PREFIX + str(info.get("version")))
    sibling = base.parent / (base.name + f".staged-{info.get('version')}")
    portable_data = base / "data"
    script = Path(tempfile.gettempdir()) / f"pyw-apply-{info.get('version')}.bat"
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        f"rem 等待旧进程 {self_pid} 退出",
        ":wait",
        f'tasklist /FI "PID eq {self_pid}" 2>nul | find "{self_pid}" >nul',
        "if %errorlevel%==0 (timeout /t 1 /nobreak >nul & goto wait)",
        f'if exist "{sibling}" rmdir /s /q "{sibling}"',
        f'move "{staged}" "{sibling}"',
        "if errorlevel 1 goto start_old",
        # Never discard an existing backup: an interrupted prior update may
        # still have the only preserved copy of a portable user's data.
        f'if exist "{backup}" goto restore_stage',
        f'move "{base}" "{backup}"',
        "if errorlevel 1 goto restore_stage",
        f'move "{sibling}" "{base}"',
        "if errorlevel 1 goto rollback",
        # Move portable data from the old-version backup into the new app.
        # On failure, rollback returns it to the backup before deleting the new app.
        f'if exist "{backup}\\data" move "{backup}\\data" "{portable_data}"',
        f'if exist "{backup}\\data" goto rollback',
        f'start "" "{base}\\{exe_name}"',
        "timeout /t 15 /nobreak >nul",
        f'tasklist /FI "IMAGENAME eq {exe_name}" 2>nul | find "{exe_name}" >nul',
        "if %errorlevel%==0 goto success",
        ":: 新版没起来，回滚旧版",
        f'if exist "{portable_data}" if not exist "{backup}\\data" move "{portable_data}" "{backup}\\data"',
        f'rmdir /s /q "{base}"',
        f'move "{backup}" "{base}"',
        f'start "" "{base}\\{exe_name}"',
        "goto finish",
        ":rollback",
        f'if exist "{portable_data}" if not exist "{backup}\\data" move "{portable_data}" "{backup}\\data"',
        f'if exist "{base}" rmdir /s /q "{base}"',
        f'if exist "{backup}" move "{backup}" "{base}"',
        f'if exist "{base}\\{exe_name}" start "" "{base}\\{exe_name}"',
        "goto finish",
        ":restore_stage",
        f'if exist "{sibling}" move "{sibling}" "{staged}"',
        ":start_old",
        f'if exist "{base}\\{exe_name}" start "" "{base}\\{exe_name}"',
        "goto finish",
        ":success",
        f'if exist "{backup}" rmdir /s /q "{backup}"',
        ":finish",
        'del "%~f0"',
    ]
    script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return script


def apply_pending_update(base: Path | None = None) -> bool:
    """启动时调用：有 pending 就起更新脚本并返回 True（调用方随后退出）。

    只在打包形态下干活；返回 False 表示没什么可做的，正常启动。
    """
    if not is_frozen_app():
        return False
    base = base or app_dir()
    info = pending_update(base)
    if info is None:
        return False
    try:
        exe = next(p for p in base.iterdir()
                   if p.suffix.lower() == ".exe" and p.is_file())
    except (OSError, StopIteration):
        return False
    try:
        script = build_updater_script(base, info, exe.name, os.getpid())
        subprocess.Popen(
            ["cmd", "/c", str(script)], cwd=str(base),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True)
    except Exception as exc:
        LOGGER.debug("启动更新脚本失败：%s", exc)
        return False
    return True


def check_in_background(on_staged: Callable[[Mapping[str, Any]], None] | None = None,
                        repo: str = DEFAULT_REPO) -> threading.Thread:
    """起后台线程查更新、下包、暂存。任何异常都吞掉，只记日志。

    只在打包形态下跑；开发模式调了也是空转（apply 那边同样只认打包形态）。
    """

    def work() -> None:
        try:
            if not is_frozen_app():
                return
            info = check_for_update(repo)
            if info is None:
                return
            staged = download_and_stage(info)
            if staged is None:
                return
            LOGGER.info("新版本 %s 已暂存，下次启动生效", info.get("version"))
            if on_staged is not None:
                try:
                    on_staged(info)
                except Exception:
                    pass
        except Exception as exc:
            LOGGER.debug("后台更新失败：%s", exc)

    thread = threading.Thread(target=work, name="app-update", daemon=True)
    thread.start()
    return thread


__all__ = [
    "API_LATEST",
    "ASSET_PREFIX",
    "DEFAULT_REPO",
    "UPDATES_DIR_NAME",
    "apply_pending_update",
    "app_dir",
    "build_updater_script",
    "check_for_update",
    "check_in_background",
    "current_version",
    "download_and_stage",
    "is_frozen_app",
    "is_newer",
    "parse_version",
    "pending_update",
]
