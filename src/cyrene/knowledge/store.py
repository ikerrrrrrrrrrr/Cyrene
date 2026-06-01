"""Knowledge base document store.

Provides CRUD operations for documents, chunks, and relations.
Mirrors the style of cyrene.entities with aiosqlite, JSON serialization, and ISO-8601 timestamps.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
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


def _row_to_document(row: aiosqlite.Row) -> dict:
    """Convert a database row to a document dict."""
    return {
        "id": row["id"],
        "name": row["name"],
        "path": row["path"],
        "content_type": row["content_type"],
        "kind": row["kind"],
        "size": row["size"],
        "status": row["status"],
        "source": row["source"],
        "title": row["title"],
        "summary": row["summary"],
        "tags": _deserialize_list(row["tags"]),
        "char_count": row["char_count"],
        "chunk_count": row["chunk_count"],
        "entity_id": row["entity_id"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "indexed_at": row["indexed_at"],
        "metadata": _deserialize_dict(row["metadata"]),
    }


def _row_to_chunk(row: aiosqlite.Row) -> dict:
    """Convert a database row to a chunk dict."""
    return {
        "id": row["id"],
        "document_id": row["document_id"],
        "ordinal": row["ordinal"],
        "content": row["content"],
        "char_start": row["char_start"],
        "char_end": row["char_end"],
        "token_count": row["token_count"],
        "embedding": row["embedding"],
        "embedding_dim": row["embedding_dim"],
        "embedding_model": row["embedding_model"],
        "created_at": row["created_at"],
    }


def _row_to_relation(row: aiosqlite.Row) -> dict:
    """Convert a database row to a relation dict."""
    return {
        "id": row["id"],
        "src_id": row["src_id"],
        "dst_id": row["dst_id"],
        "relation": row["relation"],
        "weight": row["weight"],
        "source": row["source"],
        "created_at": row["created_at"],
    }


async def create_document(
    db_path: str,
    *,
    name: str,
    path: str,
    content_type: str = "",
    kind: str = "file",
    size: int = 0,
    source: str = "upload",
    title: str = "",
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> dict:
    """Create a new document and return it."""
    doc_id = _new_id()
    now = _now()

    if metadata is None:
        metadata = {}

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            INSERT INTO kb_documents (
                id, name, path, content_type, kind, size, status, source, title,
                tags, char_count, chunk_count, entity_id, error, created_at, updated_at,
                indexed_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id,
                name,
                path,
                content_type,
                kind,
                size,
                "pending",
                source,
                title,
                _serialize_list(tags),
                0,
                0,
                None,
                "",
                now,
                now,
                None,
                _serialize_dict(metadata),
            ),
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        return _row_to_document(row) if row else {}


async def get_document(db_path: str, doc_id: str) -> dict | None:
    """Get a single document by ID."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        return _row_to_document(row) if row else None


