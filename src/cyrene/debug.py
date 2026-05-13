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
    """Log one LLM call (request + response) — FULL content, no truncation."""
    if not VERBOSE:
        return

    # Clean messages for JSON serialization (remove non-serializable fields)
    clean_messages = _clean_for_json(messages)

    entry = {
        "type": "llm_call",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "caller": caller,
        "phase": phase,
        "messages": clean_messages,
        "tools": tools,
        "response": _clean_for_json(response),
        "duration_ms": round(duration_ms, 1),
    }
    _write_entry(entry)


def log_tool_call(caller: str, tool_name: str, args: dict, result: str, duration_ms: float) -> None:
    """Log one tool execution — FULL args and result."""
    if not VERBOSE:
        return
    entry = {
        "type": "tool_call",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "caller": caller,
        "tool": tool_name,
        "args": args,
        "result": str(result),
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


def _clean_for_json(obj):
    """Recursively clean an object for JSON serialization."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_for_json(i) for i in obj]
    return str(obj)


def get_log_path() -> str:
    """Return the current debug log path, or empty string."""
    return str(_log_file) if _log_file else ""


# ---------------------------------------------------------------------------
# Event bus — 实时事件推送给 Web UI
# ---------------------------------------------------------------------------

import asyncio

_event_queue: asyncio.Queue | None = None


def enable_event_bus() -> None:
    """启用事件总线。"""
    global _event_queue
    if _event_queue is None:
        _event_queue = asyncio.Queue(maxsize=5000)


async def publish_event(event: dict) -> None:
    """发布一条事件（由 agent.py 调用）。自动初始化事件总线。"""
    if _event_queue is None:
        enable_event_bus()
    q = _event_queue
    if q is None:
        return
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        pass  # 队列满了就丢弃


async def subscribe():
    """Async generator — 供 SSE 端点消费事件流。

    自动初始化事件总线。每 15 秒发一次心跳保活。
    """
    if _event_queue is None:
        enable_event_bus()
    q = _event_queue
    if q is None:
        return
    while True:
        try:
            event = await asyncio.wait_for(q.get(), timeout=15.0)
            yield event
        except asyncio.TimeoutError:
            yield {"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}
