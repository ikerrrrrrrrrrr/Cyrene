"""Tool implementation for save_project_memory.

Lets a Workbench task agent persist a durable fact into its project's long-term
memory store — the same store shown on the project's Memory page and injected
into future runs. The project scope is resolved from the active session id, so
the agent never has to know (or be trusted with) the storage key.
"""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.workbench_context import resolve_project_data_key_for_session

TOOL_NAME = 'save_project_memory'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_save_project_memory(
    args: dict[str, Any],
    _bot: Any,
    _chat_id: int,
    _db_path: str,
    _notify_state: dict[str, bool] | None,
) -> str:
    """Persist one durable fact into the current project's memory store."""
    from cyrene.agent.state import _current_session_id

    content = str(args.get("content", "") or "").strip()
    if len(content) < 4:
        return "Nothing saved: 'content' is empty or too short."

    category = str(args.get("category", "fact") or "fact").strip().lower()
    tags = args.get("tags")

    data_key = resolve_project_data_key_for_session(_current_session_id.get())
    if not data_key or data_key == "default":
        # Not inside a Workbench project (e.g. legacy chat / scheduler run):
        # there is no project memory to write to, and "default" aliases the
        # global short-term store, which must never be written here.
        return "Not saved: project memory is only available inside a Workbench project task/chat."

    # Lazy import: the store lives in the webui layer (loaded in the server
    # process); importing it here at module load would invert package layering.
    from webui.routes_workbench_memory import add_agent_memory

    saved = add_agent_memory(data_key, content, category=category, tags=tags, source="agent")
    if not saved:
        return "Not saved (blank, too short, or out of project scope)."
    cat_label = str(saved.get("category_label") or saved.get("category") or "")
    return f"Saved to project memory [{cat_label}]: {saved.get('content') or content}"


handler = _tool_save_project_memory

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_save_project_memory"]
