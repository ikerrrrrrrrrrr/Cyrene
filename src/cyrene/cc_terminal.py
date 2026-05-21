"""WebSocket bridge for Claude Code tmux sessions via pane capture/send-keys."""

from __future__ import annotations

import asyncio
import logging
import subprocess

logger = logging.getLogger(__name__)

_FULL_REDRAW = "\x1bc"


class CCTerminalSession:
    """Mirror a tmux pane to the browser and forward keystrokes with send-keys."""

    def __init__(self, tmux_session: str):
        self.tmux_session = tmux_session
        self._running = False
        self._pane_target = tmux_session
        self._last_render = ""
        self._cols = 120
        self._rows = 32

    async def start(self, cols: int = 120, rows: int = 32) -> None:
        """Resolve the tmux pane and prime the initial render state."""
        self._cols = max(20, int(cols or 80))
        self._rows = max(6, int(rows or 24))
        pane_target = await asyncio.to_thread(self._resolve_pane_target)
        if not pane_target:
            raise RuntimeError(f"No tmux pane found for session {self.tmux_session}")
        self._pane_target = pane_target
        self._running = True

    async def stream_to_ws(self, websocket) -> None:
        """Poll tmux pane content and redraw the browser when it changes."""
        while self._running:
            try:
                rendered = await asyncio.to_thread(self._capture_pane)
            except Exception:
                logger.exception("Failed capturing tmux pane for session %s", self.tmux_session)
                break
            if rendered != self._last_render:
                self._last_render = rendered
                await websocket.send_text(_FULL_REDRAW + rendered)
            await asyncio.sleep(0.18)
        self._running = False

    async def handle_input(self, data: str) -> None:
        """Translate browser keystrokes into tmux send-keys operations."""
        if not data or not self._running:
            return
        operations = _decode_input(data)
        for args in operations:
            await asyncio.to_thread(self._run_tmux, ["tmux", "send-keys", "-t", self._pane_target, *args])

    async def handle_resize(self, cols: int, rows: int) -> None:
        """Persist browser viewport size for future capture logic."""
        self._cols = max(20, int(cols or 80))
        self._rows = max(6, int(rows or 24))

    async def stop(self) -> None:
        """Stop polling."""
        self._running = False

    def _resolve_pane_target(self) -> str:
        proc = self._run_tmux(
            ["tmux", "list-panes", "-t", self.tmux_session, "-F", "#{pane_id}"],
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"tmux list-panes failed for {self.tmux_session}")
        pane_id = proc.stdout.splitlines()[0].strip() if proc.stdout else ""
        return pane_id or self.tmux_session

    def _capture_pane(self) -> str:
        proc = self._run_tmux(
            ["tmux", "capture-pane", "-p", "-e", "-J", "-t", self._pane_target],
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"tmux capture-pane failed for {self._pane_target}")
        return proc.stdout

    def _run_tmux(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )


def _decode_input(data: str) -> list[list[str]]:
    """Convert xterm key data into tmux send-keys arguments."""
    operations: list[list[str]] = []
    literal_buffer = ""
    index = 0
    while index < len(data):
        char = data[index]
        if char == "\x1b":
            sequence = data[index:index + 3]
            mapped = _map_escape_sequence(sequence)
            if mapped is not None:
                if literal_buffer:
                    operations.append(["-l", literal_buffer])
                    literal_buffer = ""
                operations.append([mapped])
                index += len(sequence)
                continue
            if literal_buffer:
                operations.append(["-l", literal_buffer])
                literal_buffer = ""
            operations.append(["Escape"])
            index += 1
            continue
        if char in ("\r", "\n"):
            if literal_buffer:
                operations.append(["-l", literal_buffer])
                literal_buffer = ""
            operations.append(["Enter"])
            index += 1
            continue
        if char in ("\x7f", "\b"):
            if literal_buffer:
                operations.append(["-l", literal_buffer])
                literal_buffer = ""
            operations.append(["BSpace"])
            index += 1
            continue
        if char == "\t":
            if literal_buffer:
                operations.append(["-l", literal_buffer])
                literal_buffer = ""
            operations.append(["Tab"])
            index += 1
            continue
        if ord(char) < 32:
            if literal_buffer:
                operations.append(["-l", literal_buffer])
                literal_buffer = ""
            ctrl = chr(ord(char) + 96)
            operations.append([f"C-{ctrl}"])
            index += 1
            continue
        literal_buffer += char
        index += 1

    if literal_buffer:
        operations.append(["-l", literal_buffer])
    return operations


def _map_escape_sequence(sequence: str) -> str | None:
    mapping = {
        "\x1b[A": "Up",
        "\x1b[B": "Down",
        "\x1b[C": "Right",
        "\x1b[D": "Left",
        "\x1b[H": "Home",
        "\x1b[F": "End",
        "\x1bOH": "Home",
        "\x1bOF": "End",
    }
    return mapping.get(sequence)
