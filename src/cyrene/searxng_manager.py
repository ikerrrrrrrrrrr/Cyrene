"""SearXNG subprocess manager — launches SimpleXNG as a managed child process.

No Docker required. SimpleXNG is a standalone pip-installable package that
vendors SearXNG and runs it via waitress on a configurable port.
"""

import asyncio
import importlib.util
import logging
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import getproxies

import httpx
import yaml

from cyrene.config import DATA_DIR, SEARCH_PROXY

logger = logging.getLogger(__name__)

_HEALTH_CHECK_TIMEOUT = 30.0
_HEALTH_CHECK_INTERVAL = 0.5
_SIMPLEXNG_SETTINGS_PATH = DATA_DIR / "simplexng_settings.yml"


class SearXNGManager:
    """Manage a SimpleXNG subprocess lifecycle."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._url: str = ""

    @property
    def url(self) -> str:
        """The base URL of the running SimpleXNG instance, e.g. http://127.0.0.1:8888."""
        return self._url

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, port: int = 8888, host: str = "127.0.0.1") -> str:
        """Launch SimpleXNG and wait until it is ready to serve requests.

        Returns the base URL on success.  Raises RuntimeError if the process
        fails to start or doesn't become healthy within the timeout.
        """
        self._url = f"http://{host}:{port}"

        if self.is_running:
            logger.info("SimpleXNG already running at %s", self._url)
            return self._url

        fd, log_path = tempfile.mkstemp(prefix="simplexng_", suffix=".log")
        os.close(fd)
        self._stderr_path = log_path

        try:
            settings_path = _write_simplexng_settings(port, host)
            launch_cmd = _build_simplexng_launch_cmd(port, host, settings_path=settings_path)
            env = _build_simplexng_env(settings_path)
            self._process = subprocess.Popen(
                launch_cmd,
                stdout=subprocess.DEVNULL,
                stderr=open(log_path, "w"),
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "SimpleXNG is not installed. Run: pip install simplexng"
            ) from None
        except Exception as exc:
            raise RuntimeError(
                f"Failed to launch SimpleXNG: {exc}"
            ) from exc

        logger.info("SimpleXNG launching on %s (pid=%d) ...", self._url, self._process.pid)

        if not self._wait_ready():
            self._dump_stderr()
            self.stop()
            raise RuntimeError(
                f"SimpleXNG did not become healthy within {_HEALTH_CHECK_TIMEOUT}s"
            )

        logger.info("SimpleXNG ready at %s", self._url)
        return self._url

    def stop(self) -> None:
        """Terminate the SimpleXNG subprocess gracefully, then force-kill."""
        if self._process is None:
            return
        proc, self._process = self._process, None
        self._url = ""

        if proc.poll() is not None:
            logger.info("SimpleXNG process already exited (rc=%d)", proc.returncode)
            return

        logger.info("Stopping SimpleXNG (pid=%d)...", proc.pid)
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("SimpleXNG did not exit gracefully, force-killing")
                proc.kill()
                proc.wait(timeout=3)
        except Exception as exc:
            logger.warning("Error stopping SimpleXNG: %s", exc)

    def _dump_stderr(self) -> None:
        """Log the contents of the stderr capture file."""
        path = getattr(self, "_stderr_path", None)
        if not path:
            return
        try:
            text = open(path).read()
            if text.strip():
                logger.error("SimpleXNG stderr (%s):\n%s", path, text[-4000:])
        except Exception:
            pass

    def _wait_ready(self) -> bool:
        """Poll the local HTTP endpoint until the server responds 200."""
        deadline = time.monotonic() + _HEALTH_CHECK_TIMEOUT
        url = f"{self._url}/"

        while time.monotonic() < deadline:
            if self._process and self._process.poll() is not None:
                logger.error("SimpleXNG exited prematurely (rc=%d)", self._process.returncode)
                return False
            try:
                r = httpx.get(url, timeout=3.0, trust_env=False)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(_HEALTH_CHECK_INTERVAL)

        return False


_manager: SearXNGManager | None = None


def get_manager() -> SearXNGManager:
    """Return the module-level singleton SearXNGManager."""
    global _manager
    if _manager is None:
        _manager = SearXNGManager()
    return _manager


