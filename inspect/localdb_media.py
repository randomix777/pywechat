"""本地库探针七：视频/语音标记在内存里有没有。

只计数不解析：<type>43 = 视频，<type>34 = 语音，
voicelength / videolength / voice / silk 关键字。
"""
from __future__ import annotations

import ctypes
import importlib.util as _ilu
from pathlib import Path

_spec = _ilu.spec_from_file_location(
    "localdb_spike", str(Path(__file__).resolve().parent / "localdb_spike.py"))
_spike = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_spike)

MARKERS = {
    "video type43": b"<type>43</type>",
    "voice type34": b"<type>34</type>",
    "voicelength": b"voicelength",
    "videolength": b"videolength",
    "silk magic": b"#!SILK_V3",
    "voice word": b"voice",
}


def main() -> int:
    kernel32 = ctypes.windll.kernel32
    totals = {name: 0 for name in MARKERS}
    for pid in _spike._weixin_pids():
        handle = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not handle:
            continue
        counts = {name: 0 for name in MARKERS}
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
                        for name, marker in MARKERS.items():
                            counts[name] += chunk.count(marker)
                address = (base or address) + region_size
        finally:
            kernel32.CloseHandle(handle)
        print(f"pid={pid}: " + " ".join(f"{name}={counts[name]}" for name in MARKERS))
        for name in MARKERS:
            totals[name] += counts[name]
    print("总计：" + " ".join(f"{name}={totals[name]}" for name in MARKERS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
