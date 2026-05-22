#!/usr/bin/env python3
"""Cyrene 构建脚本 — 三平台打包自动化。

用法:
    python build/build.py          # 构建当前平台
    python build/build.py --clean  # 仅清理
"""

import os
import shutil
import subprocess
import sys
import struct
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = PROJECT_ROOT / "dist"
SPEC_FILE = BUILD_DIR / "cyrene.spec"

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")


def get_version() -> str:
    """从 pyproject.toml 读取版本号。"""
    import tomllib
    pyproject = PROJECT_ROOT / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def clean() -> None:
    """清理构建产物。"""
    for d in (BUILD_DIR / "build", DIST_DIR):
        if d.exists():
            shutil.rmtree(d)
            print(f"  cleaned: {d}")
    for f in BUILD_DIR.glob("*.pyc"):
        f.unlink()


def _generate_icns(img: "Image.Image", out_path: Path) -> None:
    """纯 Python 生成 .icns 文件（无需 iconutil）。"""
    import io
    types = {
        16: b"icp4", 32: b"icp5", 64: b"icp6",
        128: b"ic07", 256: b"ic08", 512: b"ic09", 1024: b"ic10",
    }
    entries = []
    for size, icn_type in types.items():
        buf = io.BytesIO()
        img.resize((size, size), Image.LANCZOS).save(buf, format="PNG")
        png_data = buf.getvalue()
        entries.append(icn_type + struct.pack(">I", len(png_data) + 8) + png_data)
    out_path.write_bytes(b"icns" + struct.pack(">I", sum(len(e) for e in entries) + 8) + b"".join(entries))


def generate_icons() -> None:
    """生成占位图标（纯色 PNG）。"""
    icon_png = BUILD_DIR / "icon.png"
    if icon_png.exists():
        return

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("  [warn] Pillow not installed, skipping icon generation")
        return

    size = 512
    img = Image.new("RGBA", (size, size), (30, 30, 50, 255))
    draw = ImageDraw.Draw(img)
    # 简单几何图案
    margin = size // 6
    draw.ellipse([margin, margin, size - margin, size - margin],
                 fill=(80, 160, 220, 255))
    draw.ellipse([margin * 2, margin * 2, size - margin * 2, size - margin * 2],
                 fill=(30, 30, 50, 200))
    img.save(icon_png)
    print(f"  generated: {icon_png}")

    # macOS .icns (纯 Python 生成，无需 iconutil)
    _generate_icns(img, BUILD_DIR / "icon.icns")
    print(f"  generated: {BUILD_DIR / 'icon.icns'}")

    # Windows .ico
    img.resize((256, 256), Image.LANCZOS).save(BUILD_DIR / "icon.ico", format="ICO")
    print(f"  generated: {BUILD_DIR / 'icon.ico'}")


def run_pyinstaller() -> None:
    """运行 PyInstaller。"""
    print("\n[PyInstaller] Building...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR / "build"),
        "--noconfirm",
        str(SPEC_FILE),
    ]
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        print("  [error] PyInstaller failed")
        sys.exit(1)
    print("  [ok] PyInstaller done")


def _codesign_mac(app_path: Path) -> None:
    """macOS 代码签名。"""
    dev_id = os.environ.get("APPLE_DEVELOPER_ID", "")
    if dev_id:
        print(f"  signing with: {dev_id}")
        subprocess.run([
            "codesign", "--deep", "--force", "--options", "runtime",
            "--sign", dev_id, str(app_path),
        ], check=True)
    else:
        # ad-hoc 签名
        print("  ad-hoc signing...")
        subprocess.run([
            "codesign", "--deep", "--force", "--sign", "-", str(app_path),
        ], check=True)
    print("  [ok] signed")


def package_mac() -> Path:
    """macOS: .app → .dmg。"""
    version = get_version()
    app_path = DIST_DIR / "Cyrene.app"

    if not app_path.exists():
        print("  [error] .app not found, check PyInstaller output")
        sys.exit(1)

    _codesign_mac(app_path)

    dmg_path = DIST_DIR / f"Cyrene-{version}.dmg"
    print(f"\n[DMG] Creating {dmg_path.name}...")
    subprocess.run([
        "hdiutil", "create",
        "-volname", "Cyrene",
        "-srcfolder", str(app_path),
        "-ov", "-format", "UDZO",
        str(dmg_path),
    ], check=True)
    print(f"  [ok] {dmg_path}")
    return dmg_path


