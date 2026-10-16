"""微信 4.x 本地库内存读取：发送人、内容、时间、媒体，全都要。

## 为什么有这个模块

UIA 文本里没有群发送人（见 docs/sender-recognition-findings.md），
头像回填慢且丢条。微信把解密后的 SQLite 页缓存在自己内存里 ——
读自己用户权限能读的内存，只读不写，就能把记录直接捡出来。
密钥都不需要。

## 能读到什么（inspect/localdb_*.py 逐条验证过）

* 联系人/群资料 record：wxid、备注名、头像串、拼音索引；
* 消息 record：发送人 wxid、内容（文本直接读，图片/视频带 CDN 元数据）、时间戳；
* 图片：dnthumburl/cdnthumbleng/thumbaeskey/md5/filekey，可下载；
* 视频：`<type>43</type>` 记录存在，同图片路数；
* 语音：内存里有 `#!SILK_V3` 音频字节 + voicelength 元数据，可提取，
  silk 转码另需解码器（本模块只管捡出来）。

## 用法

    from wechat_ai_assistant.localdb import find_conversations, read_conversation
    groups = find_conversations()          # 内存里的群 wxid 清单
    messages = read_conversation("6255144087@chatroom", limit=200)

内存里只有最近碰过的页 —— 目标群聊得多翻翻，翻得越多捡得越多。
读不到就返回已捡到的，不抛异常（调用方按"没读全"处理，别当"没有消息"）。
"""
from __future__ import annotations

import ctypes
import logging
import re
from ctypes import wintypes
from dataclasses import dataclass, field

LOGGER = logging.getLogger(__name__)

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
_PAGE_READONLY = 0x02
_PAGE_READWRITE = 0x04
_PAGE_WRITECOPY = 0x08
_PAGE_EXECUTE_READ = 0x20
_PAGE_EXECUTE_READWRITE = 0x40
_PAGE_GUARD = 0x100
_READABLE = (_PAGE_READONLY | _PAGE_READWRITE | _PAGE_WRITECOPY
             | _PAGE_EXECUTE_READ | _PAGE_EXECUTE_READWRITE)

_WXID_PATTERN = re.compile(rb"[a-zA-Z0-9_\-]{6,64}@chatroom")
_USER_PATTERN = re.compile(rb"(?:wxid_[a-zA-Z0-9_\-]{4,32}|gh_[a-zA-Z0-9_\-]{4,32})")

#: 单次扫描上限，避免把机器拖死。
SCAN_BUDGET_BYTES = 2048 * 1024 * 1024
#: 锚点往回找 record 头的最远距离。
RECORD_LOOKBEHIND = 768


class _MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", wintypes.LPVOID),
        ("AllocationBase", wintypes.LPVOID),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


@dataclass
class LocalMessage:
    """从内存捡到的一条消息。字段缺哪个留空哪个，不编造。"""
    group_wxid: str = ""
    sender_wxid: str = ""
    content: str = ""
    timestamp: int = 0
    msg_type: int = 0
    media: dict = field(default_factory=dict)
    #: 展示用类型："文本" 或 "系统通知"。系统通知不进订单解析，
    #: 只进待核对（归一化时落到 other）。
    kind: str = "文本"


#: 系统通知的行话：进群退群踢人撤回改名。中英文都要，微信两套文案。
_SYSTEM_NOTICE_PATTERNS = (
    "invited you", "has joined", "has left", "was removed", "recalled a message",
    "加入了群聊", "退出了群聊", "退出群聊", "移出群聊", "修改群名为", "撤回了一条消息",
)


def is_system_notice(text: str) -> bool:
    """这行是不是微信自己生成的系统通知（不是人说话）。"""
    lowered = (text or "").lower()
    return any(mark.lower() in lowered for mark in _SYSTEM_NOTICE_PATTERNS)


