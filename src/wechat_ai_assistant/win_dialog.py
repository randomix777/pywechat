"""Win11 原生文件对话框（IFileOpenDialog / IFileSaveDialog）。

## 为什么不用 tkinter.filedialog

tkinter 在 Windows 上调的是老 `GetOpenFileName`/`GetSaveFileName`，
长得像 Win10 甚至更早 —— Tk 这辈子没跟进 Vista 之后的 IFileDialog。
要 Win11 的样子，只能自己走 COM。

## 失败就回落

COM 初始化、父窗口句柄、用户点取消，任何一步不对都回落到
tkinter.filedialog —— 对话框弹不出来比"不够新"严重得多。
调用方只管要路径，不用管背后是哪套。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from typing import Sequence

ole32 = ctypes.OleDLL("ole32")
shell32 = ctypes.OleDLL("shell32")

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1

FOS_OVERWRITEPROMPT = 0x2
FOS_STRICTFILETYPES = 0x4
FOS_NOCHANGEDIR = 0x8
FOS_PATHMUSTEXIST = 0x800
FOS_NOREADONLYRETURN = 0x8000

SIGDN_FILESYSPATH = 0x80058000

#: 用户点了取消。调用方遇到它直接收摊，不要再弹 tkinter 兜底 ——
#: 不然取消一次弹两次框。
class UserCancelled(Exception):
    pass


def _is_cancel(hr: int) -> bool:
    return (hr & 0xFFFFFFFF) in (0x800704C7, 0x800704C8)

#: GUID 全部按 SDK 头文件（ShObjIdl_core.h）取值，凭记忆写错过两次，
#: 以后改这里先去头文件里对。
CLSID_FileOpenDialog = (0xDC1C5A9C, 0xE88A, 0x4DDE,
                        (0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7))
CLSID_FileSaveDialog = (0xC0B4E2F3, 0xBA21, 0x4773,
                        (0x8D, 0xBA, 0x33, 0x5E, 0xC9, 0x46, 0xEB, 0x8B))
IID_IFileOpenDialog = (0xD57C7288, 0xD4AD, 0x4768,
                       (0xBE, 0x02, 0x9D, 0x96, 0x95, 0x32, 0xD9, 0x60))
IID_IFileSaveDialog = (0x84BCCD23, 0x5FDE, 0x4CDB,
                       (0xAE, 0xA4, 0xAF, 0x64, 0xB8, 0x3D, 0x78, 0xAB))
IID_IShellItem = (0x43826D1E, 0xE718, 0x42EE,
                  (0xBC, 0x55, 0xA1, 0xE2, 0x61, 0xC3, 0x7B, 0xFE))


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD),
                ("Data2", wt.WORD),
                ("Data3", wt.WORD),
                ("Data4", wt.BYTE * 8)]

    @classmethod
    def from_tuple(cls, parts) -> "GUID":
        return cls(parts[0], parts[1], parts[2], (wt.BYTE * 8)(*parts[3]))


#: 函数签名一次声明：返回值自己判 HRESULT，不靠 last-error 猜
#: （last-error 可能是过期值，会把成功也报成失败）。
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID), ctypes.c_void_p, wt.DWORD,
    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
ole32.CoCreateInstance.restype = ctypes.c_long
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None
shell32.SHCreateItemFromParsingName.argtypes = [
    wt.LPCWSTR, ctypes.c_void_p,
    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
shell32.SHCreateItemFromParsingName.restype = ctypes.c_long


class COMDLG_FILTERSPEC(ctypes.Structure):
    _fields_ = [("pszName", wt.LPCWSTR),
                ("pszSpec", wt.LPCWSTR)]


def _ensure_com() -> None:
    """STA 初始化。本线程已初始化过（比如 pywin32 干过）也不报错。"""
    try:
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    except OSError:
        pass


def _guid_ptr(parts) -> ctypes.POINTER(GUID):
    guid = GUID.from_tuple(parts)
    return ctypes.pointer(guid)


def _call(vtable_index: int, this, restype, *argtypes_and_args):
    """调 COM 虚表方法。参数以 (argtype, value) 对给出。"""
    argtypes = argtypes_and_args[0::2]
    args = argtypes_and_args[1::2]
    vtable = ctypes.cast(this, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
    func_ptr = vtable.contents[vtable_index]
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    func = prototype(func_ptr)
    return func(this, *args)


def _show_dialog(save: bool, hwnd, title: str, folder: str, filename: str,
                 filetypes: Sequence[tuple[str, str]],
                 default_ext: str) -> str:
    _ensure_com()
    # owner 固定传 0（无主），不要传主窗口：
    # 同线程的 Tk 窗口当 owner 会让 Show 卡死且不出窗（实测：desktop、
    # console 当 owner 都正常，只有 Tk 窗口不行）。无主框自己置顶，
    # 调用线程 block 等它，本来就是模态效果。hwnd 参数保留只为兼容。
    del hwnd
    hwnd = 0
    clsid = CLSID_FileSaveDialog if save else CLSID_FileOpenDialog
    iid = IID_IFileSaveDialog if save else IID_IFileOpenDialog
    dialog = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        _guid_ptr(clsid), None, CLSCTX_INPROC_SERVER, _guid_ptr(iid),
        ctypes.byref(dialog))
    if hr != 0 or not dialog:
        raise OSError(f"创建文件对话框失败：0x{hr & 0xFFFFFFFF:X}")
    dlg = dialog.value
    try:
        if title:
            _call(17, dlg, ctypes.c_long, wt.LPCWSTR, title)
        options = FOS_NOCHANGEDIR | FOS_NOREADONLYRETURN
        if save:
            options |= FOS_OVERWRITEPROMPT | FOS_STRICTFILETYPES
        else:
            options |= FOS_PATHMUSTEXIST
        _call(9, dlg, ctypes.c_long, wt.DWORD, options)
        if filetypes:
            specs = (COMDLG_FILTERSPEC * len(filetypes))()
            for index, (name, pattern) in enumerate(filetypes):
                specs[index].pszName = str(name)
                specs[index].pszSpec = str(pattern)
            _call(4, dlg, ctypes.c_long, wt.UINT, len(filetypes),
                  ctypes.POINTER(COMDLG_FILTERSPEC), specs)
        if default_ext:
            ext = str(default_ext).lstrip(".")
            _call(22, dlg, ctypes.c_long, wt.LPCWSTR, ext)
        if filename:
            _call(15, dlg, ctypes.c_long, wt.LPCWSTR, str(filename))
        if folder:
            item = ctypes.c_void_p()
            hr = shell32.SHCreateItemFromParsingName(
                wt.LPCWSTR(str(folder)), None,
                _guid_ptr(IID_IShellItem), ctypes.byref(item))
            if hr == 0 and item:
                try:
                    _call(12, dlg, ctypes.c_long, ctypes.c_void_p, item.value)
                finally:
                    _call(2, item.value, ctypes.c_ulong)
        hr = _call(3, dlg, ctypes.c_long, wt.HWND, hwnd or 0)
        if _is_cancel(hr):
            raise UserCancelled()
        if hr != 0:
            raise OSError(f"对话框未确认：0x{hr & 0xFFFFFFFF:X}")
        result = ctypes.c_void_p()
        hr = _call(20, dlg, ctypes.c_long,
                   ctypes.POINTER(ctypes.c_void_p), ctypes.byref(result))
        if hr != 0 or not result:
            raise OSError("没有取到选择结果")
        item = result.value
        try:
            path_ptr = wt.LPWSTR()
            hr = _call(5, item, ctypes.c_long, wt.DWORD, SIGDN_FILESYSPATH,
                       ctypes.POINTER(wt.LPWSTR), ctypes.byref(path_ptr))
            if hr != 0 or not path_ptr:
                raise OSError("取不到文件路径")
            try:
                return str(path_ptr.value or "")
            finally:
                ole32.CoTaskMemFree(path_ptr)
        finally:
            _call(2, item, ctypes.c_ulong)
    finally:
        _call(2, dlg, ctypes.c_ulong)


def ask_saveas_path(parent_hwnd: int = 0, title: str = "",
                    initial_dir: str = "", filename: str = "",
                    filetypes: Sequence[tuple[str, str]] = (),
                    default_ext: str = "") -> str | None:
    """Win11 保存框。用户取消返回 ""；失败返回 None（调用方回落 tkinter）。

    从不抛异常：对话框弹不出来比"不够新"严重得多。
    """
    try:
        return _show_dialog(True, parent_hwnd, title, initial_dir,
                            filename, filetypes, default_ext) or ""
    except UserCancelled:
        return ""
    except Exception:
        return None


def ask_open_path(parent_hwnd: int = 0, title: str = "",
                  initial_dir: str = "",
                  filetypes: Sequence[tuple[str, str]] = ()) -> str | None:
    """Win11 打开框。用户取消返回 ""；失败返回 None（调用方回落 tkinter）。"""
    try:
        return _show_dialog(False, parent_hwnd, title, initial_dir,
                            "", filetypes, "") or ""
    except UserCancelled:
        return ""
    except Exception:
        return None


__all__ = ["ask_open_path", "ask_saveas_path"]
