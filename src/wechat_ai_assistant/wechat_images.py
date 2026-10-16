"""Decode locally cached WeChat 4.x image files.

V2 image files use a short AES-ECB section followed by a clear section and a
single-byte-XOR section. The AES key is validated against the first encrypted
block before it is ever cached. Windows process inspection is read-only.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import logging
import os
import re
import struct
from collections import Counter
from pathlib import Path
from typing import Iterable

LOGGER = logging.getLogger(__name__)

V2_MAGIC = bytes.fromhex("070856320807")
V1_AES_MAGIC = bytes.fromhex("070856310807")
V1_AES_KEY = b"cfcd208495d565ef"
_IMAGE_SIGNATURES = (
    ("jpg", b"\xff\xd8\xff"),
    ("png", b"\x89PNG\r\n\x1a\n"),
    ("gif", b"GIF8"),
    ("webp", b"RIFF"),
    ("bmp", b"BM"),
    ("wxgf", b"wxgf"),
)
_ASCII_RUN = re.compile(rb"(?<![A-Za-z0-9])([A-Za-z0-9]{16,64})(?![A-Za-z0-9])")
_UTF16_RUN = re.compile(rb"(?<![A-Za-z0-9\x00])((?:[A-Za-z0-9]\x00){16,64})(?![A-Za-z0-9\x00])")
_READABLE = 0x02 | 0x04 | 0x08 | 0x20 | 0x40 | 0x80
_PAGE_GUARD = 0x100
_MEM_COMMIT = 0x1000
_PROCESS_VM_READ = 0x0010
_PROCESS_QUERY_INFORMATION = 0x0400
_SCAN_LIMIT = 2 * 1024 * 1024 * 1024
_READ_CHUNK = 8 * 1024 * 1024
_KEY_CACHE: dict[str, bytes] = {}
_XOR_CACHE: dict[str, int] = {}


class ImageKeyUnavailable(ValueError):
    """No key could be read or validated for a cached V2 image."""


def _aes_decrypt_block(key: bytes, block: bytes) -> bytes:
    from Crypto.Cipher import AES

    return AES.new(key, AES.MODE_ECB).decrypt(block)


def _image_like(data: bytes) -> bool:
    return any(data.startswith(signature) for _ext, signature in _IMAGE_SIGNATURES)


def _v2_probe(path: Path) -> bytes | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(31)
    except OSError:
        return None
    if len(header) < 31 or header[:6] != V2_MAGIC:
        return None
    return header[15:31]


def _v2_files(account_root: Path) -> list[Path]:
    attach = account_root / "msg" / "attach"
    if not attach.is_dir():
        return []
    files = [p for p in attach.rglob("*.dat") if _v2_probe(p)]
    # A thumbnail is usually present even if a full image has not been opened.
    return sorted(files, key=lambda p: (not p.name.lower().endswith("_t.dat"), p.stat().st_size))


def _cache_path() -> Path:
    from .paths import default_data_dir

    return default_data_dir() / "image_keys.json"


def _load_cached_key(account: str) -> bytes | None:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        if data.get("_protected") == "dpapi-v1":
            from .wcdb import _dpapi_blob

            inner = json.loads(_dpapi_blob(base64.b64decode(data["blob"]), protect=False))
        else:
            inner = data
        value = inner.get(account)
        key = value.encode("ascii") if isinstance(value, str) else b""
        return key if len(key) == 16 else None
    except Exception:
        return None


def _persist_key(account: str, key: bytes) -> None:
    """Keep the validated key DPAPI-protected for the current Windows user."""
    try:
        path = _cache_path()
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("_protected") == "dpapi-v1":
                from .wcdb import _dpapi_blob

                previous = json.loads(_dpapi_blob(base64.b64decode(previous["blob"]), protect=False))
        except Exception:
            previous = {}
        previous[account] = key.decode("ascii")
        from .wcdb import _dpapi_blob

        blob = _dpapi_blob(json.dumps(previous).encode("utf-8"), protect=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps({
            "_protected": "dpapi-v1",
            "blob": base64.b64encode(blob).decode("ascii"),
        }), encoding="utf-8")
        os.replace(temp, path)
    except Exception:
        # Decryption remains available for this process even if persistence is
        # unavailable (for example, on a non-Windows development machine).
        LOGGER.debug("Could not persist the validated WeChat image key", exc_info=True)


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_uint32),
        ("PartitionId", ctypes.c_uint16),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_uint32),
        ("Protect", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
    ]


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("th32ModuleID", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("GlblcntUsage", ctypes.c_uint32),
        ("ProccntUsage", ctypes.c_uint32),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", ctypes.c_uint32),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


def _candidate_keys(chunk: bytes) -> Iterable[bytes]:
    seen: set[bytes] = set()
    for match in _ASCII_RUN.finditer(chunk):
        run = match.group(1)
        # Image keys are 16 ASCII bytes. Test windows in a longer token too,
        # since WeChat can keep the key adjacent to other hexadecimal data.
        for offset in range(min(len(run) - 15, 32)):
            candidate = run[offset:offset + 16]
            if candidate not in seen:
                seen.add(candidate)
                yield candidate
    for match in _UTF16_RUN.finditer(chunk):
        run = match.group(1)
        text = run[::2]
        for offset in range(min(len(text) - 15, 32)):
            candidate = text[offset:offset + 16]
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


def _find_weixin_pids() -> list[int]:
    try:
        import psutil

        rows = []
        for process in psutil.process_iter(["pid", "name", "memory_info"]):
            if (process.info.get("name") or "").lower() != "weixin.exe":
                continue
            try:
                rss = process.info["memory_info"].rss
            except Exception:
                rss = 0
            rows.append((rss, int(process.info["pid"])))
        return [pid for _rss, pid in sorted(rows, reverse=True)]
    except Exception:
        return []


def _scan_process_memory(probe: bytes, *, max_bytes: int = _SCAN_LIMIT) -> tuple[bytes | None, int, int]:
    """Find an AES-128 key whose ECB decryption matches a real cached image."""
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        return None, 0, 0
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.VirtualQueryEx.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(_MEMORY_BASIC_INFORMATION), ctypes.c_size_t,
    ]
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.ReadProcessMemory.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    scanned = 0
    processes = 0
    tested: set[bytes] = set()
    for pid in _find_weixin_pids():
        handle = kernel32.OpenProcess(
            _PROCESS_VM_READ | _PROCESS_QUERY_INFORMATION, False, pid)
        if not handle:
            continue
        processes += 1
        try:
            address = 0
            mbi = _MEMORY_BASIC_INFORMATION()
            while address < 0x7FFFFFFFFFFF and scanned < max_bytes:
                queried = kernel32.VirtualQueryEx(
                    handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi))
                if not queried:
                    break
                base = int(mbi.BaseAddress or 0)
                size = int(mbi.RegionSize)
                if size <= 0:
                    break
                protect = int(mbi.Protect) & 0xFF
                if (mbi.State == _MEM_COMMIT and protect & _READABLE
                        and not (int(mbi.Protect) & _PAGE_GUARD)):
                    offset = 0
                    carry = b""
                    while offset < size and scanned < max_bytes:
                        amount = min(_READ_CHUNK, size - offset, max_bytes - scanned)
                        buffer = ctypes.create_string_buffer(amount)
                        read = ctypes.c_size_t(0)
                        ok = kernel32.ReadProcessMemory(
                            handle, ctypes.c_void_p(base + offset), buffer,
                            amount, ctypes.byref(read))
                        if not ok or not read.value:
                            offset += amount
                            scanned += amount
                            carry = b""
                            continue
                        current = buffer.raw[:read.value]
                        scanned += read.value
                        joined = carry + current
                        for candidate in _candidate_keys(joined):
                            if candidate in tested:
                                continue
                            tested.add(candidate)
                            try:
                                if _image_like(_aes_decrypt_block(candidate, probe)):
                                    return candidate, scanned, processes
                            except Exception:
                                continue
                        carry = joined[-96:]
                        offset += read.value
                address = base + size
        finally:
            kernel32.CloseHandle(handle)
    return None, scanned, processes


def _extract_cfg_image_key(account_root: Path, probe: bytes) -> tuple[bytes, int] | None:
    """Derive the V2 image key from the current Weixin config object.

    The pointer offsets match the current 4.1.x config layout. The derived AES
    key is accepted only if the encrypted image block independently validates.
    """
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        return None
    # The account directory may include a local suffix that is not part of
    # the wxid stored in Weixin's config. Bind the runtime config to this cache
    # by checking that its wxid is the exact directory name or its prefix.
    account_name = account_root.name
    if not account_name:
        return None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MODULEENTRY32W)]
    kernel32.Module32FirstW.restype = ctypes.c_int
    kernel32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MODULEENTRY32W)]
    kernel32.Module32NextW.restype = ctypes.c_int
    kernel32.ReadProcessMemory.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    landmark = b"global_config"
    for pid in _find_weixin_pids():
        snapshot = kernel32.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            continue
        module_base = 0
        module_size = 0
        try:
            entry = _MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = kernel32.Module32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                if entry.szModule and entry.szModule.lower() == "weixin.dll":
                    module_base = int(entry.modBaseAddr or 0)
                    module_size = int(entry.modBaseSize)
                    break
                ok = kernel32.Module32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        if not module_base or not module_size or module_size > 0x40000000:
            continue
        handle = kernel32.OpenProcess(
            _PROCESS_VM_READ | _PROCESS_QUERY_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            def read(address: int, length: int) -> bytes | None:
                buffer = ctypes.create_string_buffer(length)
                count = ctypes.c_size_t(0)
                if not kernel32.ReadProcessMemory(
                        handle, ctypes.c_void_p(address), buffer, length,
                        ctypes.byref(count)) or count.value < length:
                    return None
                return buffer.raw[:length]

            # The landmark is present in the loaded image for supported builds,
            # but is not necessarily present in the on-disk DLL. Scan the
            # process mapping, as the upstream implementation does.
            image = read(module_base, module_size)
            if not image:
                continue
            position = -1
            for offset in range(len(image) - 8, 0, -8):
                if struct.unpack_from("<I", image, offset)[0] != len(landmark):
                    continue
                capacity = struct.unpack_from("<I", image, offset + 8)[0]
                if capacity and (capacity | 0xF) == 0xF and offset >= 16:
                    if image[offset - 16:offset - 16 + len(landmark)] == landmark:
                        position = offset
                        break
            if position < 0:
                continue

            # Version-specific 4.1.x links: landmark → v18 → cfg.
            v18_bytes = read(module_base + position - 0x138, 8)
            if not v18_bytes:
                continue
            v18 = struct.unpack("<Q", v18_bytes)[0]
            if not (0x10000 <= v18 < 0x800000000000):
                continue
            cfg_bytes = read(v18 + 0x68, 8)
            if not cfg_bytes:
                continue
            cfg = struct.unpack("<Q", cfg_bytes)[0]
            if not (0x10000 <= cfg < 0x800000000000):
                continue
            dword_bytes = read(cfg + 0x40, 4)
            if not dword_bytes:
                continue
            cfg_dword = struct.unpack("<I", dword_bytes)[0]
            size_bytes = read(cfg + 0x48 + 16, 8)
            if not size_bytes:
                continue
            wxid_size = struct.unpack("<Q", size_bytes)[0]
            if not 1 <= wxid_size <= 128:
                continue
            if wxid_size <= 15:
                wxid_bytes = read(cfg + 0x48, wxid_size)
            else:
                ptr_bytes = read(cfg + 0x48, 8)
                wxid_ptr = struct.unpack("<Q", ptr_bytes)[0] if ptr_bytes else 0
                wxid_bytes = read(wxid_ptr, wxid_size) if wxid_ptr else None
            wxid = wxid_bytes.decode("utf-8", "replace") if wxid_bytes else ""
            if not cfg_dword or not wxid:
                continue
            if not (account_name == wxid or account_name.startswith(wxid + "_")):
                continue
            aes_key = hashlib.md5(
                (str(cfg_dword) + wxid).encode("utf-8")).hexdigest()[:16].encode("ascii")
            if _image_like(_aes_decrypt_block(aes_key, probe)):
                LOGGER.info("已从微信当前配置推导并验证本账号图片密钥")
                return aes_key, cfg_dword & 0xFF
        except Exception:
            LOGGER.debug("Current Weixin config did not yield a valid image key", exc_info=True)
        finally:
            kernel32.CloseHandle(handle)
    return None


def image_aes_key(account_root: Path | str, *, refresh: bool = False) -> bytes:
    """Return an account's validated 16-byte AES image key, scanning if needed."""
    root = Path(account_root)
    account = root.name
    files = _v2_files(root)
    if not files:
        raise ImageKeyUnavailable("本机没有可用的 V2 图片缓存")
    probe = _v2_probe(files[0])
    if not probe:
        raise ImageKeyUnavailable("找不到可用于验证的图片密文块")
    if not refresh and account in _KEY_CACHE:
        cached = _KEY_CACHE[account]
        try:
            if _image_like(_aes_decrypt_block(cached, probe)):
                return cached
        except Exception:
            pass
    if not refresh:
        cached = _load_cached_key(account)
        if cached:
            try:
                if _image_like(_aes_decrypt_block(cached, probe)):
                    _KEY_CACHE[account] = cached
                    return cached
            except Exception:
                pass
    derived = _extract_cfg_image_key(root, probe)
    if derived:
        _KEY_CACHE[account] = derived[0]
        _XOR_CACHE[account] = derived[1]
        _persist_key(account, derived[0])
        return derived[0]
    key, scanned, processes = _scan_process_memory(probe)
    if key is None:
        raise ImageKeyUnavailable(
            "未找到可验证的图片密钥（已尝试只读扫描微信进程内存）；"
            "本次未要求在微信中打开图片，可能没有可用的本地图片缓存。")
    _KEY_CACHE[account] = key
    _persist_key(account, key)
    LOGGER.info("已从微信进程内存验证图片密钥（扫描 %d MB，访问 %d 个进程）",
                scanned // (1024 * 1024), processes)
    return key


def image_xor_key(account_root: Path | str, *, refresh: bool = False) -> int:
    """Infer the account XOR byte from repeated JPEG thumbnails."""
    root = Path(account_root)
    account = root.name
    if not refresh and account in _XOR_CACHE:
        return _XOR_CACHE[account]
    attach = root / "msg" / "attach"
    templates = list(attach.rglob("*_t.dat")) if attach.is_dir() else []
    scores: Counter[int] = Counter()
    for path in templates:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 34))
                tail = handle.read()
        except OSError:
            continue
        for pos in range(max(0, len(tail) - 1)):
            key = tail[pos] ^ 0xFF
            if tail[pos + 1] ^ key == 0xD9 and len(tail) - pos <= 34:
                scores[key] += 1
    if not scores:
        raise ImageKeyUnavailable("无法从本地 JPEG 缩略图尾部推导 XOR 密钥")
    key, count = scores.most_common(1)[0]
    # Reject a weak/random vote rather than quietly emitting corrupt images.
    if count < 2:
        raise ImageKeyUnavailable("本地缩略图不足以可靠推导 XOR 密钥")
    _XOR_CACHE[account] = key
    return key


