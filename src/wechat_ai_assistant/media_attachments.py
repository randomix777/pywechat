"""Resolve cached WeChat media files for analysis and result attachments."""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

#: 微信 4.x 加密图片缓存头。读取时由 wechat_images 解密。
_V4_IMAGE_MAGIC = (
    b"\x07\x08V2\x08\x07", b"\x07\x08V1\x08\x07",
    b"\x07\x08\x05V\x02\x05",
)

#: 需要去缓存里找附件的消息类型。
_MEDIA_KINDS = {"image", "video", "voice", "audio", "file"}
_KIND_ALIASES = {
    "图片": "image", "图像": "image", "image": "image",
    "视频": "video", "video": "video",
    "语音": "voice", "音频": "audio", "voice": "voice", "audio": "audio",
    "文件": "file", "file": "file",
}
_DOCUMENT_SUFFIXES = {
    ".doc", ".docx", ".pdf", ".txt", ".md", ".rtf", ".xls", ".xlsx",
    ".ppt", ".pptx", ".zip", ".rar", ".7z", ".tar", ".gz", ".csv",
    ".odt", ".ods", ".odp", ".wps", ".et", ".dps", ".json", ".xml",
}
_HARDLINK_INDEX_CACHE: dict[
    str, tuple[tuple[int, int, int, int], dict[str, list[Path]]]
] = {}


def _canonical_media_kind(message: dict[str, Any]) -> str:
    """Normalize stored/reader message labels and recognize legacy file rows."""
    raw_kind = str(message.get("kind", "") or "").strip().lower()
    kind = _KIND_ALIASES.get(raw_kind, "")
    if not kind:
        media = message.get("media") or {}
        filename = str(media.get("filename") or media.get("name") or "")
        if Path(filename).suffix.lower() in _DOCUMENT_SUFFIXES:
            kind = "file"
    if kind:
        message["kind"] = kind
    return kind


def _account_root() -> Path | None:
    try:
        from .WeChatTools import Tools
        path = Path(Tools.where_wxid_folder())
        return path if path.is_dir() else None
    except Exception:
        return None


#: 去掉这些缓存后缀后，文件名才是媒体的 MD5。
_CACHE_SUFFIXES = ("_t", "_h", "_s", "_thumb")


def _scan_caches(root: Path) -> tuple[dict[str, list[Path]], dict[str, list[Path]]]:
    """扫一遍媒体目录，返回 (按 MD5 索引, 按文件名索引)。

    微信 4.x 把缓存摊在 ``msg/attach/<会话hash>/<年月>/Img/`` 等目录里，文件名
    并不总是 MD5（文档是真实文件名），所以两个索引都要有：图片/视频/语音按
    MD5 找，文档按 XML 里的 ``filename`` 找。
    """
    by_id: dict[str, list[Path]] = {}
    by_name: dict[str, list[Path]] = {}
    # WeChat 4.1 keeps some temporary/thumbnail images outside msg/attach.
    # Include these account-local cache roots too; they are small and stay local.
    for subdir in (root / "msg" / "attach", root / "msg" / "video",
                   root / "msg" / "file", root / "cache",
                   root / "resource", root / "temp"):
        if not subdir.is_dir():
            continue
        for current, _dirs, files in os.walk(subdir):
            for name in files:
                path = Path(current) / name
                stem = path.stem.lower()
                by_id.setdefault(stem, []).append(path)
                for suffix in _CACHE_SUFFIXES:
                    if stem.endswith(suffix):
                        by_id.setdefault(stem[: -len(suffix)], []).append(path)
                        break
                by_name.setdefault(name.lower(), []).append(path)
    _index_hardlink_images(root, by_id, by_name)
    return by_id, by_name