async def list_documents(
    db_path: str,
    *,
    q: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """List documents with optional filtering."""
    query = "SELECT * FROM kb_documents WHERE 1=1"
    params: list[Any] = []

    if q:
        query += " AND (name LIKE ? OR title LIKE ? OR summary LIKE ?)"
        search_pattern = f"%{q}%"
        params.extend([search_pattern, search_pattern, search_pattern])

    if kind:
        query += " AND kind = ?"
        params.append(kind)

    if status:
        query += " AND status = ?"
        params.append(status)

    if tag:
        query += " AND tags LIKE ?"
        # Match quoted tag token in JSON array
        tag_pattern = f'%"{tag}"%'
        params.append(tag_pattern)

    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        results = [_row_to_document(row) for row in rows]

    return results


async def update_document(db_path: str, doc_id: str, **fields) -> dict | None:
    """Update specified fields of a document."""
    if not fields:
        return await get_document(db_path, doc_id)

    now = _now()
    set_clauses = ["updated_at = ?"]
    values: list[Any] = [now]

    for key, value in fields.items():
        if key == "tags":
            set_clauses.append("tags = ?")
            values.append(_serialize_list(value if isinstance(value, list) else []))
        elif key == "metadata":
            set_clauses.append("metadata = ?")
            values.append(_serialize_dict(value if isinstance(value, dict) else {}))
        elif key in (
            "status",
            "title",
            "summary",
            "char_count",
            "chunk_count",
            "error",
            "indexed_at",
            "entity_id",
        ):
            set_clauses.append(f"{key} = ?")
            values.append(value)

    values.append(doc_id)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE kb_documents SET {', '.join(set_clauses)} WHERE id = ?",
            values,
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        return _row_to_document(row) if row else None


async def delete_document(db_path: str, doc_id: str, *, remove_file: bool = True) -> bool:
    """Delete a document and cascade delete its chunks, FTS entries, and relations.

    Optionally delete the on-disk file.
    """
    # Get the document to find its path
    doc = await get_document(db_path, doc_id)
    if not doc:
        return False

    async with aiosqlite.connect(db_path) as db:
        # Delete FTS entries for all chunks of this document
        await db.execute(
            "DELETE FROM kb_chunks_fts WHERE document_id = ?",
            (doc_id,),
        )

        # Delete all chunks for this document
        await db.execute(
            "DELETE FROM kb_chunks WHERE document_id = ?",
            (doc_id,),
        )

        # Delete relations where this document is src or dst
        await db.execute(
            "DELETE FROM kb_relations WHERE src_id = ? OR dst_id = ?",
            (doc_id, doc_id),
        )

        # Delete the document itself
        await db.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
        await db.commit()

    # Delete the on-disk file
    if remove_file and doc["path"]:
        try:
            file_path = Path(doc["path"])
            if file_path.exists():
                file_path.unlink()
        except Exception:
            pass

    return True


async def upsert_document_by_path(
    db_path: str,
    *,
    path: str,
    source: str,
    **fields,
) -> dict:
    """Idempotent upsert by absolute path.

    Uses the UNIQUE INDEX idx_kb_documents_path to ensure one row per path.
    Returns the document row (newly created or updated).
    """
    now = _now()
    doc_id = _new_id()

    # Prepare field updates
    name = fields.get("name") or Path(path).name
    content_type = fields.get("content_type", "")
    kind = fields.get("kind", "file")
    size = fields.get("size", 0)
    title = fields.get("title", "")
    tags = fields.get("tags")
    metadata = fields.get("metadata")

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Try INSERT ON CONFLICT
        await db.execute(
            """
            INSERT INTO kb_documents (
                id, name, path, content_type, kind, size, status, source, title,
                tags, char_count, chunk_count, entity_id, error, created_at, updated_at,
                indexed_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                name = excluded.name,
                content_type = excluded.content_type,
                kind = excluded.kind,
                size = excluded.size,
                title = excluded.title,
                tags = excluded.tags,
                metadata = excluded.metadata,
                updated_at = ?
            """,
            (
                doc_id,
                name,
                path,
                content_type,
                kind,
                size,
                "pending",
                source,
                title,
                _serialize_list(tags),
                0,
                0,
                None,
                "",
                now,
                now,
                None,
                _serialize_dict(metadata),
                now,
            ),
        )
        await db.commit()

        # Fetch the resulting row
        cursor = await db.execute(
            "SELECT * FROM kb_documents WHERE path = ?",
            (path,),
        )
        row = await cursor.fetchone()
        return _row_to_document(row) if row else {}


async def sync_filesystem(db_path: str) -> dict:
    """Scan UPLOADS_DIR and EXPORTS_DIR for new files and upsert them as pending.

    Returns {"added": N, "total": M} where added is newly registered and total is all now in DB.
    """
    from cyrene.attachments import UPLOADS_DIR, EXPORTS_DIR, attachment_kind_from_meta
    import mimetypes

    added = 0
    dirs_to_scan = []

    if UPLOADS_DIR.exists():
        dirs_to_scan.append(("chat_upload", UPLOADS_DIR))

    if EXPORTS_DIR.exists():
        dirs_to_scan.append(("generated", EXPORTS_DIR))

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Get all existing paths
        cursor = await db.execute("SELECT path FROM kb_documents")
        existing_paths = {row["path"] for row in await cursor.fetchall()}

        for source, dir_path in dirs_to_scan:
            for file_path in dir_path.rglob("*"):
                if not file_path.is_file():
                    continue

                abs_path = str(file_path.resolve())
                if abs_path in existing_paths:
                    continue

                # New file: register it
                content_type = mimetypes.guess_type(str(file_path))[0] or ""
                kind = attachment_kind_from_meta(content_type, file_path.name)
                doc_id = _new_id()
                now = _now()

                await db.execute(
                    """
                    INSERT INTO kb_documents (
                        id, name, path, content_type, kind, size, status, source, title,
                        tags, char_count, chunk_count, entity_id, error, created_at, updated_at,
                        indexed_at, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        file_path.name,
                        abs_path,
                        content_type,
                        kind,
                        file_path.stat().st_size,
                        "pending",
                        source,
                        "",
                        _serialize_list([]),
                        0,
                        0,
                        None,
                        "",
                        now,
                        now,
                        None,
                        _serialize_dict({}),
                    ),
                )
                added += 1

        await db.commit()

        # Get total count
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM kb_documents")
        total_row = await cursor.fetchone()
        total = total_row["cnt"] if total_row else 0

    return {"added": added, "total": total}


async def replace_chunks(
    db_path: str,
    doc_id: str,
    chunks: list[dict],
) -> None:
    """Replace all chunks for a document in a transaction.

    Deletes old chunks and FTS entries, then inserts new chunks and FTS entries.
    """
    async with aiosqlite.connect(db_path) as db:
        # Delete old FTS entries
        await db.execute(
            "DELETE FROM kb_chunks_fts WHERE document_id = ?",
            (doc_id,),
        )

        # Delete old chunks
        await db.execute(
            "DELETE FROM kb_chunks WHERE document_id = ?",
            (doc_id,),
        )

        # Insert new chunks
        for chunk in chunks:
            chunk_id = chunk.get("id") or _new_id()
            await db.execute(
                """
                INSERT INTO kb_chunks (
                    id, document_id, ordinal, content, char_start, char_end,
                    token_count, embedding, embedding_dim, embedding_model, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    doc_id,
                    chunk.get("ordinal", 0),
                    chunk.get("content", ""),
                    chunk.get("char_start", 0),
                    chunk.get("char_end", 0),
                    chunk.get("token_count", 0),
                    chunk.get("embedding"),
                    chunk.get("embedding_dim", 0),
                    chunk.get("embedding_model", ""),
                    chunk.get("created_at") or _now(),
                ),
            )

            # Insert into FTS
            await db.execute(
                """
                INSERT INTO kb_chunks_fts (content, chunk_id, document_id)
                VALUES (?, ?, ?)
                """,
                (chunk.get("content", ""), chunk_id, doc_id),
            )

        await db.commit()


async def get_chunks(
    db_path: str,
    doc_id: str,
    *,
    with_embedding: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """Get all chunks for a document."""
    query = "SELECT * FROM kb_chunks WHERE document_id = ?"
    params: list[Any] = [doc_id]

    if with_embedding:
        query += " AND embedding IS NOT NULL"

    query += " ORDER BY ordinal ASC"

    if limit:
        query += " LIMIT ?"
        params.append(limit)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [_row_to_chunk(row) for row in rows]


async def iter_embedded_chunks(
    db_path: str,
    *,
    document_id: str | None = None,
) -> list[dict]:
    """Get all chunks with embeddings."""
    query = "SELECT * FROM kb_chunks WHERE embedding IS NOT NULL"
    params: list[Any] = []

    if document_id:
        query += " AND document_id = ?"
        params.append(document_id)

    query += " ORDER BY created_at ASC"

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [_row_to_chunk(row) for row in rows]


async def create_relation(
    db_path: str,
    *,
    src_id: str,
    dst_id: str,
    relation: str = "related",
    weight: float = 1.0,
    source: str = "manual",
) -> dict | None:
    """Create a relation between two documents."""
    rel_id = _new_id()
    now = _now()

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute(
                """
                INSERT INTO kb_relations (id, src_id, dst_id, relation, weight, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (rel_id, src_id, dst_id, relation, weight, source, now),
            )
            await db.commit()

            cursor = await db.execute("SELECT * FROM kb_relations WHERE id = ?", (rel_id,))
            row = await cursor.fetchone()
            return _row_to_relation(row) if row else None
        except Exception:
            # Likely UNIQUE constraint violation
            return None


async def list_relations(
    db_path: str,
    *,
    src_id: str | None = None,
) -> list[dict]:
    """List relations, optionally filtered by source ID."""
    query = "SELECT * FROM kb_relations WHERE 1=1"
    params: list[Any] = []

    if src_id:
        query += " AND src_id = ?"
        params.append(src_id)

    query += " ORDER BY created_at DESC"

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [_row_to_relation(row) for row in rows]


async def update_relation(
    db_path: str,
    rel_id: str,
    **fields,
) -> dict | None:
    """Update a relation."""
    if not fields:
        return None

    set_clauses = []
    values: list[Any] = []

    for key, value in fields.items():
        if key in ("relation", "weight", "source"):
            set_clauses.append(f"{key} = ?")
            values.append(value)

    if not set_clauses:
        return None

    values.append(rel_id)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE kb_relations SET {', '.join(set_clauses)} WHERE id = ?",
            values,
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM kb_relations WHERE id = ?", (rel_id,))
        row = await cursor.fetchone()
        return _row_to_relation(row) if row else None


async def delete_relation(db_path: str, rel_id: str) -> bool:
    """Delete a relation."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("DELETE FROM kb_relations WHERE id = ?", (rel_id,))
        await db.commit()
        return cursor.rowcount > 0


async def get_graph(
    db_path: str,
    include_auto: bool = False,
) -> dict:
    """Get a graph representation with nodes (documents) and edges (relations).

    Nodes: documents with id, label, kind.
    Edges: manual relations with source field. When include_auto=True and embeddings
    are configured, also includes automatic similarity edges based on vector cosine similarity.

    Returns {"nodes": [...], "edges": [...]}.
    """
    from cyrene.knowledge import embeddings

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Get all documents as nodes
        cursor = await db.execute("SELECT id, name, kind FROM kb_documents ORDER BY name")
        doc_rows = await cursor.fetchall()
        nodes = [
            {
                "id": row["id"],
                "label": row["name"],
                "kind": row["kind"],
            }
            for row in doc_rows
        ]

        # Get all manual relations as edges
        cursor = await db.execute(
            "SELECT id, src_id, dst_id, relation, weight, source FROM kb_relations ORDER BY created_at"
        )
        rel_rows = await cursor.fetchall()
        manual_edges = [
            {
                "id": row["id"],
                "from": row["src_id"],
                "to": row["dst_id"],
                "relation": row["relation"],
                "weight": row["weight"],
                "source": row["source"],
            }
            for row in rel_rows
        ]

        edges = manual_edges

        # Build auto edges if enabled and embeddings are configured
        if include_auto and embeddings.is_configured():
            # Get all documents with embedded chunks
            cursor = await db.execute("SELECT DISTINCT document_id FROM kb_chunks WHERE embedding IS NOT NULL")
            doc_ids_with_embeddings = {row["document_id"] for row in await cursor.fetchall()}

            # Build per-document mean embeddings
            doc_embeddings = {}
            for doc_id in doc_ids_with_embeddings:
                cursor = await db.execute(
                    "SELECT embedding, embedding_dim FROM kb_chunks WHERE document_id = ? AND embedding IS NOT NULL ORDER BY ordinal",
                    (doc_id,),
                )
                chunk_rows = await cursor.fetchall()
                if not chunk_rows:
                    continue

                # Unpack vectors and check dimension consistency
                vectors = []
                target_dim = None
                for row in chunk_rows:
                    if row["embedding"] is None:
                        continue
                    vec = embeddings.unpack_vector(row["embedding"])
                    if target_dim is None:
                        target_dim = len(vec)
                    elif len(vec) != target_dim:
                        # Inconsistent dimensions, skip this document
                        target_dim = None
                        vectors = []
                        break
                    vectors.append(list(vec))

                if not vectors or target_dim is None:
                    continue

                # Compute mean embedding
                mean_vec = [sum(v[i] for v in vectors) / len(vectors) for i in range(target_dim)]
                doc_embeddings[doc_id] = mean_vec

            # Compute similarity between all pairs and collect high-similarity edges
            auto_edge_candidates = []
            doc_ids = list(doc_embeddings.keys())
            for i in range(len(doc_ids)):
                for j in range(i + 1, len(doc_ids)):
                    doc_a = doc_ids[i]
                    doc_b = doc_ids[j]

                    vec_a = doc_embeddings[doc_a]
                    vec_b = doc_embeddings[doc_b]

                    # Vectors must have equal dimension (already checked above)
                    if len(vec_a) != len(vec_b):
                        continue

                    sim = embeddings.cosine(vec_a, vec_b)

                    if sim >= 0.82:
                        # Check if pair is already connected by manual edge
                        already_connected = any(
                            (e["from"] == doc_a and e["to"] == doc_b)
                            or (e["from"] == doc_b and e["to"] == doc_a)
                            for e in manual_edges
                        )
                        if not already_connected:
                            auto_edge_candidates.append({
                                "doc_a": doc_a,
                                "doc_b": doc_b,
                                "sim": sim,
                            })

            # Sort by similarity and cap at 200
            auto_edge_candidates.sort(key=lambda x: x["sim"], reverse=True)
            auto_edge_candidates = auto_edge_candidates[:200]

            # Convert to edge format
            for idx, candidate in enumerate(auto_edge_candidates):
                doc_a = candidate["doc_a"]
                doc_b = candidate["doc_b"]
                sim = candidate["sim"]
                auto_edge = {
                    "id": f"auto:{doc_a}:{doc_b}",
                    "from": doc_a,
                    "to": doc_b,
                    "relation": "similar",
                    "weight": round(sim, 3),
                    "source": "auto",
                }
                edges.append(auto_edge)

    return {"nodes": nodes, "edges": edges}


async def get_stats(db_path: str) -> dict:
    """Get knowledge base statistics."""
    from cyrene.knowledge.embeddings import is_configured

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Get counts
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM kb_documents")
        doc_count = (await cursor.fetchone())["cnt"] or 0

        cursor = await db.execute("SELECT COUNT(*) as cnt FROM kb_chunks")
        chunk_count = (await cursor.fetchone())["cnt"] or 0

        # Get status breakdown
        cursor = await db.execute(
            "SELECT status, COUNT(*) as cnt FROM kb_documents GROUP BY status"
        )
        status_rows = await cursor.fetchall()
        status_counts = {row["status"]: row["cnt"] for row in status_rows}

        # Get kind breakdown
        cursor = await db.execute(
            "SELECT kind, COUNT(*) as cnt FROM kb_documents GROUP BY kind"
        )
        kind_rows = await cursor.fetchall()
        kind_counts = {row["kind"]: row["cnt"] for row in kind_rows}

    return {
        "documents": doc_count,
        "chunks": chunk_count,
        "by_status": status_counts,
        "by_kind": kind_counts,
        "embedding_configured": is_configured(),
    }
