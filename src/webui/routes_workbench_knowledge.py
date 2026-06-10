"""Workspace-scoped knowledge base API for the new Workbench UI.

This module is intentionally INDEPENDENT from the legacy ``routes_knowledge.py``
(which the old ``--agent`` UI uses). It exposes a parallel set of endpoints
under ``/api/workbench/knowledge/*`` so the two UIs never share request code.

The only thing shared is the pure data layer (``cyrene.knowledge.store`` /
``ingest`` / ``retrieve``) — that *is* the backend interface we reuse.

Per-workspace isolation: every request carries a ``workspace`` query param
(the Workbench project id). It resolves to its own ``kb_<workspace>.db`` file
via :func:`cyrene.config.get_knowledge_db_path`, so each workspace/project owns
a separate knowledge base. A missing/blank workspace falls back to ``default``.
"""

import asyncio
import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, UploadFile
from fastapi.responses import JSONResponse, FileResponse

from cyrene.attachments import (
    UPLOADS_DIR as _UPLOADS_DIR,
    attachment_kind_from_meta,
    is_uploaded_attachment_path,
    is_exported_attachment_path,
)

# Cache of knowledge-db paths whose tables have already been created, so we
# init each workspace db lazily (on first touch) exactly once per process.
_kb_initialized: set[str] = set()
_kb_init_lock = asyncio.Lock()


def _safe_workspace_id(workspace_id: str | None) -> str:
    """Sanitize a workspace id into a filesystem-safe key (defaults to 'default')."""
    raw = str(workspace_id or "").strip()
    if not raw:
        return "default"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    return cleaned or "default"


def _safe_upload_name(filename: str) -> str:
    """Sanitize a filename for upload."""
    raw = Path(str(filename or "upload.bin")).name
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    return sanitized or "upload.bin"


async def _ensure_kb_db(workspace_id: str | None) -> str:
    """Resolve a workspace to its kb db path, creating tables on first use."""
    from cyrene.config import get_knowledge_db_path
    from cyrene.db import init_knowledge_db

    wid = _safe_workspace_id(workspace_id)
    db_path = str(get_knowledge_db_path(wid))
    if db_path not in _kb_initialized:
        async with _kb_init_lock:
            if db_path not in _kb_initialized:
                await init_knowledge_db(db_path)
                _kb_initialized.add(db_path)
    return db_path


