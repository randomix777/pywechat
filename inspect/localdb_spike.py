"""本地库解密探针一：扫内存找 key，对上库文件 salt。

只做三件事，全是只读：
1. 读自己用户权限能读的 Weixin.exe 内存，找 `x'<64hex><32hex>'` 特征；
2. 把候选里的 salt 和 message_0.db 拷贝头 16 字节比对；
3. 不解密、不改任何东西，只报告"第几个候选对上了"。

跑法（管理员权限不需要，同用户即可）：
    .\\.venv\\Scripts\\python.exe inspect\\localdb_spike.py
"""
from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes

DB_COPY = r"C:\Users\almz7\AppData\Local\Temp\opencode\msg0.db"

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_GUARD = 0x100
READABLE = PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE

# WCDB 缓存的 raw key 格式：x'<64hex enc_key><32hex salt>'（ASCII 明文躺在内存里）
KEY_PATTERN = re.compile(rb"x'([0-9a-fA-F]{64})([0-9a-fA-F]{32})'")
# Qt 系内存里常见 UTF-16：x'6D...' 每个字符后跟 \x00
KEY_PATTERN_UTF16 = re.compile(
    rb"x\x00'\x00([0-9a-fA-F]\x00){64}'\x00([0-9a-fA-F]\x00){32}'\x00")


def _clean_utf16(raw: bytes) -> str:
    return raw.decode("utf-16-le", errors="replace").replace("\x00", "")


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", wintypes.LPVOID),
        ("AllocationBase", wintypes.LPVOID),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def _weixin_pids() -> list[int]:
    import psutil

    return [proc.info["pid"] for proc in psutil.process_iter(["pid", "name"])
            if (proc.info["name"] or "").lower() == "weixin.exe"]


def _scan_process(pid: int, limit_mb: int = 2048, salt: bytes = b"") -> set[tuple[str, str]]:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        print(f"  pid={pid} 打不开（权限），跳过")
        return set()
    found: set[tuple[str, str]] = set()
    salt_hits = 0
    scanned = 0
    address = 0
    mbi = MEMORY_BASIC_INFORMATION()
    try:
        while address < 0x7FFFFFFFFFFF and scanned < limit_mb * 1024 * 1024:
            ret = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(address),
                                          ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not ret:
                break
            region_size = int(mbi.RegionSize)
            base = int(mbi.BaseAddress) if isinstance(mbi.BaseAddress, int) else mbi.BaseAddress
            if (mbi.State == MEM_COMMIT and (mbi.Protect & READABLE)
                    and not (mbi.Protect & PAGE_GUARD) and region_size <= 256 * 1024 * 1024):
                buffer = ctypes.create_string_buffer(region_size)
                nread = ctypes.c_size_t(0)
                if kernel32.ReadProcessMemory(handle, ctypes.c_void_p(base),
                                              buffer, region_size, ctypes.byref(nread)):
                    chunk = buffer.raw[:nread.value]
                    for match in KEY_PATTERN.finditer(chunk):
                        found.add((match.group(1).decode(), match.group(2).decode()))
                    for match in KEY_PATTERN_UTF16.finditer(chunk):
                        found.add((_clean_utf16(match.group(1)), _clean_utf16(match.group(2))))
                    # 库文件 salt 原样躺在内存里吗？躺着就说明 salt 假设是对的，
                    # key 一定在它附近（下一步再抠位置）。
                    if salt:
                        pos = chunk.find(salt)
                        while pos != -1:
                            salt_hits += 1
                            if salt_hits <= 6:
                                start = max(0, pos - 128)
                                blob = chunk[start:pos + 144]
                                print(f"    salt 命中 #{salt_hits}（前后 128 字节）：\n    {blob.hex()}")
                            pos = chunk.find(salt, pos + 1)
                    # 裸奔版：没有 x'' 包裹的 96 hex 连排（误报多，只计数不采信）
                    scanned += nread.value
            address = (base or address) + region_size
    finally:
        kernel32.CloseHandle(handle)
    print(f"  pid={pid} 扫了约 {scanned // 1024 // 1024} MB，找到 {len(found)} 个候选，salt 原样命中 {salt_hits} 次")
    return found


def main() -> int:
    with open(DB_COPY, "rb") as handle:
        file_salt = handle.read(16).hex()
    print(f"库文件头 16 字节（salt 候选）：{file_salt}")
    salt_bytes = bytes.fromhex(file_salt)

    pids = _weixin_pids()
    print(f"Weixin.exe 进程：{pids}")
    candidates: set[tuple[str, str]] = set()
    for pid in pids:
        candidates |= _scan_process(pid, salt=salt_bytes)

    hits = [(key, salt) for key, salt in candidates if salt.lower() == file_salt.lower()]
    print(f"\n共 {len(candidates)} 个候选，其中 salt 对上库文件的：{len(hits)} 个")
    for key, salt in hits:
        print(f"  HIT enc_key={key[:16]}…{key[-8:]} salt={salt}")
    if not hits and candidates:
        print("salt 没对上：把前 3 个候选的 salt 列出来备查：")
        for _, salt in sorted(candidates)[:3]:
            print(f"  salt={salt}")
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
