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
    print(f"Cyrene smoke test OK: v{get_version()}")
    for name, version in modules.items():
        print(f"{name}={version}")


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
        runpy.run_module("simplexng", run_name="__main__")
        raise SystemExit(0)

    if "--launch-web" in sys.argv:
        sys.argv.remove("--launch-web")
        sys.argv.append("--web")
        from cyrene.local_cli import main
        main()
        raise SystemExit(0)

    if "--gui" not in sys.argv:
        sys.argv.append("--gui")

    from cyrene.local_cli import main
    main()