def register_workbench_knowledge_routes(router: APIRouter) -> None:
    """Register workspace-scoped knowledge routes for the Workbench UI."""
    from cyrene.knowledge import store, ingest, retrieve

    @router.get("/api/workbench/knowledge/documents")
    async def wb_list_documents(
        workspace: str = "",
        q: str = None,
        kind: str = None,
        status: str = None,
        tag: str = None,
        source: str = None,
        limit: int = 200,
    ):
        """List documents in a workspace's knowledge base."""
        try:
            db_path = await _ensure_kb_db(workspace)
            documents = await store.list_documents(
                db_path, q=q, kind=kind, status=status, tag=tag, source=source, limit=limit
            )
            return {"documents": documents, "workspace": _safe_workspace_id(workspace)}
        except Exception as e:
            return JSONResponse({"error": f"List failed: {str(e)}"}, status_code=400)

    @router.get("/api/workbench/knowledge/stats")
    async def wb_get_stats(workspace: str = ""):
        """Aggregate stats for a workspace's knowledge base."""
        try:
            db_path = await _ensure_kb_db(workspace)
            return await store.get_stats(db_path)
        except Exception as e:
            return JSONResponse({"error": f"Stats failed: {str(e)}"}, status_code=400)

    @router.get("/api/workbench/knowledge/documents/{doc_id}")
    async def wb_get_document(doc_id: str, workspace: str = ""):
        """Get a document with its chunks and relations."""
        try:
            db_path = await _ensure_kb_db(workspace)
            doc = await store.get_document(db_path, doc_id)
            if not doc:
                return JSONResponse({"error": "not found"}, status_code=404)
            chunks = await store.get_chunks(db_path, doc_id, with_embedding=False, limit=200)
            relations = await store.list_relations(db_path, src_id=doc_id)
            return {**doc, "chunks": chunks, "relations": relations}
        except Exception as e:
            return JSONResponse({"error": f"Get failed: {str(e)}"}, status_code=400)

    @router.post("/api/workbench/knowledge/documents")
    async def wb_upload_documents(files: list[UploadFile], workspace: str = ""):
        """Upload one or more documents into a workspace's knowledge base."""
        if not files:
            return JSONResponse({"error": "no files uploaded"}, status_code=400)

        try:
            db_path = await _ensure_kb_db(workspace)
        except Exception as e:
            return JSONResponse({"error": f"Workspace init failed: {str(e)}"}, status_code=400)

        _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        documents: list[dict[str, Any]] = []
        now = datetime.now().strftime("%Y%m%d_%H%M%S")

        for index, file in enumerate(files, start=1):
            try:
                safe_name = _safe_upload_name(file.filename or "")
                target = _UPLOADS_DIR / f"{now}_{index:02d}_{safe_name}"
                content = await file.read()
                target.write_bytes(content)
                content_hash = store.content_hash_bytes(content)

                content_type = str(
                    file.content_type
                    or mimetypes.guess_type(str(target))[0]
                    or "application/octet-stream"
                )
                kind = attachment_kind_from_meta(content_type, target.name)

                doc = await store.upsert_document_by_path(
                    db_path,
                    path=str(target.resolve()),
                    source="kb_upload",
                    name=file.filename or safe_name,
                    content_type=content_type,
                    kind=kind,
                    size=len(content),
                    content_hash=content_hash,
                )
                # If this content already existed, drop the freshly written dupe.
                if doc.get("path") and str(Path(doc["path"]).resolve()) != str(target.resolve()):
                    target.unlink(missing_ok=True)
                documents.append(doc)

                if doc.get("status") in {"pending", "error"}:
                    asyncio.create_task(ingest.index_document(db_path, doc["id"]))
            except Exception as e:
                return JSONResponse(
                    {"error": f"Failed to upload {file.filename}: {str(e)}"}, status_code=400
                )

        return {"documents": documents}

    @router.patch("/api/workbench/knowledge/documents/{doc_id}")
    async def wb_update_document(doc_id: str, body: dict, workspace: str = ""):
        """Update document metadata (title / tags / summary)."""
        try:
            db_path = await _ensure_kb_db(workspace)
            allowed_fields = {"title", "tags", "summary", "entity_id"}
            filtered = {k: v for k, v in (body or {}).items() if k in allowed_fields}
            if not filtered:
                doc = await store.get_document(db_path, doc_id)
                return doc or JSONResponse({"error": "not found"}, status_code=404)
            updated = await store.update_document(db_path, doc_id, **filtered)
            return updated or JSONResponse({"error": "not found"}, status_code=404)
        except Exception as e:
            return JSONResponse({"error": f"Update failed: {str(e)}"}, status_code=400)

    @router.post("/api/workbench/knowledge/documents/{doc_id}/reindex")
    async def wb_reindex_document(doc_id: str, workspace: str = ""):
        """Re-run extraction + indexing for a document."""
        try:
            db_path = await _ensure_kb_db(workspace)
            doc = await store.get_document(db_path, doc_id)
            if not doc:
                return JSONResponse({"error": "not found"}, status_code=404)
            asyncio.create_task(ingest.reindex_document(db_path, doc_id))
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"error": f"Reindex failed: {str(e)}"}, status_code=400)

    @router.delete("/api/workbench/knowledge/documents/{doc_id}")
    async def wb_delete_document(doc_id: str, workspace: str = ""):
        """Delete a document (and its on-disk file)."""
        try:
            db_path = await _ensure_kb_db(workspace)
            success = await store.delete_document(db_path, doc_id, remove_file=True)
            return {"ok": success}
        except Exception as e:
            return JSONResponse({"error": f"Delete failed: {str(e)}"}, status_code=400)

    @router.get("/api/workbench/knowledge/documents/{doc_id}/raw")
    async def wb_get_document_raw(doc_id: str, workspace: str = ""):
        """Download / preview the original document file."""
        try:
            db_path = await _ensure_kb_db(workspace)
            doc = await store.get_document(db_path, doc_id)
            if not doc:
                return JSONResponse({"error": "not found"}, status_code=404)
            path_str = doc.get("path", "")
            if not path_str:
                return JSONResponse({"error": "no file path"}, status_code=404)
            if not (is_uploaded_attachment_path(path_str) or is_exported_attachment_path(path_str)):
                return JSONResponse({"error": "file not in allowed paths"}, status_code=403)
            file_path = Path(path_str)
            if not file_path.exists():
                return JSONResponse({"error": "file not found on disk"}, status_code=404)
            return FileResponse(
                str(file_path), media_type=doc.get("content_type", "application/octet-stream")
            )
        except Exception as e:
            return JSONResponse({"error": f"Raw access failed: {str(e)}"}, status_code=400)

    @router.get("/api/workbench/knowledge/search")
    async def wb_search_knowledge(workspace: str = "", q: str = "", k: int = 8):
        """Full-text / hybrid search within a workspace's knowledge base."""
        try:
            if not q.strip():
                return {"results": []}
            db_path = await _ensure_kb_db(workspace)
            results = await retrieve.search_knowledge(db_path, q, k=k)
            return {"results": results}
        except Exception as e:
            return JSONResponse({"error": f"Search failed: {str(e)}"}, status_code=400)
