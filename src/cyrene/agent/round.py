"""Round and live-round tracking.  Depends on ``session`` and ``state``."""

import logging
from datetime import datetime, timezone
from typing import Any

from cyrene.agent.message import _round_started_iso, _round_title_from_entry
from cyrene.agent.session import _load_pending_question, _load_session_messages
from cyrene.agent.state import (
    _active_main_round_id,
    _active_main_round_public_prompt,
    _active_main_round_started_at,
    _MAIN_INBOX_AGENT_ID,
)

logger = logging.getLogger(__name__)


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


def _session_round_entries() -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    messages = _load_session_messages()
    for msg in messages:
        round_id = str(msg.get("round_id", "")).strip()
        if not round_id:
            continue
        entry = entries.setdefault(round_id, {
            "id": round_id, "title": "", "prompt": "", "last_user": "", "last_assistant": "",
            "status": "done", "pending_guidance": 0, "subagent_count": 0, "running_subagents": 0,
            "started_at": _round_started_iso(round_id), "updated_at": _round_started_iso(round_id),
        })
        if msg.get("round_title"):
            entry["title"] = str(msg.get("round_title") or "").strip()
        role = str(msg.get("role") or "")
        content = str(msg.get("content") or "").strip()
        if role == "user" and content:
            if not entry["prompt"]:
                entry["prompt"] = content
            entry["last_user"] = content
        elif role == "assistant" and content:
            entry["last_assistant"] = content
            if not entry["title"] and bool(msg.get("system_initiated")):
                entry["title"] = "proactive check-in"
    return entries


def _main_inbox_pending_by_round() -> dict[str, int]:
    from cyrene.inbox import get_unread_messages
    counts: dict[str, int] = {}
    for message in get_unread_messages(_MAIN_INBOX_AGENT_ID):
        if str(message.get("type", "")).strip() != "guidance":
            continue
        round_id = str(message.get("round_id", "")).strip()
        if not round_id:
            continue
        counts[round_id] = counts.get(round_id, 0) + 1
    return counts


def _pending_question_live_entry() -> dict[str, Any]:
    pending = _load_pending_question()
    round_id = str(pending.get("round_id", "")).strip()
    if not round_id:
        return {}
    return {
        "id": round_id, "title": str(pending.get("round_title", "")).strip(),
        "prompt": str(pending.get("text", "")).strip(), "last_user": "",
        "last_assistant": str(pending.get("text", "")).strip(),
        "status": "queued", "pending_guidance": 0, "subagent_count": 0, "running_subagents": 0,
        "started_at": _round_started_iso(round_id),
        "updated_at": str(pending.get("asked_at", "")).strip() or datetime.now(timezone.utc).isoformat(),
    }


