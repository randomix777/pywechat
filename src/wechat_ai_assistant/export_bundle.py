"""导出完整包：把核对明细连同本地缓存的图片/视频/音频/附件打包成一个 zip。

商户要的不是一张只有占位符的表格 —— 是「这一批聊天内容，连同图、视频、
文件，一起留档」。所以这里把明细表和实体媒体文件放进同一个压缩包：

    明细.csv          消息表格（Excel/WPS 直接打开，含附件相对路径）
    图片/             图片（从本地缓存解密；WXGF 动图可能保留原容器）
    视频/             视频（本机多为缩略图）
    音频/             语音/音频（微信原始 .silk/.amr）
    附件/             Word/PDF/文本等文件
    AI分析结果.md    最后一步展示的 AI 分析报告（仅在已完成分析时写入）
    导出说明.txt      来源会话、条数、时间、未导出附件清单

只打包微信本机**已缓存且能读**的媒体；没缓存到、密钥不可用或格式不支持的，
在表格里保留文字占位，并在说明文件里分开列出 —— 不会凭空造内容。
"""
from __future__ import annotations

import csv
import io
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable

#: 消息类型 → 包内目录。图/视频/音频/文件各归一处。
KIND_DIRS = {
    "image": "图片",
    "video": "视频",
    "voice": "音频",
    "audio": "音频",
    "file": "附件",
}

_SILK_MAGIC = (b"\x02#!SILK", b"#!SILK", b"\x02#!")
_AMR_MAGIC = b"#!AMR"


def _safe_part(text: Any, limit: int = 40) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "", str(text or "")).strip()
    return cleaned[:limit]


def _read_media(path: str, kind: str) -> tuple[bytes, str]:
    """读一个媒体文件，返回 (字节, 扩展名)。图片走解码，其余尽力识别真实类型。"""
    file_path = Path(path)
    if kind == "image":
        from .media_attachments import image_bytes

        data, ext = image_bytes(str(file_path))
        return data, ext
    data = file_path.read_bytes()
    ext = file_path.suffix.lower().lstrip(".") or "bin"
    if ext == "dat":
        ext = _sniff_ext(data, kind, file_path)
    return data, ext


def _sniff_ext(data: bytes, kind: str, file_path: Path) -> str:
    """微信缓存里的 .dat 不带真实后缀，尽最大努力认出容器格式。"""
    if kind == "video":
        if len(data) > 12 and data[4:8] == b"ftyp":
            return "mp4"
        return "mp4" if b"ftyp" in data[:64] else "dat"
    if any(data.startswith(magic) for magic in _SILK_MAGIC):
        return "silk"
    if data.startswith(_AMR_MAGIC):
        return "amr"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    # 认不出来就沿用它原本的名字，至少文件能打开。
    return file_path.suffix.lower().lstrip(".")


def _arc_name(row: dict, seq: dict, kind: str, ext: str) -> str:
    """包内文件名：序号_时间_发送人.扩展名。同类型内序号递增，不会重名。"""
    seq[kind] = seq.get(kind, 0) + 1
    stamp = _safe_part(str(row.get("sent_at", "") or ""), 16).replace(" ", "_").replace(":", "")
    sender = _safe_part(str(row.get("sender", "") or ""), 20) or "未知"
    base = f"{seq[kind]:04d}_{stamp}_{sender}".strip("_")
    return f"{base}.{ext}"


