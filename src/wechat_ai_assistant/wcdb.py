"""微信 4.x 本地库解密读取：会话、群名、人数，全都要真名。

## 为什么有这个模块

4.1 起 UIA 整条路死了，内存投票只能认出常用群（本机 200 群认出 21 个）。
微信自己的 `db_storage` 里有全量答案：`session/SessionTable`（最近会话，
带时间、摘要）、`contact/contact`（备注/昵称明文）、`contact/chat_room` +
`chatroom_member`（群与人数）。库是 SQLCipher4 加密的，密钥用
[`wcdb-key-tool`](https://github.com/TANGandXUE/wcdb-key-tool)（MIT，
见 `third_party/wcdb-key-tool/`）一次提取，之后一直复用。

## 用法

    from wechat_ai_assistant.wcdb import WcdbReader
    reader = WcdbReader(db_storage_dir, keys_path)
    if reader.available:
        sessions = reader.list_sessions()   # 最近会话，有名有时间
        groups = reader.list_groups()       # 全部群，有名有人数

解密只读活文件的拷贝，解密结果只落工作目录、用完即删。密钥不对
（微信大版本升级换盐）时验签失败，调用方回落内存路，别当"没有会话"。

## 不碰的东西

取密钥本身由上游工具做（`ensure_keys` 只负责调用它，不复刻它的扫描）。
本模块只做：验签 → 解密拷贝 → 查两张表。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import platform
import shutil
import sqlite3
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

LOGGER = logging.getLogger(__name__)

PAGE_SIZE = 4096
KEY_SIZE = 32
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
RESERVE_SIZE = 80  # IV(16) + HMAC-SHA512(64)
SQLITE_MAGIC = b"SQLite format 3\x00"

#: 第 2 步只需要这两张表所在的库，别的不碰。
#: 读历史再加消息库（WAL 一起合，否则今天的消息看不到）。
NEEDED_DATABASES = ("contact/contact.db", "session/session.db")
#: 消息库前缀。手机导入会建新分片（message_1.db…），读时全扫，
#: 不能写死 message_0（写死那次漏掉了整个导入群，见补记）。
MESSAGE_SHARD_PREFIX = "message/message_"
MESSAGE_SHARD_SUFFIX = ".db"

#: 密钥文件名（应用数据目录下）。内容是 `{库相对路径: {enc_key, salt}}`。
WCDB_KEYS_NAME = "wcdb_keys.json"

#: DPAPI 信封标记。第三方提取工具写的是明文 JSON，应用拿到后包一层再落盘；
#: 读侧两种都认，老的明文文件直接可用（首次读到后由调用方包起来）。
KEYS_PROTECTED_MARK = "dpapi-v1"
#: 钥匙文件里的微信版本元信息。load_keys 建索引时跳过（见下）。
KEYS_VERSION_META = "_wechat_version"


def _dpapi_blob(data: bytes, protect: bool) -> bytes:
    """DPAPI 加解密（当前 Windows 用户绑定）。不可用抛错，调用方定去留。"""
    if platform.system() != "Windows":
        raise WcdbUnavailable("DPAPI 只在 Windows 可用")
    try:
        import win32crypt  # type: ignore
    except ImportError as exc:
        raise WcdbUnavailable("缺少 pywin32，钥匙保护暂不可用") from exc
    if protect:
        # Keep the old DPAPI entropy so existing WCDB key files remain readable.
        return win32crypt.CryptProtectData(data, "pyweixin-wcdb", None, None, None, 0x1)
    return win32crypt.CryptUnprotectData(data, None, None, None, 0x1)[1]


def _write_keys_raw(path: Path, data: dict) -> None:
    """原子写钥匙文件（临时文件 + 替换），顺手收紧权限。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - best effort on Windows
        pass


