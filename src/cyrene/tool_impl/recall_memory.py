"""Tool implementation for RecallMemory."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    Any,
    _get_short_term_context,
    _json_result,
    _truncate,
    read_shallow_memory,
    recall_conversations,
)

TOOL_NAME = 'RecallMemory'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_recall_memory(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Recall archived session history plus persisted memory."""
    query = str(args.get("query", "") or "").strip()
    session_id = str(args.get("session_id", "") or "").strip()
    date = str(args.get("date", "") or "").strip()
    limit = max(1, min(int(args.get("limit", 5) or 5), 10))
    include_soul = bool(args.get("include_soul", True))
    include_short_term = bool(args.get("include_short_term", True))

    matches = recall_conversations(
        query=query,
        session_id=session_id,
        date=date,
        limit=limit,
    )
    payload: dict[str, Any] = {
        "query": query,
        "session_id": session_id,
        "date": date,
        "matches": [
            {
                "date": item.get("date", ""),
                "timestamp": item.get("timestamp", ""),
                "archive_session_id": item.get("archive_session_id", ""),
                "session_title": item.get("session_title", ""),
                "round_id": item.get("round_id", ""),
                "round_title": item.get("round_title", ""),
                "user": item.get("user_body", ""),
                "assistant": item.get("assistant_body", ""),
            }
            for item in matches
        ],
    }
    if include_short_term:
        payload["short_term_memory"] = _get_short_term_context(
            max_chars=1800,
            header="[Short-term cross-session memory:]",
        )
    if include_soul:
        payload["soul_memory"] = _truncate(read_shallow_memory(), 3000)
    if not payload["matches"]:
        payload["note"] = "No archived session matches found for the given filters."
    return _json_result(payload)


handler = _tool_recall_memory

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_recall_memory"]
