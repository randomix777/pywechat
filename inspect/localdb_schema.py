"""本地库探针四：从内存捡 CREATE TABLE，学表结构。

只读内存。schema 是连续 ASCII，好捡；先知道表名列名，
下一步才能把 leaf 页里的 cell 对上意义。
"""
from __future__ import annotations

import ctypes
import importlib.util as _ilu
import re
from pathlib import Path

_spec = _ilu.spec_from_file_location(
    "localdb_spike", str(Path(__file__).resolve().parent / "localdb_spike.py"))
_spike = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_spike)

START = re.compile(rb"CREATE TABLE [\w\[\]\"]+")


def _scan(pid: int) -> set[bytes]:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
    found: set[bytes] = set()
    if not handle:
        return found
    scanned = 0
    address = 0
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
                    for match in START.finditer(chunk):
                        stmt = chunk[match.start():match.start() + 8192]
                        end = stmt.find(b");")
                        if end != -1:
                            found.add(stmt[:end + 2])
            address = (base or address) + region_size
    finally:
        kernel32.CloseHandle(handle)
    return found


def main() -> int:
    stmts: set[bytes] = set()
    for pid in _spike._weixin_pids():
        stmts |= _scan(pid)
    print(f"共 {len(stmts)} 条建表语句去重后：")
    for stmt in sorted(stmts):
        try:
            text = stmt.decode("utf-8", errors="replace")
        except Exception:
            continue
        name = text[13:60].split("(")[0].strip()
        print(f"\n--- {name} ---")
        print(text[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