def _trim_footer(data: bytes) -> bytes:
    if data.startswith(b"\xff\xd8"):
        end = data.rfind(b"\xff\xd9")
        if end >= 0 and len(data) - (end + 2) <= 32:
            return data[:end + 2]
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        end = data.rfind(b"IEND\xaeB`\x82")
        if end >= 0 and len(data) - (end + 8) <= 32:
            return data[:end + 8]
    return data


def decrypt_v2(data: bytes, aes_key: bytes, xor_key: int) -> bytes:
    """Decrypt a V2 image payload after validating its header and segment sizes."""
    return _decrypt_aes_xor(data, aes_key, xor_key, V2_MAGIC)


def _decrypt_aes_xor(data: bytes, aes_key: bytes, xor_key: int,
                     expected_magic: bytes) -> bytes:
    if len(data) < 31 or data[:6] != expected_magic:
        raise ValueError("微信图片缓存头与预期格式不匹配")
    aes_size, xor_size = struct.unpack_from("<II", data, 6)
    if aes_size <= 0 or xor_size <= 0 or xor_size > len(data) - 15:
        raise ValueError("V2 图片分段长度无效")
    aes_block_size = aes_size + (16 - aes_size % 16 if aes_size % 16 else 16)
    aes_start = 15
    aes_end = aes_start + aes_block_size
    xor_start = len(data) - xor_size
    if len(aes_key) != 16 or aes_end > xor_start:
        raise ValueError("V2 图片 AES 段长度不匹配")
    encrypted = data[aes_start:aes_end]
    if len(encrypted) % 16:
        raise ValueError("V2 图片 AES 段未按块对齐")
    from Crypto.Cipher import AES

    clear_aes = AES.new(aes_key, AES.MODE_ECB).decrypt(encrypted)
    pad = clear_aes[-1]
    if 1 <= pad <= 16 and clear_aes.endswith(bytes([pad]) * pad):
        clear_aes = clear_aes[:-pad]
    clear_xor = bytes(byte ^ (xor_key & 0xFF) for byte in data[xor_start:])
    result = _trim_footer(clear_aes + data[aes_end:xor_start] + clear_xor)
    if not _image_like(result):
        raise ValueError("解密结果没有有效的图片或 WXGF 文件头")
    return result


