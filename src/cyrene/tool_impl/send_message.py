"""Tool implementation for send_message."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy

TOOL_NAME = 'send_message'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_send_user_message(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    text = str(args.get("text", "") or "").strip()
    if not text:
        return "Error: 'text' is required."
    from cyrene.agent.state import _current_agent_id, _current_client_request_id, _current_round_id
    from cyrene.agent.session import append_system_message
    from cyrene.agent.message import _insert_intermediate_user_reply

    sender = str(_current_agent_id.get() or "").strip()
    if sender not in {"main", "scheduler"}:
        return "Only the main agent can send a user-visible WebUI message. Subagents must report via quit or send_agent_message."

    if sender == "scheduler":
        await append_system_message(
            text,
            message_meta={"scheduled": True},
            publish_event={"scheduled": True},
        )
        if _notify_state is not None:
            _notify_state["sent"] = True
        return "Scheduled message sent to the user."

    round_id = str(_current_round_id.get() or "").strip()
    if not round_id:
        await append_system_message(text)
        if _notify_state is not None:
            _notify_state["sent"] = True
        return "System message sent to the user."

    client_request_id = str(_current_client_request_id.get() or "").strip()
    await _insert_intermediate_user_reply(
        text,
        round_id=round_id,
        client_request_id=client_request_id,
    )
    if _notify_state is not None:
        _notify_state["sent"] = True
    return "Mid-run message sent to the user."


handler = _tool_send_user_message

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_send_user_message"]