def _read_keys_raw(path: Path | str) -> dict:
    """读原始钥匙文件：明文 JSON 和 DPAPI 信封都认。坏了就直说。"""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise WcdbUnavailable(f"没有密钥文件（先提取一次）：{exc}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise WcdbUnavailable("密钥文件损坏")
    if not isinstance(data, dict):
        raise WcdbUnavailable("密钥文件损坏")
    if data.get("_protected") != KEYS_PROTECTED_MARK:
        return data
    try:
        blob = base64.b64decode(str(data.get("blob") or ""))
        inner = json.loads(_dpapi_blob(blob, protect=False).decode("utf-8"))
    except Exception:
        raise WcdbUnavailable("密钥文件已加密但本机解不开（换了电脑或用户？），重提一次")
    if not isinstance(inner, dict):
        raise WcdbUnavailable("密钥文件损坏")
    return inner


def protect_keys_file(path: Path | str) -> bool:
    """把明文钥匙文件包成 DPAPI 信封。已包过返回 True，失败返回 False。

    从不抛异常：DPAPI 不可用就保持明文（现状），绝不能把一次成功的
    提取因为"包不上信封"变成失败。
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("_protected") == KEYS_PROTECTED_MARK:
        return True
    try:
        blob = _dpapi_blob(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                           protect=True)
    except WcdbUnavailable:
        LOGGER.debug("DPAPI unavailable, keys file stays plaintext", exc_info=True)
        return False
    try:
        _write_keys_raw(path, {"_protected": KEYS_PROTECTED_MARK,
                               "blob": base64.b64encode(blob).decode("ascii")})
    except OSError:
        return False
    return True


def record_keys_version(path: Path | str, version: str) -> bool:
    """记下提钥匙时的微信版本。失败返回 False，不影响主流程。"""
    if not version:
        return False
    try:
        data = _read_keys_raw(Path(path))
    except WcdbUnavailable:
        return False
    data[KEYS_VERSION_META] = str(version)
    try:
        raw = json.loads(Path(path).read_bytes().decode("utf-8"))
        enveloped = isinstance(raw, dict) and raw.get("_protected") == KEYS_PROTECTED_MARK
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if enveloped:
        # 已加密的文件保持加密：重包一次，不让它变回明文。
        try:
            blob = _dpapi_blob(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                               protect=True)
        except WcdbUnavailable:
            return False
        data = {"_protected": KEYS_PROTECTED_MARK,
                "blob": base64.b64encode(blob).decode("ascii")}
    try:
        _write_keys_raw(Path(path), data)
    except OSError:
        return False
    return True


def read_keys_version(path: Path | str) -> str:
    """读出记录的微信版本。没有/读不出返回 ""（调用方当未知，不催重提）。"""
    try:
        data = _read_keys_raw(Path(path))
    except WcdbUnavailable:
        return ""
    value = data.get(KEYS_VERSION_META, "")
    return str(value) if isinstance(value, (str, int, float)) else ""


def wechat_major_minor(version: str) -> str:
    """大版本比对只看到 minor：4.1.5 和 4.1.6 用同一套盐的概率极大。"""
    try:
        parts = str(version or "").strip().split(".")
        return f"{int(parts[0])}.{int(parts[1])}"
    except (IndexError, ValueError, TypeError):
        return ""


def keys_version_stale(recorded: str, current: str) -> bool:
    """记录版和当前版大版本不同 → 盐很可能换了。任一未知都不算过期。"""
    old, new = wechat_major_minor(recorded), wechat_major_minor(current)
    return bool(old and new and old != new)


class WcdbUnavailable(Exception):
    """解密库不可用：没密钥、文件不在、验签不过。调用方回落内存路。"""


class WcdbNoLocalData(Exception):
    """库可读，但这个会话本地没存消息（没在电脑上打开过，或太久被清掉）。

    这不是读取失败，不回落其它路 —— 回落也读不到，只会把明确的原因
    换成含糊的界面报错。调用方如实显示指引。
    """


# ------------------------------------------------------------ AES-CBC
# Windows CNG（bcrypt.dll，系统自带），不引入新依赖。Linux/Mac 不跑这条。


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    import ctypes.wintypes as wt

    bcrypt = ctypes.WinDLL("bcrypt")
    h_alg = wt.HANDLE()
    if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0) != 0:
        raise WcdbUnavailable("打不开系统 AES（bcrypt）")
    try:
        mode = "ChainingModeCBC\x00".encode("utf-16-le")
        if bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, len(mode), 0) != 0:
            raise WcdbUnavailable("设不了 AES-CBC 模式")
        h_key = wt.HANDLE()
        if bcrypt.BCryptGenerateSymmetricKey(
                h_alg, ctypes.byref(h_key), None, 0, key, len(key), 0) != 0:
            raise WcdbUnavailable("装不进解密密钥")
        try:
            iv_buf = ctypes.create_string_buffer(iv, len(iv))
            out_buf = ctypes.create_string_buffer(len(data))
            out_len = ctypes.c_ulong(0)
            if bcrypt.BCryptDecrypt(
                    h_key, data, len(data), None, iv_buf, len(iv),
                    out_buf, len(out_buf), ctypes.byref(out_len), 0) != 0:
                raise WcdbUnavailable("AES 解密失败")
            return out_buf.raw[:out_len.value]
        finally:
            bcrypt.BCryptDestroyKey(h_key)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


# ------------------------------------------------------------ 验签解密


def verify_enc_key(enc_key: bytes, page1: bytes) -> bool:
    """HMAC-SHA512 验签（SQLCipher4 规范）：对了才值得往下解。

    命中了不代表读到的就是对的东西，验签通过才算数。
    """
    if len(enc_key) != KEY_SIZE or len(page1) < PAGE_SIZE:
        return False
    return _verify_page(enc_key, page1[:SALT_SIZE], page1, 1)


def _verify_page(enc_key: bytes, salt: bytes, page: bytes, pgno: int) -> bool:
    """单页 HMAC 验签。pgno==1 的密文从偏移 16 开始（前 16 是明文 salt）。

    ``salt`` 每库固定（第 1 页头 16 字节），调用方透传，WAL 帧页共用它。
    """
    if len(enc_key) != KEY_SIZE or len(salt) != SALT_SIZE or len(page) < PAGE_SIZE:
        return False
    if pgno == 1:
        ciphertext = page[SALT_SIZE:PAGE_SIZE - RESERVE_SIZE]
    else:
        ciphertext = page[:PAGE_SIZE - RESERVE_SIZE]
    iv = page[PAGE_SIZE - RESERVE_SIZE:PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SIZE)
    stored = page[PAGE_SIZE - HMAC_SIZE:PAGE_SIZE]
    hm = hmac_mod.new(mac_key, ciphertext + iv, hashlib.sha512)
    hm.update(struct.pack("<i", pgno))
    return hmac_mod.compare_digest(hm.digest(), stored)


def _decrypt_page(enc_key: bytes, page: bytes, pgno: int) -> bytes:
    iv = page[PAGE_SIZE - RESERVE_SIZE:PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
    if pgno == 1:
        # 前 16 字节是明文 salt，不是密文 —— 从偏移 16 开始解。
        plain = _aes_cbc_decrypt(enc_key, iv, page[SALT_SIZE:PAGE_SIZE - RESERVE_SIZE])
        return SQLITE_MAGIC + plain + b"\x00" * RESERVE_SIZE
    plain = _aes_cbc_decrypt(enc_key, iv, page[:PAGE_SIZE - RESERVE_SIZE])
    return plain + b"\x00" * RESERVE_SIZE


def decrypt_database(src: Path, dst: Path, enc_key_hex: str) -> None:
    """解密单个库到 dst。验签不过直接抛 :class:`WcdbUnavailable`。"""
    try:
        enc_key = bytes.fromhex(enc_key_hex)
    except ValueError:
        raise WcdbUnavailable(f"密钥不是十六进制：{src.name}")
    with open(src, "rb") as handle:
        page1 = handle.read(PAGE_SIZE)
    if not verify_enc_key(enc_key, page1):
        raise WcdbUnavailable(f"密钥对不上（微信可能升级换盐了）：{src.name}")
    total = src.stat().st_size
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        pgno = 0
        while True:
            page = fin.read(PAGE_SIZE)
            if not page:
                break
            pgno += 1
            if len(page) < PAGE_SIZE:
                page = page + b"\x00" * (PAGE_SIZE - len(page))
            fout.write(_decrypt_page(enc_key, page, pgno))
    LOGGER.info("解密 %s（%d 页）", src.name, pgno)


#: WAL 魔数（SQLite 标准，与是否加密无关）。
_WAL_MAGIC = (0x377F0682, 0x377F0683)


def _read_wal_frames(wal: Path) -> tuple[int, list[tuple[int, bytes]]]:
    """读 WAL 帧：返回 (页大小, [(页号, 加密页内容)])，只要已提交的。

    帧头 24 字节里第 2 个 int 是 commit 标记：非 0 表示"提交到这里"，
    之后没标记的尾巴是没提交的，不要。页内容与库文件同版式（含保留区）。
    """
    data = wal.read_bytes()
    if len(data) < 32:
        return 0, []
    magic, _version, page_size = struct.unpack(">III", data[:12])
    if magic not in _WAL_MAGIC or page_size not in (1024, 2048, 4096, 8192):
        raise WcdbUnavailable(f"WAL 头不对：{wal.name}")
    frames: list[tuple[int, bytes]] = []
    committed: list[tuple[int, bytes]] = []
    pos = 32
    stride = 24 + page_size
    while pos + stride <= len(data):
        pgno, commit, _s1, _s2, _c1, _c2 = struct.unpack(">IIIIII", data[pos:pos + 24])
        page = data[pos + 24:pos + 24 + page_size]
        if len(page) < page_size or pgno < 1:
            break
        frames.append((pgno, page))
        if commit:
            committed = list(frames)
        pos += stride
    return page_size, committed


def apply_wal(db_path: Path, wal_path: Path, enc_key: bytes, salt: bytes) -> int:
    """把 WAL 已提交帧合进已解密的库文件。返回合入的帧数。

    帧页逐页验签（同库同 salt），验不过的帧跳过不合 —— 坏帧不能污染整库。
    """
    page_size, frames = _read_wal_frames(wal_path)
    if not frames:
        return 0
    if page_size != PAGE_SIZE:
        LOGGER.warning("WAL 页大小 %d 与库不一致，跳过 WAL", page_size)
        return 0
    merged = 0
    with open(db_path, "r+b") as handle:
        for pgno, enc_page in frames:
            # WAL 帧页是加密形态（含保留区），验签+解密后回填明文页。
            if len(enc_page) < PAGE_SIZE:
                continue
            if not _verify_page(enc_key, salt, enc_page, pgno):
                LOGGER.debug("WAL 第 %d 页验签不过，跳过", pgno)
                continue
            plain = _decrypt_page(enc_key, enc_page, pgno)
            handle.seek((pgno - 1) * PAGE_SIZE)
            handle.write(plain)
            merged += 1
    return merged


# ------------------------------------------------------------ 查询


@dataclass
class WcdbConversation:
    """解密库里的一行会话。display 为空时调用方显示 username。"""

    username: str
    display: str
    kind: str  # "group" / "friend"，与 ConversationKind 对齐
    member_count: int = 0
    last_timestamp: int = 0
    summary: str = ""


def display_name(row: Any) -> str:
    """显示名：备注优先，其次昵称，都没有回空串（调用方显示原文）。"""
    remark = str(row["remark"] if "remark" in row.keys() else "" or "").strip()
    if remark:
        return remark
    nick = str(row["nick_name"] if "nick_name" in row.keys() else "" or "").strip()
    return nick


def read_sessions(contact_db: Path, session_db: Path) -> list[WcdbConversation]:
    """最近会话（按最后时间倒序）：群和好友都在这，带名、人数、摘要。"""
    contact = sqlite3.connect(str(contact_db))
    contact.row_factory = sqlite3.Row
    try:
        people = {row["username"]: row for row in
                  contact.execute("SELECT username, remark, nick_name FROM contact")}
        counts: dict[str, int] = {}
        try:
            for room_id, total in contact.execute(
                    "SELECT room_id, COUNT(*) FROM chatroom_member GROUP BY room_id"):
                target = contact.execute(
                    "SELECT username FROM contact WHERE id=?", (room_id,)).fetchone()
                if target is not None:
                    counts[str(target[0])] = int(total)
        except sqlite3.OperationalError:
            LOGGER.debug("chatroom_member 不可读，人数记 0", exc_info=True)
    finally:
        contact.close()
    session = sqlite3.connect(str(session_db))
    session.row_factory = sqlite3.Row
    try:
        try:
            rows = session.execute(
                "SELECT username, type, last_timestamp, summary FROM SessionTable "
                "ORDER BY sort_timestamp DESC").fetchall()
        except sqlite3.OperationalError:
            rows = session.execute("SELECT username FROM SessionTable").fetchall()
    finally:
        session.close()
    out: list[WcdbConversation] = []
    for row in rows:
        username = str(row["username"] or "")
        if not username:
            continue
        person = people.get(username)
        display = display_name(person) if person is not None else ""
        kind = "group" if username.endswith("@chatroom") else "friend"
        try:
            last_ts = int(row["last_timestamp"] or 0) if "last_timestamp" in row.keys() else 0
        except (TypeError, ValueError):
            last_ts = 0
        summary = str(row["summary"] or "") if "summary" in row.keys() else ""
        out.append(WcdbConversation(
            username=username, display=display, kind=kind,
            member_count=counts.get(username, 0),
            last_timestamp=last_ts, summary=summary))
    return out


def read_all_friends(contact_db: Path) -> list[WcdbConversation]:
    """通讯录全部好友（local_type=1）：备注优先，按名排序。"""
    contact = sqlite3.connect(str(contact_db))
    contact.row_factory = sqlite3.Row
    try:
        rows = contact.execute(
            "SELECT username, remark, nick_name FROM contact "
            "WHERE local_type=1 AND username NOT LIKE '%@chatroom'").fetchall()
    finally:
        contact.close()
    out = [WcdbConversation(username=str(row["username"]),
                             display=display_name(row), kind="friend")
           for row in rows if str(row["username"] or "")]
    out.sort(key=lambda item: (item.display or item.username).lower())
    return out


def read_all_groups(contact_db: Path) -> list[WcdbConversation]:
    """通讯录里全部群（不限最近）：按昵称排序，带人数。"""
    contact = sqlite3.connect(str(contact_db))
    contact.row_factory = sqlite3.Row
    try:
        rows = contact.execute(
            "SELECT username, remark, nick_name FROM contact "
            "WHERE username LIKE '%@chatroom'").fetchall()
        counts: dict[str, int] = {}
        try:
            for room_id, total in contact.execute(
                    "SELECT room_id, COUNT(*) FROM chatroom_member GROUP BY room_id"):
                target = contact.execute(
                    "SELECT username FROM contact WHERE id=?", (room_id,)).fetchone()
                if target is not None:
                    counts[str(target[0])] = int(total)
        except sqlite3.OperationalError:
            pass
    finally:
        contact.close()
    out = [WcdbConversation(
        username=str(row["username"]), display=display_name(row), kind="group",
        member_count=counts.get(str(row["username"]), 0)) for row in rows]
    out.sort(key=lambda item: (item.display or item.username).lower())
    return out


#: 消息类型映射。文字和系统通知进解析，其它按政策进待核对（kind 照写）。
MESSAGE_KINDS = {
    1: "文本",
    3: "图片",
    34: "语音",
    43: "视频",
    47: "表情",
    48: "位置",
    49: "应用",
    50: "通话",
    10000: "系统通知",
}

_ATTACHMENT_SUFFIXES = frozenset({
    ".doc", ".docx", ".pdf", ".txt", ".md", ".rtf", ".xls", ".xlsx",
    ".ppt", ".pptx", ".zip", ".rar", ".7z", ".tar", ".gz", ".csv",
    ".odt", ".ods", ".odp", ".wps", ".et", ".dps", ".json", ".xml",
})


@dataclass
class WcdbMessage:
    """解密库里的一条消息。content 只有文本/系统通知有正文，其它留空。"""

    sender_username: str = ""
    sender_display: str = ""
    content: str = ""
    kind: str = "文本"
    timestamp: int = 0
    source_id: str = ""
    media: dict = field(default_factory=dict)


def _strip_tags(text: str, limit: int = 200) -> str:
    """系统通知的 XML 剥标签取字，待核对队列里能看懂就行。"""
    import re

    clean = re.sub(r"<[^>]{1,200}>", " ", text or "")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit]


def _media_metadata(value: object) -> dict[str, str]:
    """Keep attachment locators and decode metadata from a media message XML.

    These values stay in the local orders database and must never be logged.
    The old implementation retained only MD5s, which made a cache miss look
    indistinguishable from an image row whose CDN reference had been discarded.
    """
    if isinstance(value, (bytes, bytearray)):
        value = _maybe_decompress(value)
        value = bytes(value).decode("utf-8", errors="replace") if isinstance(value, (bytes, bytearray)) else value
    raw = str(value or "")
    if "<" not in raw or ">" not in raw:
        return {}
    import re
    import xml.etree.ElementTree as ET

    attrs: dict[str, str] = {}
    text_values: dict[str, str] = {}
    attribute_names = {
        "md5", "videomd5", "filemd5", "originsourcemd5",
        "filename", "name", "aeskey", "cdnthumbaeskey",
        "cdnthumburl", "cdnmidimgurl", "cdnhdimgurl", "cdnbigimgurl",
        "cdnthumbmd5", "cdnmidmd5", "cdnfileid", "fileid", "filekey",
        "encryver", "cdnthumblength", "cdnmidimglength", "cdnmidimgsize",
        "cdnhdlength", "length", "hevc_mid_size", "cdnthumbwidth",
        "cdnthumbheight", "cdnmidwidth", "cdnmidheight", "cdnhdwidth",
        "cdnhdheight",
    }
    identifier_names = {
        "md5", "videomd5", "filemd5", "originsourcemd5",
        "filename", "name", "fileid", "filekey",
    }
    try:
        root = ET.fromstring(raw)
        for node in root.iter():
            for key, item in node.attrib.items():
                if item and key.lower() in attribute_names:
                    attrs[key.lower()] = str(item)
            for child in node:
                key = child.tag.rsplit("}", 1)[-1].lower()
                if child.text and key in {
                        "md5", "videomd5", "filemd5", "filename", "name",
                        "title", "fileext", "ext", "extension"}:
                    text_values[key] = child.text.strip()
                    if key in identifier_names:
                        attrs[key] = child.text.strip()
    except ET.ParseError:
        # Weixin prefixes message XML with ``sender_wxid:``; regex fallback
        # also handles those rows without needing to rewrite their content.
        # Values are persisted for local CDN/cache resolution only.
        names = "|".join(sorted(attribute_names, key=len, reverse=True))
        for key, item in re.findall(
                rf'(?i)\b({names})=["\']([^"\']+)', raw):
            attrs[key.lower()] = item
        for key, item in re.findall(
                r'(?is)<(title|fileext|ext|extension)\b[^>]*>(.*?)</\1\s*>', raw):
            text_values[key.lower()] = re.sub(r"<[^>]+>", "", item).strip()

    # 微信文件消息通常把可见文件名放在 appmsg/title，把扩展名另放在
    # appattach/fileext；只读 filename/name 会把它误判成普通“应用”消息。
    filename = attrs.get("filename") or attrs.get("name") or ""
    title = text_values.get("title", "")
    fileext = (text_values.get("fileext") or text_values.get("ext")
               or text_values.get("extension") or "").strip().lstrip(".")
    candidate = next((name for name in (filename, title)
                      if Path(name).suffix.lower() in _ATTACHMENT_SUFFIXES), "")
    if not candidate:
        candidate = filename or title or (f"附件.{fileext}" if fileext else "")
    if candidate and fileext and not Path(candidate).suffix:
        candidate = f"{candidate}.{fileext}"
    if candidate and Path(candidate).suffix.lower() in _ATTACHMENT_SUFFIXES:
        attrs["filename"] = candidate
    return attrs


#: ZSTD 帧魔数（小端）。WCDB 把压缩列直接存成 ZSTD 帧：
#: 短文本是明文 `发送人:\n正文`，长一点的就整段压成帧，
#: 不解看到的就是一串二进制，正文和发送人都丢了。
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _maybe_decompress(content: object) -> object:
    """ZSTD 帧就解开，不是就原样返回。从不抛错。

    zstandard 缺装时保持旧行为（空正文），不断链 —— 但 requirements
    里声明了它，正常安装不会缺。
    """
    if not isinstance(content, (bytes, bytearray)) or len(content) < 16:
        return content
    if bytes(content[:4]) != _ZSTD_MAGIC:
        return content
    try:
        import zstandard
    except ImportError:
        LOGGER.debug("zstandard 缺装，压缩消息解不开", exc_info=True)
        return content
    try:
        return zstandard.ZstdDecompressor().decompress(
            bytes(content), max_output_size=4 * 1024 * 1024)
    except Exception:
        LOGGER.debug("ZSTD 解帧失败，留空", exc_info=True)
        return content


def _split_sender(content: object) -> tuple[str, str]:
    """文本正文拆 `发送人:\\n正文`。没前缀算会话本人（私聊）或未知。"""
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8")
        except Exception:
            return "", ""
    else:
        text = str(content or "")
    if ":\n" in text:
        left, body = text.split(":\n", 1)
        # 发送人只能是微信号形态：原样含空格/换行的不算（正文里的冒号换行）。
        if left and len(left) <= 64 and " " not in left and "\t" not in left \
                and "\n" not in left:
            return left.strip(), body
    return "", text


def message_table_name(username: str) -> str:
    """会话的消息表名。WECHAT 按用户名 md5 分表。"""
    import hashlib

    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def message_table_exists(message_dbs: Sequence[Path], username: str) -> bool:
    """本地有没有存过这个会话的消息（扫全部分片）。没有=电脑上没打开过，别硬读。"""
    if isinstance(message_dbs, (str, Path)):
        message_dbs = [Path(message_dbs)]
    table = message_table_name(username)
    for message_db in message_dbs:
        con = sqlite3.connect(str(message_db))
        try:
            hit = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if hit is not None:
                return True
        except sqlite3.OperationalError:
            continue
        finally:
            con.close()
    return False


def read_messages(message_dbs: Sequence[Path], contact_db: Path, username: str,
                  limit: int = 200) -> list[WcdbMessage]:
    """读指定会话最新 ``limit`` 条（时间正序返回），扫全部分片再合并。

    手机导入会建新分片（message_1.db…），只读 message_0 会漏。
    分片间按时间排、server_id 去重（导入与在线时间不交叠，重复极少）。
    都没表=本地没存过这个会话的消息，返回空表，不抛错（上游记 0 条覆盖，
    不断链；要指引时上游用 message_table_exists 先判）。
    """
    if isinstance(message_dbs, (str, Path)):
        message_dbs = [Path(message_dbs)]
    table = message_table_name(username)
    per_shard = max(1, int(limit or 0))
    gathered: list[sqlite3.Row] = []
    for message_db in message_dbs:
        messages = sqlite3.connect(str(message_db))
        messages.row_factory = sqlite3.Row
        try:
            try:
                tables = {row[0] for row in messages.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.OperationalError as exc:
                raise WcdbUnavailable(f"消息库打不开：{exc}")
            if table not in tables:
                continue
            cols = {info[1] for info in messages.execute(
                "PRAGMA table_info(%s)" % table).fetchall()}
            need = {"local_id", "server_id", "local_type", "sort_seq",
                    "create_time", "message_content"}
            if not need.issubset(cols):
                raise WcdbUnavailable(f"消息表结构对不上（版本变了）：{table}")
            gathered.extend(messages.execute(
                "SELECT local_id, server_id, local_type, sort_seq, create_time,"
                " message_content FROM %s ORDER BY sort_seq DESC LIMIT ?" % table,
                (per_shard,)).fetchall())
        finally:
            messages.close()
    # 跨分片合并：server_id 有值时它是全局唯一键，否则退化成
    # （时间，发送人，内容）三元组去重；最后按时间正序、取最新 limit 条。
    seen: set[Any] = set()
    merged: list[dict[str, Any]] = []
    for row in gathered:
        try:
            server_id = int(row["server_id"] or 0)
        except (TypeError, ValueError):
            server_id = 0
        text = row["message_content"]
        text_key = bytes(text).decode("utf-8", errors="replace") if isinstance(
            text, bytes) else str(text or "")
        key = ("srv", server_id) if server_id else (
            "row", row["create_time"], text_key[:120])
        if key in seen:
            continue
        seen.add(key)
        merged.append({
            "local_id": row["local_id"], "server_id": server_id,
            "local_type": row["local_type"], "sort_seq": row["sort_seq"],
            "create_time": row["create_time"], "message_content": row["message_content"],
        })
    merged.sort(key=lambda item: (
        int(item["create_time"] or 0), int(item["sort_seq"] or 0)))
    rows = merged[-max(1, int(limit or 0)):]
    # 发送人批量解备注：一条群几百条消息只查一轮通讯录。
    # 先解压：长文本/图片元数据是 ZSTD 整段压的，不解连发送人前缀都看不到。
    senders: set[str] = set()
    plain_text: dict[int, str] = {}
    for index, row in enumerate(rows):
        try:
            if int(row["local_type"] or 0) == 1:
                raw = _maybe_decompress(row["message_content"])
                text = raw.decode("utf-8", errors="replace") if isinstance(
                    raw, (bytes, bytearray)) else str(raw or "")
                plain_text[index] = text
                sender = _split_sender(text)[0]
                if sender:
                    senders.add(sender)
        except (TypeError, ValueError):
            continue
    resolved: dict[str, str] = {}
    if senders:
        contact = sqlite3.connect(str(contact_db))
        contact.row_factory = sqlite3.Row
        try:
            for row in contact.execute(
                    "SELECT username, remark, nick_name FROM contact WHERE username IN (%s)" %
                    ",".join("?" * len(senders)), tuple(sorted(senders))):
                resolved[str(row["username"])] = display_name(row) or str(row["username"])
        except sqlite3.OperationalError:
            LOGGER.debug("发送人备注解不出，用原文", exc_info=True)
        finally:
            contact.close()
    out: list[WcdbMessage] = []
    for index, row in enumerate(rows):
        try:
            local_type = int(row["local_type"] or 0)
        except (TypeError, ValueError):
            local_type = 0
        kind = MESSAGE_KINDS.get(local_type, "其他")
        media = _media_metadata(row["message_content"])
        if kind == "应用" and (media.get("filemd5") or
                                Path(media.get("filename", "")).suffix.lower() in
                                _ATTACHMENT_SUFFIXES):
            kind = "文件"
        sender, body = ("", "")
        if local_type == 1:
            # 解压过的正文直接用（上面批量解备注时已解好，不解两遍）。
            sender, body = _split_sender(plain_text.get(index, ""))
        elif local_type == 10000:
            body = _strip_tags(row["message_content"] if isinstance(
                row["message_content"], str) else "")
        try:
            timestamp = int(row["create_time"] or 0)
        except (TypeError, ValueError):
            timestamp = 0
        try:
            server_id = int(row["server_id"] or 0)
        except (TypeError, ValueError):
            server_id = 0
        if server_id:
            source_id = f"wcdb-{server_id}"
        else:
            source_id = f"wcdb-{row['local_id']}-{timestamp}"
        out.append(WcdbMessage(
            sender_username=sender,
            sender_display=resolved.get(sender, sender),
            content=body if kind in ("文本", "系统通知") else "",
            kind=kind, timestamp=timestamp, source_id=source_id,
            media=media if kind in {"图片", "语音", "视频", "文件"} else {}))
    # rows 进来已经是时间正序（合并时排好），直接返回。
    return out


# ------------------------------------------------------------ 门面


class WcdbReader:
    """解密库会话来源。钥匙、文件、验签缺任何一个都报不可用，不抛别的错。"""

    def __init__(self, db_storage: Path | str, keys_path: Path | str,
                 workdir: Path | str | None = None):
        self.db_storage = Path(db_storage)
        self.keys_path = Path(keys_path)
        self.workdir = Path(workdir) if workdir else Path(tempfile.gettempdir()) / "pyw-wcdb"
        self._keys: dict | None = None
        self._error = ""

    @property
    def error(self) -> str:
        """上次不可用的原因（界面提示用）。"""
        return self._error

    def load_keys(self) -> dict:
        """读密钥文件。结构 `{库相对路径: {enc_key, salt}}`，坏了当没有。

        明文老文件和 DPAPI 信封都认（见 :func:`_read_keys_raw`）；
        ``_`` 开头的元信息（比如微信版本）不进索引。
        """
        if self._keys is not None:
            return self._keys
        data = _read_keys_raw(self.keys_path)
        normalized = {}
        for key, value in data.items():
            text = str(key)
            if text.startswith("_"):
                continue
            normalized[text.replace("\\", "/")] = value
        self._keys = normalized
        if not self._keys:
            raise WcdbUnavailable("密钥文件是空的")
        return self._keys

    def prepare(self, extra: Sequence[str] = ()) -> dict[str, Path]:
        """拷贝活文件 → 解密到工作目录 → WAL 合入 → 返回解密后路径。

        ``extra`` 追加要的库（读历史加 ``message/message_0.db``）。
        用完调用方负责删（见 :meth:`cleanup`）。任何一步失败都抛
        :class:`WcdbUnavailable`，调用方回落内存路。
        """
        keys = self.load_keys()
        wanted = (*NEEDED_DATABASES, *(str(item) for item in extra))
        ready: dict[str, Path] = {}
        try:
            for rel in wanted:
                info = keys.get(rel) or keys.get(rel.replace("/", "\\"))
                if not isinstance(info, dict) or not info.get("enc_key"):
                    raise WcdbUnavailable(
                        f"缺 {rel} 的密钥：微信新建了数据库或大版本升级换了盐，"
                        "点「重新提取密钥」补提一次再读")
                try:
                    enc_key = bytes.fromhex(str(info["enc_key"]))
                except ValueError:
                    raise WcdbUnavailable(f"密钥不是十六进制：{rel}")
                src = self.db_storage / rel
                if not src.is_file():
                    raise WcdbUnavailable(f"找不到活库文件：{rel}")
                tmp_src = self.workdir / (rel.replace("/", "_") + ".src")
                tmp_dst = self.workdir / (rel.replace("/", "_") + ".db")
                self.workdir.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copyfile(src, tmp_src)
                except OSError as exc:
                    raise WcdbUnavailable(f"拷贝活库被锁：{exc}")
                try:
                    decrypt_database(tmp_src, tmp_dst, str(info["enc_key"]))
                    # WAL：今天的新消息还在里面，不合就看不到。
                    # 合不上也不致命 —— 主库快照照样可用。
                    wal_src = self.db_storage / (rel + "-wal")
                    if wal_src.is_file():
                        try:
                            self._merge_wal(wal_src, tmp_dst, enc_key)
                        except Exception:
                            LOGGER.debug("WAL 合入失败，用主库快照", exc_info=True)
                finally:
                    try:
                        tmp_src.unlink(missing_ok=True)
                    except OSError:
                        pass
                ready[rel] = tmp_dst
        except Exception:
            self.cleanup(ready)
            raise
        return ready

    def _merge_wal(self, wal_src: Path, db_dst: Path, enc_key: bytes) -> None:
        """拷贝 WAL → 解帧 → 验签 → 合进已解密库。坏帧跳过。"""
        tmp_wal = self.workdir / (wal_src.name.replace("/", "_") + ".wal")
        try:
            shutil.copyfile(wal_src, tmp_wal)
        except OSError as exc:
            raise WcdbUnavailable(f"拷贝 WAL 被锁：{exc}")
        try:
            with open(db_dst, "rb") as handle:
                salt = handle.read(SALT_SIZE)
            merged = apply_wal(db_dst, tmp_wal, enc_key, salt)
            if merged:
                LOGGER.info("WAL 合入 %s（%d 帧）", wal_src.name, merged)
        finally:
            try:
                tmp_wal.unlink(missing_ok=True)
            except OSError:
                pass

    def cleanup(self, ready: dict[str, Path] | None = None) -> None:
        """删掉解密产物。读完就删，不留明文过夜。"""
        targets = list((ready or {}).values())
        try:
            targets.extend(self.workdir.glob("*.db"))
        except OSError:
            pass
        for path in targets:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    @property
    def available(self) -> bool:
        """钥匙和文件都在（不解密，只看门）。界面据此决定显不显示入口。"""
        try:
            keys = self.load_keys()
        except WcdbUnavailable as exc:
            self._error = str(exc)
            return False
        for rel in NEEDED_DATABASES:
            if rel not in keys or not (self.db_storage / rel).is_file():
                self._error = f"缺 {rel} 的密钥或文件"
                return False
        self._error = ""
        return True

    def verify_keys(self) -> tuple[bool, str]:
        """验一把钥匙真能开门：挑有钥匙的库读首页验签。

        毫秒级，只读 4K，不扫内存。微信升级换盐后这里先报错，
        商户在"读取"之前就知道要重提，而不是读一半才炸。
        有一个库能打开就算可用；文件残缺（不够一页）跳过，
        那不是钥匙的错。
        """
        try:
            keys = self.load_keys()
        except WcdbUnavailable as exc:
            return False, str(exc)
        first_failure = ""
        candidates = (*NEEDED_DATABASES,
                      MESSAGE_SHARD_PREFIX + "0" + MESSAGE_SHARD_SUFFIX)
        for rel in candidates:
            info = keys.get(rel) or keys.get(rel.replace("/", "\\"))
            if not isinstance(info, dict) or not info.get("enc_key"):
                continue
            src = self.db_storage / rel
            if not src.is_file():
                continue
            try:
                with open(src, "rb") as handle:
                    page1 = handle.read(PAGE_SIZE)
            except OSError:
                continue
            if len(page1) < PAGE_SIZE:
                continue
            try:
                enc_key = bytes.fromhex(str(info["enc_key"]))
            except ValueError:
                return False, f"密钥不是十六进制：{rel}"
            if verify_enc_key(enc_key, page1):
                return True, "钥匙可用"
            if not first_failure:
                first_failure = (f"钥匙打不开 {rel}：微信可能升级换了盐，"
                                 "点「重新提取密钥」提一次再读")
        if first_failure:
            return False, first_failure
        return False, "没有能验证的库文件"

    def unkeyed_databases(self) -> list[str]:
        """磁盘上有、钥匙里没有的库（微信新建了分片/库，钥匙还是旧的）。

        返回相对路径表。空表=钥匙齐全。这是"要重提"的唯一信号，
        调用方据此自动重提或指引商户点按钮。
        """
        try:
            keys = self.load_keys()
        except WcdbUnavailable:
            return list(NEEDED_DATABASES)
        missing: list[str] = []
        wanted = list(NEEDED_DATABASES)
        try:
            names = [p.name for p in (self.db_storage / "message").iterdir()]
            for name in sorted(names):
                if name.startswith("message_") and name.endswith(MESSAGE_SHARD_SUFFIX):
                    middle = name[len("message_"):-len(MESSAGE_SHARD_SUFFIX)]
                    if middle.isdigit():
                        wanted.append("message/" + name)
        except OSError:
            pass
        for rel in wanted:
            if not (self.db_storage / rel).is_file():
                continue
            if rel not in keys and rel.replace("/", "\\") not in keys:
                missing.append(rel)
        return missing

    def message_shards(self) -> list[str]:
        """消息库全部分片（message_0.db、message_1.db…）。

        手机导入会建新分片，只读 message_0 会漏。按数字后缀排序。
        有钥匙的正常读；没钥匙的不在这里跳过 —— prepare 会报
        "缺密钥、去重提"，跳过只会让导入群静默 0 条（之前吃过的亏）。
        """
        found: list[tuple[int, str]] = []
        try:
            names = [p.name for p in (self.db_storage / "message").iterdir()]
        except OSError:
            return []
        for name in names:
            if not (name.startswith("message_") and name.endswith(MESSAGE_SHARD_SUFFIX)):
                continue
            middle = name[len("message_"):-len(MESSAGE_SHARD_SUFFIX)]
            if not middle.isdigit():
                continue
            rel = "message/" + name
            if not (self.db_storage / rel).is_file():
                continue
            found.append((int(middle), rel))
        found.sort()
        return [rel for _, rel in found]

    def list_conversations(self, kind: str = "") -> list[WcdbConversation]:
        """最近会话（解密库）。kind 为空要全部：冷群、通讯录好友都并进来。

        最近会话在前（会话表按最后时间倒序），冷群和通讯录好友
        （没时间）跟在后面 —— 上游按这个顺序排，和微信一致。
        这里只保证齐全不重复（同 username 去重，会话版优先）。
        """
        ready = self.prepare()
        try:
            contact_db = ready["contact/contact.db"]
            session_db = ready["session/session.db"]
            found = read_sessions(contact_db, session_db)
            seen = {item.username for item in found}
            if not kind or kind == "group":
                # 最近里没有的冷群也并进来（显示名、人数都有，只是没时间）。
                for item in read_all_groups(contact_db):
                    if item.username not in seen:
                        seen.add(item.username)
                        found.append(item)
            if not kind or kind == "friend":
                # 通讯录好友（local_type=1），最近里没有的也并进来。
                for item in read_all_friends(contact_db):
                    if item.username not in seen:
                        seen.add(item.username)
                        found.append(item)
            if kind in ("group", "friend"):
                found = [item for item in found if item.kind == kind]
            return found
        finally:
            self.cleanup(ready)

    def read_conversation_messages(self, username: str,
                                   limit: int = 200) -> list[WcdbMessage]:
        """读指定会话的消息（扫全部分片，含 WAL 今天的新消息）。用完即删。"""
        shards = self.message_shards()
        if not shards:
            raise WcdbUnavailable("没有可读的消息库分片")
        ready = self.prepare(shards)
        try:
            message_dbs = [ready[rel] for rel in shards]
            return read_messages(message_dbs,
                                 ready["contact/contact.db"],
                                 username, limit)
        finally:
            self.cleanup(ready)


def derive_conversation_kind(username: str) -> str:
    """与 ConversationKind 对齐的极简判定，供调用方复用。"""
    return "group" if str(username or "").endswith("@chatroom") else "friend"


__all__ = [
    "MESSAGE_KINDS",
    "MESSAGE_SHARD_PREFIX",
    "MESSAGE_SHARD_SUFFIX",
    "NEEDED_DATABASES",
    "WCDB_KEYS_NAME",
    "WcdbConversation",
    "WcdbMessage",
    "WcdbNoLocalData",
    "WcdbReader",
    "WcdbUnavailable",
    "apply_wal",
    "decrypt_database",
    "derive_conversation_kind",
    "display_name",
    "message_table_exists",
    "message_table_name",
    "read_all_friends",
    "read_all_groups",
    "read_messages",
    "read_sessions",
    "verify_enc_key",
]
