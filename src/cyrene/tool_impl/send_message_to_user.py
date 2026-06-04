"""Tool implementation for send_message_to_user."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    datetime,
    time,
    timezone,
)

TOOL_NAME = 'send_message_to_user'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_send_message_to_user(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Send a message directly to the user. Only available to subagents responding to @mentions."""
    text = str(args.get("text", "") or "").strip()
    if not text:
        return "Error: 'text' is required."

    from cyrene.subagent import _direct_message_mode
    if not _direct_message_mode.get():
        return (
            "Error: send_message_to_user is only available when responding to a direct "
            "user message via @mention. Use quit with your result for normal rounds."
        )

    from cyrene.agent.state import _current_agent_id, _current_round_id
    from cyrene import debug as _debug_module
    agent_id = _current_agent_id.get() or "subagent"
    round_id = str(_current_round_id.get() or "").strip()
    await _debug_module.publish_event({
        "type": "agent_comm",
        "from": agent_id,
        "to": "user",
        "content": text,
        "summary": text[:100].replace("\n", " ").strip() + ("..." if len(text) > 100 else ""),
        "msg_type": "reply",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "round_id": round_id,
        "message_id": f"reply_{agent_id}_{int(time.time() * 1000)}",
    })
    if _notify_state is not None:
        _notify_state["sent"] = True
    _direct_message_mode.set(False)
    return "Message sent. Now act on the user's guidance — adjust your approach and continue working with your other tools."


handler = _tool_send_message_to_user

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_send_message_to_user"]