def _legacy_xor(data: bytes) -> tuple[bytes, str] | None:
    for ext, signature in _IMAGE_SIGNATURES[:-1]:
        key = data[0] ^ signature[0]
        decoded = bytes(byte ^ key for byte in data)
        if decoded.startswith(signature):
            if ext == "webp" and decoded[8:12] != b"WEBP":
                continue
            return decoded, ext
    return None


def _wxgf_to_jpeg(data: bytes) -> bytes | None:
    import shutil
    import subprocess
    import tempfile

    executable = shutil.which("ffmpeg")
    if not executable:
        try:
            import imageio_ffmpeg

            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    start = data.find(b"\x00\x00\x00\x01")
    if start < 0:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="wechat-ai-assistant-wxgf-") as temp:
            source = Path(temp) / "image.hevc"
            target = Path(temp) / "image.jpg"
            source.write_bytes(data[start:])
            result = subprocess.run(
                [executable, "-y", "-v", "error", "-i", str(source),
                 "-frames:v", "1", str(target)],
                capture_output=True, timeout=30, check=False)
            if result.returncode == 0 and target.is_file():
                jpeg = target.read_bytes()
                if jpeg.startswith(b"\xff\xd8\xff"):
                    return jpeg
    except Exception:
        LOGGER.debug("WXGF-to-JPEG conversion failed", exc_info=True)
    return None