def _index_hardlink_images(root: Path, by_id: dict[str, list[Path]],
                           by_name: dict[str, list[Path]]) -> None:
    """Resolve WeChat 4.x image MD5s through its encrypted hardlink index.

    On current Weixin builds, image files are named by an internal cache key,
    not the message XML's MD5. ``hardlink.db`` maps that MD5 to two directory
    IDs and a filename; ``dir2id`` resolves the IDs to the account's attach
    folders. A filename-only scan silently misses these valid local images.
    """
    storage = root / "db_storage"
    index = storage / "hardlink" / "hardlink.db"
    if not index.is_file():
        return
    wal = index.with_name(index.name + "-wal")
    try:
        main_stat = index.stat()
        wal_stat = wal.stat() if wal.is_file() else None
        fingerprint = (
            main_stat.st_mtime_ns, main_stat.st_size,
            wal_stat.st_mtime_ns if wal_stat else 0,
            wal_stat.st_size if wal_stat else 0,
        )
    except OSError:
        return
    cache_key = str(root.resolve())
    cached = _HARDLINK_INDEX_CACHE.get(cache_key)
    if cached and cached[0] == fingerprint:
        for md5, paths in cached[1].items():
            for path in paths:
                if path.is_file():
                    by_id.setdefault(md5, []).append(path)
                    by_name.setdefault(path.name.lower(), []).append(path)
        return
    from .paths import default_data_dir

    keys_path = default_data_dir() / "wcdb_keys.json"
    if not keys_path.is_file():
        return

    reader = None
    ready: dict[str, Path] = {}
    connection = None
    indexed: dict[str, list[Path]] = {}
    try:
        import sqlite3
        from .wcdb import WcdbReader

        reader = WcdbReader(storage, keys_path)
        ready = reader.prepare(["hardlink/hardlink.db"])
        connection = sqlite3.connect(str(ready["hardlink/hardlink.db"]))
        connection.row_factory = sqlite3.Row
        directories = {
            int(row["rowid"]): str(row["username"] or "")
            for row in connection.execute("SELECT rowid, username FROM dir2id")
        }
        attach = root / "msg" / "attach"
        attach_resolved = attach.resolve()
        rows = connection.execute(
            "SELECT md5, file_name, dir1, dir2 FROM image_hardlink_info_v4"
        )
        for row in rows:
            md5 = str(row["md5"] or "").strip().lower()
            filename = str(row["file_name"] or "").strip()
            if not md5 or not filename or Path(filename).name != filename:
                continue
            try:
                dir1 = directories.get(int(row["dir1"]))
                dir2 = directories.get(int(row["dir2"]))
            except (TypeError, ValueError):
                continue
            if not dir1 or not dir2 or any(
                    Path(part).name != part or part in {".", ".."}
                    for part in (dir1, dir2)):
                continue
            candidate = attach / dir1 / dir2 / "Img" / filename
            try:
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(attach_resolved) or not resolved.is_file():
                    continue
            except (OSError, ValueError):
                continue
            indexed.setdefault(md5, []).append(resolved)
            by_id.setdefault(md5, []).append(resolved)
            by_name.setdefault(filename.lower(), []).append(resolved)
        _HARDLINK_INDEX_CACHE[cache_key] = (fingerprint, indexed)
    except Exception:
        # Hardlink support is an additional index; an unavailable key or a
        # version-specific schema must not hide files found by the directory scan.
        LOGGER.debug("Could not resolve WeChat 4.x image hardlink index", exc_info=True)
        return
    finally:
        if connection is not None:
            connection.close()
        if reader is not None and ready:
            try:
                reader.cleanup(ready)
            except Exception:
                pass


def _file_index(root: Path) -> dict[str, list[Path]]:
    """只按 MD5 索引（视频缩略图等少数调用点用）。"""
    return _scan_caches(root)[0]


def _is_encrypted_cache(path: Path) -> bool:
    """Return whether a .dat path uses a recognized encrypted image header."""
    try:
        with Path(path).open("rb") as handle:
            return handle.read(6) in _V4_IMAGE_MAGIC
    except OSError:
        return False


