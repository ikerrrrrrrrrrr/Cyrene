"""Message utilities: identity, dedup, merge, intermediate replies, round helpers.

Depends on ``state`` (for ContextVars) but not on ``session``, ``guidance``,
or ``coordinator``.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from cyrene.agent.state import (
    _current_round_id,
    _pending_intermediate_user_replies,
    _reply_stream_writer,
    _ui_round_assistant_meta,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message identity and dedup
# ---------------------------------------------------------------------------

def _ensure_message_identity(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for message in messages:
        if not isinstance(message, dict):
            continue
        if not str(message.get("message_id", "")).strip():
            message["message_id"] = f"msg_{uuid4().hex}"
    return messages


def _dedupe_messages_by_id(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep message order while preferring the latest version for each message_id."""
    deduped: list[dict[str, Any]] = []
    seen_index: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        message_id = str(message.get("message_id", "")).strip()
        if message_id and message_id in seen_index:
            deduped[seen_index[message_id]] = message
            continue
        if message_id:
            seen_index[message_id] = len(deduped)
        deduped.append(message)
    return deduped


def _merge_message_sequence(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge two persisted message sequences without regressing newer entries."""
    incoming_by_id = {
        str(message.get("message_id", "")).strip(): message
        for message in incoming
        if isinstance(message, dict) and str(message.get("message_id", "")).strip()
    }

    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for message in existing:
        if not isinstance(message, dict):
            continue
        message_id = str(message.get("message_id", "")).strip()
        if message_id and message_id in incoming_by_id:
            merged.append(incoming_by_id[message_id])
            seen_ids.add(message_id)
            continue
        merged.append(message)
        if message_id:
            seen_ids.add(message_id)

    for message in incoming:
        if not isinstance(message, dict):
            continue
        message_id = str(message.get("message_id", "")).strip()
        if message_id and message_id in seen_ids:
            continue
        merged.append(message)
        if message_id:
            seen_ids.add(message_id)

    return _dedupe_messages_by_id(merged)


# ---------------------------------------------------------------------------
# Persisted-prefix helpers
# ---------------------------------------------------------------------------

def _message_suffix_after_persisted_prefix(
    messages: list[dict[str, Any]],
    base_messages: list[dict[str, Any]],
    fallback_prefix_len: int,
) -> list[dict[str, Any]]:
    """Return newly produced messages after the persisted history prefix."""
    base_ids = {
        str(message.get("message_id", "")).strip()
        for message in base_messages
        if isinstance(message, dict) and str(message.get("message_id", "")).strip()
    }
    if base_ids:
        index = 0
        while index < len(messages):
            message = messages[index]
            message_id = str(message.get("message_id", "")).strip() if isinstance(message, dict) else ""
            if not message_id or message_id not in base_ids:
                break
            index += 1
        if index > 0:
            return messages[index:]

    prefix_len = max(0, min(fallback_prefix_len, len(messages)))
    return messages[prefix_len:]


def _is_replaceable_live_message(entry: dict[str, Any], round_id: str) -> bool:
    """Return True for persisted messages that belong to the active live run."""
    if not round_id:
        return False
    if str(entry.get("round_id", "")).strip() != round_id:
        return False
    return not str(entry.get("queued_guidance_id", "")).strip()


# ---------------------------------------------------------------------------
# Intermediate replies
# ---------------------------------------------------------------------------

def _flush_intermediate_user_replies(messages: list[dict[str, Any]]) -> None:
    pending = _pending_intermediate_user_replies.get()
    if not pending:
        return
    existing_ids = {str(m.get("message_id", "")).strip() for m in messages if isinstance(m, dict)}
    for entry in pending:
        _ensure_message_identity([entry])
        mid = str(entry.get("message_id", "")).strip()
        if mid and mid in existing_ids:
            continue
        messages.append(dict(entry))
        if mid:
            existing_ids.add(mid)
    pending.clear()


async def _insert_intermediate_user_reply(
    content: str,
    round_id: str,
    client_request_id: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "round_id": round_id,
        "intermediate_reply": True,
    }
    if attachments:
        assistant_entry["attachments"] = [dict(item) for item in attachments if isinstance(item, dict)]
    if client_request_id:
        assistant_entry["client_request_id"] = client_request_id

    from cyrene.agent.session import get_session_labels

    labels = get_session_labels(round_id)
    if labels.get("round_title"):
        assistant_entry["round_title"] = labels["round_title"]

    _ensure_message_identity([assistant_entry])

    pending = _pending_intermediate_user_replies.get()
    if pending is not None:
        pending.append(dict(assistant_entry))

    from cyrene.agent.session import _load_session_state, _write_session_messages_locked
    from cyrene.agent.state import _ensure_session, _current_session_id, _publish_runtime_event

    async with _ensure_session(_current_session_id.get()).session_state_lock:
        state = _load_session_state()
        existing = state.get("messages", [])
        full_messages = list(existing) if isinstance(existing, list) else []
        full_messages.append(dict(assistant_entry))
        _ensure_message_identity(full_messages)
        await _write_session_messages_locked(state, full_messages)

    await _publish_runtime_event({
        "type": "assistant_message",
        "round_id": round_id,
        "client_request_id": client_request_id,
        "intermediate": True,
        "message_id": assistant_entry.get("message_id", ""),
    })
    return assistant_entry


# ---------------------------------------------------------------------------
# Entry builders
# ---------------------------------------------------------------------------

def _assistant_entry_from_response(response: dict[str, Any], round_id: str, include_tool_calls: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = {"role": "assistant", "content": response.get("content") or ""}
    if response.get("reasoning_content"):
        entry["reasoning_content"] = response["reasoning_content"]
    if include_tool_calls and response.get("tool_calls"):
        entry["tool_calls"] = response["tool_calls"]
    if response.get("usage"):
        entry["usage"] = response["usage"]
    if round_id:
        entry["round_id"] = round_id
    extra_meta = _ui_round_assistant_meta.get()
    if extra_meta:
        entry.update(extra_meta)
    return entry


def _apply_assistant_meta(entry: dict[str, Any]) -> dict[str, Any]:
    extra_meta = _ui_round_assistant_meta.get()
    if extra_meta:
        entry.update(extra_meta)
    return entry


# ---------------------------------------------------------------------------
# Label / fallback helpers
# ---------------------------------------------------------------------------

def _fallback_label(text: str, limit: int = 48) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip().strip("[](){}<>\"'`，。！？；：,.;!?")
    return compact[:limit] or "Untitled"


def _extract_json_object(text: str) -> dict[str, Any]:
    source = str(text or "").strip()
    if not source:
        return {}
    try:
        data = json.loads(source)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", source, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _tool_result_requests_user_input(result: str) -> bool:
    payload = _extract_json_object(result)
    return str(payload.get("status", "")).strip() == "awaiting_user"


# ---------------------------------------------------------------------------
# Round timestamp helpers
# ---------------------------------------------------------------------------

def _round_epoch_ms(round_id: str) -> int | None:
    match = re.fullmatch(r"round_(\d+)", str(round_id or "").strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _round_started_iso(round_id: str) -> str | None:
    epoch_ms = _round_epoch_ms(round_id)
    if epoch_ms is None:
        return None
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _is_placeholder_reply(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return normalized in {
        "", "done", "done.", "finished", "finished.",
        "ok", "ok.", "okay", "okay.",
        "完成", "完成。", "已完成", "已完成。",
    }


def _round_title_from_entry(entry: dict[str, Any]) -> str:
    return (
        str(entry.get("title", "")).strip()
        or _fallback_label(entry.get("last_user") or entry.get("prompt") or entry.get("id"), limit=40)
    )
