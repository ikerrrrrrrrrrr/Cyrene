"""
Debug logging for LLM calls. Logs every request/response to a file.
Activated by `python -m cyrene.local_cli --verbose`.
"""

import json
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from cyrene.config import DATA_DIR

logger = logging.getLogger(__name__)

VERBOSE = True
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

    # Generate event_id so this entry is queryable via get_full_event()
    import uuid as _uuid
    event_id = f"evt_{_uuid.uuid4().hex[:12]}"

    entry = {
        "type": "llm_call",
        "event_id": event_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "caller": caller,
        "phase": phase,
        "messages": clean_messages,
        "tools": tools,
        "response": _clean_for_json(response),
        "duration_ms": round(duration_ms, 1),
    }
    _write_entry(entry)
    # Also store in _full_events for fast lookup
    _full_events[event_id] = dict(entry)


def log_tool_call(caller: str, tool_name: str, args: dict, result: str, duration_ms: float) -> None:
    """Log one tool execution — FULL args and result."""
    if not VERBOSE:
        return
    import uuid as _uuid
    event_id = f"evt_{_uuid.uuid4().hex[:12]}"
    entry = {
        "type": "tool_call",
        "event_id": event_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "caller": caller,
        "tool": tool_name,
        "args": args,
        "result": str(result),
        "duration_ms": round(duration_ms, 1),
    }
    _write_entry(entry)
    _full_events[event_id] = dict(entry)



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
import uuid as _uuid

_event_queue: asyncio.Queue | None = None
_recent_events: deque[dict] = deque(maxlen=500)
_full_events: dict[str, dict] = {}
_MAX_FULL_EVENTS = 1000


def enable_event_bus() -> None:
    """启用事件总线。"""
    global _event_queue
    if _event_queue is None:
        _event_queue = asyncio.Queue(maxsize=5000)


async def publish_event(event: dict) -> None:
    """发布一条事件（由 agent.py 调用）。自动初始化事件总线。

    为 llm_call 和 tool_call 事件生成唯一 event_id，并存储完整数据到 _full_events。
    """
    if "timestamp" not in event:
        event = {**event, "timestamp": datetime.now(timezone.utc).isoformat()}

    # 为 llm_call 和 tool_call 生成 event_id 并保留完整数据
    if event.get("type") in ("llm_call", "tool_call"):
        event_id = f"evt_{_uuid.uuid4().hex[:12]}"
        event["event_id"] = event_id
        _full_events[event_id] = dict(event)
        # 控制 _full_events 大小
        if len(_full_events) > _MAX_FULL_EVENTS:
            overflow = len(_full_events) - _MAX_FULL_EVENTS
            for key in list(_full_events.keys())[:overflow]:
                _full_events.pop(key, None)

    _recent_events.append(event)
    if _event_queue is None:
        enable_event_bus()
    q = _event_queue
    if q is None:
        return
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        pass  # 队列满了就丢弃


def _search_debug_logs(event_id: str) -> dict | None:
    """Search all debug log files on disk for *event_id*."""
    if not DATA_DIR.exists():
        return None
    log_files = sorted(DATA_DIR.glob("debug_*.jsonl"), reverse=True)
    for log_file in log_files:
        if not log_file.exists():
            continue
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if entry.get("event_id") == event_id:
                        return entry
        except Exception:
            continue
    return None


def get_full_event(event_id: str) -> dict | None:
    """Return the full event data for *event_id*.

    Checks the in-memory _full_events dict first, then falls back to
    all debug JSONL log files on disk for persistence across daemon restarts.
    """
    # 1) Check in-memory dict
    event = _full_events.get(event_id)
    if event is not None:
        return event

    # 2) Fall back to debug log files on disk
    return _search_debug_logs(event_id)


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


def get_recent_events(limit: int = 200) -> list[dict]:
    """Return a copy of the most recent runtime events for live UI overlays."""
    if limit <= 0:
        return []
    return list(_recent_events)[-limit:]
