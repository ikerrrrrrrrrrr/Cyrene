"""Tool implementation for browser_request_takeover."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _json_result,
)

TOOL_NAME = 'browser_request_takeover'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_browser_request_takeover(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene import debug
    from cyrene.browser import get_session
    from cyrene.agent.state import _current_agent_id, _current_client_request_id, _current_round_id
    from cyrene.agent.session import _clear_pending_question, _upsert_pending_question, get_session_labels

    if _current_agent_id.get() != "main":
        return "Only the main agent can request a browser takeover."
    round_id = str(_current_round_id.get() or "").strip()
    if not round_id:
        return "Cannot request a browser takeover outside an active chat round."

    reason = str(args.get("reason") or "").strip() or "请在浏览器窗口完成登录，然后点「我已完成登录」。"

    try:
        session = await get_session()
    except Exception as exc:
        return f"Browser takeover unavailable (Playwright/Chromium not ready): {exc}"
    current_url = await session.current_url()

    # Ask in the app FIRST (the standard question popup), then open the real
    # browser window. The confirmation lives in the app's question UI — the
    # browser panel only shows a passive "waiting for login" placeholder.
    await debug.publish_event({
        "type": "browser_takeover_request",
        "round_id": round_id,
        "url": current_url,
        "reason": reason,
    })
    labels = get_session_labels(round_id)
    question = await _upsert_pending_question({
        "text": reason,
        "round_id": round_id,
        "round_title": labels.get("round_title", ""),
        "client_request_id": str(_current_client_request_id.get() or "").strip(),
        "options": ["我已完成登录"],
        "allow_custom": False,
        "meta": {"kind": "browser_takeover", "url": current_url},
    })
    try:
        await session.switch_to_headed(current_url)
    except Exception as exc:
        # Couldn't open the window — undo the pending question and clear the panel.
        try:
            await _clear_pending_question(str(question.get("id", "")))
        except Exception:
            pass
        await debug.publish_event({"type": "browser_takeover_cancelled", "round_id": round_id})
        return f"Failed to open the browser window for takeover: {exc}"
    return _json_result({
        "status": "awaiting_user",
        "question_id": question.get("id", ""),
        "takeover": True,
    })


handler = _tool_browser_request_takeover

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_browser_request_takeover"]
