"""Tool implementation for delete_entity."""

from __future__ import annotations

from cyrene import tool_legacy as _legacy

TOOL_NAME = 'delete_entity'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_delete_entity(args, bot, chat_id, db_path, notify_state):
    from cyrene.entities import delete_entity
    success = await delete_entity(db_path, args["id"], permanent=args.get("permanent", False))
    return "已删除事务。" if success else f"未找到事务 {args['id']}"


handler = _tool_delete_entity

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_delete_entity"]
