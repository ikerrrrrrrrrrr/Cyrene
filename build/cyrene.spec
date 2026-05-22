# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Cyrene — macOS / Windows / Linux 三平台支持。"""

import sys
from pathlib import Path

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

# webui static
_static_dir = _SRC / "webui" / "static"
if _static_dir.is_dir():
    for f in _static_dir.rglob("*"):
        if f.is_file() and "__pycache__" not in f.parts:
            dest = str(f.relative_to(_SRC).parent)
            _datas.append((str(f), dest))

# .env 模板（打包模式首次启动时复制到用户数据目录）
_env_tpl = _PROJECT_ROOT / ".env.example"
if _env_tpl.exists():
    _datas.append((str(_env_tpl), "."))

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
    "webview", "webview.platforms.cocoa", "webview.platforms.winforms", "webview.platforms.gtk",
]

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
    binaries=[],
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
