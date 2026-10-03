# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格：把商户软件打成一个可双击的 exe。

    python deploy/build_windows.py

产物：dist/微信群接单助手/（onedir）或 dist/微信群接单助手.exe（onefile）。

**onedir 是默认选择**：微信自动化依赖 pywinauto/pywin32 的原生 DLL 和
隐藏导入，onefile 每次启动都要把它们解包到临时目录，启动慢且更容易被杀软
误报。onedir 启动快，行为可预期。
"""
import sys
from pathlib import Path

# A PyInstaller spec file is exec'd, not imported, so ``__file__`` does not
# exist. PyInstaller injects ``SPECPATH`` (the directory holding this file).
ROOT = Path(SPECPATH).resolve().parents[0]
sys.path.insert(0, str(ROOT / "src"))

# 微信自动化与界面依赖的原生/隐藏导入。漏掉任何一个都会在商户机器上
# 表现为「双击没反应」或「一开就崩」，而不是在打包机上暴露。
HIDDEN = [
    "pywinauto",
    "pywinauto.controls",
    "pywinauto.controls.uiawrapper",
    "pywinauto.timings",
    "pywinauto.application",
    # pywinauto's UIA wrapper imports numpy and comtypes at module load time.
    # Excluding them makes the built exe fail on first import, which the
    # merchant sees as "双击没反应" -- not a build-machine-only problem.
    "numpy",
    "numpy.core",
    "comtypes",
    "comtypes.client",
    "win32api",
    "win32con",
    "win32gui",
    "win32process",
    "win32clipboard",
    "win32crypt",
    "win32service",
    "pywintypes",
    "pythoncom",
    "psutil",
    "pyautogui",
    "pydoc_data",
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "sqlite3",
    "business_agent",
    "business_agent.backends",
    "business_agent.contracts",
    "business_agent.core",
    "order_agent",
]

EXCLUDES = [
    # Test and developer tooling stays out of the shipped product. numpy is
    # deliberately NOT excluded -- pywinauto needs it.
    "pytest", "pydoc", "doctest", "pip", "IPython", "notebook",
    "pandas", "matplotlib", "setuptools",
]

a = Analysis(
    [str(ROOT / "src" / "pyweixin" / "launcher.py")],
    pathex=[str(ROOT / "src")]
    + [str(p) for p in sorted((ROOT / "packages").glob("*/src"))],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="微信群接单助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    # 关窗时不要留下「已停止工作」的弹窗。
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="微信群接单助手",
)
