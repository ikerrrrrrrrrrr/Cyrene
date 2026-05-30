"""Entity (事务) management system.

Supports tracking and managing various entity types:
- task, project, decision, knowledge, relationship, event, resource, idea, problem, habit
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite


def _now() -> str:
    """Return current time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    """Generate a new UUID string."""
    return str(uuid.uuid4())


def _serialize_list(items: list | None) -> str:
    """Serialize a list to JSON string."""
    return json.dumps(items or [])


def _serialize_dict(d: dict | None) -> str:
    """Serialize a dict to JSON string."""
    return json.dumps(d or {})


def _deserialize_list(s: str | None) -> list:
    """Deserialize a JSON string to list."""
    if not s:
        return []
    try:
        return json.loads(s)
    except Exception:
        return []


def _deserialize_dict(s: str | None) -> dict:
    """Deserialize a JSON string to dict."""
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


async def _schedule_entity_reminder(
    db_path: str,
    entity_id: str,
    title: str,
    due_date: str,
) -> str:
    """Create a reminder task for the entity and return the task_id."""
    from cyrene.config import OWNER_ID

    task_id = _new_id()
    now = _now()
    prompt = f"提醒用户：{title} 到期了"
    chat_id = OWNER_ID if OWNER_ID is not None else 0

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO scheduled_tasks (id, chat_id, prompt, schedule_type, schedule_value, next_run, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, chat_id, prompt, "once", due_date, due_date, now),
        )
        await db.commit()

    return task_id


def _row_to_entity(row: aiosqlite.Row) -> dict:
    """Convert a database row to an entity dict with deserialized fields."""
    return {
        "id": row["id"],
        "type": row["type"],
        "title": row["title"],
        "content": row["content"],
        "status": row["status"],
        "tags": _deserialize_list(row["tags"]),
        "priority": row["priority"],
        "effort": row["effort"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_referenced_at": row["last_referenced_at"],
        "due_date": row["due_date"],
        "parent_id": row["parent_id"],
        "linked_ids": _deserialize_list(row["linked_ids"]),
        "people": _deserialize_list(row["people"]),
        "source": row["source"],
        "source_round_id": row["source_round_id"],
        "confidence": row["confidence"],
        "metadata": _deserialize_dict(row["metadata"]),
    }


async def create_entity(
    db_path: str,
    *,
    type: str,
    title: str,
    content: str = "",
    status: str = "active",
    tags: list[str] | None = None,
    priority: str = "medium",
    effort: str | None = None,
    due_date: str | None = None,
    parent_id: str | None = None,
    linked_ids: list[str] | None = None,
    people: list[str] | None = None,
    source: str = "extracted",
    source_round_id: str | None = None,
    confidence: float = 1.0,
    metadata: dict | None = None,
) -> dict:
    """Create a new entity and return it with all fields populated.

    If source=="explicit" and due_date is set, automatically creates a reminder task.
    """
    entity_id = _new_id()
    now = _now()

    if metadata is None:
        metadata = {}

    # If explicit source with due_date, create reminder task
    if source == "explicit" and due_date:
        reminder_task_id = await _schedule_entity_reminder(db_path, entity_id, title, due_date)
        metadata["reminder_task_id"] = reminder_task_id

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            INSERT INTO entities (
                id, type, title, content, status, tags, priority, effort,
                created_at, updated_at, last_referenced_at, due_date, parent_id,
                linked_ids, people, source, source_round_id, confidence, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_id,
                type,
                title,
                content,
                status,
                _serialize_list(tags),
                priority,
                effort,
                now,
                now,
                now,
                due_date,
                parent_id,
                _serialize_list(linked_ids),
                _serialize_list(people),
                source,
                source_round_id,
                confidence,
                _serialize_dict(metadata),
            ),
        )
        await db.commit()

        # Fetch and return the created entity
        cursor = await db.execute("SELECT * FROM entities WHERE id = ?", (entity_id,))
        row = await cursor.fetchone()
        return _row_to_entity(row) if row else {}


