"""Entity-related API endpoints for the Web UI."""

from fastapi import APIRouter
from cyrene.entities import (
    create_entity,
    update_entity,
    delete_entity,
    get_entity,
    list_entities,
    query_entities,
    list_candidates,
    promote_candidate,
    reject_candidate,
)

_CREATE_FIELDS = {
    "type", "title", "content", "status", "tags", "priority", "effort",
    "due_date", "parent_id", "linked_ids", "people", "source",
    "source_round_id", "confidence", "metadata",
}
_UPDATE_FIELDS = {
    "status", "priority", "due_date", "content", "tags", "people",
    "title", "effort", "metadata", "linked_ids", "parent_id",
}


def register_entity_routes(router: APIRouter, db_path: str) -> None:
    @router.get("/api/entities")
    async def api_list_entities(
        type: str = None,
        status: str = None,
        has_due_date: bool = False,
        q: str = None,
        limit: int = 100,
    ):
        if q:
            return await query_entities(db_path, q=q, type=type, limit=limit)
        return await list_entities(db_path, type=type, status=status, has_due_date=has_due_date, limit=limit)

    @router.post("/api/entities")
    async def api_create_entity(body: dict):
        filtered = {k: v for k, v in body.items() if k in _CREATE_FIELDS}
        return await create_entity(db_path, **filtered)

    @router.get("/api/entities/candidates")
    async def api_list_candidates():
        """List all candidate entities."""
        return await list_candidates(db_path)

    @router.post("/api/entities/candidates/{candidate_id}/approve")
    async def api_approve_candidate(candidate_id: str):
        """Promote a candidate to a full entity."""
        result = await promote_candidate(db_path, candidate_id)
        return result or {"error": "not found"}

    @router.delete("/api/entities/candidates/{candidate_id}")
    async def api_reject_candidate(candidate_id: str):
        """Reject a candidate entity."""
        success = await reject_candidate(db_path, candidate_id)
        return {"ok": success}

    @router.get("/api/entities/{entity_id}")
    async def api_get_entity(entity_id: str):
        """Get a single entity by ID."""
        return await get_entity(db_path, entity_id) or {"error": "not found"}

    @router.put("/api/entities/{entity_id}")
    async def api_update_entity(entity_id: str, body: dict):
        filtered = {k: v for k, v in body.items() if k in _UPDATE_FIELDS}
        return await update_entity(db_path, entity_id, **filtered) or {"error": "not found"}

    @router.delete("/api/entities/{entity_id}")
    async def api_delete_entity(entity_id: str, permanent: bool = False):
        """Delete or archive an entity."""
        success = await delete_entity(db_path, entity_id, permanent=permanent)
        return {"ok": success}
