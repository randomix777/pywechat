"""本地库探针五：4K 对齐扫 b-tree 叶页，盲解析 cell 捞文本。

只读内存。页头校验：首字节 0x0D（表叶）/0x05（表内）/0x0A（索引叶），
cell 数 1..2000，指针数组每个 <4096 且 >=8（叶页除外页1）。
cell 解析只做 varint + serial，走 TEXT（奇数 serial>=13）与整数，
拼不出完整 record 的直接扔 —— 先看能捞出什么，再写针对性解析。
"""
from __future__ import annotations

import ctypes
import importlib.util as _ilu
import struct
from pathlib import Path

_spec = _ilu.spec_from_file_location(
    "localdb_spike", str(Path(__file__).resolve().parent / "localdb_spike.py"))
_spike = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_spike)

PAGE = 4096


def _varint(data: bytes, pos: int):
    result = 0
    for i in range(9):
        if pos + i >= len(data):
            return None, pos
        byte = data[pos + i]
        if i == 8:
            result = (result << 8) | byte
            return result, pos + 9
        result = (result << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return result, pos + i + 1
    return None, pos


def _parse_record(payload: bytes):
    """返回 (texts, ints)。解析失败返回 None。"""
    header_len, pos = _varint(payload, 0)
    if header_len is None or header_len <= 0 or header_len > len(payload):
        return None
    serials = []
    end = header_len
    while pos < end:
        serial, pos = _varint(payload, pos)
        if serial is None:
            return None
        serials.append(serial)
    values_text, values_int = [], []
    for serial in serials:
        if serial == 0:
            values_text.append("")
        elif serial <= 4:
            size = serial
            values_int.append(int.from_bytes(payload[pos:pos + size], "big", signed=False))
            pos += size
        elif serial == 5:
            values_int.append(int.from_bytes(payload[pos:pos + 6], "big", signed=False))
            pos += 6
        elif serial == 6:
            values_int.append(int.from_bytes(payload[pos:pos + 8], "big", signed=False))
            pos += 8
        elif serial == 7:
            values_int.append(int.from_bytes(payload[pos:pos + 8], "big", signed=True) / 1.0)
            pos += 8
        elif serial in (8, 9):
            values_int.append(0 if serial == 8 else 1)
        elif serial >= 12:
            if serial % 2 == 0:
                size = (serial - 12) // 2
                pos += size
            else:
                size = (serial - 13) // 2
                raw = payload[pos:pos + size]
                pos += size
                try:
                    text = raw.decode("utf-8")
                except Exception:
                    continue
                values_text.append(text)
        else:
            return None
    return values_text, values_int


def _looks_like_leaf(page: bytes) -> bool:
    if len(page) < 16 or page[0] != 0x0D:
        return False
    cell_count = struct.unpack(">H", page[3:5])[0]
    if not 1 <= cell_count <= 2000:
        return False
    free_start = struct.unpack(">H", page[1:3])[0]
    if free_start != 0 and (free_start >= PAGE or free_start < 8):
        return False
    for i in range(min(cell_count, 8)):
        ptr = struct.unpack(">H", page[8 + 2 * i:10 + 2 * i])[0]
        if ptr >= PAGE or ptr < 8:
            return False
    return True


def _cells(page: bytes):
    cell_count = struct.unpack(">H", page[3:5])[0]
    out = []
    for i in range(cell_count):
        ptr = struct.unpack(">H", page[8 + 2 * i:10 + 2 * i])[0]
        if ptr + 2 > len(page):
            continue
        payload_len, pos = _varint(page, ptr)
        if payload_len is None or pos + payload_len > len(page):
            continue
        _rowid, pos = _varint(page, pos)
        if _rowid is None:
            continue
        parsed = _parse_record(page[pos:pos + payload_len])
        if parsed is not None:
            out.append(parsed)
    return out


def _scan(pid: int):
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
    stats = {"mb": 0, "pages": 0, "cells": 0}
    hits: list[tuple[list, list]] = []
    if not handle:
        return stats, hits
    address = 0
    scanned = 0
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
                    stats["mb"] += nread.value
                    for off in range(0, len(chunk) - PAGE, PAGE):
                        page = chunk[off:off + PAGE]
                        if _looks_like_leaf(page):
                            stats["pages"] += 1
                            for texts, ints in _cells(page):
                                stats["cells"] += 1
                                interesting = [t for t in texts if len(t) >= 2][:6]
                                if interesting and len(hits) < 40:
                                    hits.append((interesting, ints[:8]))
            address = (base or address) + region_size
    finally:
        kernel32.CloseHandle(handle)
    stats["mb"] //= 1024 * 1024
    return stats, hits


def main() -> int:
    for pid in _spike._weixin_pids():
        stats, hits = _scan(pid)
        print(f"pid={pid}: {stats['mb']}MB 叶页={stats['pages']} cell={stats['cells']}")
        for texts, ints in hits[:15]:
            preview = " | ".join(t[:40].replace("\n", "⏎") for t in texts[:4])
            print(f"  文本={preview}")
            print(f"  整数={ints}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
