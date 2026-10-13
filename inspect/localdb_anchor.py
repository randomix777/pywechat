"""本地库探针六：锚点倒推 record。

已知群名（UTF-8）在内存里出现 50+ 次。从每次出现往回最多 2KB，
逐字节试当 record 头：varint 头长 + serials 自洽 + TEXT 段覆盖锚点
且严格 UTF-8 可解。命中即打印整条 record 的全部字段。
"""
from __future__ import annotations

import ctypes
import importlib.util as _ilu
from pathlib import Path

_spec = _ilu.spec_from_file_location(
    "localdb_spike", str(Path(__file__).resolve().parent / "localdb_spike.py"))
_spike = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_spike)

ANCHOR = "锦绣中华".encode("utf-8")
ANCHORS = [
    "锦绣中华".encode("utf-8"),
    "6255144087@chatroom".encode("utf-8"),
]


def _varint(data: bytes, pos: int):
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


def _try_record(chunk: bytes, start: int, anchor_abs: int):
    """chunk[start:] 当 record 头试解析。返回字段表或 None。"""
    header_len, pos = _varint(chunk, start)
    if header_len is None or not 2 <= header_len <= 200:
        return None
    serials = []
    rel = pos - start
    while rel < header_len:
        serial, pos = _varint(chunk, pos)
        if serial is None or serial in (10, 11):
            return None
        serials.append(serial)
        rel = pos - start
    if rel != header_len or not serials:
        return None
    fields = []
    covered = False
    for serial in serials:
        if serial == 0:
            fields.append(("null", None))
        elif serial <= 6:
            size = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}[serial]
            fields.append(("int", int.from_bytes(chunk[pos:pos + size], "big", signed=(serial == 6))))
            pos += size
        elif serial == 7:
            fields.append(("float", None))
            pos += 8
        elif serial in (8, 9):
            fields.append(("int", 0 if serial == 8 else 1))
        elif serial >= 12:
            if serial % 2 == 0:
                pos += (serial - 12) // 2
                fields.append(("blob", None))
            else:
                size = (serial - 13) // 2
                if size > 65536:
                    return None
                raw = chunk[pos:pos + size]
                try:
                    text = raw.decode("utf-8")
                    text.encode("utf-8")
                    if "\ud800" <= max(text, default="") <= "\udfff":
                        return None
                except Exception:
                    return None
                if pos <= anchor_abs < pos + size:
                    covered = True
                fields.append(("text", text))
                pos += size
        else:
            return None
    if not covered:
        return None
    return fields


def _scan_region(chunk: bytes, base: int, anchor: bytes, back: int = 768, limit: int = 30):
    """返回命中的 (record起始, 字段表)。"""
    out = []
    pos = 0
    while True:
        pos = chunk.find(anchor, pos)
        if pos == -1:
            break
        for start in range(max(0, pos - back), pos):
            # 预过滤：record 头第一个 varint 是头长，小数字单字节。
            # 不是就连解析都不试，2000 次里只剩几十次真解析。
            if chunk[start] == 0 or chunk[start] >= 0x40:
                continue
            fields = _try_record(chunk, start, pos)
            if fields is not None:
                out.append((base + start, fields))
                if len(out) >= limit:
                    return out
                break
        pos += len(anchor)
    return out


def main() -> int:
    kernel32 = ctypes.windll.kernel32
    total_regions = 0
    shown = 0
    for pid in _spike._weixin_pids():
        print(f"开始扫 pid={pid}…", flush=True)
        handle = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not handle:
            print(f"pid={pid} 打不开，跳过", flush=True)
            continue
        address = 0
        scanned = 0
        regions = 0
        mbi = _spike.MEMORY_BASIC_INFORMATION()
        try:
            while address < 0x7FFFFFFFFFFF and scanned < 2048 * 1024 * 1024:
                ret = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(address),
                                              ctypes.byref(mbi), ctypes.sizeof(mbi))
                if not ret:
                    break
                region_size = int(mbi.RegionSize)
                base = int(mbi.BaseAddress) if isinstance(mbi.BaseAddress, int) else mbi.BaseAddress
                if (mbi.State == _spike.MEM_COMMIT and (mbi.Protect & _spike.READABLE)
                        and not (mbi.Protect & _spike.PAGE_GUARD)
                        and region_size <= 256 * 1024 * 1024):
                    buffer = ctypes.create_string_buffer(region_size)
                    nread = ctypes.c_size_t(0)
                    if kernel32.ReadProcessMemory(handle, ctypes.c_void_p(base),
                                                  buffer, region_size, ctypes.byref(nread)):
                        chunk = buffer.raw[:nread.value]
                        scanned += nread.value
                        regions += 1
                        if regions % 200 == 0:
                            print(f"  pid={pid} 已扫 {scanned // 1024 // 1024}MB/{regions}区…",
                                  flush=True)
                        if any(a in chunk for a in ANCHORS):
                            total_regions += 1
                            for anchor in ANCHORS:
                                if anchor not in chunk:
                                    continue
                                print(f"pid={pid} 在 0x{base:x} 找到锚点 {anchor[:20]!r}，倒推中…",
                                      flush=True)
                                for start, fields in _scan_region(chunk, base, anchor):
                                    shown += 1
                                    if shown > 12:
                                        return 0
                                    print(f"--- record @ 0x{start:x}（{len(fields)} 字段）---",
                                          flush=True)
                                    for kind, value in fields:
                                        if kind == "text":
                                            preview = str(value)[:200].replace("\n", "⏎")
                                            print(f"  text: {preview!r}", flush=True)
                                        else:
                                            print(f"  {kind}: {value}", flush=True)
                address = (base or address) + region_size
        finally:
            kernel32.CloseHandle(handle)
    print(f"含锚点的内存区：{total_regions} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
