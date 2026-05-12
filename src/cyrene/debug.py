"""
Debug logging for LLM calls. Logs every request/response to a file.
Activated by `python -m cyrene.local_cli --verbose`.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from cyrene.config import DATA_DIR

logger = logging.getLogger(__name__)

VERBOSE = False
_log_file: Path | None = None


def init_debug_log() -> None:
    """Create a timestamped debug log file."""
    global _log_file
    if not VERBOSE:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    _log_file = DATA_DIR / f"debug_{ts}.jsonl"
    _write_entry({"type": "session_start", "timestamp": datetime.now(timezone.utc).isoformat()})
    logger.info("Debug log: %s", _log_file)


def _write_entry(entry: dict) -> None:
    """Append a JSON line to the debug log."""
    if _log_file is None:
        return
    try:
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def log_llm_call(
    caller: str,
    phase: str,
    messages: list,
    tools: list | None,
    response: dict,
    duration_ms: float,
) -> None:
    """Log one LLM call (request + response)."""
    if not VERBOSE:
        return

    # Strip content for readability but keep structure
    entry = {
        "type": "llm_call",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "caller": caller,
        "phase": phase,
        "num_messages": len(messages),
        "messages_preview": _preview_messages(messages),
        "tools": [t.get("function", {}).get("name", "?") for t in (tools or [])],
        "response_content_preview": (response.get("content") or "")[:200],
        "response_tool_calls": [
            {"name": tc.get("function", {}).get("name", "?"), "args": tc.get("function", {}).get("arguments", "")[:100]}
            for tc in (response.get("tool_calls") or [])
        ],
        "duration_ms": round(duration_ms, 1),
    }
    _write_entry(entry)


def log_tool_call(caller: str, tool_name: str, args: dict, result: str, duration_ms: float) -> None:
    """Log one tool execution."""
    if not VERBOSE:
        return
    entry = {
        "type": "tool_call",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "caller": caller,
        "tool": tool_name,
        "args": args,
        "result_preview": str(result)[:200],
        "duration_ms": round(duration_ms, 1),
    }
    _write_entry(entry)


def log_chat_filter(text: str, result: str, duration_ms: float) -> None:
    """Log chat filter translation."""
    if not VERBOSE:
        return
    entry = {
        "type": "chat_filter",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "input_preview": text[:200],
        "output_preview": result[:200],
        "duration_ms": round(duration_ms, 1),
    }
    _write_entry(entry)


def _preview_messages(messages: list) -> list:
    """Return a compact preview of messages (truncated)."""
    preview = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        tool_calls = m.get("tool_calls")
        entry = {"role": role}
        if content:
            entry["content_preview"] = str(content)[:150]
        if tool_calls:
            entry["tool_calls"] = len(tool_calls)
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
        preview.append(entry)
    return preview


def get_log_path() -> str:
    """Return the current debug log path, or empty string."""
    return str(_log_file) if _log_file else ""