async def update_entity(db_path: str, entity_id: str, **fields) -> dict | None:
    """Update specified fields of an entity and return the updated entity."""
    if not fields:
        return await get_entity(db_path, entity_id)

    # Build the update query dynamically
    now = _now()
    set_clauses = ["updated_at = ?", "last_referenced_at = ?"]
    values: list[Any] = [now, now]

    for key, value in fields.items():
        if key == "tags":
            set_clauses.append("tags = ?")
            values.append(_serialize_list(value if isinstance(value, list) else []))
        elif key == "linked_ids":
            set_clauses.append("linked_ids = ?")
            values.append(_serialize_list(value if isinstance(value, list) else []))
        elif key == "people":
            set_clauses.append("people = ?")
            values.append(_serialize_list(value if isinstance(value, list) else []))
        elif key == "metadata":
            set_clauses.append("metadata = ?")
            values.append(_serialize_dict(value if isinstance(value, dict) else {}))
        elif key in ("status", "priority", "content", "title", "effort", "due_date", "parent_id"):
            set_clauses.append(f"{key} = ?")
            values.append(value)

    values.append(entity_id)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE entities SET {', '.join(set_clauses)} WHERE id = ?",
            values,
        )
        await db.commit()

        # Fetch and return the updated entity
        cursor = await db.execute("SELECT * FROM entities WHERE id = ?", (entity_id,))
        row = await cursor.fetchone()
        return _row_to_entity(row) if row else None


async def delete_entity(db_path: str, entity_id: str, permanent: bool = False) -> bool:
    """Delete or archive an entity.

    If permanent=False (default), sets status to 'archived' (soft delete).
    If permanent=True, deletes the entity permanently.
    Also cancels any associated reminder task if metadata has reminder_task_id.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Get the entity to check for reminder_task_id
        cursor = await db.execute("SELECT metadata FROM entities WHERE id = ?", (entity_id,))
        row = await cursor.fetchone()

        if row is None:
            return False

        metadata = _deserialize_dict(row["metadata"])
        reminder_task_id = metadata.get("reminder_task_id")

        if permanent:
            await db.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        else:
            await db.execute("UPDATE entities SET status = ? WHERE id = ?", ("archived", entity_id))

        # Cancel reminder task if it exists
        if reminder_task_id:
            await db.execute(
                "UPDATE scheduled_tasks SET status = ? WHERE id = ?",
                ("cancelled", reminder_task_id),
            )

        await db.commit()
        return True


async def get_entity(db_path: str, entity_id: str) -> dict | None:
    """Get a single entity by ID."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM entities WHERE id = ?", (entity_id,))
        row = await cursor.fetchone()
        return _row_to_entity(row) if row else None


async def list_entities(
    db_path: str,
    *,
    type: str | None = None,
    status: str | None = None,
    has_due_date: bool = False,
    limit: int = 100,
) -> list[dict]:
    """List entities with optional filtering.

    Args:
        type: Filter by entity type (optional)
        status: Filter by status (default: None = all)
        has_due_date: If True, only return entities with due_date
        limit: Maximum number of results
    """
    query = "SELECT * FROM entities WHERE 1=1"
    params: list[Any] = []

    if type:
        query += " AND type = ?"
        params.append(type)

    if status:
        query += " AND status = ?"
        params.append(status)
    else:
        query += " AND status NOT IN ('archived', 'abandoned')"

    if has_due_date:
        query += " AND due_date IS NOT NULL"

    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [_row_to_entity(row) for row in rows]


