"""本地库探针三：carve 内存里的解密 SQLite 页。

只读内存，不注入不修改。统计：
1. `SQLite format 3` 页头出现次数（缓存的 page1）；
2. `CREATE TABLE` 出现次数（缓存的 schema 页）；
3. 群名（UTF-8 与 UTF-16 两版）在内存里出现几次 —— 出现即表示
   消息正文正躺在内存里，可捡。
"""
from __future__ import annotations

import ctypes
import importlib.util as _ilu
from pathlib import Path

_spec = _ilu.spec_from_file_location(
    "localdb_spike", str(Path(__file__).resolve().parent / "localdb_spike.py"))
_spike = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_spike)

GROUP_UTF8 = "锦绣中华".encode("utf-8")
GROUP_UTF16 = "锦绣中华".encode("utf-16-le")


def _scan(pid: int) -> dict:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
    stats = {"mb": 0, "sqlite_magic": 0, "create_table": 0,
             "group_utf8": 0, "group_utf16": 0, "samples": []}
    if not handle:
        return stats
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
                    stats["sqlite_magic"] += chunk.count(b"SQLite format 3\x00")
                    stats["create_table"] += chunk.count(b"CREATE TABLE")
                    stats["group_utf8"] += chunk.count(GROUP_UTF8)
                    stats["group_utf16"] += chunk.count(GROUP_UTF16)
                    if GROUP_UTF8 in chunk and len(stats["samples"]) < 3:
                        pos = chunk.find(GROUP_UTF8)
                        blob = chunk[max(0, pos - 120):pos + 200]
                        text = "".join(chr(b) if 32 <= b < 127 or b >= 128 else "·"
                                       for b in blob)
                        stats["samples"].append(text)
            address = (base or address) + region_size
    finally:
        kernel32.CloseHandle(handle)
    stats["mb"] //= 1024 * 1024
    return stats


def main() -> int:
    for pid in _spike._weixin_pids():
        stats = _scan(pid)
        print(f"pid={pid}: {stats['mb']}MB magic={stats['sqlite_magic']} "
              f"create_table={stats['create_table']} "
              f"群名utf8={stats['group_utf8']} utf16={stats['group_utf16']}")
        for sample in stats["samples"]:
            print(f"    上下文：{sample[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
