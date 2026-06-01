"""Tests for knowledge base API routes.

Tests cover document CRUD via HTTP endpoints, relations management, search,
graph retrieval, and integration with the knowledge store.
"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Patch missing PIL dependency before any cyrene import
pil_mock = MagicMock()
pil_mock.__version__ = "9.0.0"
sys.modules["PIL"] = pil_mock
pil_mock.Image = MagicMock()

from cyrene import db
from webui.routes import register_routes


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        import asyncio
        asyncio.run(db.init_db(db_path))
        yield db_path


@pytest.fixture
def client(temp_db):
    """Create a FastAPI test client with knowledge routes."""
    app = FastAPI()
    register_routes(app, bot=None, db_path=temp_db)
    return TestClient(app)


class TestKnowledgeRoutes:
    """Test knowledge base API routes."""

    @pytest.mark.asyncio
    async def test_stats_empty(self, client):
        """Test getting stats on an empty knowledge base."""
        response = client.get("/api/knowledge/stats")
        assert response.status_code == 200
        data = response.json()
        assert "documents" in data
        assert data["documents"] == 0
        assert data["chunks"] == 0

    @pytest.mark.asyncio
    async def test_list_documents_empty(self, client):
        """Test listing documents when empty."""
        response = client.get("/api/knowledge/documents")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    @pytest.mark.asyncio
    async def test_search_empty(self, client):
        """Test searching empty knowledge base."""
        response = client.get("/api/knowledge/search", params={"q": "test"})
        assert response.status_code == 200
        data = response.json()
        assert data.get("results") == []

    @pytest.mark.asyncio
    async def test_search_no_query(self, client):
        """Test searching with no query."""
        response = client.get("/api/knowledge/search")
        assert response.status_code == 200
        data = response.json()
        assert data.get("results") == []

    @pytest.mark.asyncio
    async def test_get_graph_empty(self, client):
        """Test getting graph on empty knowledge base."""
        response = client.get("/api/knowledge/graph")
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data
        assert "edges" in data
        assert len(data["nodes"]) == 0
        assert len(data["edges"]) == 0

    @pytest.mark.asyncio
    async def test_get_nonexistent_document(self, client):
        """Test getting a nonexistent document."""
        response = client.get("/api/knowledge/documents/nonexistent_id")
        assert response.status_code == 404
        data = response.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_create_relation_missing_fields(self, client):
        """Test creating a relation with missing fields."""
        response = client.post(
            "/api/knowledge/relations",
            json={"src_id": "doc1"},  # Missing dst_id, relation
        )
        assert response.status_code == 400
        data = response.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_delete_nonexistent_document(self, client):
        """Test deleting a nonexistent document."""
        response = client.delete("/api/knowledge/documents/nonexistent_id")
        assert response.status_code == 200
        data = response.json()
        assert data.get("ok") is False

    @pytest.mark.asyncio
    async def test_update_nonexistent_document(self, client):
        """Test updating a nonexistent document."""
        response = client.patch(
            "/api/knowledge/documents/nonexistent_id",
            json={"title": "New Title"},
        )
        assert response.status_code == 404
        data = response.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_delete_nonexistent_relation(self, client):
        """Test deleting a nonexistent relation."""
        response = client.delete("/api/knowledge/relations/nonexistent_rel")
        assert response.status_code == 200
        data = response.json()
        assert data.get("ok") is False

    @pytest.mark.asyncio
    async def test_update_nonexistent_relation(self, client):
        """Test updating a nonexistent relation."""
        response = client.patch(
            "/api/knowledge/relations/nonexistent_rel",
            json={"relation": "related"},
        )
        assert response.status_code == 404
        data = response.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_sync_documents(self, client, temp_db):
        """Test syncing documents from filesystem."""
        response = client.post("/api/knowledge/sync")
        assert response.status_code == 200
        data = response.json()
        assert "added" in data or "total" in data

    @pytest.mark.asyncio
    async def test_upload_no_files(self, client):
        """Test uploading with no files."""
        response = client.post("/api/knowledge/documents", data={})
        # FastAPI returns 422 for missing multipart files parameter
        assert response.status_code in [400, 422]

    @pytest.mark.asyncio
    async def test_import_missing_path(self, client):
        """Test importing with missing path."""
        response = client.post("/api/knowledge/import", json={})
        assert response.status_code == 400
        data = response.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_import_nonexistent_path(self, client):
        """Test importing a nonexistent file."""
        response = client.post(
            "/api/knowledge/import",
            json={"path": "/nonexistent/path/file.txt"},
        )
        # Should either return 404 or 403 depending on workspace guards
        assert response.status_code in [403, 404]

    @pytest.mark.asyncio
    async def test_get_document_raw_missing_path(self, client, temp_db):
        """Test getting raw file for document without path."""
        from cyrene.knowledge import store
        import asyncio

        # Create a document without a path (edge case)
        doc = await store.create_document(
            temp_db,
            name="test",
            path="/nonexistent/path",
            content_type="text/plain",
            kind="file",
            size=0,
        )

        response = client.get(f"/api/knowledge/documents/{doc['id']}/raw")
        # Should return either 403 (forbidden outside allowed paths) or 404 (not found)
        assert response.status_code in [403, 404]

    @pytest.mark.asyncio
    async def test_reindex_nonexistent_document(self, client):
        """Test reindexing a nonexistent document."""
        response = client.post(
            "/api/knowledge/documents/nonexistent_id/reindex"
        )
        assert response.status_code == 404


class TestKnowledgeToolSearchKnowledge:
    """Test the SearchKnowledge tool handler."""

    @pytest.mark.asyncio
    async def test_search_knowledge_tool_empty(self, temp_db):
        """Test SearchKnowledge tool on empty database."""
        from cyrene.tools import _tool_search_knowledge

        result = await _tool_search_knowledge(
            {"query": "test", "k": 6},
            _bot=None,
            _chat_id=-1,
            _db_path=temp_db,
            _notify_state=None,
        )
        assert isinstance(result, str)
        assert "No matching documents" in result or "error" in result.lower()

    @pytest.mark.asyncio
    async def test_search_knowledge_tool_missing_query(self, temp_db):
        """Test SearchKnowledge tool with missing query."""
        from cyrene.tools import _tool_search_knowledge

        result = await _tool_search_knowledge(
            {"k": 6},
            _bot=None,
            _chat_id=-1,
            _db_path=temp_db,
            _notify_state=None,
        )
        assert isinstance(result, str)
        assert "error" in result.lower() or "required" in result.lower()

    @pytest.mark.asyncio
    async def test_search_knowledge_tool_with_chunk(self, temp_db):
        """Test SearchKnowledge tool finding a document."""
        from cyrene.knowledge import store
        from cyrene.tools import _tool_search_knowledge
        import asyncio

        # Create a test document
        doc = await store.create_document(
            temp_db,
            name="test.md",
            path="/tmp/test.md",
            content_type="text/markdown",
            kind="code",
            size=100,
        )

        # Add a chunk with a specific keyword
        await store.replace_chunks(
            temp_db,
            doc["id"],
            [
                {
                    "content": "This is a test document about the knowledge base system.",
                    "char_start": 0,
                    "char_end": 57,
                }
            ],
        )

        # Search for the keyword
        result = await _tool_search_knowledge(
            {"query": "knowledge base", "k": 6},
            _bot=None,
            _chat_id=-1,
            _db_path=temp_db,
            _notify_state=None,
        )
        assert isinstance(result, str)
        # Should find the document
        assert "test.md" in result or "Found" in result
