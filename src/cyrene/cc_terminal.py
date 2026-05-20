"""WebSocket terminal bridge for Claude Code tmux sessions."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import select
import signal
import struct
import subprocess
import termios
from pathlib import Path

logger = logging.getLogger(__name__)


class CCTerminalSession:
    """Attach a temporary PTY client to a tmux session and stream it to the UI."""

    def __init__(self, tmux_session: str):
        self.tmux_session = tmux_session
        self._master_fd: int | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._running = False

    async def start(self, cols: int = 120, rows: int = 32) -> None:
        """Spawn a tmux client bound to a dedicated PTY."""
        master_fd, slave_fd = os.openpty()
        try:
            self._master_fd = master_fd
            self._set_winsize(cols, rows)
            env = dict(os.environ)
            env.setdefault("TERM", "xterm-256color")
            self._proc = subprocess.Popen(
                ["tmux", "attach-session", "-t", self.tmux_session],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
                env=env,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            self._master_fd = None
            raise
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                pass

        os.set_blocking(master_fd, False)
        self._running = True

    async def stream_to_ws(self, websocket) -> None:
        """Continuously forward PTY output to the browser."""
        while self._running:
            try:
                chunk = await asyncio.to_thread(self._read_chunk)
            except Exception:
                logger.exception("Failed reading tmux PTY stream")
                break
            if chunk is None:
                break
            if not chunk:
                continue
            await websocket.send_text(chunk.decode("utf-8", errors="replace"))
        self._running = False

    async def handle_input(self, data: str) -> None:
        """Forward raw keyboard input bytes to the PTY."""
        if not data or self._master_fd is None:
            return
        await asyncio.to_thread(os.write, self._master_fd, data.encode("utf-8", errors="ignore"))

    async def handle_resize(self, cols: int, rows: int) -> None:
        """Resize the PTY so tmux redraws for the browser viewport."""
        if self._master_fd is None:
            return
        await asyncio.to_thread(self._set_winsize, cols, rows)
        if self._proc is not None:
            try:
                os.kill(self._proc.pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass

    async def stop(self) -> None:
        """Terminate the temporary tmux client and release the PTY."""
        self._running = False

        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    await asyncio.to_thread(proc.wait, 1.0)
                except subprocess.TimeoutExpired:
                    logger.warning("tmux PTY process did not exit promptly for session %s", self.tmux_session)

        master_fd = self._master_fd
        self._master_fd = None
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass

    def _read_chunk(self) -> bytes | None:
        if self._master_fd is None:
            return None
        ready, _, _ = select.select([self._master_fd], [], [], 0.25)
        if not ready:
            return b""
        try:
            return os.read(self._master_fd, 65536)
        except BlockingIOError:
            return b""
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                return None
            raise

    def _set_winsize(self, cols: int, rows: int) -> None:
        if self._master_fd is None:
            return
        safe_cols = max(20, int(cols or 80))
        safe_rows = max(6, int(rows or 24))
        packed = struct.pack("HHHH", safe_rows, safe_cols, 0, 0)
        fcntl = __import__("fcntl")
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, packed)