def _cache_has_v4_images(root: Path) -> bool:
    """这台机器的图片缓存是不是 4.0 加密格式（抽样看几个 .dat 头）。

    是的话，找不到明文的图片消息多半是"已缓存但没解密"，而不是"没缓存"，
    说明文件里要分开写，别让商户以为图片丢了。
    """
    attach = root / "msg" / "attach"
    if not attach.is_dir():
        return False
    checked = 0
    for current, _dirs, files in os.walk(attach):
        for name in files:
            if not name.lower().endswith(".dat"):
                continue
            if _is_encrypted_cache(Path(current) / name):
                return True
            checked += 1
            if checked >= 40:
                return False
    return False


def _first(candidates: list[Path], allowed: set[str]) -> Path | None:
    """在候选里挑后缀允许的，优先非 .dat 且路径最短的一个。"""
    usable = [p for p in candidates if p.suffix.lower() in allowed]
    if not usable:
        return None
    return sorted(usable, key=lambda p: (p.suffix.lower() == ".dat", len(str(p))))[0]


def _best_image_cache(candidates: list[Path]) -> Path | None:
    """Prefer a decoded copy, otherwise original > standard > thumbnail."""
    readable = _first(candidates, {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})
    if readable is not None:
        return readable
    def tier_rank(path: Path) -> int:
        name = path.name.lower()
        if name.endswith("_h.dat"):
            return 0
        if name.endswith("_t.dat"):
            return 2
        return 1

    encrypted = [path for path in candidates if path.suffix.lower() == ".dat"]
    return min(encrypted, key=lambda path: (
        tier_rank(path), len(str(path))), default=None)


