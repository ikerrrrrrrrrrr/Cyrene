"""应用内更新检查器 — 通过 GitHub Releases API 检查、下载、安装更新。"""

import asyncio
import logging
import os
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
_DEFAULT_REPO = "ikerrrrrrrrrrr/Cyrene"
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
        return "win64.zip"
    elif sys.platform.startswith("linux"):
        return "x86_64.AppImage"
    return sys.platform


async def check_for_update() -> UpdateInfo:
    """查询 GitHub Releases API，比较版本。"""
    current = _current_version()
    url = f"{_GITHUB_API}/latest"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
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

    async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total > 0:
                        progress_callback(downloaded, total)

    logger.info("Downloaded update to %s (%d bytes)", dest, downloaded)
    return dest


def get_restart_script(update_file: Path) -> str:
    """生成平台特定的重启更新脚本。"""
    if sys.platform == "darwin":
        return _restart_script_macos(update_file)
    elif sys.platform == "win32":
        return _restart_script_windows(update_file)
    else:
        return _restart_script_linux(update_file)


def _restart_script_macos(dmg_path: Path) -> str:
    """macOS: 挂载 DMG，替换 .app，重启。"""
    return f"""#!/bin/bash
# Cyrene updater — macOS
sleep 2
echo "Mounting update..."
hdiutil attach "{dmg_path}" -nobrowse -quiet
VOL="/Volumes/Cyrene"
if [ -d "$VOL" ]; then
    rm -rf /Applications/Cyrene.app
    cp -R "$VOL/Cyrene.app" /Applications/
    hdiutil detach "$VOL" -quiet
    echo "Update complete, restarting..."
    open /Applications/Cyrene.app
else
    echo "Update failed: DMG not mounted"
    exit 1
fi
rm -f "{dmg_path}"
"""


def _restart_script_windows(zip_path: Path) -> str:
    """Windows: 解压 zip 覆盖安装目录，重启。"""
    return f"""@echo off
:: Cyrene updater — Windows
timeout /t 2 /nobreak >nul
echo Installing update...
powershell -Command "Expand-Archive -Path '{zip_path}' -DestinationPath '$env:LOCALAPPDATA\\Cyrene' -Force"
if %errorlevel% equ 0 (
    echo Update complete, restarting...
    start "" "$env:LOCALAPPDATA\\Cyrene\\Cyrene.exe"
    del "{zip_path}"
) else (
    echo Update failed
    pause
)
"""


def _restart_script_linux(appimage_path: Path) -> str:
    """Linux: 替换 AppImage，重启。"""
    return f"""#!/bin/bash
# Cyrene updater — Linux
sleep 2
echo "Installing update..."
chmod +x "{appimage_path}"
INSTALL_DIR="$HOME/.local/bin"
mkdir -p "$INSTALL_DIR"
mv "{appimage_path}" "$INSTALL_DIR/Cyrene.AppImage"
echo "Update complete, restarting..."
"$INSTALL_DIR/Cyrene.AppImage" &
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