def _write_simplexng_settings(port: int, host: str) -> Path:
    """Write the SimpleXNG settings file managed by Cyrene."""
    try:
        from simplexng.settings import get_bundled_template

        template_path = get_bundled_template()
    except Exception as exc:
        raise RuntimeError(f"Could not locate SimpleXNG settings template: {exc}") from exc

    settings = yaml.safe_load(Path(template_path).read_text(encoding="utf-8"))
    settings["server"]["port"] = port
    settings["server"]["bind_address"] = host
    settings["server"]["secret_key"] = secrets.token_hex(16)

    formats = settings.setdefault("search", {}).setdefault("formats", [])
    if "json" not in formats:
        formats.append("json")

    proxy_url = _get_effective_search_proxy()
    outgoing = settings.setdefault("outgoing", {})
    if proxy_url:
        outgoing["proxies"] = {"all://": [proxy_url]}
        outgoing["extra_proxy_timeout"] = 10
        outgoing["request_timeout"] = max(float(outgoing.get("request_timeout") or 3.0), 15.0)
    else:
        outgoing.pop("proxies", None)
        outgoing.pop("extra_proxy_timeout", None)

    _SIMPLEXNG_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Generated by Cyrene. Do not edit while Cyrene is running.\n"
        f"# Port: {port}, Host: {host}\n"
        f"# Proxy: {'configured' if proxy_url else 'not configured'}\n\n"
        f"{yaml.dump(settings, default_flow_style=False, sort_keys=False)}"
    )
    _SIMPLEXNG_SETTINGS_PATH.write_text(content, encoding="utf-8")
    return _SIMPLEXNG_SETTINGS_PATH


def _build_simplexng_env(settings_path: Path) -> dict[str, str]:
    """Build environment for the SimpleXNG child process."""
    env = os.environ.copy()
    env["SEARXNG_SETTINGS_PATH"] = str(settings_path)
    proxy_url = _get_effective_search_proxy()
    if proxy_url:
        env["HTTP_PROXY"] = proxy_url
        env["HTTPS_PROXY"] = proxy_url
        env["http_proxy"] = proxy_url
        env["https_proxy"] = proxy_url
    env["NO_PROXY"] = _merge_no_proxy(env.get("NO_PROXY") or env.get("no_proxy") or "")
    env["no_proxy"] = env["NO_PROXY"]
    return env


def _get_effective_search_proxy() -> str:
    """Return the configured or system proxy if it is reachable."""
    proxy_url = (SEARCH_PROXY or "").strip()
    if not proxy_url:
        proxies = getproxies()
        proxy_url = (
            proxies.get("https")
            or proxies.get("http")
            or proxies.get("all")
            or proxies.get("all://")
            or ""
        )
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return ""
    if not _is_proxy_reachable(proxy_url):
        logger.warning("Ignoring unreachable search proxy: %s", proxy_url)
        return ""
    return proxy_url


def _is_proxy_reachable(proxy_url: str, timeout: float = 1.5) -> bool:
    parsed = urlparse(proxy_url)
    host = parsed.hostname
    port = parsed.port
    if not host:
        return False
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _merge_no_proxy(existing: str) -> str:
    entries = [item.strip() for item in existing.split(",") if item.strip()]
    required = ["127.0.0.1", "localhost", "::1"]
    lowered = {item.lower() for item in entries}
    for item in required:
        if item.lower() not in lowered:
            entries.append(item)
    return ",".join(entries)


def _build_simplexng_launch_cmd(port: int, host: str, *, settings_path: Path | None = None) -> list[str]:
    """Build a launch command compatible with different SimpleXNG package layouts."""
    args = ["-p", str(port), "-H", host]
    if settings_path is not None:
        args.extend(["--settings", str(settings_path)])

    # In a PyInstaller frozen build, sys.executable is the app binary itself.
    # Running it with "-m" would launch another full instance — recursive spawn.
    # Instead we use a trampoline flag that run_cyrene.py understands.
    if getattr(sys, "frozen", False):
        return [sys.executable, "--launch-simplexng", *args]

    if importlib.util.find_spec("simplexng.__main__") is not None:
        return [sys.executable, "-m", "simplexng", *args]

    if importlib.util.find_spec("simplexng.simplexng") is not None:
        return [sys.executable, "-m", "simplexng.simplexng", *args]

    script_path = Path(sys.executable).resolve().parent / "simplexng"
    if script_path.exists():
        return [str(script_path), *args]

    raise FileNotFoundError("Could not locate a runnable SimpleXNG entrypoint")


async def start_searxng(port: int = 8888, host: str = "127.0.0.1") -> str:
    """Convenience: start SimpleXNG in a thread and return the URL."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_manager().start, port, host)


def stop_searxng() -> None:
    """Convenience: stop the SimpleXNG subprocess."""
    get_manager().stop()