def package_win() -> Path:
    """Windows: onedir → .zip。"""
    version = get_version()
    dir_path = DIST_DIR / "Cyrene"

    if not dir_path.exists():
        print("  [error] Cyrene dir not found, check PyInstaller output")
        sys.exit(1)

    zip_path = DIST_DIR / f"Cyrene-{version}-win64.zip"
    print(f"\n[ZIP] Creating {zip_path.name}...")
    shutil.make_archive(
        str(zip_path.with_suffix("")),
        "zip",
        str(DIST_DIR),
        "Cyrene",
    )
    print(f"  [ok] {zip_path}")
    return zip_path


def package_linux() -> list[Path]:
    """Linux: onedir → .tar.gz + .AppImage。"""
    version = get_version()
    dir_path = DIST_DIR / "Cyrene"

    if not dir_path.exists():
        print("  [error] Cyrene dir not found, check PyInstaller output")
        sys.exit(1)

    outputs = []

    # tar.gz
    tar_path = DIST_DIR / f"Cyrene-{version}-x86_64.tar.gz"
    print(f"\n[TAR] Creating {tar_path.name}...")
    shutil.make_archive(
        str(tar_path.with_suffix("").with_suffix("")),
        "gztar",
        str(DIST_DIR),
        "Cyrene",
    )
    outputs.append(tar_path)
    print(f"  [ok] {tar_path}")

    # AppImage
    appimage_path = _create_appimage(dir_path, version)
    if appimage_path:
        outputs.append(appimage_path)

    return outputs


def _create_appimage(dir_path: Path, version: str) -> Path | None:
    """创建 AppImage。"""
    appimagetool = shutil.which("appimagetool")
    if not appimagetool:
        print("  [warn] appimagetool not found, skipping AppImage")
        return None

    appdir = DIST_DIR / "Cyrene.AppDir"
    if appdir.exists():
        shutil.rmtree(appdir)
    shutil.copytree(dir_path, appdir)

    # 创建 .desktop 文件
    desktop = appdir / "cyrene.desktop"
    desktop.write_text("""[Desktop Entry]
Type=Application
Name=Cyrene
Comment=AI Agent That Evolves
Exec=Cyrene
Icon=cyrene
Terminal=false
Categories=Utility;ArtificialIntelligence;
""")

    # 复制图标
    icon_src = BUILD_DIR / "icon.png"
    if icon_src.exists():
        shutil.copy(icon_src, appdir / "cyrene.png")

    # 创建 AppRun
    apprun = appdir / "AppRun"
    apprun.write_text("""#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/Cyrene" "$@"
""")
    apprun.chmod(0o755)

    output_path = DIST_DIR / f"Cyrene-{version}-x86_64.AppImage"
    print(f"\n[AppImage] Creating {output_path.name}...")
    subprocess.run([
        appimagetool, str(appdir), str(output_path),
    ], check=False)

    shutil.rmtree(appdir, ignore_errors=True)

    if output_path.exists():
        print(f"  [ok] {output_path}")
        return output_path
    return None


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Build Cyrene")
    parser.add_argument("--clean", action="store_true", help="仅清理构建产物")
    parser.add_argument("--skip-icons", action="store_true", help="跳过图标生成")
    args = parser.parse_args()

    print(f"Cyrene Builder — {sys.platform}")
    print(f"  project: {PROJECT_ROOT}")

    if args.clean:
        clean()
        return

    clean()

    if not args.skip_icons:
        generate_icons()

    run_pyinstaller()

    # 平台特定打包
    print(f"\n[Package] {sys.platform}")
    if IS_MAC:
        result = package_mac()
        print(f"\nDone: {result}")
    elif IS_WIN:
        result = package_win()
        print(f"\nDone: {result}")
    elif IS_LINUX:
        results = package_linux()
        print(f"\nDone:")
        for r in results:
            print(f"  {r}")
    else:
        print("  [warn] unknown platform, no packaging step")


if __name__ == "__main__":
    main()
