"""应用内更新检查器 — 通过 GitHub Releases API 检查、下载、安装更新。"""

import asyncio
import hashlib
import logging
import os
import shlex
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx
from packaging.version import Version

from cyrene.config import BASE_DIR
from cyrene.version import get_version

logger = logging.getLogger(__name__)

# GitHub 仓库配置
_DEFAULT_REPO = "Yongchu-Yitao/Cyrene"
_UPDATE_REPO = os.environ.get("UPDATE_REPO", _DEFAULT_REPO)
_GITHUB_API = f"https://api.github.com/repos/{_UPDATE_REPO}/releases"


def _current_version() -> str:
    """从 pyproject.toml 读取当前版本。"""
    return get_version()


@dataclass
class UpdateInfo:
    available: bool
    current_version: str
    latest_version: str
    download_url: str = ""
    release_notes: str = ""
    asset_name: str = ""
    asset_size: int = 0


def _platform_filter() -> str:
    """返回当前平台的 asset 匹配关键词。"""
    if sys.platform == "darwin":
        return ".dmg"
    elif sys.platform == "win32":
        return "win64.exe"
    elif sys.platform.startswith("linux"):
        return "x64.AppImage"
    return sys.platform


async def check_for_update() -> UpdateInfo:
    """查询 GitHub Releases API，比较版本。"""
    current = _current_version()
    url = f"{_GITHUB_API}/latest"

    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.debug("GitHub API returned %d", resp.status_code)
                return UpdateInfo(available=False, current_version=current, latest_version="")

            data = resp.json()
            tag: str = data.get("tag_name", "")
            latest = tag.lstrip("v")

            if not latest:
                return UpdateInfo(available=False, current_version=current, latest_version="")

            try:
                cur_v = Version(current)
                new_v = Version(latest)
            except ValueError:
                logger.debug("Invalid version format: cur=%s latest=%s", current, latest)
                return UpdateInfo(available=False, current_version=current, latest_version=latest)

            if new_v <= cur_v:
                return UpdateInfo(
                    available=False,
                    current_version=current,
                    latest_version=latest,
                )

            # 查找匹配当前平台的 asset
            platform_key = _platform_filter()
            asset_url = ""
            asset_name = ""
            asset_size = 0

            for asset in data.get("assets", []):
                name: str = asset.get("name", "")
                if platform_key in name.lower():
                    asset_url = asset.get("browser_download_url", "")
                    asset_name = name
                    asset_size = asset.get("size", 0)
                    break

            # 如果没找到精确匹配，使用第一个 asset
            if not asset_url and data.get("assets"):
                first = data["assets"][0]
                asset_url = first.get("browser_download_url", "")
                asset_name = first.get("name", "")
                asset_size = first.get("size", 0)

            return UpdateInfo(
                available=True,
                current_version=current,
                latest_version=latest,
                download_url=asset_url,
                release_notes=data.get("body", ""),
                asset_name=asset_name,
                asset_size=asset_size,
            )

    except Exception as exc:
        logger.debug("Update check failed: %s", exc)
        return UpdateInfo(available=False, current_version=current, latest_version="")


