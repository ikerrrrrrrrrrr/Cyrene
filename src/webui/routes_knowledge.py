"""Knowledge base API endpoints for the Web UI."""

import asyncio
import mimetypes
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
from cyrene.tools import _resolve_workspace_path


def _safe_upload_name(filename: str) -> str:
    """Sanitize a filename for upload."""
    import re
    raw = Path(str(filename or "upload.bin")).name
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    return sanitized or "upload.bin"


def register_knowledge_routes(router: APIRouter, db_path: str) -> None:
    """Register knowledge base API routes."""
    from cyrene.knowledge import store, ingest, retrieve

    @router.post("/api/knowledge/documents")
    async def api_upload_documents(files: list[UploadFile]):
        """Upload documents to the knowledge base."""
        if not files:
            return JSONResponse({"error": "no files uploaded"}, status_code=400)

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
                    file.content_type or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
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
                if doc.get("path") and str(Path(doc["path"]).resolve()) != str(target.resolve()):
                    target.unlink(missing_ok=True)
                documents.append(doc)

                if doc.get("status") in {"pending", "error"}:
                    asyncio.create_task(ingest.index_document(db_path, doc["id"]))
            except Exception as e:
                return JSONResponse({"error": f"Failed to upload {file.filename}: {str(e)}"}, status_code=400)

        return {"documents": documents}

    @router.post("/api/knowledge/import")
    async def api_import_document(body: dict):
        """Import a document from an existing path."""
        path_str = body.get("path", "").strip()
        if not path_str:
            return JSONResponse({"error": "path is required"}, status_code=400)

        try:
            resolved_path = _resolve_workspace_path(path_str)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=403)

        if not resolved_path.exists() or not resolved_path.is_file():
            return JSONResponse({"error": "file not found"}, status_code=404)

        try:
            content_type = mimetypes.guess_type(str(resolved_path))[0] or "application/octet-stream"
            kind = attachment_kind_from_meta(content_type, resolved_path.name)
            size = resolved_path.stat().st_size
            content_hash = store.content_hash_file(resolved_path)

            doc = await store.upsert_document_by_path(
                db_path,
                path=str(resolved_path.resolve()),
                source="import",
                name=resolved_path.name,
                content_type=content_type,
                kind=kind,
                size=size,
                content_hash=content_hash,
            )

            if doc.get("status") in {"pending", "error"}:
                asyncio.create_task(ingest.index_document(db_path, doc["id"]))
            return doc
        except Exception as e:
            return JSONResponse({"error": f"Failed to import: {str(e)}"}, status_code=400)

    @router.post("/api/knowledge/sync")
    async def api_sync_documents():
        """Sync documents from filesystem."""
        try:
            result = await store.sync_filesystem(db_path)
            asyncio.create_task(ingest.process_pending(db_path))
            return result
        except Exception as e:
            return JSONResponse({"error": f"Sync failed: {str(e)}"}, status_code=400)

    @router.get("/api/knowledge/documents")
    async def api_list_documents(
        q: str = None,
        kind: str = None,
        status: str = None,
        tag: str = None,
        source: str = None,
        limit: int = 200,
    ):
        """List documents."""
        try:
            return await store.list_documents(
                db_path, q=q, kind=kind, status=status, tag=tag, source=source, limit=limit
            )
        except Exception as e:
            return JSONResponse({"error": f"List failed: {str(e)}"}, status_code=400)

    @router.get("/api/knowledge/documents/{doc_id}")
    async def api_get_document(doc_id: str):
        """Get a document with its chunks and relations."""
        try:
            doc = await store.get_document(db_path, doc_id)
            if not doc:
                return JSONResponse({"error": "not found"}, status_code=404)

            chunks = await store.get_chunks(db_path, doc_id, with_embedding=False, limit=200)
            relations = await store.list_relations(db_path, src_id=doc_id)

            return {**doc, "chunks": chunks, "relations": relations}
        except Exception as e:
            return JSONResponse({"error": f"Get failed: {str(e)}"}, status_code=400)

    @router.patch("/api/knowledge/documents/{doc_id}")
    async def api_update_document(doc_id: str, body: dict):
        """Update document metadata."""
        try:
            allowed_fields = {"title", "tags", "summary", "entity_id"}
            filtered = {k: v for k, v in body.items() if k in allowed_fields}

            if not filtered:
                doc = await store.get_document(db_path, doc_id)
                return doc or JSONResponse({"error": "not found"}, status_code=404)

            updated = await store.update_document(db_path, doc_id, **filtered)
            return updated or JSONResponse({"error": "not found"}, status_code=404)
        except Exception as e:
            return JSONResponse({"error": f"Update failed: {str(e)}"}, status_code=400)

    @router.post("/api/knowledge/documents/{doc_id}/reindex")
    async def api_reindex_document(doc_id: str):
        """Reindex a document."""
        try:
            doc = await store.get_document(db_path, doc_id)
            if not doc:
                return JSONResponse({"error": "not found"}, status_code=404)

            asyncio.create_task(ingest.reindex_document(db_path, doc_id))
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"error": f"Reindex failed: {str(e)}"}, status_code=400)

    @router.delete("/api/knowledge/documents/{doc_id}")
    async def api_delete_document(doc_id: str):
        """Delete a document."""
        try:
            success = await store.delete_document(db_path, doc_id, remove_file=True)
            return {"ok": success}
        except Exception as e:
            return JSONResponse({"error": f"Delete failed: {str(e)}"}, status_code=400)

    @router.get("/api/knowledge/documents/{doc_id}/raw")
    async def api_get_document_raw(doc_id: str):
        """Download/preview the raw document file."""
        try:
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

            return FileResponse(str(file_path), media_type=doc.get("content_type", "application/octet-stream"))
        except Exception as e:
            return JSONResponse({"error": f"Raw access failed: {str(e)}"}, status_code=400)

    @router.get("/api/knowledge/search")
    async def api_search_knowledge(q: str = "", k: int = 6):
        """Search the knowledge base."""
        try:
            if not q.strip():
                return {"results": []}

            results = await retrieve.search_knowledge(db_path, q, k=k)
            return {"results": results}
        except Exception as e:
            return JSONResponse({"error": f"Search failed: {str(e)}"}, status_code=400)

    @router.get("/api/knowledge/graph")
    async def api_get_graph(include_auto: bool = True):
        """Get the knowledge graph."""
        try:
            graph = await store.get_graph(db_path, include_auto=include_auto)
            return graph
        except Exception as e:
            return JSONResponse({"error": f"Graph failed: {str(e)}"}, status_code=400)

    @router.post("/api/knowledge/relations")
    async def api_create_relation(body: dict):
        """Create a relation between documents."""
        try:
            src_id = body.get("src_id", "").strip()
            dst_id = body.get("dst_id", "").strip()
            relation = body.get("relation", "related").strip()
            weight = float(body.get("weight", 1.0))

            if not src_id or not dst_id or not relation:
                return JSONResponse(
                    {"error": "src_id, dst_id, and relation are required"}, status_code=400
                )

            rel = await store.create_relation(db_path, src_id=src_id, dst_id=dst_id, relation=relation, weight=weight)
            return rel
        except Exception as e:
            return JSONResponse({"error": f"Create relation failed: {str(e)}"}, status_code=400)

    @router.patch("/api/knowledge/relations/{rel_id}")
    async def api_update_relation(rel_id: str, body: dict):
        """Update a relation."""
        try:
            allowed_fields = {"relation", "weight"}
            filtered = {k: v for k, v in body.items() if k in allowed_fields}

            if not filtered:
                return JSONResponse({"error": "no fields to update"}, status_code=400)

            updated = await store.update_relation(db_path, rel_id, **filtered)
            return updated or JSONResponse({"error": "not found"}, status_code=404)
        except Exception as e:
            return JSONResponse({"error": f"Update relation failed: {str(e)}"}, status_code=400)

    @router.delete("/api/knowledge/relations/{rel_id}")
    async def api_delete_relation(rel_id: str):
        """Delete a relation."""
        try:
            success = await store.delete_relation(db_path, rel_id)
            return {"ok": success}
        except Exception as e:
            return JSONResponse({"error": f"Delete relation failed: {str(e)}"}, status_code=400)

    @router.get("/api/knowledge/stats")
    async def api_get_stats():
        """Get knowledge base statistics."""
        try:
            stats = await store.get_stats(db_path)
            return stats
        except Exception as e:
            return JSONResponse({"error": f"Stats failed: {str(e)}"}, status_code=400)
