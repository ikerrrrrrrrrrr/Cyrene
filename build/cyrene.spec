# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Cyrene — macOS / Windows / Linux 三平台支持。"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, copy_metadata

_PROJECT_ROOT = Path(SPECPATH).resolve().parent
_SRC = _PROJECT_ROOT / "src"
_ENTRY = str(Path(SPECPATH).resolve() / "run_cyrene.py")
_IS_MAC = sys.platform == "darwin"
_IS_WIN = sys.platform == "win32"

# 从 pyproject.toml 读取版本号
import tomllib
with open(_PROJECT_ROOT / "pyproject.toml", "rb") as _f:
    _version = tomllib.load(_f)["project"]["version"]

# ---- 静态数据文件 ----
_datas = []
_binaries = []

# webui static
_static_dir = _SRC / "webui" / "static"
if _static_dir.is_dir():
    for f in _static_dir.rglob("*"):
        if f.is_file() and "__pycache__" not in f.parts:
            dest = str(f.relative_to(_SRC).parent)
            _datas.append((str(f), dest))

# macOS 原生窗口助手 (Swift 编译产物)
_win_bin = Path(SPECPATH).resolve() / "cyrene_window"
if _IS_MAC and _win_bin.exists():
    _datas.append((str(_win_bin), "."))

# .env 模板（打包模式首次启动时复制到用户数据目录）
_env_tpl = _PROJECT_ROOT / ".env.example"
if _env_tpl.exists():
    _datas.append((str(_env_tpl), "."))

# pyproject（供打包后读取当前版本号）
_pyproject = _PROJECT_ROOT / "pyproject.toml"
if _pyproject.exists():
    _datas.append((str(_pyproject), "."))

# ---- 隐藏导入 ----
_hidden = [
    "webui", "webui.server", "webui.routes",
    "cyrene", "cyrene.agent", "cyrene.attachments", "cyrene.bot",
    "cyrene.cc_bridge", "cyrene.cc_learner", "cyrene.cc_terminal",
    "cyrene.cli", "cyrene.config", "cyrene.conversations", "cyrene.db",
    "cyrene.debug", "cyrene.inbox", "cyrene.llm", "cyrene.local_cli",
    "cyrene.mcp_manager", "cyrene.memory", "cyrene.onboarding",
    "cyrene.pattern", "cyrene.report_export", "cyrene.scheduler",
    "cyrene.search", "cyrene.searxng_manager", "cyrene.settings_store",
    "cyrene.setup", "cyrene.shells", "cyrene.short_term", "cyrene.soul",
    "cyrene.subagent", "cyrene.tools",
    "jinja2", "jinja2.ext",
    "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "websockets", "aiosqlite", "apscheduler", "croniter",
    "httpx", "python_multipart", "sniffio", "simplexng",
]

if not _IS_MAC:
    _hidden.append("webview")
    if _IS_WIN:
        _hidden.append("webview.platforms.winforms")
    else:
        _hidden.append("webview.platforms.gtk")


def _collect_package(name: str) -> None:
    """Collect package modules, data files, and metadata for frozen builds."""
    global _datas, _binaries, _hidden
    try:
        datas, binaries, hiddenimports = collect_all(name)
    except Exception as exc:
        print(f"[warn] collect_all({name!r}) failed: {exc}")
        return

    _datas.extend(datas)
    _binaries.extend(binaries)
    _hidden.extend(hiddenimports)
    try:
        _datas.extend(copy_metadata(name))
    except Exception:
        pass


for _package in (
    "httpx",
    "httpcore",
    "anyio",
    "certifi",
    "sniffio",
    "h11",
    "idna",
    "jinja2",
    "uvicorn",
    "websockets",
    "python_multipart",
    "aiosqlite",
    "apscheduler",
    "croniter",
    "simplexng",
):
    _collect_package(_package)

_datas = list(dict.fromkeys(_datas))
_binaries = list(dict.fromkeys(_binaries))
_hidden = list(dict.fromkeys(_hidden))

# Linux: bundle PyGObject + GTK typelibs for pywebview native window
if not _IS_MAC and not _IS_WIN:
    try:
        _collect_package("gi")
    except Exception as exc:
        print(f"[warn] gi collection failed (GTK native window will be unavailable): {exc}")

# ---- 排除 ----
_excludes = [
    "tkinter", "matplotlib", "numpy", "pandas", "scipy",
    "PIL._tkinter_finder", "curses",
]

# ---- 图标 ----
_icon = None
_icon_dir = Path(SPECPATH).resolve()
if _IS_MAC and (_icon_dir / "icon.icns").exists():
    _icon = str(_icon_dir / "icon.icns")
elif _IS_WIN and (_icon_dir / "icon.ico").exists():
    _icon = str(_icon_dir / "icon.ico")

# ============================
a = Analysis(
    [_ENTRY],
    pathex=[str(_SRC)],
    binaries=_binaries,
    datas=_datas,
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Cyrene",
    icon=_icon,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Cyrene",
)

if _IS_MAC:
    app = BUNDLE(
        coll,
        name="Cyrene.app",
        icon=_icon,
        bundle_identifier="com.cyrene.app",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "CFBundleShortVersionString": _version,
            "CFBundleName": "Cyrene",
        },
    )
