"""本地库解密探针二（自动版）：扫内存找 salt，取其后 32 字节当 key，
逐个试解密 page1（AES-256-CBC，IV 在保留区里换位置试）。

只读拷贝文件 + 只读内存，不碰原库，没有写操作。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

sys.path.insert(0, "src")

from Crypto.Cipher import AES  # noqa: E402

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "localdb_spike", str(__import__("pathlib").Path(__file__).resolve().parent / "localdb_spike.py"))
_spike = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_spike)

MEM_COMMIT = _spike.MEM_COMMIT
PAGE_EXECUTE_READ = _spike.PAGE_EXECUTE_READ
PAGE_EXECUTE_READWRITE = _spike.PAGE_EXECUTE_READWRITE
PAGE_GUARD = _spike.PAGE_GUARD
PAGE_READONLY = _spike.PAGE_READONLY
PAGE_READWRITE = _spike.PAGE_READWRITE
PAGE_WRITECOPY = _spike.PAGE_WRITECOPY
READABLE = _spike.READABLE
MEMORY_BASIC_INFORMATION = _spike.MEMORY_BASIC_INFORMATION
_weixin_pids = _spike._weixin_pids

DB_COPY = r"C:\Users\almz7\AppData\Local\Temp\opencode\msg0.db"
PAGE_SIZE = 4096
RESERVE = 80
USABLE = PAGE_SIZE - RESERVE


def _salt_offsets(pid: int, salt: bytes) -> list[tuple[int, bytes]]:
    """返回 [(命中地址, 后面 64 字节), ...]。"""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not handle:
        return []
    hits: list[tuple[int, bytes]] = []
    address = 0
    mbi = MEMORY_BASIC_INFORMATION()
    try:
        while address < 0x7FFFFFFFFFFF:
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
                    pos = chunk.find(salt)
                    while pos != -1:
                        after = chunk[pos + len(salt):pos + len(salt) + 64]
                        hits.append((base + pos, after))
                        pos = chunk.find(salt, pos + 1)
            address = (base or address) + region_size
    finally:
        kernel32.CloseHandle(handle)
    return hits


def _try_keys(data: bytes, keys: list[bytes]) -> bool:
    page1 = data[:PAGE_SIZE]
    ciphertext = page1[:USABLE]
    reserve = page1[USABLE:]
    ivs = {
        "reserve[:16]": reserve[:16],
        "reserve[16:32]": reserve[16:32],
        "reserve[-16:]": reserve[-16:],
    }
    for ki, key in enumerate(keys):
        if len(key) != 32:
            continue
        for name, iv in ivs.items():
            try:
                plain = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)
            except Exception:
                continue
            if plain.startswith(b"SQLite format 3\x00"):
                print(f"*** 命中 key#{ki} + {name} ***")
                print(f"    前 96 字节：{plain[:96]!r}")
                return True
    return False


def main() -> int:
    with open(DB_COPY, "rb") as handle:
        data = handle.read()
    salt = data[:16]
    print(f"库拷贝 {len(data)} 字节，salt={salt.hex()}")

    keys: list[bytes] = []
    for pid in _weixin_pids():
        for address, after in _salt_offsets(pid, salt):
            if len(after) >= 32:
                keys.append(after[:32])
                print(f"  pid={pid} addr=0x{address:x} 后32字节={after[:32].hex()}")
    print(f"共 {len(keys)} 个候选 key，开始试解密…")
    if _try_keys(data, keys):
        return 0
    print("都没中：key 不在 salt 后面，或版式假设不对")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
