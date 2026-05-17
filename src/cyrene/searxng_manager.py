"""SearXNG subprocess manager — launches SimpleXNG as a managed child process.

No Docker required. SimpleXNG is a standalone pip-installable package that
vendors SearXNG and runs it via waitress on a configurable port.
"""

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import time

import httpx

logger = logging.getLogger(__name__)

_HEALTH_CHECK_TIMEOUT = 30.0
_HEALTH_CHECK_INTERVAL = 0.5


class SearXNGManager:
    """Manage a SimpleXNG subprocess lifecycle."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._url: str = ""

    @property
    def url(self) -> str:
        """The base URL of the running SearXNG instance, e.g. http://127.0.0.1:8888."""
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
            logger.info("SearXNG already running at %s", self._url)
            return self._url

        fd, log_path = tempfile.mkstemp(prefix="simplexng_", suffix=".log")
        os.close(fd)
        self._stderr_path = log_path

        try:
            self._process = subprocess.Popen(
                [sys.executable, "-m", "simplexng", "-p", str(port), "-H", host],
                stdout=subprocess.DEVNULL,
                stderr=open(log_path, "w"),
            )
        except FileNotFoundError:
            raise RuntimeError(
                "SimpleXNG is not installed. Run: pip install simplexng"
            ) from None
        except Exception as exc:
            raise RuntimeError(
                f"Failed to launch SimpleXNG: {exc}"
            ) from exc

        logger.info("SearXNG launching on %s (pid=%d) ...", self._url, self._process.pid)

        if not self._wait_ready():
            self._dump_stderr()
            self.stop()
            raise RuntimeError(
                f"SearXNG did not become healthy within {_HEALTH_CHECK_TIMEOUT}s"
            )

        logger.info("SearXNG ready at %s", self._url)
        return self._url

    def stop(self) -> None:
        """Terminate the SimpleXNG subprocess gracefully, then force-kill."""
        if self._process is None:
            return
        proc, self._process = self._process, None
        self._url = ""

        if proc.poll() is not None:
            logger.info("SearXNG process already exited (rc=%d)", proc.returncode)
            return

        logger.info("Stopping SearXNG (pid=%d)...", proc.pid)
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("SearXNG did not exit gracefully, force-killing")
                proc.kill()
                proc.wait(timeout=3)
        except Exception as exc:
            logger.warning("Error stopping SearXNG: %s", exc)

    def _dump_stderr(self) -> None:
        """Log the contents of the stderr capture file."""
        path = getattr(self, "_stderr_path", None)
        if not path:
            return
        try:
            text = open(path).read()
            if text.strip():
                logger.error("SearXNG stderr (%s):\n%s", path, text[-4000:])
        except Exception:
            pass

    def _wait_ready(self) -> bool:
        """Poll the health-check endpoint until the server responds 200."""
        deadline = time.monotonic() + _HEALTH_CHECK_TIMEOUT
        url = f"{self._url}/search?q=healthcheck&format=json"

        while time.monotonic() < deadline:
            if self._process and self._process.poll() is not None:
                logger.error("SearXNG exited prematurely (rc=%d)", self._process.returncode)
                return False
            try:
                r = httpx.get(url, timeout=3.0)
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


async def start_searxng(port: int = 8888, host: str = "127.0.0.1") -> str:
    """Convenience: start SearXNG in a thread and return the URL."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_manager().start, port, host)


def stop_searxng() -> None:
    """Convenience: stop the SearXNG subprocess."""
    get_manager().stop()
