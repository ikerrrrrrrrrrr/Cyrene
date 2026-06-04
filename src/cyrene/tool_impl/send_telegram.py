"""Tool implementation for send_telegram."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy

TOOL_NAME = 'send_telegram'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_send_message(args: dict[str, Any], bot: Any, chat_id: int, _db_path: str, notify_state: dict[str, bool] | None) -> str:
    text = str(args.get("text", ""))
    if bot is not None:
        await bot.send_message(chat_id=chat_id, text=text)
    if notify_state is not None:
        notify_state["sent"] = True
    return "Message sent."


handler = _tool_send_message

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_send_message"]
