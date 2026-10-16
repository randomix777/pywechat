"""Product paths, including first-run compatibility with the old data folder."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

APP_DIRECTORY = "wechat-ai-assistant"
LEGACY_APP_DIRECTORY = "pyweixin"
PORTABLE_MARKER = "portable.flag"
_PORTABLE_MIGRATION_LOCK = threading.Lock()


def _windows_data_paths() -> tuple[Path, Path]:
    base_value = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    base = Path(base_value) if base_value else Path.home()
    if not base_value:
        return base / ".wechat-ai-assistant", base / ".pyweixin"
    return base / APP_DIRECTORY, base / LEGACY_APP_DIRECTORY


def _portable_data_dir() -> Path | None:
    """Return the data folder beside a packaged app marked as portable."""
    if not getattr(sys, "frozen", False):
        return None
    app_dir = Path(sys.executable).resolve().parent
    if not (app_dir / PORTABLE_MARKER).is_file():
        return None
    return app_dir / "data"


def _has_entries(path: Path) -> bool:
    try:
        return path.is_dir() and next(path.iterdir(), None) is not None
    except OSError:
        return False


def _migrate_existing_data(portable_dir: Path, candidates: tuple[Path, Path]) -> None:
    """Copy existing per-user data once; leave the source folder untouched."""
    with _PORTABLE_MIGRATION_LOCK:
        staging: Path | None = None
        source: Path | None = None
        try:
            portable_dir.mkdir(parents=True, exist_ok=True)
            if _has_entries(portable_dir):
                return
            source = next((path for path in candidates if _has_entries(path)), None)
            if source is None or source.resolve() == portable_dir.resolve():
                return

            staging = Path(tempfile.mkdtemp(prefix=".wechat-ai-data-", dir=portable_dir.parent))
            shutil.copytree(source, staging, dirs_exist_ok=True)
            if portable_dir.exists():
                if _has_entries(portable_dir):
                    return
                portable_dir.rmdir()
            staging.replace(portable_dir)
        except OSError as exc:
            raise RuntimeError(
                f"无法准备便携版数据目录：{source or '无旧数据'} → {portable_dir}。"
                "请确认程序文件夹可写后重试。"
            ) from exc
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def default_data_dir() -> Path:
    """Return portable data beside a marked app or the per-user data directory.

    ``WECHAT_AI_CLIENT_DATA_DIR`` takes precedence. The old
    ``PYWEIXIN_CLIENT_DATA_DIR`` remains supported for existing launch scripts.
    A portable build copies existing per-user data once and leaves the source intact.
    """
    override = (os.environ.get("WECHAT_AI_CLIENT_DATA_DIR")
                or os.environ.get("PYWEIXIN_CLIENT_DATA_DIR"))
    if override:
        return Path(override).expanduser()

    portable_dir = _portable_data_dir()
    if portable_dir is not None:
        _migrate_existing_data(portable_dir, _windows_data_paths())
        return portable_dir

    new_path, old_path = _windows_data_paths()
    if new_path.exists() or not old_path.exists():
        return new_path
    return old_path
