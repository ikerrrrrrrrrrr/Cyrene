"""Tool implementation for update_entity."""

from __future__ import annotations

from cyrene import tool_legacy as _legacy

TOOL_NAME = 'update_entity'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_update_entity(args, bot, chat_id, db_path, notify_state):
    from cyrene.entities import update_entity
    field = args["field"]
    value = args["value"]
    entity = await update_entity(db_path, args["id"], **{field: value})
    if entity is None:
        return f"未找到事务 {args['id']}"
    return f"已更新事务 {entity['title']} 的 {field}"


handler = _tool_update_entity

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_update_entity"]