def decode_image_file(path: str | Path, *, account_root: Path | str | None = None,
                      aes_key: bytes | None = None,
                      xor_key: int | None = None) -> tuple[bytes, str]:
    """Read and decode a local WeChat image; return ``(bytes, extension)``."""
    file_path = Path(path)
    data = file_path.read_bytes()
    if data.startswith(V2_MAGIC):
        root = Path(account_root) if account_root else _account_root()
        if root is None:
            raise ImageKeyUnavailable("无法定位当前微信账号的本地图片目录")
        key = aes_key or image_aes_key(root)
        xor = image_xor_key(root) if xor_key is None else xor_key
        data = decrypt_v2(data, key, xor)
    elif data.startswith(V1_AES_MAGIC):
        root = Path(account_root) if account_root else _account_root()
        if root is None:
            raise ImageKeyUnavailable("无法定位当前微信账号的本地图片目录")
        xor = image_xor_key(root) if xor_key is None else xor_key
        data = _decrypt_aes_xor(data, V1_AES_KEY, xor, V1_AES_MAGIC)
    elif file_path.suffix.lower() == ".dat":
        legacy = _legacy_xor(data)
        if legacy is None:
            if data.startswith(bytes.fromhex("070805560205")):
                raise ValueError("检测到旧版 V1 图片缓存格式，当前尚未实现该旧格式解码")
            raise ValueError("无法识别的微信图片缓存格式")
        data, ext = legacy
        return data, ext
    ext = next((name for name, signature in _IMAGE_SIGNATURES
                if data.startswith(signature)), "")
    if ext == "webp" and data[8:12] != b"WEBP":
        ext = ""
    if ext == "wxgf":
        jpeg = _wxgf_to_jpeg(data)
        if jpeg is not None:
            return jpeg, "jpg"
        return data, "wxgf"
    if not ext:
        raise ValueError("解密数据不是可识别的图片")
    return data, ext


