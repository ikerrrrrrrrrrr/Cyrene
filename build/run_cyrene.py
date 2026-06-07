"""PyInstaller 入口 — 原生桌面窗口模式启动 Cyrene。"""
import sys

import anyio
import aiosqlite
import apscheduler
import certifi
import croniter
import h11
import httpcore
import httpx
import importlib
import jinja2
import multipart
import simplexng
import sniffio
import websockets


def _run_smoke_test() -> None:
    """Verify frozen runtime can import critical dependencies before release."""
    from cyrene.version import get_version

    modules = {
        "httpx": httpx.__version__,
        "httpcore": getattr(httpcore, "__version__", "unknown"),
        "anyio": getattr(anyio, "__version__", "unknown"),
        "certifi": getattr(certifi, "__version__", "unknown"),
        "h11": getattr(h11, "__version__", "unknown"),
        "sniffio": getattr(sniffio, "__version__", "unknown"),
        "websockets": getattr(websockets, "__version__", "unknown"),
        "jinja2": getattr(jinja2, "__version__", "unknown"),
        "aiosqlite": getattr(aiosqlite, "__version__", "unknown"),
        "apscheduler": getattr(apscheduler, "__version__", "unknown"),
        "croniter": getattr(croniter, "__version__", "unknown"),
        "simplexng": getattr(simplexng, "__version__", "unknown"),
        "multipart": getattr(multipart, "__version__", "unknown"),
    }
    # Smoke-test imports for modules with C extensions that are
    # historically fragile in PyInstaller frozen builds.
    _smoke_imports = {
        "PIL": None,
        "pypdf": None,
        "reportlab": None,
        "mcp": None,
        "uvicorn": None,
        "fastapi": None,
        "pydantic_core": None,
        "starlette": None,
    }
    for _name in _smoke_imports:
        try:
            mod = importlib.import_module(_name)
            _smoke_imports[_name] = getattr(mod, "__version__", "ok")
        except Exception as exc:
            _smoke_imports[_name] = f"FAILED: {exc}"
    print(f"Cyrene smoke test OK: v{get_version()}")
    for name, version in modules.items():
        print(f"{name}={version}")
    for _name, _ver in _smoke_imports.items():
        print(f"{_name}={_ver}")


def _write_crash_log(exc: BaseException) -> None:
    """Write traceback to cyrene_error.log in the OS temp dir.

    On Windows with console=False the process has no console, so Electron's
    stderr pipe may not receive PyInstaller's C-level output. Writing directly
    from Python guarantees a readable crash log on every platform.
    """
    import os, tempfile, traceback, datetime
    log_path = os.path.join(tempfile.gettempdir(), "cyrene_error.log")
    try:
        with open(log_path, "a", encoding="utf-8") as _f:
            _f.write(f"\n--- {datetime.datetime.now().isoformat()} ---\n")
            traceback.print_exc(file=_f)
    except Exception:
        pass


if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        _run_smoke_test()
        raise SystemExit(0)

    # In a PyInstaller frozen build, sys.executable is the app binary itself.
    # External code (searxng_manager, cli) used to call "sys.executable -m ..."
    # which would launch another full instance of the app — recursive spawning.
    # These flags let the frozen binary act as a trampoline for bundled modules.
    if "--launch-simplexng" in sys.argv:
        sys.argv.remove("--launch-simplexng")
        import runpy
        runpy.run_module("simplexng.simplexng", run_name="__main__")
        raise SystemExit(0)

    if "--launch-web" in sys.argv:
        sys.argv.remove("--launch-web")
        if "--electron" in sys.argv:
            sys.argv.remove("--electron")
            sys.argv.append("--electron-mode")
        else:
            sys.argv.append("--web")
        try:
            from cyrene.local_cli import main
            main()
        except Exception as _exc:
            _write_crash_log(_exc)
            raise
        raise SystemExit(0)

    if "--gui" not in sys.argv:
        sys.argv.append("--gui")

    try:
        from cyrene.local_cli import main
        main()
    except Exception as _exc:
        _write_crash_log(_exc)
        raise
