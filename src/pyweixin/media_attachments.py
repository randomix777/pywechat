"""Resolve cached WeChat media files for analysis and result attachments."""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

_IMAGE_MAGIC = {
    "jpeg": b"\xff\xd8\xff", "png": b"\x89PNG", "gif": b"GIF8",
    "bmp": b"BM", "webp": b"RIFF",
}

#: 微信 4.0 图片缓存头（AES-128-ECB + 尾部 XOR）。当前解不开，
#: 见到就如实标注，别当成坏图或"本地没有"。
_V4_IMAGE_MAGIC = (b"\x07\x08V2\x08\x07", b"\x07\x08V1\x08\x07")

#: 需要去缓存里找附件的消息类型。
_MEDIA_KINDS = {"image", "video", "voice", "audio", "file"}


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
    for subdir in (root / "msg" / "attach", root / "msg" / "video",
                   root / "msg" / "file"):
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
    return by_id, by_name


def _file_index(root: Path) -> dict[str, list[Path]]:
    """只按 MD5 索引（视频缩略图等少数调用点用）。"""
    return _scan_caches(root)[0]


def _is_encrypted_cache(path: Path) -> bool:
    """微信 4.0 的 AES 加密图片缓存（.dat）—— 目前解不开。"""
    try:
        return Path(path).read_bytes()[:6] in _V4_IMAGE_MAGIC
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


def attach_cached_files(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add locally cached files for each media message, preserving order.

    找得到就挂上；图片是 4.0 加密格式就如实标注（不挂一个解不开的 .dat）。
    """
    root = _account_root()
    if root is None:
        for message in messages:
            if str(message.get("kind", "")).lower() in _MEDIA_KINDS:
                message["attachments"] = []
                message["media_status"] = "无法访问微信本地媒体目录"
        return messages
    by_id, by_name = _scan_caches(root)
    v4_images: bool | None = None
    for message in messages:
        kind = str(message.get("kind", "")).lower()
        if kind not in _MEDIA_KINDS:
            continue
        media = message.get("media") or {}
        md5 = str(media.get("md5") or media.get("videomd5") or media.get("filemd5") or "").lower()
        filename = str(media.get("filename") or media.get("name") or "").strip()
        if not md5 and not filename:
            message["attachments"] = []
            message["media_status"] = "读取到媒体消息，但本次读取未保存附件标识"
            continue
        candidates = list(by_id.get(md5) or []) if md5 else []
        picked: Path | None = None
        status = "本地缓存中未找到附件"
        if kind == "image":
            picked = _first(candidates, {".jpg", ".jpeg", ".png", ".webp", ".gif", ".dat"})
            if picked is not None and _is_encrypted_cache(picked):
                picked, status = None, "微信4.0加密图片，暂未解密"
            elif picked is None:
                if v4_images is None:
                    v4_images = _cache_has_v4_images(root)
                if v4_images:
                    status = "微信4.0加密图片，暂未解密"
        elif kind == "video":
            picked = _first(candidates, {".mp4", ".mov", ".3gp"}) or _first(candidates, {".jpg", ".jpeg", ".png"})
        elif kind == "file":
            picked = _first(candidates, {".docx", ".doc", ".pdf", ".txt", ".md", ".rtf",
                                         ".xlsx", ".xls", ".pptx", ".ppt", ".zip"})
            if picked is None and filename:
                picked = _first(list(by_name.get(filename.lower()) or []), {".docx", ".doc", ".pdf",
                                 ".txt", ".md", ".rtf", ".xlsx", ".xls", ".pptx", ".ppt", ".zip"})
        else:
            picked = _first(candidates, {".aud", ".silk", ".amr", ".dat", ".wav", ".mp3"})
        if picked is not None:
            message["attachments"] = [str(picked)]
            message["media_status"] = "附件已关联"
        else:
            message["attachments"] = []
            message["media_status"] = status
    return messages


def image_bytes(path: str) -> tuple[bytes, str]:
    """Read a cached WeChat image and decode the legacy ``.dat`` format.

    Returns ``(data, ext)`` where ``ext`` is a real image extension
    (``jpg``/``png``/``gif``/``webp``) — the exporter needs the bytes and a
    truthful name, ``image_data_url`` needs a MIME type.
    """
    file_path = Path(path)
    data = file_path.read_bytes()
    if file_path.suffix.lower() == ".dat" and data:
        for mime, magic in _IMAGE_MAGIC.items():
            key = data[0] ^ magic[0]
            decoded = bytes(byte ^ key for byte in data)
            if decoded.startswith(magic):
                data = decoded
                break
    if data.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    elif data.startswith(b"\x89PNG"):
        ext = "png"
    elif data.startswith(b"GIF8"):
        ext = "gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        ext = "webp"
    else:
        raise ValueError("图片缓存无法识别")
    return data, ext


def image_data_url(path: str) -> str:
    """Read a cached WeChat image; decode the legacy single-byte XOR .dat format."""
    try:
        data, ext = image_bytes(path)
    except ValueError:
        raise
    mime = {"jpg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}[ext]
    if len(data) > 2 * 1024 * 1024:
        try:
            from io import BytesIO
            from PIL import Image
            with Image.open(BytesIO(data)) as source:
                image = source.convert("RGB")
                image.thumbnail((1600, 1600))
                output = BytesIO()
                image.save(output, format="JPEG", quality=82, optimize=True)
                data, mime = output.getvalue(), "image/jpeg"
        except Exception:
            pass
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
