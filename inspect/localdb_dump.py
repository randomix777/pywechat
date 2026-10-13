"""salt 命中点周围 ±512 字节 hexdump，找 key 藏在哪。"""
from __future__ import annotations

import ctypes
import importlib.util as _ilu
from pathlib import Path

_spec = _ilu.spec_from_file_location(
    "localdb_spike", str(Path(__file__).resolve().parent / "localdb_spike.py"))
_spike = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_spike)

DB_COPY = r"C:\Users\almz7\AppData\Local\Temp\opencode\msg0.db"


def main() -> int:
    with open(DB_COPY, "rb") as handle:
        salt = handle.read(16)
    import ctypes as _ct
    kernel32 = _ct.windll.kernel32
    shown = 0
    for pid in _spike._weixin_pids():
        handle = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not handle:
            continue
        address = 0
        mbi = _spike.MEMORY_BASIC_INFORMATION()
        try:
            while address < 0x7FFFFFFFFFFF and shown < 6:
                ret = kernel32.VirtualQueryEx(handle, _ct.c_void_p(address),
                                              _ct.byref(mbi), _ct.sizeof(mbi))
                if not ret:
                    break
                region_size = int(mbi.RegionSize)
                base = int(mbi.BaseAddress) if isinstance(mbi.BaseAddress, int) else mbi.BaseAddress
                if (mbi.State == _spike.MEM_COMMIT and (mbi.Protect & _spike.READABLE)
                        and not (mbi.Protect & _spike.PAGE_GUARD)
                        and region_size <= 256 * 1024 * 1024):
                    buffer = _ct.create_string_buffer(region_size)
                    nread = _ct.c_size_t(0)
                    if kernel32.ReadProcessMemory(handle, _ct.c_void_p(base),
                                                  buffer, region_size, _ct.byref(nread)):
                        chunk = buffer.raw[:nread.value]
                        pos = chunk.find(salt)
                        while pos != -1 and shown < 6:
                            shown += 1
                            start = max(0, pos - 512)
                            blob = chunk[start:pos + 528]
                            print(f"=== 命中 #{shown} 地址偏移 {pos}，前后共 {len(blob)} 字节 ===")
                            for off in range(0, len(blob), 32):
                                row = blob[off:off + 32]
                                text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
                                print(f"  +{start + off - (pos - start):5d} {row.hex()}  {text}")
                            pos = chunk.find(salt, pos + 1)
                address = (base or address) + region_size
        finally:
            kernel32.CloseHandle(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
