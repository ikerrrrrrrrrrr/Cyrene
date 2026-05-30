"""Map-related API endpoints for the Web UI."""

import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from cyrene.config import STATE_FILE


def register_map_routes(router: APIRouter) -> None:
    @router.get("/api/map/pins")
    async def get_map_pins():
        """Return all map pins and routes from the current session state."""
        if not STATE_FILE.exists():
            return JSONResponse({"pins": [], "routes": []})
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return JSONResponse({
            "pins": state.get("map_pins", []),
            "routes": state.get("map_routes", []),
        })