def _account_root() -> Path | None:
    try:
        from .WeChatTools import Tools

        root = Path(Tools.where_wxid_folder())
        return root if root.is_dir() else None
    except Exception:
        return None


def extract_cached_images(account_root: Path | str, output_dir: Path | str) -> dict[str, int]:
    """Extract the best locally available copy of each cached image."""
    root, destination = Path(account_root), Path(output_dir)
    files = _v2_files(root)
    if not files:
        raise ImageKeyUnavailable("本机没有 V2 图片缓存")
    aes_key = image_aes_key(root)
    xor_key = image_xor_key(root)
    by_stem: dict[str, list[Path]] = {}
    for path in files:
        lower = path.name.lower()
        stem = path.name[:-6] if lower.endswith(("_t.dat", "_h.dat")) else path.stem
        by_stem.setdefault(stem.lower(), []).append(path)
    def tier_rank(path: Path) -> int:
        name = path.name.lower()
        if name.endswith("_h.dat"):
            return 0
        if name.endswith("_t.dat"):
            return 2
        return 1

    counts = Counter()
    destination.mkdir(parents=True, exist_ok=True)
    for stem, candidates in by_stem.items():
        picked = min(candidates, key=tier_rank)
        try:
            decoded, ext = decode_image_file(
                picked, account_root=root, aes_key=aes_key, xor_key=xor_key)
            tier_suffix = ("_original" if picked.name.lower().endswith("_h.dat") else
                           "_thumb" if picked.name.lower().endswith("_t.dat") else "")
            output = destination / f"{stem}{tier_suffix}.{ext}"
            output.write_bytes(decoded)
            counts[ext] += 1
        except Exception as exc:
            counts["failed"] += 1
            LOGGER.warning("图片提取失败 (%s): %s", picked.name, exc)
    return {str(key): int(value) for key, value in counts.items()}


__all__ = [
    "ImageKeyUnavailable", "decode_image_file", "decrypt_v2", "extract_cached_images",
    "image_aes_key", "image_xor_key",
]