def _csv_bytes(pairs: Iterable[tuple[dict, list]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["会话", "时间", "发送人", "类型", "内容", "附件"])
    for row, rels in pairs:
        writer.writerow([
            row.get("group", ""), row.get("sent_at", ""), row.get("sender", ""),
            row.get("kind", ""), row.get("content", ""), "；".join(rels),
        ])
    return buffer.getvalue().encode("utf-8-sig")


def _readme(rows: list, groups: list, missing: list, generated_at: str,
            encrypted: list | None = None, *, has_analysis: bool = False,
            analysis_role: str = "") -> str:
    encrypted = encrypted or []
    lines = [
        "微信AI智能助理 · 导出完整包",
        f"生成时间：{generated_at}",
        "来源会话：" + ("、".join(groups) if groups else "未知"),
        f"共 {len(rows)} 条消息。",
        "",
        "目录说明：",
        "  明细.csv    消息表格（Excel/WPS 直接打开；最后一列是包内附件路径）",
        "  图片/       从微信本地缓存解密；WXGF 动图可能保留原容器格式",
        "  视频/       视频（本机多为缩略图）",
        "  音频/       语音/音频（微信原始格式 .silk/.amr，可用微信或转换工具播放）",
        "  附件/       Word / PDF / 文本等文件",
        *(["  AI分析结果.md    本次 AI 整理报告（Markdown 格式）"] if has_analysis else []),
        "",
        "说明：",
        "  只有微信本机已缓存的媒体文件会出现在包里；未缓存到的消息在"
        "「明细.csv」里保留文字占位。",
    ]
    if encrypted:
        lines.append(
            f"  另有 {len(encrypted)} 条图片消息因缺少解密密钥或缓存格式不支持，"
            "明细里保留［图片］占位：")
        for row in encrypted[:50]:
            lines.append(
                f"    - {row.get('group', '')} {row.get('sent_at', '')} "
                f"{row.get('sender', '')}（{row.get('content', '')}）")
        if len(encrypted) > 50:
            lines.append(f"    …其余 {len(encrypted) - 50} 条见明细.csv")
    if missing:
        lines.append(f"  本次有 {len(missing)} 条消息的附件没在微信本机缓存里找到：")
        for row in missing[:100]:
            lines.append(
                f"    - {row.get('group', '')} {row.get('sent_at', '')} "
                f"{row.get('sender', '')}（{row.get('content', '')}）")
        if len(missing) > 100:
            lines.append(f"    …其余 {len(missing) - 100} 条见明细.csv")
    if not missing and not encrypted:
        lines.append("  本次所选消息的附件都已随包导出。")
    if has_analysis and analysis_role:
        lines.extend(["", f"AI 整理角色：{analysis_role}"])
    return "\n".join(lines) + "\n"


def build_export_bundle(rows: list, media_by_id: dict | None = None,
                        *, generated_at: str | None = None,
                        analysis_report: str = "",
                        analysis_role: str = "") -> tuple[bytes, dict]:
    """把明细行 + 本地缓存媒体拼成 zip，返回 (字节, 摘要)。

    ``media_by_id`` 是 ``{message_id: media_dict}``（media_json 解析后的形状），
    用来按 MD5 关联微信缓存文件；缺了它只有表格、没有实体附件。
    """
    from .media_attachments import attach_cached_files

    rows = list(rows or [])
    media_by_id = media_by_id or {}
    messages = [
        {"kind": str(row.get("kind", "") or ""),
         "media": media_by_id.get(str(row.get("id", "") or ""), {}) or {}}
        for row in rows
    ]
    attach_cached_files(messages)

    groups: list[str] = []
    for row in rows:
        name = str(row.get("group", "") or "")
        if name and name not in groups:
            groups.append(name)

    seq: dict[str, int] = {}
    pairs: list[tuple[dict, list]] = []
    missing: list[dict] = []
    encrypted: list[dict] = []
    report = str(analysis_report or "").strip()
    role = str(analysis_role or "").strip()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for row, message in zip(rows, messages):
            kind = str(message.get("kind", "") or "").lower()
            folder = KIND_DIRS.get(kind, "")
            rels: list[str] = []
            for path in message.get("attachments") or []:
                try:
                    data, ext = _read_media(str(path), kind)
                except Exception as exc:
                    if kind == "image":
                        message["media_status"] = f"图片缓存解密失败：{exc}"
                    continue
                name = _arc_name(row, seq, kind or "其他", ext)
                arcname = f"{folder}/{name}" if folder else name
                archive.writestr(arcname, data)
                rels.append(arcname)
            if not rels and kind in KIND_DIRS:
                status = str(message.get("media_status", "") or "")
                (encrypted if "解密" in status or "密钥" in status else missing).append(row)
            pairs.append((row, rels))
        archive.writestr("明细.csv", _csv_bytes(pairs))
        stamp = generated_at or time.strftime("%Y-%m-%d %H:%M")
        if report:
            report_lines = ["# AI 分析结果"]
            if role:
                report_lines.extend(["", f"整理角色：{role}"])
            report_lines.extend(["", f"导出时间：{stamp}", "", report, ""])
            archive.writestr("AI分析结果.md", "\n".join(report_lines).encode("utf-8"))
        archive.writestr(
            "导出说明.txt",
            _readme(rows, groups, missing, stamp, encrypted,
                    has_analysis=bool(report), analysis_role=role).encode("utf-8"))

    summary = {"rows": len(rows), "missing": len(missing),
               "encrypted": len(encrypted), "groups": groups}
    return buffer.getvalue(), summary


__all__ = ["KIND_DIRS", "build_export_bundle"]