def read_varint(data: bytes, pos: int):
    """读 SQLite varint，返回 (值, 下一位置)，读不出返回 (None, pos)。"""
    result = 0
    for i in range(9):
        if pos + i >= len(data):
            return None, pos
        byte = data[pos + i]
        if i == 8:
            return (result << 8) | byte, pos + 9
        result = (result << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return result, pos + i + 1
    return None, pos


def _parse_fields(chunk: bytes, serials: list, pos: int):
    """按 serial 表走值区。返回 (字段表, 跨度表, 结束位置)，走不通返回 None。

    跨度表元素是 (kind, 起, 止)，认领锚点时只认 TEXT 字段的跨度 ——
    子解析（record 的一部分）也能自洽，必须卡死"锚点在某个 TEXT 值里"，
    否则满内存都是误认领。
    """
    fields: list = []
    spans: list = []
    for serial in serials:
        if pos > len(chunk):
            return None
        if serial == 0:
            fields.append(("null", None))
            spans.append(("null", pos, pos))
        elif 1 <= serial <= 4:
            fields.append(("int", int.from_bytes(chunk[pos:pos + serial], "big", signed=True)))
            spans.append(("int", pos, pos + serial))
            pos += serial
        elif serial == 5:
            fields.append(("int", int.from_bytes(chunk[pos:pos + 6], "big", signed=True)))
            spans.append(("int", pos, pos + 6))
            pos += 6
        elif serial == 6:
            fields.append(("int", int.from_bytes(chunk[pos:pos + 8], "big", signed=True)))
            spans.append(("int", pos, pos + 8))
            pos += 8
        elif serial == 7:
            fields.append(("float", None))
            spans.append(("float", pos, pos + 8))
            pos += 8
        elif serial in (8, 9):
            fields.append(("int", 0 if serial == 8 else 1))
            spans.append(("int", pos, pos))
        elif serial >= 12:
            if serial % 2 == 0:
                spans.append(("blob", pos, pos + (serial - 12) // 2))
                pos += (serial - 12) // 2
                fields.append(("blob", None))
            else:
                size = (serial - 13) // 2
                if size > 65536:
                    return None
                raw = chunk[pos:pos + size]
                try:
                    text = raw.decode("utf-8")
                    if "\ud800" <= max(text, default="") <= "\udfff":
                        return None
                except Exception:
                    return None
                fields.append(("text", text))
                spans.append(("text", pos, pos + size))
                pos += size
        else:
            return None
    return fields, spans, pos


def _parse_header(chunk: bytes, start: int):
    """解析 record 头，返回 (serial表, 值区起始) 或 None。"""
    header_len, pos = read_varint(chunk, start)
    if header_len is None or not 2 <= header_len <= 200:
        return None
    serials = []
    consumed = pos - start
    while consumed < header_len:
        serial, pos = read_varint(chunk, pos)
        if serial is None or serial in (10, 11):
            return None
        serials.append(serial)
        consumed = pos - start
    if consumed != header_len or not serials:
        return None
    return serials, pos


def parse_record(chunk: bytes, start: int):
    """把 chunk[start:] 当 SQLite record 头试解析。

    成功返回 [(kind, value)]，kind 是 text/int/float/null/blob；
    失败返回 None。纯函数，不碰内存不碰文件。
    """
    header = _parse_header(chunk, start)
    if header is None:
        return None
    serials, pos = header
    parsed = _parse_fields(chunk, serials, pos)
    return parsed[0] if parsed is not None else None


def find_record_before(chunk: bytes, anchor_pos: int, back: int = RECORD_LOOKBEHIND):
    """从锚点往回找包含它的 record。返回 (起始偏移, 字段表) 或 None。

    认领条件：解析自洽 + 锚点落在某个 TEXT 字段的跨度内。
    多个候选取跨度最长的 —— 子解析（record 的一部分）也能自洽，
    但一定比完整 record 短；先到先得会被子解析截胡。
    """
    best = None
    for start in range(max(0, anchor_pos - back), anchor_pos):
        first = chunk[start]
        if first == 0 or first >= 0x40:
            continue
        header = _parse_header(chunk, start)
        if header is None:
            continue
        serials, pos = header
        parsed = _parse_fields(chunk, serials, pos)
        if parsed is None:
            continue
        fields, spans, end = parsed
        claimed = any(
            kind == "text" and span_start <= anchor_pos < span_end
            for kind, span_start, span_end in spans
        )
        if claimed and (best is None or (end - start) > (best[2] - best[0])):
            best = (start, fields, end)
    if best is None:
        return None
    return best[0], best[1]


#: wxid → 显示名缓存（同一进程内有效，微信重启/切号后 stale ——
#: 调用方读到空时清一次再试，见 _read_via_localdb 注释）。
#: 键是 ASCII wxid，不含密钥材料，可放心放内存。
_NAME_CACHE: dict[str, str] = {}

#: 群名 → 群 wxid 缓存（与 _NAME_CACHE 同寿命、同 stale 语义）。
#: 投票制解析一次全量扫描十几秒，同一群名别扫第二遍。
_GROUP_WXID_CACHE: dict[str, str] = {}


def clear_name_caches() -> None:
    """清掉群名/联系人显示名缓存。切号、重启微信后调一次。"""
    _NAME_CACHE.clear()
    _GROUP_WXID_CACHE.clear()


def _iter_regions(handle) -> object:
    kernel32 = ctypes.windll.kernel32
    address = 0
    mbi = _MemoryBasicInformation()
    while address < 0x7FFFFFFFFFFF:
        ret = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(address),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not ret:
            return
        region_size = int(mbi.RegionSize)
        base = int(mbi.BaseAddress) if isinstance(mbi.BaseAddress, int) else mbi.BaseAddress
        if (mbi.State == MEM_COMMIT and (mbi.Protect & _READABLE)
                and not (mbi.Protect & _PAGE_GUARD)
                and region_size <= 256 * 1024 * 1024):
            buffer = ctypes.create_string_buffer(region_size)
            nread = ctypes.c_size_t(0)
            if kernel32.ReadProcessMemory(handle, ctypes.c_void_p(base),
                                          buffer, region_size, ctypes.byref(nread)):
                yield base, buffer.raw[:nread.value]
        address = (base or address) + region_size


def _weixin_pids() -> list[int]:
    import psutil

    return [proc.info["pid"] for proc in psutil.process_iter(["pid", "name"])
            if (proc.info["name"] or "").lower() == "weixin.exe"]


def _open_process(pid: int):
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    return handle or None


def close_quietly(kernel32, handle) -> None:
    """关句柄。mock 句柄、坏句柄都不该把读取带崩。"""
    try:
        kernel32.CloseHandle(handle)
    except Exception:
        pass


def _looks_like_identifier(text: str) -> bool:
    """wxid、群 id 这类机器名不配当消息正文。"""
    if not text:
        return True
    if text.endswith("@chatroom"):
        return True
    if text.startswith(("wxid_", "gh_")):
        return True
    if len(text) <= 64 and all(ch.isascii() and (ch.isalnum() or ch in "_-.@") for ch in text):
        # 纯 ASCII 机器名（wxid、群 id、md5 之类）不配当正文。但别卡太死：
        # 短词（"好"、"OK"）可能是真内容，8 字以上才判机器名。
        return len(text) > 8
    return False


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


#: 搜索框/成员列表/FTS 索引行的味道：正文里出现这些就不是聊天消息。
_SNIPPET_MARKERS = (
    "</member>", "</nickname>", "<member>", "chat_type", "record_tab_type",
    "query_word", "HtmlFile", "WeNoteHtml",
)
_PHONE_SNIPPET_PATTERN = re.compile(r"^\d{5,}\s*[,，]\s*.{1,30}$")


def _looks_like_snippet(text: str) -> bool:
    """搜索结果、成员 XML、索引行 —— 不是人说的话。"""
    if not text:
        return True
    if any(mark in text for mark in _SNIPPET_MARKERS):
        return True
    if _PHONE_SNIPPET_PATTERN.match(text.strip()):
        return True
    return False


def resolve_group_wxid(display_name: str, limit: int = 5) -> str | None:
    """备注名/群名 → 群 wxid。翻资料 record 里同时出现名字和 `xxx@chatroom`。

    同一个名字可能出现在多条消息正文里（转发、接龙、口头提到群名），
    第一命中就返回会串到别的群 —— 下单读的就是错群。改成全量投票：
    出现次数最多的 wxid 胜出；并列第一（分不清是哪个群）返回 None，
    调用方回落老链路，别当"没有这个群"，更别读错群。

    ``limit`` 是历史参数，保留兼容，不再提前截断（截断正是串群的根因）。

    已经是 wxid 的输入（第 2 步列表里选出来的行）直接认：名单本身
    就是刚从内存里扫出来的，不用再扫一遍证明它存在。
    找不到返回 None —— 调用方回落老链路，别当"没有这个群"。
    """
    text = (display_name or "").strip()
    if not text:
        return None
    if _WXID_PATTERN.fullmatch(text.encode("ascii", errors="ignore")):
        return text
    if text in _GROUP_WXID_CACHE:
        return _GROUP_WXID_CACHE[text]
    anchor = text.encode("utf-8", errors="ignore")
    if not anchor:
        return None
    from collections import Counter

    votes: Counter[str] = Counter()
    kernel32 = ctypes.windll.kernel32
    scanned = 0
    for pid in _weixin_pids():
        handle = _open_process(pid)
        if not handle:
            continue
        try:
            for _, chunk in _iter_regions(handle):
                scanned += len(chunk)
                if scanned > SCAN_BUDGET_BYTES:
                    break
                pos = 0
                while True:
                    pos = chunk.find(anchor, pos)
                    if pos == -1:
                        break
                    found = find_record_before(chunk, pos)
                    pos += len(anchor)
                    if found is None:
                        continue
                    _, fields = found
                    for kind, value in fields:
                        if kind == "text" and isinstance(value, str) \
                                and value.endswith("@chatroom"):
                            votes[value] += 1
        finally:
            close_quietly(kernel32, handle)
    if not votes:
        return None
    top = votes.most_common(2)
    if len(top) > 1 and top[0][1] == top[1][1]:
        # 两个群票数一样：分不清，别猜。
        return None
    wxid = top[0][0]
    _GROUP_WXID_CACHE[text] = wxid
    return wxid


#: 聊天记录面板标题里的群名。例：`"水果群"的` / `Chat history of "水果群"`。
#: 中文 `xx的`，英文 `Chat history of xx`，两种都覆盖。
_GROUP_TITLE_PATTERN = re.compile(
    r'^[""\']?(.{1,64}?)[""\']?的$|^Chat (?:history of|History for) ["\']?(.{1,64}?)["\']?$'
)


def extract_group_name_from_title(title: str) -> str:
    """从聊天记录面板标题里解析群名。解析不出返回空串。"""
    text = (title or "").strip()
    if not text:
        return ""
    match = _GROUP_TITLE_PATTERN.match(text)
    if match:
        return (match.group(1) or match.group(2) or "").strip()
    return ""


def _is_clean_name(text: str) -> bool:
    """能摆上桌面的名字：不含控制字符、私用区、代理对和换行。

    二进制误解析会吐出 ``\\x14`` 这类控制字符，消息粘贴会带 ``\\n``；
    真群名/备注名里不会有这些。显示一串带控制符的"名字"，商户搜都
    搜不到，还不如显示编号再手动备注。表情和生僻字不受影响
    （符号/标记类照收，零宽连接符是格式类 Cf，也收）。
    """
    import unicodedata

    for char in text:
        category = unicodedata.category(char)
        if category in ("Zl", "Zp"):
            return False
        if category[0] == "C" and category != "Cf":
            return False
    return True


def _vote_name(candidates: list) -> str:
    """一堆候选里投票选名字：出现次数最多胜出，并列取短的。

    背景：锚点附近的 record 不一定是资料行，也可能是消息行 ——
    消息正文比群名长，"取最长"会取到正文头上。但名字在每行资料里
    都出现，正文各不相同，所以按出现次数投票。超长串（>40字）先
    过滤，基本不可能是名字。并列都只有 1 次时取最短的，超过 40 字
    就认怂回空串，不瞎猜。

    内存里没有资料行时，投出来的可能是某条重复出现的消息正文
    （"好的，谢谢"这种）。wxid 归属是对的（票都是这个群名下的），
    只是显示名不对 —— 读一次历史（面板标题校准）或手动备注一次
    就能纠正。带控制符/换行的直接不要，宁可显示编号。
    """
    from collections import Counter

    pool = [text.strip() for text in candidates
            if isinstance(text, str) and text.strip() and len(text.strip()) <= 40
            and "<" not in text and ">" not in text
            and not text.strip().lower().startswith("http")
            and _is_clean_name(text.strip())
            and not re.search(r"\.(pdf|doc|xls|ppt|zip|htm|mp4|mp3)$",
                              text.strip().lower())]
    if not pool:
        return ""
    ranked = Counter(pool).most_common()
    top_count = ranked[0][1]
    tied = sorted([text for text, count in ranked if count == top_count], key=len)
    return tied[0]


def resolve_contact_name(wxid: str) -> str:
    """wxid → 显示名（备注/昵称里最长的中文串，拼音索引不要）。

    资料 record 里备注和拼音缩写混在一起：含中文的才算名字，
    都没有就回空串，调用方直接显示 wxid。
    同一进程内记住结果：一条群几百条消息别扫几百遍内存。
    """
    anchor = (wxid or "").strip().encode("ascii", errors="ignore")
    if not anchor:
        return ""
    if anchor.decode("ascii") in _NAME_CACHE:
        return _NAME_CACHE[anchor.decode("ascii")]
    kernel32 = ctypes.windll.kernel32
    scanned = 0
    candidates: list[str] = []
    for pid in _weixin_pids():
        handle = _open_process(pid)
        if not handle:
            continue
        try:
            for _, chunk in _iter_regions(handle):
                scanned += len(chunk)
                if scanned > SCAN_BUDGET_BYTES:
                    break
                pos = 0
                while True:
                    pos = chunk.find(anchor, pos)
                    if pos == -1:
                        break
                    found = find_record_before(chunk, pos)
                    pos += len(anchor)
                    if found is None:
                        continue
                    _, fields = found
                    for kind, value in fields:
                        if kind == "text" and isinstance(value, str) and _has_cjk(value):
                            candidates.append(value)
        finally:
            close_quietly(kernel32, handle)
    best = _vote_name(candidates)
    _NAME_CACHE[anchor.decode("ascii")] = best
    return best


def find_conversations() -> list[str]:
    """扫内存里的群 wxid 清单（`xxx@chatroom`）。只读内存。"""
    return sorted(_map_group_names().keys())


def _map_group_names() -> dict[str, str]:
    """一次扫描建群 wxid → 显示名映射（备注名优先拿最长的中文串）。

    单遍是因为全扫描一次就够慢了，不能一个群扫一遍。
    名字解不出就空串，调用方显示 wxid。
    """
    kernel32 = ctypes.windll.kernel32
    candidates: dict[str, list[str]] = {}
    attempted: dict[str, int] = {}
    scanned = 0
    for pid in _weixin_pids():
        handle = _open_process(pid)
        if not handle:
            continue
        try:
            for _, chunk in _iter_regions(handle):
                scanned += len(chunk)
                if scanned > SCAN_BUDGET_BYTES:
                    break
                pos = 0
                while True:
                    match = _WXID_PATTERN.search(chunk, pos)
                    if not match:
                        break
                    pos = match.end()
                    wxid = match.group(0).decode("ascii", errors="ignore")
                    # 没解析过的先占位，保证名单齐全（名解不出显示 wxid）。
                    candidates.setdefault(wxid, [])
                    # 同一个群最多试 20 处：资料 record 藏在几十次消息命中之后
                    # （真机实测首个中文名出现在第 39 次），5 次根本够不着；
                    # 20 次后收益骤减（25→30 个），再多就是烧时间。
                    if attempted.get(wxid, 0) >= 20:
                        continue
                    attempted[wxid] = attempted.get(wxid, 0) + 1
                    found = find_record_before(chunk, match.start())
                    if found is None:
                        continue
                    _, fields = found
                    bucket = candidates[wxid]
                    for kind, value in fields:
                        if kind == "text" and isinstance(value, str) and _has_cjk(value):
                            bucket.append(value)
        finally:
            close_quietly(kernel32, handle)
    return {wxid: _vote_name(texts) for wxid, texts in candidates.items()}


def list_group_conversations() -> list[tuple[str, str]]:
    """内存群列表 [(显示名, wxid)]。名解不出就用 wxid 顶上，保证可点可读。"""
    return [(name or wxid, wxid) for wxid, name in sorted(_map_group_names().items())]


_TEXT_STAMP_PATTERN = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?")
#: 不带年的（通知行常见 "02-13 17:58"），按当年算，跨年误差认了。
_TEXT_STAMP_SHORT_PATTERN = re.compile(
    r"(?<!\d)(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?")


def _timestamp_from_texts(texts: list) -> int:
    """从文本字段里抠时间（"2026-10-12 15:20"这种）。抠不出返回 0。"""
    from datetime import datetime

    for text in texts:
        if not isinstance(text, str):
            continue
        match = _TEXT_STAMP_PATTERN.search(text)
        if match:
            parts = [int(part or 0) for part in match.groups()]
        else:
            short = _TEXT_STAMP_SHORT_PATTERN.search(text)
            if not short:
                continue
            year = datetime.now().astimezone().year
            parts = [year] + [int(part or 0) for part in short.groups()]
        try:
            moment = datetime(*parts).astimezone()
            epoch = int(moment.timestamp())
        except Exception:
            continue
        if 1577836800 <= epoch <= 1893456000:
            return epoch
    return 0


def _message_from_fields(group_wxid: str, fields: list) -> LocalMessage | None:
    """字段表提炼成消息。认得出发送人/内容/时间才算一条，缺的留空。

    内容必须有中文，或者（有发送人且有时间且长度够）—— 纯英文也可能是
    真消息，但",0,0,0"这种碎行一定不是。碎行在锚点附近很多，
    不拦的话捡回来一半是垃圾。
    """
    texts = [value for kind, value in fields if kind == "text" and value]
    ints = [value for kind, value in fields if kind == "int" and isinstance(value, int)]
    if not texts:
        return None
    sender = ""
    for text in texts:
        if text == group_wxid:
            continue
        if text.endswith("@chatroom") or text.startswith(("wxid_", "gh_")):
            sender = text
            break
    if "</" in sender or ">" in sender:
        # 成员 XML 碎片（TA[Lenan ...]</nickname>）冒充的发送人，不要。
        sender = ""
    timestamp = 0
    for value in ints:
        # 秒级（2020~2030）或毫秒级时间戳
        if 1577836800 <= value <= 1893456000:
            timestamp = value
            break
        if 1577836800000 <= value <= 1893456000000:
            timestamp = value // 1000
            break
    if not timestamp:
        # 有些记录把时间存成文本（"2026-10-12 15:20"），整数位是别的。
        timestamp = _timestamp_from_texts(texts)
    bodies = [text for text in texts
              if text != group_wxid and not _looks_like_identifier(text)
              and not _looks_like_snippet(text)]
    content = ""
    kind = "文本"
    cjk = [text for text in bodies if _has_cjk(text)]
    if cjk:
        content = max(cjk, key=len)
    elif sender and timestamp:
        long_enough = [text for text in bodies if len(text.strip()) >= 2]
        if long_enough:
            content = max(long_enough, key=len)
    media: dict[str, str] = {}
    for text in texts:
        if "<img" not in text.lower() and "<msg" not in text.lower():
            continue
        try:
            from .wcdb import _media_metadata

            media = _media_metadata(text)
        except Exception:
            media = {}
        if media.get("md5") or media.get("cdnthumburl") or media.get("cdnmidimgurl"):
            kind = "图片"
            content = ""
            break
    if not content and kind != "图片":
        # 系统通知不要归属：有字样就留，没发送人没时间也留。
        system_texts = [text for text in texts
                        if text != group_wxid and is_system_notice(text)]
        if system_texts:
            content = max(system_texts, key=len)
            kind = "系统通知"
    if not content and kind != "图片":
        return None
    if is_system_notice(content):
        kind = "系统通知"
    return LocalMessage(group_wxid=group_wxid, sender_wxid=sender,
                        content=content, timestamp=timestamp, kind=kind,
                        media=media)


def read_conversation(group_wxid: str, limit: int = 200) -> list[LocalMessage]:
    """读指定群在内存里的消息（从新往旧捡到够数或扫完）。只读内存。

    读不到、读不全都正常返回已捡到的 —— 调用方按"没读全"处理，
    别当"没有消息"。内存里只有最近碰过的页，目标群得多翻翻。
    """
    kernel32 = ctypes.windll.kernel32
    anchor = group_wxid.encode("ascii", errors="ignore")
    if not anchor:
        return []
    messages: list[LocalMessage] = []
    seen: set[tuple[str, int, str]] = set()
    scanned = 0
    for pid in _weixin_pids():
        handle = _open_process(pid)
        if not handle:
            continue
        try:
            for _, chunk in _iter_regions(handle):
                scanned += len(chunk)
                if scanned > SCAN_BUDGET_BYTES or len(messages) >= limit:
                    break
                pos = 0
                while len(messages) < limit:
                    pos = chunk.find(anchor, pos)
                    if pos == -1:
                        break
                    found = find_record_before(chunk, pos)
                    pos += len(anchor)
                    if found is None:
                        continue
                    _, fields = found
                    message = _message_from_fields(group_wxid, fields)
                    if message is None:
                        continue
                    key = (message.sender_wxid, message.timestamp, message.content)
                    if key in seen:
                        continue
                    seen.add(key)
                    messages.append(message)
        finally:
            close_quietly(kernel32, handle)
        if len(messages) >= limit:
            break
    return messages
