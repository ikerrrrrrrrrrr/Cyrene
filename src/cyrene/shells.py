"""Persistent independent shell sessions for long-running agent workflows."""

import asyncio
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cyrene import debug
from cyrene.config import WORKSPACE_DIR

_shells: dict[str, dict[str, Any]] = {}
_shell_lock = asyncio.Lock()
_shell_counter = 0


def _resolve_cwd(path_str: str) -> Path:
    candidate = Path(path_str or ".")
    path = candidate if candidate.is_absolute() else WORKSPACE_DIR / candidate
    resolved = path.resolve()
    workspace = WORKSPACE_DIR.resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError(f"Path escapes workspace: {path_str}")
    return resolved


def _short_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")
    except Exception:
        return "—"


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m:02d}:{s:02d}"


async def _publish_shell_update(shell_id: str) -> None:
    snap = get_shell_snapshot(shell_id)
    if not snap:
        return
    await debug.publish_event({
        "type": "shell_update",
        "shell_id": shell_id,
        "status": snap.get("status", ""),
        "title": snap.get("title", ""),
        "cwd": snap.get("cwd", ""),
        "round_id": snap.get("roundId", ""),
    })


async def _append_lines(shell_id: str, kind: str, text: str) -> None:
    text = str(text or "")
    if not text:
        return
    async with _shell_lock:
        shell = _shells.get(shell_id)
        if shell is None:
            return
        for raw_line in text.splitlines():
            shell["lines"].append({"kind": kind, "text": raw_line})
        shell["updated_at"] = datetime.now(timezone.utc).isoformat()
    await _publish_shell_update(shell_id)


async def _pump_stream(shell_id: str, stream: asyncio.StreamReader | None, kind: str) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = await stream.readline()
            if not chunk:
                break
            await _append_lines(shell_id, kind, chunk.decode("utf-8", errors="replace").rstrip("\n"))
    except Exception:
        await _append_lines(shell_id, "err", f"[{kind} stream error]")


async def _watch_shell(shell_id: str) -> None:
    proc = None
    async with _shell_lock:
        shell = _shells.get(shell_id)
        if shell is not None:
            proc = shell.get("proc")
    if proc is None:
        return
    try:
        code = await proc.wait()
    except Exception:
        code = -1
    async with _shell_lock:
        shell = _shells.get(shell_id)
        if shell is None:
            return
        shell["status"] = "done" if code == 0 else "err"
        shell["exit_code"] = code
        shell["updated_at"] = datetime.now(timezone.utc).isoformat()
    await _publish_shell_update(shell_id)


async def start_shell(command: str = "", cwd: str = ".", title: str = "", round_id: str = "") -> dict[str, Any]:
    """Start an independent persistent shell session."""
    global _shell_counter
    shell_bin = os.environ.get("SHELL") or "/bin/bash"
    resolved_cwd = _resolve_cwd(cwd)
    env = dict(os.environ)
    env["PS1"] = ""
    env.setdefault("TERM", "dumb")
    proc = await asyncio.create_subprocess_exec(
        shell_bin,
        "-i",
        cwd=str(resolved_cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _shell_counter += 1
    shell_id = f"shell_{int(time.time() * 1000)}_{_shell_counter}"
    now = datetime.now(timezone.utc).isoformat()
    async with _shell_lock:
        _shells[shell_id] = {
            "id": shell_id,
            "title": title.strip() or "independent shell",
            "cwd": str(resolved_cwd.relative_to(WORKSPACE_DIR.resolve())) if resolved_cwd != WORKSPACE_DIR.resolve() else ".",
            "pid": proc.pid,
            "status": "running",
            "round_id": round_id,
            "created_at": now,
            "updated_at": now,
            "exit_code": None,
            "proc": proc,
            "lines": deque(maxlen=240),
        }
        _shells[shell_id]["stdout_task"] = asyncio.create_task(_pump_stream(shell_id, proc.stdout, "out"))
        _shells[shell_id]["stderr_task"] = asyncio.create_task(_pump_stream(shell_id, proc.stderr, "err"))
        _shells[shell_id]["watch_task"] = asyncio.create_task(_watch_shell(shell_id))
    await _append_lines(shell_id, "meta", f"[shell started: {shell_bin}]")
    if command.strip():
        await send_shell(shell_id, command)
    return get_shell_snapshot(shell_id) or {}


async def send_shell(shell_id: str, command: str, wait_ms: int = 700) -> dict[str, Any]:
    """Send a command to a running shell and return the updated snapshot."""
    async with _shell_lock:
        shell = _shells.get(shell_id)
        if shell is None:
            raise ValueError(f"Unknown shell: {shell_id}")
        proc = shell.get("proc")
        if proc is None or proc.stdin is None:
            raise ValueError(f"Shell {shell_id} is not writable")
        if shell.get("status") != "running":
            raise ValueError(f"Shell {shell_id} is not running")
        proc.stdin.write((command.rstrip("\n") + "\n").encode("utf-8"))
        await proc.stdin.drain()
        shell["lines"].append({"kind": "prompt", "text": f"$ {command}"})
        shell["updated_at"] = datetime.now(timezone.utc).isoformat()
    await _publish_shell_update(shell_id)
    await asyncio.sleep(max(0, wait_ms) / 1000)
    return get_shell_snapshot(shell_id) or {}


async def close_shell(shell_id: str) -> dict[str, Any]:
    """Terminate a persistent shell session."""
    async with _shell_lock:
        shell = _shells.get(shell_id)
        if shell is None:
            raise ValueError(f"Unknown shell: {shell_id}")
        proc = shell.get("proc")
        if proc and proc.returncode is None:
            proc.terminate()
    await asyncio.sleep(0.1)
    return get_shell_snapshot(shell_id) or {}


def get_shell_snapshot(shell_id: str) -> dict[str, Any] | None:
    shell = _shells.get(shell_id)
    if shell is None:
        return None
    created_at = shell.get("created_at")
    elapsed = "—"
    if created_at:
        try:
            created_dt = datetime.fromisoformat(str(created_at)).astimezone(timezone.utc)
            elapsed = _format_duration((datetime.now(timezone.utc) - created_dt).total_seconds())
        except Exception:
            elapsed = "—"
    return {
        "id": shell_id,
        "title": shell.get("title", "independent shell"),
        "cwd": shell.get("cwd", "."),
        "pid": shell.get("pid", "—"),
        "status": shell.get("status", "running"),
        "roundId": shell.get("round_id", ""),
        "createdAt": _short_time(shell.get("created_at")),
        "updatedAt": _short_time(shell.get("updated_at")),
        "elapsed": elapsed,
        "lines": list(shell.get("lines", [])),
    }


def list_shells(include_exited: bool = False) -> list[dict[str, Any]]:
    items = []
    for shell_id, shell in _shells.items():
        if not include_exited and shell.get("status") != "running":
            continue
        snap = get_shell_snapshot(shell_id)
        if snap:
            items.append(snap)
    items.sort(key=lambda item: item.get("createdAt", ""), reverse=True)
    return items
