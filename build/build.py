#!/usr/bin/env python3
"""Cyrene 构建脚本 — 三平台打包自动化。

用法:
    python build/build.py          # 构建当前平台
    python build/build.py --clean  # 仅清理
"""

import os
import platform
import shutil
import subprocess
import sys
import struct
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = PROJECT_ROOT / "dist"
SPEC_FILE = BUILD_DIR / "cyrene.spec"
WEB_LOGO_PATH = PROJECT_ROOT / "src" / "webui" / "static" / "app" / "logo-mark.png"

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
    from PIL import Image

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


def _load_logo_image() -> "Image.Image | None":
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("  [warn] Pillow not installed, skipping icon generation")
        return None

    logo_src = BUILD_DIR / "logo-source.png"
    if logo_src.exists():
        raw = Image.open(logo_src).convert("RGBA")
        # Source artwork includes a wordmark below the emblem; crop it out and
        # turn the near-white background transparent so platform icons stay clean.
        crop = raw.crop((260, 140, 1015, 800))
        pixels = crop.load()
        for y in range(crop.height):
            for x in range(crop.width):
                r, g, b, a = pixels[x, y]
                if r > 245 and g > 245 and b > 245:
                    pixels[x, y] = (255, 255, 255, 0)
        bbox = crop.getbbox()
        if not bbox:
            return crop
        trimmed = crop.crop(bbox)
        size = 1024
        padding = 110
        scale = min((size - 2 * padding) / trimmed.width, (size - 2 * padding) / trimmed.height)
        resized = trimmed.resize((int(trimmed.width * scale), int(trimmed.height * scale)), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        left = (size - resized.width) // 2
        top = (size - resized.height) // 2
        canvas.alpha_composite(resized, (left, top))
        return canvas

    size = 512
    img = Image.new("RGBA", (size, size), (30, 30, 50, 255))
    draw = ImageDraw.Draw(img)
    margin = size // 6
    draw.ellipse([margin, margin, size - margin, size - margin], fill=(80, 160, 220, 255))
    draw.ellipse([margin * 2, margin * 2, size - margin * 2, size - margin * 2], fill=(30, 30, 50, 200))
    return img


def generate_icons() -> None:
    """Generate icons from the checked-in logo source when available."""
    from PIL import Image

    icon_png = BUILD_DIR / "icon.png"
    img = _load_logo_image()
    if img is None:
        return
    img.save(icon_png)
    print(f"  generated: {icon_png}")

    _generate_icns(img, BUILD_DIR / "icon.icns")
    print(f"  generated: {BUILD_DIR / 'icon.icns'}")

    img.resize((256, 256), Image.LANCZOS).save(BUILD_DIR / "icon.ico", format="ICO")
    print(f"  generated: {BUILD_DIR / 'icon.ico'}")

    WEB_LOGO_PATH.parent.mkdir(parents=True, exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save(WEB_LOGO_PATH)
    print(f"  generated: {WEB_LOGO_PATH}")


def build_webui_js() -> None:
    """编译 JSX → compiled/*.js，供 PyInstaller 打包进静态资源。

    compiled/ 在 .gitignore 中不入库，必须在打包前先编译，否则
    Python frozen binary 里 static/app/compiled/ 为空，前端全 404。
    """
    webui_dir = PROJECT_ROOT / "src" / "webui"
    build_script = webui_dir / "build-jsx.mjs"
    if not build_script.exists():
        print("  [warn] build-jsx.mjs not found, skipping JSX build")
        return

    print("\n[WebUI] Compiling JSX...")
    # Install npm deps if node_modules is absent
    if not (webui_dir / "node_modules").exists():
        result = subprocess.run(["npm", "install"], cwd=str(webui_dir))
        if result.returncode != 0:
            print("  [error] npm install failed in src/webui")
            sys.exit(1)

    result = subprocess.run(["node", "build-jsx.mjs"], cwd=str(webui_dir))
    if result.returncode != 0:
        print("  [error] JSX build failed")
        sys.exit(1)
    print("  [ok] JSX compiled")


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
    """macOS 代码签名 — 与 v0.3.6 完全相同的方式。"""
    dev_id = os.environ.get("APPLE_DEVELOPER_ID", "")
    if dev_id:
        print(f"  signing with: {dev_id}")
        subprocess.run([
            "codesign", "--deep", "--force", "--options", "runtime",
            "--sign", dev_id, str(app_path),
        ], check=True)
    else:
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
    with tempfile.TemporaryDirectory(prefix="cyrene-dmg-") as tmp_dir:
        staging_dir = Path(tmp_dir) / "Cyrene"
        staging_dir.mkdir(parents=True, exist_ok=True)

        staged_app = staging_dir / "Cyrene.app"
        shutil.copytree(app_path, staged_app, symlinks=True)

        apps_link = staging_dir / "Applications"
        if apps_link.exists() or apps_link.is_symlink():
            apps_link.unlink()
        apps_link.symlink_to("/Applications")

        subprocess.run([
            "hdiutil", "create",
            "-volname", "Cyrene",
            "-srcfolder", str(staging_dir),
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
    appimage_env = os.environ.copy()
    appimage_env.setdefault("ARCH", _appimage_arch())
    result = subprocess.run([
        appimagetool, str(appdir), str(output_path),
    ], check=False, env=appimage_env)

    shutil.rmtree(appdir, ignore_errors=True)

    require_appimage = os.environ.get("CYRENE_REQUIRE_APPIMAGE") == "1"
    if result.returncode != 0:
        print(f"  [error] appimagetool failed with exit code {result.returncode}")
        if require_appimage:
            sys.exit(result.returncode)

    if output_path.exists():
        print(f"  [ok] {output_path}")
        return output_path

    if require_appimage:
        print("  [error] AppImage output missing after appimagetool completed")
        sys.exit(1)
    return None


def _appimage_arch() -> str:
    machine = platform.machine().lower()
    arch_map = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }
    return arch_map.get(machine, machine or "x86_64")


def run_electron_builder() -> None:
    """Run electron-builder to package the Electron app around the PyInstaller bundle."""
    electron_dir = PROJECT_ROOT / "electron"

    def find_electron_builder() -> str | None:
        """Locate the electron-builder binary, checking common locations."""
        import shutil
        # 1) check PATH (npx may not be available on Windows CI)
        exe = shutil.which("electron-builder")
        if exe:
            return exe
        # 2) check node_modules/.bin directly
        bin_dir = electron_dir / "node_modules" / ".bin"
        candidates = ["electron-builder", "electron-builder.cmd"]
        for name in candidates:
            p = bin_dir / name
            if p.exists():
                return str(p)
        return None

    eb = find_electron_builder()
    if not eb:
        print("  [warn] electron-builder not found, skipping Electron packaging")
        print("  [hint] Run: cd electron && npm install")
        return

    print(f"\n[electron-builder] Packaging...")
    cmd = [eb]
    if IS_MAC:
        cmd.append("--mac")
    elif IS_WIN:
        cmd.append("--win")
    elif IS_LINUX:
        cmd.append("--linux")

    # On Windows, electron-builder is a .cmd file that needs shell=True
    # (otherwise CreateProcess fails with "not a valid Win32 application").
    result = subprocess.run(cmd, cwd=str(electron_dir), shell=IS_WIN)
    if result.returncode != 0:
        print("  [error] electron-builder failed")
        sys.exit(1)
    print("  [ok] electron-builder done")

    # macOS: sign the .app and repackage.
    if IS_MAC:
        mac_app = PROJECT_ROOT / "dist-electron" / "mac" / "Cyrene.app"
        if mac_app.exists():
            print(f"\n[macOS] Re-signing {mac_app}...")
            _codesign_mac(mac_app)
            # Strict verification: codesign -v --deep --strict checks that
            # EVERY Mach-O in the bundle is properly signed.
            _v = subprocess.run(
                ["codesign", "-v", "--deep", "--strict", str(mac_app)],
                capture_output=True, text=True, timeout=60,
            )
            if _v.returncode == 0:
                print("  [ok] strict verification passed")
            else:
                print(f"  [error] strict verification FAILED:\n{_v.stderr}")
                sys.exit(1)
            # Re-create the DMG — electron-builder's own DMG was built from
            # the unsigned .app (before we signed python-bundle).
            # hdiutil create with staging preserves resource forks and
            # extended attributes (code signatures) on macOS.
            import tempfile, shutil, glob
            version = get_version()
            dmg_path = PROJECT_ROOT / "dist-electron" / f"Cyrene-{version}-mac.dmg"
            _old_dmgs = sorted(glob.glob(str(PROJECT_ROOT / "dist-electron" / f"Cyrene-{version}-mac*.dmg")))
            with tempfile.TemporaryDirectory(prefix="cyrene-dmg-") as tmp_dir:
                staging_dir = Path(tmp_dir) / "Cyrene"
                staging_dir.mkdir(parents=True, exist_ok=True)
                staged_app = staging_dir / "Cyrene.app"
                # cp -R preserves extended attributes
                subprocess.run(["cp", "-R", str(mac_app), str(staged_app)], check=True)
                apps_link = staging_dir / "Applications"
                apps_link.symlink_to("/Applications")
                subprocess.run([
                    "hdiutil", "create",
                    "-volname", "Cyrene",
                    "-srcfolder", str(staging_dir),
                    "-ov", "-format", "UDZO",
                    str(dmg_path),
                ], check=True)
            for old in _old_dmgs:
                if old != str(dmg_path):
                    Path(old).unlink(missing_ok=True)
            print(f"  created: {dmg_path.name}")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Build Cyrene")
    parser.add_argument("--clean", action="store_true", help="仅清理构建产物")
    parser.add_argument("--skip-icons", action="store_true", help="跳过图标生成")
    parser.add_argument("--pyinstaller-only", action="store_true", help="只跑 PyInstaller，跳过 Electron 打包")
    args = parser.parse_args()

    print(f"Cyrene Builder — {sys.platform}")
    print(f"  project: {PROJECT_ROOT}")

    if args.clean:
        clean()
        return

    clean()

    if not args.skip_icons:
        generate_icons()

    build_webui_js()
    run_pyinstaller()

    if args.pyinstaller_only:
        print(f"\nDone: {DIST_DIR / 'Cyrene'}")
        return

    # Electron 打包
    run_electron_builder()

    # 列出产物
    electron_dist = PROJECT_ROOT / "dist-electron"
    if electron_dist.exists():
        print(f"\nDone: {electron_dist}")
        for f in sorted(electron_dist.iterdir()):
            print(f"  {f.name}")


if __name__ == "__main__":
    main()
