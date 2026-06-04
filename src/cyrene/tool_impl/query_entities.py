"""Tool implementation for query_entities."""

from __future__ import annotations

from cyrene import tool_legacy as _legacy

TOOL_NAME = 'query_entities'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_query_entities(args, bot, chat_id, db_path, notify_state):
    from cyrene.entities import query_entities
    entities = await query_entities(
        db_path,
        q=args.get("q", ""),
        type=args.get("type"),
        due_before=args.get("due_before"),
    )
    if not entities:
        return "没有找到匹配的事务。"
    lines = [f"- [{e['type']}] {e['title']}" for e in entities]
    return f"找到 {len(entities)} 条事务：\n" + "\n".join(lines)


handler = _tool_query_entities

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_query_entities"]
