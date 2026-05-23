"""WebSocket bridge for Claude Code tmux sessions via pane capture/send-keys."""

from __future__ import annotations

import asyncio
import codecs
import logging
import subprocess

logger = logging.getLogger(__name__)


def _c1_to_7bit(data: bytes) -> bytes:
    """Convert 8-bit C1 control chars to 7-bit ANSI sequences.

    Crucially, this ONLY converts C1 chars that appear as standalone
    bytes, NOT bytes that are part of valid UTF-8 multi-byte sequences
    (where 0x80-0x9f serve as continuation bytes for box-drawing
    characters, CJK, etc.).
    """
    if not data:
        return data
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b >= 0xc0 and b <= 0xfd:
            # UTF-8 multi-byte start — copy the whole sequence verbatim
            # (start byte + N continuation bytes)
            if b < 0xe0:
                seq_len = 2
            elif b < 0xf0:
                seq_len = 3
            else:
                seq_len = 4
            out.extend(data[i:i + seq_len])
            i += seq_len
        elif 0x80 <= b <= 0x9f:
            # Standalone (not continuation) C1 control char
            replacement = {
                0x90: b"\x1bP",  # DCS
                0x9b: b"\x1b[",  # CSI
                0x9c: b"\x1b\\", # ST
                0x9d: b"\x1b]",  # OSC
                0x9e: b"\x1b^",  # PM
                0x9f: b"\x1b_",  # APC
            }.get(b, b"")
            out.extend(replacement)
            i += 1
        else:
            out.append(b)
            i += 1
    return bytes(out)


def _utf8_latin1_fallback(exc: UnicodeDecodeError) -> tuple[str, int]:
    """For invalid UTF-8 bytes: emit their Latin-1 codepoints and continue.

    This lets the rest of the string (valid UTF-8 multi-byte sequences,
    ANSI sequences, ASCII) decode correctly.
    """
    return ("".join(chr(b) for b in exc.object[exc.start:exc.end]), exc.end)


codecs.register_error("utf8+latin1", _utf8_latin1_fallback)

class CCTerminalSession:
    """Mirror a tmux pane to the browser and forward keystrokes with send-keys."""

    def __init__(self, tmux_session: str):
        self.tmux_session = tmux_session
        self._running = False
        self._pane_target = tmux_session
        self._last_render = ""
        self._first_frame = True
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
                if self._first_frame:
                    self._first_frame = False
                    await websocket.send_text("\x1bc" + rendered)
                else:
                    await websocket.send_text(rendered)
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
        proc = subprocess.run(
            ["tmux", "capture-pane", "-p", "-e", "-J", "-t", self._pane_target],
            capture_output=True, text=False, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"tmux capture-pane failed for {self._pane_target}")
        data = proc.stdout or b""
        # tmux-256color outputs 8-bit C1 control chars for colours,
        # attributes etc.  Convert them to standard 7-bit ANSI before
        # decoding so xterm.js understands them.  The converter is
        # UTF-8-aware so it won't corrupt multi-byte sequences whose
        # continuation bytes happen to be in the C1 range.
        data = _c1_to_7bit(data)
        # Decode as UTF-8 with latin-1 fallback for any remaining
        # non-UTF-8 bytes — keeps orphan bytes visible.
        return data.decode("utf-8", errors="utf8+latin1") if data else ""

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