async def download_update(
    url: str,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Path | None:
    """下载更新包到临时目录。"""
    if not url:
        return None

    dest = Path(tempfile.gettempdir()) / "Cyrene_update" / Path(url).name
    dest.parent.mkdir(parents=True, exist_ok=True)

    hasher = hashlib.sha256()
    downloaded = 0

    async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(65536):
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total > 0:
                        progress_callback(downloaded, total)

    checksum = hasher.hexdigest()
    size_mb = downloaded / (1024 * 1024)
    logger.info(
        "Downloaded update: %s (%d bytes / %.1f MB), SHA256=%s",
        dest, downloaded, size_mb, checksum,
    )
    return dest


def get_restart_script(update_file: Path) -> str:
    """生成平台特定的重启更新脚本。"""
    if sys.platform == "darwin":
        return _restart_script_macos(update_file)
    elif sys.platform == "win32":
        return _restart_script_windows(update_file)
    else:
        return _restart_script_linux(update_file)


def _current_app_executable() -> Path | None:
    raw = os.environ.get("CYRENE_APP_EXECUTABLE", "").strip()
    return Path(raw).expanduser() if raw else None


def _current_macos_app_bundle() -> Path:
    app_exe = _current_app_executable()
    if app_exe:
        for parent in app_exe.parents:
            if parent.suffix == ".app":
                return parent
    return Path("/Applications/Cyrene.app")


def _restart_script_macos(dmg_path: Path) -> str:
    """macOS: 挂载 DMG，替换 .app，重启。

    优先覆盖当前实际安装位置，而不是写死 /Applications。
    所有输出重定向到 /tmp/cyrene_update.log 用于诊断。
    """
    app_bundle = _current_macos_app_bundle()
    app_bundle_q = shlex.quote(str(app_bundle))
    dmg_path_q = shlex.quote(str(dmg_path))
    return (
        '#!/bin/bash\n'
        '# Cyrene updater — macOS\n'
        'set -e\n'
        'exec >>/tmp/cyrene_update.log 2>&1\n'
        'echo "=== Cyrene update $(date) ==="\n'
        f'echo "DMG: {dmg_path_q}"\n'
        f'echo "Target app: {app_bundle_q}"\n'
        'sleep 2\n'
        'echo "Mounting update..."\n'
        # Detach ALL existing Cyrene mounts first, then force mount at
        # /Volumes/Cyrene regardless of DMG's internal volume name
        'for vol in /Volumes/Cyrene*; do\n'
        '  [ -d "$vol" ] && hdiutil detach "$vol" -quiet 2>/dev/null\n'
        'done\n'
        f'hdiutil attach {dmg_path_q} -nobrowse -quiet -mountpoint /Volumes/Cyrene\n'
        'echo "attach exit code: $?"\n'
        'VOL="/Volumes/Cyrene"\n'
        'if [ -d "$VOL" ]; then\n'
        '    echo "Found volume, installing..."\n'
        f'    rm -rf {app_bundle_q}\n'
        '    echo "rm exit code: $?"\n'
        f'    mkdir -p {shlex.quote(str(app_bundle.parent))}\n'
        f'    cp -R "$VOL/Cyrene.app" {shlex.quote(str(app_bundle.parent))}/\n'
        '    echo "cp exit code: $?"\n'
        f'    ls -la {app_bundle_q}\n'
        '    hdiutil detach "$VOL" -quiet\n'
        '    echo "Update complete, restarting..."\n'
        f'    open {app_bundle_q}\n'
        'else\n'
        '    echo "Update failed: DMG not mounted"\n'
        '    echo "hdiutil attach result:"\n'
        '    ls -la /Volumes/ 2>&1\n'
        '    exit 1\n'
        'fi\n'
        f'rm -f {dmg_path_q}\n'
        'echo "Done."\n'
    )


def _restart_script_windows(exe_path: Path) -> str:
    """Windows: 以管理员权限运行 NSIS 安装程序（静默模式）覆盖安装，重启。

    使用 PowerShell 的 Start-Process -Verb RunAs 请求 UAC 提升，
    解决 DETACHED_PROCESS 无法弹出 UAC 提示导致安装静默失败的问题。
    """
    app_exe = _current_app_executable() or Path(r"%LOCALAPPDATA%\Programs\Cyrene\Cyrene.exe")
    return f"""@echo off
setlocal
:: Cyrene updater — Windows
set LOG="%TEMP%\\cyrene_update.log"
>>%LOG% echo === Cyrene update %date% %time% ===
>>%LOG% echo EXE: {exe_path}
>>%LOG% echo TARGET: {app_exe}
>>%LOG% echo STARTED: %date% %time%

:: 等待主进程完全退出释放文件锁
timeout /t 3 /nobreak >nul

>>%LOG% echo Launching elevated installer via PowerShell...
:: PowerShell Start-Process -Verb RunAs 会正确弹出 UAC 提升提示
:: -Wait 让脚本等待安装完成再继续
powershell -Command "Start-Process -FilePath '{exe_path}' -ArgumentList '/S' -Verb RunAs -Wait -WindowStyle Hidden"
set RC=%errorlevel%
>>%LOG% echo PowerShell exit code: %RC%
>>%LOG% echo UPDATED: %date% %time%

if %RC% equ 0 (
    >>%LOG% echo Update installer completed, verifying...
    :: 额外等待确保文件写入完成
    timeout /t 1 /nobreak >nul
    >>%LOG% echo App start: {app_exe}
    start "" "{app_exe}"
    del "{exe_path}"
) else (
    >>%LOG% echo Update failed (error %RC%) — possible causes:
    >>%LOG% echo   - UAC elevation was cancelled by user
    >>%LOG% echo   - Installer failed to write to target directory
    >>%LOG% echo   - Antivirus blocked the installer
    timeout /t 5 /nobreak >nul
)
endlocal
"""


def _restart_script_linux(appimage_path: Path) -> str:
    """Linux: 覆盖当前 AppImage，重启。"""
    current_exe = _current_app_executable()
    target_path = current_exe if current_exe else Path.home() / ".local" / "bin" / "Cyrene.AppImage"
    appimage_path_q = shlex.quote(str(appimage_path))
    target_path_q = shlex.quote(str(target_path))
    target_parent_q = shlex.quote(str(target_path.parent))
    return f"""#!/bin/bash
# Cyrene updater — Linux
set -e
exec >>/tmp/cyrene_update.log 2>&1
echo "=== Cyrene update $(date) ==="
echo "AppImage: {appimage_path_q}"
echo "Target: {target_path_q}"
sleep 2
echo "Installing update..."
chmod +x {appimage_path_q}
mkdir -p {target_parent_q}
cp {appimage_path_q} {target_path_q}.new
chmod +x {target_path_q}.new
mv {target_path_q}.new {target_path_q}
echo "install exit code: $?"
if [ -f {target_path_q} ]; then
    rm -f {appimage_path_q}
    echo "Update complete, restarting..."
    {target_path_q} &
else
    echo "Update failed: target missing"
    exit 1
fi
"""


# ---- 内存中的更新状态（供 Web UI 查询）----

_latest_update_info: UpdateInfo | None = None
_download_progress: dict = {"downloaded": 0, "total": 0, "done": False, "path": ""}


def get_cached_update_info() -> UpdateInfo | None:
    return _latest_update_info


def set_cached_update_info(info: UpdateInfo) -> None:
    global _latest_update_info
    _latest_update_info = info


def get_download_progress() -> dict:
    return dict(_download_progress)


# ---- 后台任务 ----

async def background_check() -> None:
    """启动时运行的后台更新检查。"""
    info = await check_for_update()
    set_cached_update_info(info)
    if info.available:
        logger.info(
            "Update available: %s → %s (%s)",
            info.current_version, info.latest_version, info.asset_name,
        )
