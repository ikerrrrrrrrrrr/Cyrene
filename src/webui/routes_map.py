"""Map-related API endpoints for the Web UI."""

import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from cyrene.config import STATE_FILE


def register_map_routes(router: APIRouter) -> None:
    @router.get("/api/map/pins")
    async def get_map_pins(session_id: str = ""):
        """Return all map pins and routes from the session state.

        Without ``session_id`` this reads the default session (legacy UI);
        with it, the per-session state file (workbench conversations).
        """
        state_file = STATE_FILE
        if session_id.strip():
            from cyrene.agent.state import _session_state_file
            state_file = _session_state_file(session_id.strip())
        if not state_file.exists():
            return JSONResponse({"pins": [], "routes": []})
        state = json.loads(state_file.read_text(encoding="utf-8"))
        return JSONResponse({
            "pins": state.get("map_pins", []),
            "routes": state.get("map_routes", []),
        })
