"""Tool implementation for list_entities."""

from __future__ import annotations

from cyrene import tool_legacy as _legacy

TOOL_NAME = 'list_entities'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_list_entities(args, bot, chat_id, db_path, notify_state):
    from cyrene.entities import list_entities
    entities = await list_entities(
        db_path,
        type=args.get("type"),
        status=args.get("status", "active"),
        limit=args.get("limit", 50),
    )
    if not entities:
        return "没有找到符合条件的事务。"
    lines = [f"- [{e['type']}] {e['title']}（{e['status']}）{' 截止：'+e['due_date'] if e.get('due_date') else ''}" for e in entities]
    return f"找到 {len(entities)} 条事务：\n" + "\n".join(lines)


handler = _tool_list_entities

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_list_entities"]
