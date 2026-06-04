"""Tool definition entry for quit.

The handler is registered lazily by ``cyrene.agent`` to avoid importing the
agent package from the tool registry during startup.
"""

from __future__ import annotations

from cyrene import tool_legacy as _legacy

TOOL_NAME = "quit"
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)
handler = None

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler"]
