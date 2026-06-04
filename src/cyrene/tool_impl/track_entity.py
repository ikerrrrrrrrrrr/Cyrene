"""Tool implementation for track_entity."""

from __future__ import annotations

from cyrene import tool_legacy as _legacy

TOOL_NAME = 'track_entity'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_track_entity(args, bot, chat_id, db_path, notify_state):
    from cyrene.entities import create_entity
    entity = await create_entity(
        db_path,
        type=args.get("type", "task"),
        title=args["title"],
        content=args.get("content", ""),
        priority=args.get("priority", "medium"),
        due_date=args.get("due_date"),
        people=args.get("people", []),
        tags=args.get("tags", []),
        source=args.get("source", "extracted"),
        confidence=args.get("confidence", 1.0),
        source_round_id=args.get("source_round_id"),
    )
    return f"已记录事务：{entity['title']}（ID: {entity['id'][:8]}）"


handler = _tool_track_entity

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_track_entity"]