async def query_entities(
    db_path: str,
    q: str = "",
    *,
    type: str | None = None,
    status: str | None = None,
    due_before: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Search entities by keyword and apply filters.

    Args:
        q: Search keyword (matches title and content)
        type: Filter by entity type (optional)
        status: Filter by status (None = exclude only archived/abandoned)
        due_before: Filter to entities with due_date before this time (ISO 8601)
        limit: Maximum number of results
    """
    query = "SELECT * FROM entities WHERE 1=1"
    params: list[Any] = []

    if q:
        query += " AND (title LIKE ? OR content LIKE ?)"
        search_pattern = f"%{q}%"
        params.extend([search_pattern, search_pattern])

    if type:
        query += " AND type = ?"
        params.append(type)

    if status:
        query += " AND status = ?"
        params.append(status)
    else:
        query += " AND status NOT IN ('archived', 'abandoned')"

    if due_before:
        query += " AND due_date IS NOT NULL AND due_date < ?"
        params.append(due_before)

    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [_row_to_entity(row) for row in rows]


async def add_candidate(
    db_path: str,
    *,
    type: str,
    title: str,
    content: str = "",
    confidence: float,
    source_round_id: str | None = None,
    raw_text: str | None = None,
) -> str:
    """Add a candidate entity and return its ID."""
    candidate_id = _new_id()
    now = _now()

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO entity_candidates (id, type, title, content, confidence, source_round_id, raw_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (candidate_id, type, title, content, confidence, source_round_id, raw_text, now),
        )
        await db.commit()

    return candidate_id


async def list_candidates(db_path: str, limit: int = 50) -> list[dict]:
    """List all candidate entities."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM entity_candidates ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "type": row["type"],
                "title": row["title"],
                "content": row["content"],
                "confidence": row["confidence"],
                "source_round_id": row["source_round_id"],
                "raw_text": row["raw_text"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]


async def promote_candidate(db_path: str, candidate_id: str) -> dict | None:
    """Promote a candidate to a full entity."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM entity_candidates WHERE id = ?",
            (candidate_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        # Create the entity
        entity = await create_entity(
            db_path,
            type=row["type"],
            title=row["title"],
            content=row["content"],
            confidence=row["confidence"],
            source="extracted",
            source_round_id=row["source_round_id"],
        )

        # Delete the candidate
        await db.execute("DELETE FROM entity_candidates WHERE id = ?", (candidate_id,))
        await db.commit()

        return entity


async def reject_candidate(db_path: str, candidate_id: str) -> bool:
    """Reject a candidate and lower the type confidence."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT type FROM entity_candidates WHERE id = ?",
            (candidate_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            return False

        entity_type = row["type"]

        # Update type confidence: lower by 0.05
        await db.execute(
            """
            INSERT INTO entity_type_confidence (type, adjustment, sample_count, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(type) DO UPDATE SET
                adjustment = adjustment - 0.05,
                sample_count = sample_count + 1,
                updated_at = ?
            """,
            (entity_type, -0.05, 1, _now(), _now()),
        )

        # Delete the candidate
        await db.execute("DELETE FROM entity_candidates WHERE id = ?", (candidate_id,))
        await db.commit()

        return True


async def process_candidates(db_path: str) -> list[dict]:
    """Automatically promote candidates with confidence >= 0.8 to full entities.

    Returns the list of promoted entities.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM entity_candidates WHERE confidence >= 0.8 ORDER BY created_at ASC",
        )
        rows = await cursor.fetchall()

    promoted = []
    for row in rows:
        result = await promote_candidate(db_path, row["id"])
        if result:
            promoted.append(result)

    return promoted


async def has_similar_entity(db_path: str, type: str, title: str) -> bool:
    """Check if a similar entity already exists (same type + overlapping title).

    Checks both the ``entities`` and ``entity_candidates`` tables.
    Uses substring matching so "买点菜" matches "记得买菜" etc.
    """
    search = f"%{title}%"
    async with aiosqlite.connect(db_path) as db:
        # Check entities
        cursor = await db.execute(
            "SELECT COUNT(*) FROM entities WHERE type = ? AND title LIKE ?",
            (type, search),
        )
        row = await cursor.fetchone()
        if row and row[0] > 0:
            return True

        # Check candidates
        cursor = await db.execute(
            "SELECT COUNT(*) FROM entity_candidates WHERE type = ? AND title LIKE ?",
            (type, search),
        )
        row = await cursor.fetchone()
        if row and row[0] > 0:
            return True

    return False


async def adjust_type_confidence(db_path: str, type: str, delta: float) -> None:
    """Adjust the confidence adjustment for a specific entity type."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO entity_type_confidence (type, adjustment, sample_count, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(type) DO UPDATE SET
                adjustment = adjustment + ?,
                sample_count = sample_count + 1,
                updated_at = ?
            """,
            (type, delta, 1, _now(), delta, _now()),
        )
        await db.commit()
