"""Knowledge base search and retrieval.

Provides hybrid search combining FTS5 and vector embeddings via Reciprocal Rank Fusion.
"""

import re

from cyrene.knowledge import store, embeddings


async def search_knowledge(
    db_path: str,
    query: str,
    *,
    k: int = 6,
    document_id: str | None = None,
) -> list[dict]:
    """Search knowledge base using hybrid FTS5 + vector approach.

    Returns list of dicts with:
    - chunk_id, document_id, document_name, content, score, mode
    where mode is "fts", "vector", or "hybrid".
    """
    import aiosqlite

    if not query or not query.strip():
        return []

    query = query.strip()
    results_by_chunk_id = {}  # chunk_id -> {chunk_info, scores}

    # =========================================================================
    # FTS5 Path
    # =========================================================================
    fts_results = []

    # Prepare query for FTS5
    if len(query) < 3:
        # Too short for trigram; fall back to LIKE
        like_pattern = f"%{query}%"
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = """
                SELECT chunk_id, document_id, content
                FROM kb_chunks_fts
                WHERE content LIKE ?
            """
            params = [like_pattern]
            if document_id:
                sql += " AND document_id = ?"
                params.append(document_id)
            sql += " LIMIT ?"
            params.append(k * 4)

            cursor = await db.execute(sql, params)
            fts_results = [dict(row) for row in await cursor.fetchall()]
    else:
        # Use MATCH with a quoted phrase; escape internal quotes FIRST, then wrap
        fts_query = '"' + query.replace('"', '""') + '"'

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = """
                SELECT chunk_id, document_id, content, rank
                FROM kb_chunks_fts
                WHERE kb_chunks_fts MATCH ?
            """
            params = [fts_query]
            if document_id:
                sql += " AND document_id = ?"
                params.append(document_id)
            sql += " ORDER BY rank LIMIT ?"
            params.append(k * 4)

            try:
                cursor = await db.execute(sql, params)
                rows = await cursor.fetchall()
                fts_results = [dict(row) for row in rows]
            except Exception:
                # Fall back to LIKE if MATCH fails
                like_pattern = f"%{query}%"
                sql = """
                    SELECT chunk_id, document_id, content
                    FROM kb_chunks_fts
                    WHERE content LIKE ?
                """
                params = [like_pattern]
                if document_id:
                    sql += " AND document_id = ?"
                    params.append(document_id)
                sql += " LIMIT ?"
                params.append(k * 4)
                cursor = await db.execute(sql, params)
                fts_results = [dict(row) for row in await cursor.fetchall()]

    # Process FTS results
    for rank, result in enumerate(fts_results):
        chunk_id = result["chunk_id"]
        if chunk_id not in results_by_chunk_id:
            results_by_chunk_id[chunk_id] = {
                "chunk_id": chunk_id,
                "document_id": result["document_id"],
                "content": result["content"],
                "fts_rank": rank,
                "vector_rank": None,
            }

    # =========================================================================
    # Vector Path (if configured)
    # =========================================================================
    vector_results = []
    if embeddings.is_configured():
        try:
            # Embed the query
            query_embedding = await embeddings.embed_texts([query])
            query_vector = query_embedding[0]
            query_dim = len(query_vector)

            # Get all embedded chunks
            embedded_chunks = await store.iter_embedded_chunks(
                db_path, document_id=document_id
            )

            # Compute cosine similarity
            scores = []
            for chunk in embedded_chunks:
                if chunk["embedding"] is None:
                    continue
                chunk_vector = embeddings.unpack_vector(chunk["embedding"])
                # Skip if dimensions don't match
                if len(chunk_vector) != query_dim:
                    continue
                score = embeddings.cosine(query_vector, chunk_vector)
                scores.append((score, chunk))

            # Sort by score descending
            scores.sort(reverse=True, key=lambda x: x[0])

            # Take top k*4
            for rank, (score, chunk) in enumerate(scores[: k * 4]):
                chunk_id = chunk["id"]
                if chunk_id not in results_by_chunk_id:
                    results_by_chunk_id[chunk_id] = {
                        "chunk_id": chunk_id,
                        "document_id": chunk["document_id"],
                        "content": chunk["content"],
                        "fts_rank": None,
                        "vector_rank": rank,
                    }
                else:
                    results_by_chunk_id[chunk_id]["vector_rank"] = rank
        except Exception:
            # Embedding failed; just use FTS results
            pass

    # =========================================================================
    # Merge using Reciprocal Rank Fusion (RRF)
    # =========================================================================
    merged = []
    for chunk_data in results_by_chunk_id.values():
        fts_rank = chunk_data["fts_rank"]
        vec_rank = chunk_data["vector_rank"]

        # RRF formula: score = sum(1 / (60 + rank)) for each rank
        rrf_score = 0.0
        modes = []

        if fts_rank is not None:
            rrf_score += 1.0 / (60 + fts_rank)
            modes.append("fts")

        if vec_rank is not None:
            rrf_score += 1.0 / (60 + vec_rank)
            modes.append("vector")

        mode = "hybrid" if len(modes) > 1 else (modes[0] if modes else "unknown")

        merged.append(
            {
                "chunk_id": chunk_data["chunk_id"],
                "document_id": chunk_data["document_id"],
                "content": chunk_data["content"],
                "score": rrf_score,
                "mode": mode,
            }
        )

    # Sort by score descending
    merged.sort(reverse=True, key=lambda x: x["score"])

    # Fetch document names and take top k
    final_results = []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        for item in merged[:k]:
            cursor = await db.execute(
                "SELECT name FROM kb_documents WHERE id = ?",
                (item["document_id"],),
            )
            doc_row = await cursor.fetchone()
            doc_name = doc_row["name"] if doc_row else "Unknown"

            final_results.append(
                {
                    "chunk_id": item["chunk_id"],
                    "document_id": item["document_id"],
                    "document_name": doc_name,
                    "content": item["content"],
                    "score": item["score"],
                    "mode": item["mode"],
                }
            )

    return final_results