def get_live_rounds() -> list[dict[str, Any]]:
    entries = _session_round_entries()

    from cyrene.subagent import _registry

    for info in _registry.values():
        round_id = str(info.get("round_id", "")).strip()
        if not round_id:
            continue
        entry = entries.setdefault(round_id, {
            "id": round_id, "title": "", "prompt": str(info.get("task") or "").strip(),
            "last_user": "", "last_assistant": "", "status": "done", "pending_guidance": 0,
            "subagent_count": 0, "running_subagents": 0,
            "started_at": _round_started_iso(round_id) or info.get("created_at"),
            "updated_at": info.get("updated_at") or _round_started_iso(round_id),
        })
        entry["subagent_count"] += 1
        sub_status = str(info.get("status") or "done")
        if sub_status in ("running", "waiting", "resumed"):
            entry["running_subagents"] += 1
            entry["status"] = "running"
        if not entry.get("prompt"):
            entry["prompt"] = str(info.get("task") or "").strip()
        if info.get("updated_at"):
            entry["updated_at"] = info.get("updated_at")
        if info.get("created_at") and not entry.get("started_at"):
            entry["started_at"] = info.get("created_at")

    for round_id, pending_count in _main_inbox_pending_by_round().items():
        entry = entries.setdefault(round_id, {
            "id": round_id, "title": "", "prompt": "", "last_user": "", "last_assistant": "",
            "status": "queued", "pending_guidance": 0, "subagent_count": 0, "running_subagents": 0,
            "started_at": _round_started_iso(round_id), "updated_at": _round_started_iso(round_id),
        })
        entry["pending_guidance"] = pending_count
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        if entry["status"] != "running":
            entry["status"] = "queued"

    pending_question_entry = _pending_question_live_entry()
    if pending_question_entry:
        round_id = pending_question_entry["id"]
        entry = entries.setdefault(round_id, pending_question_entry)
        if not entry.get("title"):
            entry["title"] = pending_question_entry["title"]
        if not entry.get("prompt"):
            entry["prompt"] = pending_question_entry["prompt"]
        if not entry.get("last_assistant"):
            entry["last_assistant"] = pending_question_entry["last_assistant"]
        if entry.get("status") != "running":
            entry["status"] = "queued"
        entry["updated_at"] = pending_question_entry["updated_at"]

    if _active_main_round_id:
        entry = entries.setdefault(_active_main_round_id, {
            "id": _active_main_round_id, "title": "", "prompt": _active_main_round_public_prompt,
            "last_user": _active_main_round_public_prompt, "last_assistant": "", "status": "running",
            "pending_guidance": 0, "subagent_count": 0, "running_subagents": 0,
            "started_at": (datetime.fromtimestamp(_active_main_round_started_at, tz=timezone.utc).isoformat()
                          if _active_main_round_started_at else _round_started_iso(_active_main_round_id)),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        entry["status"] = "running"
        if _active_main_round_public_prompt and not entry.get("prompt"):
            entry["prompt"] = _active_main_round_public_prompt
        if _active_main_round_started_at and not entry.get("started_at"):
            entry["started_at"] = datetime.fromtimestamp(_active_main_round_started_at, tz=timezone.utc).isoformat()
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()

    live_entries: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for entry in entries.values():
        if entry.get("status") not in ("running", "queued") and not entry.get("pending_guidance", 0):
            continue
        started_at = entry.get("started_at")
        elapsed = "—"
        if started_at:
            try:
                started_dt = datetime.fromisoformat(str(started_at)).astimezone(timezone.utc)
                elapsed = _format_duration((now - started_dt).total_seconds())
            except Exception:
                elapsed = "—"
        live_entries.append({
            "id": entry["id"], "title": _round_title_from_entry(entry),
            "prompt": entry.get("prompt", ""), "lastUser": entry.get("last_user", ""),
            "lastAssistant": entry.get("last_assistant", ""), "status": entry.get("status", "queued"),
            "pendingGuidance": int(entry.get("pending_guidance", 0) or 0),
            "subagentCount": int(entry.get("subagent_count", 0) or 0),
            "runningSubagents": int(entry.get("running_subagents", 0) or 0),
            "startedAt": started_at or "", "updatedAt": entry.get("updated_at", "") or "", "elapsed": elapsed,
        })

    live_entries.sort(key=lambda item: item.get("startedAt") or "", reverse=True)
    return live_entries


def query_live_rounds(round_id: str = "") -> str:
    rounds = get_live_rounds()
    if round_id:
        rounds = [item for item in rounds if item.get("id") == round_id]
    if not rounds:
        if round_id:
            return f"No live round found for {round_id}."
        return "No live rounds are currently running."
    lines: list[str] = []
    for r in rounds:
        log_line = (
            f"[{r['status'].upper()}] {r['title']} "
            f"(id={r['id']}, elapsed={r['elapsed']})"
        )
        if r.get("pendingGuidance"):
            log_line += f" pending={r['pendingGuidance']}"
        lines.append(log_line)
    return "\n".join(lines)