def attach_cached_files(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add locally cached files for each media message, preserving order.

    找得到就挂上；加密图片在 image_bytes() 读取时按本地格式解密。
    """
    root = _account_root()
    if root is None:
        for message in messages:
            if _canonical_media_kind(message) in _MEDIA_KINDS:
                message["attachments"] = []
                message["media_status"] = "无法访问微信本地媒体目录"
        return messages
    by_id, by_name = _scan_caches(root)
    v4_images: bool | None = None
    for message in messages:
        kind = _canonical_media_kind(message)
        if kind not in _MEDIA_KINDS:
            continue
        media = message.get("media") or {}
        media_ids = list(dict.fromkeys(
            str(media.get(key) or "").strip().lower()
            for key in ("md5", "videomd5", "filemd5", "originsourcemd5",
                        "cdnthumbmd5", "cdnmidmd5", "fileid", "filekey")
            if str(media.get(key) or "").strip()
        ))
        filename = str(media.get("filename") or media.get("name") or "").strip()
        if not media_ids and not filename:
            message["attachments"] = []
            message["media_status"] = "读取到媒体消息，但本次读取未保存附件标识"
            continue
        candidates = list(dict.fromkeys(
            path for media_id in media_ids for path in (by_id.get(media_id) or [])
        ))
        picked: Path | None = None
        status = "本地缓存中未找到附件"
        if kind == "image":
            picked = _best_image_cache(candidates)
            if picked is not None and _is_encrypted_cache(picked):
                status = "已关联微信加密图片缓存，读取时尝试解密"
            elif picked is None:
                if v4_images is None:
                    v4_images = _cache_has_v4_images(root)
                if any(media.get(key) for key in (
                        "cdnthumburl", "cdnmidimgurl", "cdnhdimgurl", "cdnbigimgurl")):
                    status = "本地缓存未命中；消息保留了微信 CDN 图片引用"
                elif v4_images:
                    status = "微信4.0加密图片未找到对应本地缓存"
        elif kind == "video":
            picked = _first(candidates, {".mp4", ".mov", ".3gp"}) or _first(candidates, {".jpg", ".jpeg", ".png"})
        elif kind == "file":
            picked = _first(candidates, _DOCUMENT_SUFFIXES)
            if picked is None and filename:
                picked = _first(list(by_name.get(filename.lower()) or []), _DOCUMENT_SUFFIXES)
        else:
            picked = _first(candidates, {".aud", ".silk", ".amr", ".dat", ".wav", ".mp3"})
        if picked is not None:
            message["attachments"] = [str(picked)]
            message["media_status"] = (
                "附件已关联，读取时解密" if kind == "image" and _is_encrypted_cache(picked)
                else "附件已关联")
        else:
            message["attachments"] = []
            message["media_status"] = status
    return messages


def image_bytes(path: str) -> tuple[bytes, str]:
    """Read a cached WeChat image and decode its local ``.dat`` format.

    Returns ``(data, ext)`` where ``ext`` is the decoded format — the exporter
    needs the bytes and a truthful name, ``image_data_url`` needs a MIME type.
    """
    from .wechat_images import decode_image_file

    try:
        return decode_image_file(path)
    except ValueError as exc:
        if "微信" in str(exc) or "密钥" in str(exc):
            raise
        raise ValueError("图片缓存无法识别或解密") from exc


def image_data_url(path: str) -> str:
    """Decode a cached WeChat image and return a bounded static WebP data URL.

    Animated formats are represented by their first frame; vision requests only
    receive a still image. WebP reduces inline request size; JPEG is retained as
    a fallback for Pillow builds without a working WebP encoder.
    """
    try:
        data, ext = image_bytes(path)
    except ValueError:
        raise
    if ext not in {"jpg", "jpeg", "png", "gif", "webp", "bmp"}:
        raise ValueError("WXGF/HEVC 动图未能转换为可显示图片；请安装可用的 ffmpeg")
    try:
        from io import BytesIO
        from PIL import Image, ImageOps, features

        with Image.open(BytesIO(data)) as source:
            source.seek(0)
            frame = ImageOps.exif_transpose(source.copy())
        if "A" in frame.getbands() or "transparency" in frame.info:
            rgba = frame.convert("RGBA")
            image = Image.new("RGB", rgba.size, (255, 255, 255))
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = frame.convert("RGB")
        image.thumbnail((1600, 1600))
        output = BytesIO()
        image_format = "WEBP" if features.check("webp") else "JPEG"

        def encode(quality: int) -> None:
            output.seek(0)
            output.truncate()
            if image_format == "WEBP":
                image.save(output, format="WEBP", quality=quality, method=4)
            else:
                image.save(output, format="JPEG", quality=quality, optimize=True)

        for quality in (82, 72, 62):
            try:
                encode(quality)
            except Exception:
                if image_format != "WEBP":
                    raise
                image_format = "JPEG"
                encode(quality)
            if output.tell() <= 1024 * 1024:
                break
        if output.tell() > 1024 * 1024:
            image.thumbnail((1280, 1280))
            encode(62)
        data = output.getvalue()
        if len(data) > 1024 * 1024:
            raise ValueError("转换后的图片超过视觉分析大小限制")
    except Exception as exc:
        if isinstance(exc, ValueError) and "视觉分析大小限制" in str(exc):
            raise
        raise ValueError("图片缓存无法转换为 AI 可读的静态 WebP/JPEG") from exc
    mime = "image/webp" if image_format == "WEBP" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def document_text(path: str, *, max_chars: int = 20000) -> str:
    """Extract searchable text from a bounded DOCX/PDF/text attachment."""
    file_path = Path(path)
    if file_path.stat().st_size > 25 * 1024 * 1024:
        raise ValueError("附件超过 25 MB，未提取")
    suffix = file_path.suffix.lower()
    if suffix == ".docx":
        import xml.etree.ElementTree as ET
        from zipfile import BadZipFile, ZipFile
        try:
            with ZipFile(file_path) as archive:
                xml = archive.read("word/document.xml")
        except (BadZipFile, KeyError) as exc:
            raise ValueError("DOCX 文件无法读取") from exc
        root = ET.fromstring(xml)
        text = "\n".join(node.text or "" for node in root.iter()
                          if node.tag.rsplit("}", 1)[-1] == "t")
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF 文本提取组件未安装") from exc
        reader = PdfReader(str(file_path), strict=False)
        text = "\n".join(page.extract_text() or "" for page in reader.pages[:200])
    elif suffix in {".txt", ".md", ".rtf"}:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    else:
        return ""
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[附件正文过长，已截取前 {max_chars:,} 个字符]"
    return text


__all__ = ["attach_cached_files", "document_text", "image_bytes", "image_data_url"]
