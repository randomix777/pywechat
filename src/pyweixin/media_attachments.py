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


def _account_root() -> Path | None:
    try:
        from .WeChatTools import Tools
        path = Path(Tools.where_wxid_folder())
        return path if path.is_dir() else None
    except Exception:
        return None


def _file_index(root: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for subdir in (root / "msg" / "attach", root / "msg" / "video",
                   root / "msg" / "file"):
        if not subdir.is_dir():
            continue
        for current, _dirs, files in os.walk(subdir):
            for name in files:
                path = Path(current) / name
                stem = path.stem.lower()
                index.setdefault(stem, []).append(path)
                for suffix in ("_t", "_h", "_s"):
                    if stem.endswith(suffix):
                        index.setdefault(stem[:-2], []).append(path)
    return index


def attach_cached_files(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add local cached files matching the media MD5 while preserving message order."""
    root = _account_root()
    if root is None:
        for message in messages:
            if str(message.get("kind", "")).lower() in {"image", "video", "voice", "file"}:
                message["attachments"] = []
                message["media_status"] = "无法访问微信本地媒体目录"
        return messages
    index = _file_index(root)
    for message in messages:
        if str(message.get("kind", "")).lower() not in {"image", "video", "voice", "file"}:
            continue
        media = message.get("media") or {}
        md5 = str(media.get("md5") or media.get("videomd5") or media.get("filemd5") or "").lower()
        if not md5:
            message["attachments"] = []
            message["media_status"] = "读取到媒体消息，但本次读取未保存附件标识"
            continue
        candidates = index.get(md5) or []
        kind = str(message.get("kind", "")).lower()
        preferred = [p for p in candidates if not p.stem.lower().endswith("_t")]
        if kind == "image":
            preferred = [p for p in preferred if p.suffix.lower() in {".dat", ".jpg", ".jpeg", ".png", ".webp", ".gif"}]
        elif kind == "video":
            preferred = [p for p in preferred if p.suffix.lower() in {".mp4", ".dat", ".mov", ".3gp"}]
        elif kind == "file":
            preferred = [p for p in candidates if p.suffix.lower() in
                         {".docx", ".pdf", ".txt", ".md", ".rtf"}]
        else:
            preferred = [p for p in preferred if p.suffix.lower() in {".aud", ".silk", ".amr", ".dat"}]
        if preferred:
            message["attachments"] = [str(sorted(preferred, key=lambda p: (p.suffix.lower() == ".dat", len(str(p))))[0])]
            message["media_status"] = "附件已关联"
        else:
            message["attachments"] = []
            message["media_status"] = "本地缓存中未找到附件"
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
