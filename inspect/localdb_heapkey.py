"""微信 4.x 本地库取 key 重试探针（只读内存 + 只读拷贝）。

背景（docs/sender-recognition-findings.md 补记四）：
微信把 SQLCipher pragma 字符串编进了二进制（4096 页 / PBKDF2-SHA512），
salt 躺在内存的 schema 页里，但 raw key 没有 `x'hex'` ASCII 形态，
4.1.15.55 上全私有堆 52.7 万高熵候选直接试解无命中。

微信一升级就重跑这个：`python inspect/localdb_heapkey.py <db拷贝>`，
命中会直接打出 key。page1 切片是关键 —— 前 16 字节是明文 salt，
密文从偏移 16 开始（之前探针把 salt 当密文解，永远对不上）。

只读：不写微信内存、不写原库文件。
"""
from __future__ import annotations

import ctypes
import math
import pathlib
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from Crypto.Cipher import AES  # noqa: E402

from wechat_ai_assistant import localdb  # noqa: E402

MEM_PRIVATE = 0x20000
MAGIC = b"SQLite format 3\x00"


def _entropy(block: bytes) -> float:
    if len(set(block)) < 24:
        return 0.0
    n = len(block)
    return -sum((c / n) * math.log2(c / n) for c in Counter(block).values())


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print('用法：localdb_heapkey.py <db拷贝路径>')
        return 2
    data = pathlib.Path(args[0]).read_bytes()
    PAGE, RESERVE = 4096, 80
    page1 = data[:PAGE]
    reserve = page1[PAGE - RESERVE:PAGE]
    ct = page1[16:PAGE - RESERVE]
    assert len(ct) % 16 == 0
    print(f"库 {len(data)} 字节，salt={data[:16].hex()}", flush=True)

    ivs = [reserve[:16], reserve[16:32], reserve[-16:]]
    kernel32 = ctypes.windll.kernel32
    tested = 0
    seen: set[bytes] = set()
    for pid in localdb._weixin_pids():
        handle = localdb._open_process(pid)
        if not handle:
            continue
        try:
            addr = 0
            mbi = localdb._MemoryBasicInformation()
            while addr < 0x7FFFFFFFFFFF:
                ok = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr),
                                             ctypes.byref(mbi), ctypes.sizeof(mbi))
                if not ok:
                    break
                size = int(mbi.RegionSize)
                base = int(mbi.BaseAddress)
                addr = base + size
                if not (mbi.State == localdb.MEM_COMMIT
                        and (mbi.Protect & localdb._READABLE)
                        and not (mbi.Protect & localdb._PAGE_GUARD)
                        and mbi.Type == MEM_PRIVATE
                        and size <= 256 * 1024 * 1024):
                    continue
                buf = ctypes.create_string_buffer(size)
                nread = ctypes.c_size_t(0)
                if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(base),
                                                 buf, size, ctypes.byref(nread)):
                    continue
                chunk = buf.raw[:nread.value]
                for off in range(0, len(chunk) - 32, 8):
                    block = chunk[off:off + 32]
                    if block in seen or _entropy(block) < 4.75:
                        continue
                    seen.add(block)
                    tested += 1
                    for iv in ivs:
                        try:
                            pt = AES.new(block, AES.MODE_CBC, iv).decrypt(ct)
                        except Exception:
                            continue
                        if pt.startswith(MAGIC):
                            print(f"*** KEY={block.hex()}", flush=True)
                            return 0
                if tested % 200000 == 0 and tested:
                    print(f"  …已试 {tested} 个", flush=True)
        finally:
            localdb.close_quietly(kernel32, handle)
    print(f"扫完，共试 {tested} 个，无命中（key 不在私有堆连续 32B 里，或 IV/页参数变了）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
