"""Tests for the knowledge base module.

Covers document CRUD, chunking, FTS search, relations, and cascade deletion.
All tests run with embeddings UNCONFIGURED to exercise FTS5 only.
"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Patch missing PIL dependency before any cyrene import
pil_mock = MagicMock()
pil_mock.__version__ = "9.0.0"
sys.modules["PIL"] = pil_mock
pil_mock.Image = MagicMock()

from cyrene import db
from cyrene.knowledge import store, embeddings


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        # Initialize tables
        import asyncio

        asyncio.run(db.init_db(db_path))
        yield db_path


class TestDocumentCRUD:
    """Test document CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_document(self, temp_db):
        """Test creating a document."""
        doc = await store.create_document(
            temp_db,
            name="test.md",
            path="/tmp/test.md",
            content_type="text/markdown",
            kind="code",
            size=100,
            source="chat_upload",
            title="Test Document",
        )

        assert doc["name"] == "test.md"
        assert doc["status"] == "pending"
        assert doc["kind"] == "code"
        assert doc["source"] == "chat_upload"
        assert doc["title"] == "Test Document"

    @pytest.mark.asyncio
    async def test_get_document(self, temp_db):
        """Test retrieving a document."""
        created = await store.create_document(
            temp_db,
            name="test.md",
            path="/tmp/test.md",
        )

        retrieved = await store.get_document(temp_db, created["id"])
        assert retrieved is not None
        assert retrieved["id"] == created["id"]
        assert retrieved["name"] == "test.md"

    @pytest.mark.asyncio
    async def test_get_nonexistent_document(self, temp_db):
        """Test retrieving a nonexistent document."""
        result = await store.get_document(temp_db, "nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_documents(self, temp_db):
        """Test listing documents."""
        doc1 = await store.create_document(
            temp_db,
            name="file1.pdf",
            path="/tmp/file1.pdf",
            kind="pdf",
        )
        doc2 = await store.create_document(
            temp_db,
            name="file2.md",
            path="/tmp/file2.md",
            kind="code",
        )

        all_docs = await store.list_documents(temp_db)
        assert len(all_docs) == 2

        pdf_docs = await store.list_documents(temp_db, kind="pdf")
        assert len(pdf_docs) == 1
        assert pdf_docs[0]["id"] == doc1["id"]

    @pytest.mark.asyncio
    async def test_update_document(self, temp_db):
        """Test updating a document."""
        doc = await store.create_document(
            temp_db,
            name="test.md",
            path="/tmp/test.md",
        )

        updated = await store.update_document(
            temp_db,
            doc["id"],
            status="indexed",
            title="Updated Title",
            char_count=500,
        )

        assert updated["status"] == "indexed"
        assert updated["title"] == "Updated Title"
        assert updated["char_count"] == 500

    @pytest.mark.asyncio
    async def test_update_document_tags(self, temp_db):
        """Test updating document tags."""
        doc = await store.create_document(
            temp_db,
            name="test.md",
            path="/tmp/test.md",
            tags=["tag1"],
        )

        updated = await store.update_document(
            temp_db,
            doc["id"],
            tags=["tag1", "tag2"],
        )

        assert "tag1" in updated["tags"]
        assert "tag2" in updated["tags"]


class TestUpsertIdempotency:
    """Test idempotent upsert by path."""

    @pytest.mark.asyncio
    async def test_upsert_creates_new(self, temp_db):
        """Test upsert creates a new document."""
        doc = await store.upsert_document_by_path(
            temp_db,
            path="/tmp/doc1.md",
            source="chat_upload",
            name="doc1.md",
            title="Original",
        )

        assert doc["name"] == "doc1.md"
        assert doc["title"] == "Original"

    @pytest.mark.asyncio
    async def test_upsert_updates_same_path(self, temp_db):
        """Test upsert with same path updates the row."""
        doc1 = await store.upsert_document_by_path(
            temp_db,
            path="/tmp/doc1.md",
            source="chat_upload",
            name="doc1.md",
            title="Original",
        )

        doc2 = await store.upsert_document_by_path(
            temp_db,
            path="/tmp/doc1.md",
            source="kb_upload",
            name="doc1.md",
            title="Updated",
        )

        # Should be the same document (same ID)
        assert doc1["id"] == doc2["id"]
        # Title should be updated
        assert doc2["title"] == "Updated"

        # Verify only one row in DB
        all_docs = await store.list_documents(temp_db)
        assert len(all_docs) == 1

    @pytest.mark.asyncio
    async def test_upsert_deduplicates_same_content_hash(self, temp_db):
        """Test upsert with identical content hash returns the existing row."""
        digest = store.content_hash_bytes(b"same document bytes")
        doc1 = await store.upsert_document_by_path(
            temp_db,
            path="/tmp/doc1.md",
            source="chat_upload",
            name="doc1.md",
            content_hash=digest,
        )

        doc2 = await store.upsert_document_by_path(
            temp_db,
            path="/tmp/renamed-doc1.md",
            source="kb_upload",
            name="renamed-doc1.md",
            content_hash=digest,
        )

        assert doc1["id"] == doc2["id"]
        assert doc2["path"] == "/tmp/doc1.md"
        all_docs = await store.list_documents(temp_db)
        assert len(all_docs) == 1

    @pytest.mark.asyncio
    async def test_deduplicate_documents_backfills_existing_rows(self, temp_db, tmp_path):
        """Existing path-only duplicate rows are collapsed by content hash."""
        file1 = tmp_path / "doc1.txt"
        file2 = tmp_path / "doc2.txt"
        file1.write_bytes(b"legacy duplicate bytes")
        file2.write_bytes(b"legacy duplicate bytes")

        doc1 = await store.create_document(temp_db, name="doc1.txt", path=str(file1))
        doc2 = await store.create_document(temp_db, name="doc2.txt", path=str(file2))
        assert doc1["id"] != doc2["id"]

        result = await store.deduplicate_documents(temp_db)

        assert result["updated_hashes"] == 1
        assert result["removed_duplicates"] == 1
        all_docs = await store.list_documents(temp_db)
        assert len(all_docs) == 1
        assert all_docs[0]["id"] == doc1["id"]
        assert all_docs[0]["content_hash"] == store.content_hash_file(file1)


class TestChunks:
    """Test chunk operations."""

    @pytest.mark.asyncio
    async def test_replace_chunks(self, temp_db):
        """Test replacing chunks for a document."""
        doc = await store.create_document(
            temp_db,
            name="test.md",
            path="/tmp/test.md",
        )

        chunks = [
            {
                "id": None,
                "ordinal": 0,
                "content": "First chunk of text.",
                "char_start": 0,
                "char_end": 20,
                "token_count": 5,
            },
            {
                "id": None,
                "ordinal": 1,
                "content": "Second chunk of text.",
                "char_start": 20,
                "char_end": 41,
                "token_count": 5,
            },
        ]

        await store.replace_chunks(temp_db, doc["id"], chunks)

        retrieved = await store.get_chunks(temp_db, doc["id"])
        assert len(retrieved) == 2
        assert retrieved[0]["content"] == "First chunk of text."
        assert retrieved[1]["content"] == "Second chunk of text."

    @pytest.mark.asyncio
    async def test_replace_chunks_twice(self, temp_db):
        """Test that replace_chunks actually replaces old chunks."""
        doc = await store.create_document(
            temp_db,
            name="test.md",
            path="/tmp/test.md",
        )

        chunks1 = [
            {
                "id": None,
                "ordinal": 0,
                "content": "Old chunk.",
                "char_start": 0,
                "char_end": 10,
            }
        ]

        await store.replace_chunks(temp_db, doc["id"], chunks1)

        chunks2 = [
            {
                "id": None,
                "ordinal": 0,
                "content": "New chunk 1.",
                "char_start": 0,
                "char_end": 12,
            },
            {
                "id": None,
                "ordinal": 1,
                "content": "New chunk 2.",
                "char_start": 12,
                "char_end": 24,
            },
        ]

        await store.replace_chunks(temp_db, doc["id"], chunks2)

        retrieved = await store.get_chunks(temp_db, doc["id"])
        assert len(retrieved) == 2
        assert all("New chunk" in c["content"] for c in retrieved)


class TestChunkText:
    """Test text chunking function."""

    def test_chunk_text_simple(self):
        """Test chunking simple text."""
        from cyrene.knowledge.ingest import chunk_text

        text = "This is a sentence. This is another sentence. And a third."
        chunks = chunk_text(text, target_chars=20, overlap=5)

        assert len(chunks) > 0
        assert all(len(chunk[0]) > 0 for chunk in chunks)
        # Verify char positions are valid
        for text_chunk, start, end in chunks:
            assert text[start:end].strip() == text_chunk

    def test_chunk_text_with_paragraphs(self):
        """Test chunking respects paragraph boundaries."""
        from cyrene.knowledge.ingest import chunk_text

        text = "Para 1\n\nPara 2\n\nPara 3"
        chunks = chunk_text(text, target_chars=100)

        assert len(chunks) > 0

    def test_chunk_text_empty(self):
        """Test chunking empty text."""
        from cyrene.knowledge.ingest import chunk_text

        chunks = chunk_text("")
        assert len(chunks) == 0

    def test_chunk_text_cjk(self):
        """Test chunking Chinese text."""
        from cyrene.knowledge.ingest import chunk_text

        text = "这是第一句中文。这是第二句中文。这是第三句中文。" * 10
        chunks = chunk_text(text, target_chars=50)

        assert len(chunks) > 0

    def test_chunk_text_short_single_chunk(self):
        """Test that short text yields exactly one chunk."""
        from cyrene.knowledge.ingest import chunk_text

        # Create text well under target_chars (default 800)
        text = "这是短文本。" * 8  # ~48 chars
        chunks = chunk_text(text)

        assert len(chunks) == 1, f"Expected 1 chunk for short text, got {len(chunks)}"

    def test_chunk_text_long_multiple_chunks(self):
        """Test that long text yields reasonable number of chunks (not hundreds)."""
        from cyrene.knowledge.ingest import chunk_text

        # Create long text (~5000 chars)
        text = "Sentence about retrieval. " * 220  # ~5720 chars
        chunks = chunk_text(text, target_chars=800, overlap=120)

        assert len(chunks) < 20, f"Expected < 20 chunks for ~5700 char text, got {len(chunks)}"
        assert len(chunks) > 0, "Expected at least 1 chunk"

    def test_chunk_text_positions_advancing(self):
        """Test that consecutive chunks advance their char positions."""
        from cyrene.knowledge.ingest import chunk_text

        text = "Sentence. " * 500  # ~5000 chars
        chunks = chunk_text(text, target_chars=800, overlap=120)

        # Check each chunk's char_start strictly increases
        for i in range(len(chunks) - 1):
            assert chunks[i][1] < chunks[i+1][1], "Chunk positions not advancing"


class TestDeleteCascade:
    """Test cascade deletion of chunks and relations."""

    @pytest.mark.asyncio
    async def test_delete_document_deletes_chunks(self, temp_db):
        """Test deleting a document also deletes its chunks."""
        doc = await store.create_document(
            temp_db,
            name="test.md",
            path="/tmp/test.md",
        )

        chunks = [
            {
                "id": None,
                "ordinal": 0,
                "content": "Chunk 1",
                "char_start": 0,
                "char_end": 7,
            },
            {
                "id": None,
                "ordinal": 1,
                "content": "Chunk 2",
                "char_start": 7,
                "char_end": 14,
            },
        ]

        await store.replace_chunks(temp_db, doc["id"], chunks)

        # Verify chunks exist
        chunks_before = await store.get_chunks(temp_db, doc["id"])
        assert len(chunks_before) == 2

        # Delete document
        deleted = await store.delete_document(temp_db, doc["id"], remove_file=False)
        assert deleted

        # Verify chunks are gone
        chunks_after = await store.get_chunks(temp_db, doc["id"])
        assert len(chunks_after) == 0

    @pytest.mark.asyncio
    async def test_delete_document_deletes_relations(self, temp_db):
        """Test deleting a document deletes its relations."""
        doc1 = await store.create_document(
            temp_db,
            name="doc1.md",
            path="/tmp/doc1.md",
        )
        doc2 = await store.create_document(
            temp_db,
            name="doc2.md",
            path="/tmp/doc2.md",
        )

        # Create relation
        rel = await store.create_relation(
            temp_db,
            src_id=doc1["id"],
            dst_id=doc2["id"],
            relation="related",
        )
        assert rel is not None

        # Delete doc1
        await store.delete_document(temp_db, doc1["id"], remove_file=False)

        # Verify relation is gone
        relations = await store.list_relations(temp_db)
        assert len(relations) == 0


class TestRelations:
    """Test relation operations."""

    @pytest.mark.asyncio
    async def test_create_relation(self, temp_db):
        """Test creating a relation."""
        doc1 = await store.create_document(
            temp_db,
            name="doc1.md",
            path="/tmp/doc1.md",
        )
        doc2 = await store.create_document(
            temp_db,
            name="doc2.md",
            path="/tmp/doc2.md",
        )

        rel = await store.create_relation(
            temp_db,
            src_id=doc1["id"],
            dst_id=doc2["id"],
            relation="references",
            weight=0.8,
        )

        assert rel is not None
        assert rel["src_id"] == doc1["id"]
        assert rel["dst_id"] == doc2["id"]
        assert rel["relation"] == "references"
        assert rel["weight"] == 0.8

    @pytest.mark.asyncio
    async def test_list_relations(self, temp_db):
        """Test listing relations."""
        doc1 = await store.create_document(
            temp_db,
            name="doc1.md",
            path="/tmp/doc1.md",
        )
        doc2 = await store.create_document(
            temp_db,
            name="doc2.md",
            path="/tmp/doc2.md",
        )
        doc3 = await store.create_document(
            temp_db,
            name="doc3.md",
            path="/tmp/doc3.md",
        )

        rel1 = await store.create_relation(
            temp_db,
            src_id=doc1["id"],
            dst_id=doc2["id"],
        )
        rel2 = await store.create_relation(
            temp_db,
            src_id=doc1["id"],
            dst_id=doc3["id"],
        )

        all_rels = await store.list_relations(temp_db)
        assert len(all_rels) == 2

        doc1_rels = await store.list_relations(temp_db, src_id=doc1["id"])
        assert len(doc1_rels) == 2

    @pytest.mark.asyncio
    async def test_get_graph(self, temp_db):
        """Test getting the knowledge graph."""
        doc1 = await store.create_document(
            temp_db,
            name="doc1.md",
            path="/tmp/doc1.md",
        )
        doc2 = await store.create_document(
            temp_db,
            name="doc2.md",
            path="/tmp/doc2.md",
        )

        await store.create_relation(
            temp_db,
            src_id=doc1["id"],
            dst_id=doc2["id"],
            relation="related",
        )

        graph = await store.get_graph(temp_db)
        assert "nodes" in graph
        assert "edges" in graph
        assert len(graph["nodes"]) == 2
        assert len(graph["edges"]) == 1


class TestSearch:
    """Test FTS5 search functionality."""

    @pytest.mark.asyncio
    async def test_search_fts_english(self, temp_db):
        """Test FTS5 search with English text."""
        from cyrene.knowledge.retrieve import search_knowledge

        doc = await store.create_document(
            temp_db,
            name="test.md",
            path="/tmp/test.md",
        )

        chunks = [
            {
                "id": None,
                "ordinal": 0,
                "content": "The quick brown fox jumps over the lazy dog",
                "char_start": 0,
                "char_end": 44,
            }
        ]

        await store.replace_chunks(temp_db, doc["id"], chunks)

        results = await search_knowledge(temp_db, "quick brown")
        assert len(results) > 0
        assert results[0]["mode"] == "fts"
        assert "quick" in results[0]["content"].lower()

    @pytest.mark.asyncio
    async def test_search_fts_chinese(self, temp_db):
        """Test FTS5 search with Chinese text."""
        from cyrene.knowledge.retrieve import search_knowledge

        doc = await store.create_document(
            temp_db,
            name="test.md",
            path="/tmp/test.md",
        )

        chunks = [
            {
                "id": None,
                "ordinal": 0,
                "content": "这是一个关于知识库的中文文本",
                "char_start": 0,
                "char_end": 16,
            }
        ]

        await store.replace_chunks(temp_db, doc["id"], chunks)

        results = await search_knowledge(temp_db, "知识库")
        assert len(results) > 0
        assert results[0]["mode"] == "fts"

    @pytest.mark.asyncio
    async def test_search_no_results(self, temp_db):
        """Test search with no matching results."""
        from cyrene.knowledge.retrieve import search_knowledge

        doc = await store.create_document(
            temp_db,
            name="test.md",
            path="/tmp/test.md",
        )

        chunks = [
            {
                "id": None,
                "ordinal": 0,
                "content": "Hello world",
                "char_start": 0,
                "char_end": 11,
            }
        ]

        await store.replace_chunks(temp_db, doc["id"], chunks)

        results = await search_knowledge(temp_db, "nonexistent")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_short_query(self, temp_db):
        """Test search with short query (< 3 chars, uses LIKE)."""
        from cyrene.knowledge.retrieve import search_knowledge

        doc = await store.create_document(
            temp_db,
            name="test.md",
            path="/tmp/test.md",
        )

        chunks = [
            {
                "id": None,
                "ordinal": 0,
                "content": "a quick fox",
                "char_start": 0,
                "char_end": 11,
            }
        ]

        await store.replace_chunks(temp_db, doc["id"], chunks)

        results = await search_knowledge(temp_db, "fox")
        assert len(results) > 0


class TestStats:
    """Test statistics function."""

    @pytest.mark.asyncio
    async def test_get_stats(self, temp_db):
        """Test getting knowledge base statistics."""
        doc1 = await store.create_document(
            temp_db,
            name="doc1.pdf",
            path="/tmp/doc1.pdf",
            kind="pdf",
        )
        doc1 = await store.update_document(temp_db, doc1["id"], status="indexed")

        doc2 = await store.create_document(
            temp_db,
            name="doc2.md",
            path="/tmp/doc2.md",
            kind="code",
        )

        chunks = [
            {
                "id": None,
                "ordinal": 0,
                "content": "Chunk text",
                "char_start": 0,
                "char_end": 10,
            }
        ]
        await store.replace_chunks(temp_db, doc1["id"], chunks)

        stats = await store.get_stats(temp_db)

        assert stats["documents"] == 2
        assert stats["chunks"] == 1
        assert stats["by_status"]["indexed"] == 1
        assert stats["by_status"]["pending"] == 1
        assert stats["by_kind"]["pdf"] == 1
        assert stats["by_kind"]["code"] == 1
        assert stats["embedding_configured"] is False


class TestEmbeddingsGracefulDegradation:
    """Test that system works without embeddings configured."""

    @pytest.mark.asyncio
    async def test_embeddings_unconfigured(self):
        """Verify embeddings are not configured in tests."""
        assert not embeddings.is_configured()

    @pytest.mark.asyncio
    async def test_ingest_without_embeddings(self, temp_db):
        """Test document indexing works without embeddings."""
        from cyrene.knowledge.ingest import index_document

        # Create a temp file
        with TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("This is test content for indexing.", encoding="utf-8")

            doc = await store.create_document(
                temp_db,
                name="test.txt",
                path=str(test_file),
                kind="code",
            )

            # Index without embeddings
            await index_document(temp_db, doc["id"])

            # Verify indexed
            indexed = await store.get_document(temp_db, doc["id"])
            assert indexed["status"] == "indexed"
            assert indexed["chunk_count"] > 0

            # Verify chunks have no embeddings
            chunks = await store.get_chunks(temp_db, doc["id"])
            assert len(chunks) > 0
            assert all(chunk["embedding"] is None for chunk in chunks)


class TestSyncFilesystem:
    """Test filesystem synchronization."""

    @pytest.mark.asyncio
    async def test_sync_filesystem_new_files(self, temp_db, monkeypatch):
        """Test sync_filesystem discovers and registers new files."""
        with TemporaryDirectory() as tmpdir_uploads:
            with TemporaryDirectory() as tmpdir_exports:
                uploads_path = Path(tmpdir_uploads)
                exports_path = Path(tmpdir_exports)

                # Create test files
                (uploads_path / "file1.txt").write_text("Content 1")
                (uploads_path / "file2.md").write_text("Content 2")
                (exports_path / "generated.txt").write_text("Generated content")

                # Monkeypatch the directories
                monkeypatch.setattr("cyrene.attachments.UPLOADS_DIR", uploads_path)
                monkeypatch.setattr("cyrene.attachments.EXPORTS_DIR", exports_path)

                # Sync should find 3 new files
                result = await store.sync_filesystem(temp_db)
                assert result["added"] == 3
                assert result["total"] == 3

                # Verify documents appear in list
                docs = await store.list_documents(temp_db)
                assert len(docs) == 3

                # Check sources
                doc_sources = {doc["name"]: doc["source"] for doc in docs}
                assert doc_sources["file1.txt"] == "chat_upload"
                assert doc_sources["file2.md"] == "chat_upload"
                assert doc_sources["generated.txt"] == "generated"

                # Sync again should add 0 (idempotent)
                result2 = await store.sync_filesystem(temp_db)
                assert result2["added"] == 0
                assert result2["total"] == 3


class TestProcessPending:
    """Test pending document processing."""

    @pytest.mark.asyncio
    async def test_process_pending(self, temp_db):
        """Test processing pending documents."""
        from cyrene.knowledge.ingest import process_pending

        with TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "pending.txt"
            test_file.write_text("Content to be indexed.")

            # Create a pending document
            doc = await store.create_document(
                temp_db,
                name="pending.txt",
                path=str(test_file),
                kind="code",
            )

            # Verify it's pending
            before = await store.get_document(temp_db, doc["id"])
            assert before["status"] == "pending"

            # Process pending
            await process_pending(temp_db)

            # Verify it's indexed
            after = await store.get_document(temp_db, doc["id"])
            assert after["status"] == "indexed"
            assert after["chunk_count"] > 0


class TestUpdateAndDeleteRelation:
    """Test relation update and delete operations."""

    @pytest.mark.asyncio
    async def test_update_relation(self, temp_db):
        """Test updating a relation."""
        doc1 = await store.create_document(
            temp_db,
            name="doc1.md",
            path="/tmp/doc1.md",
        )
        doc2 = await store.create_document(
            temp_db,
            name="doc2.md",
            path="/tmp/doc2.md",
        )

        # Create a relation
        rel = await store.create_relation(
            temp_db,
            src_id=doc1["id"],
            dst_id=doc2["id"],
            relation="references",
            weight=0.5,
        )

        # Update it
        updated = await store.update_relation(
            temp_db,
            rel["id"],
            relation="depends_on",
            weight=0.9,
        )

        assert updated["relation"] == "depends_on"
        assert updated["weight"] == 0.9

    @pytest.mark.asyncio
    async def test_delete_relation(self, temp_db):
        """Test deleting a relation."""
        doc1 = await store.create_document(
            temp_db,
            name="doc1.md",
            path="/tmp/doc1.md",
        )
        doc2 = await store.create_document(
            temp_db,
            name="doc2.md",
            path="/tmp/doc2.md",
        )

        # Create a relation
        rel = await store.create_relation(
            temp_db,
            src_id=doc1["id"],
            dst_id=doc2["id"],
            relation="related",
        )

        # Verify it exists
        relations_before = await store.list_relations(temp_db)
        assert len(relations_before) == 1

        # Delete it
        deleted = await store.delete_relation(temp_db, rel["id"])
        assert deleted is True

        # Verify it's gone
        relations_after = await store.list_relations(temp_db)
        assert len(relations_after) == 0


class TestGetGraphWithoutAuto:
    """Test get_graph with include_auto=True but embeddings unconfigured."""

    @pytest.mark.asyncio
    async def test_get_graph_no_auto_edges_unconfigured(self, temp_db):
        """Test that get_graph(include_auto=True) returns no auto edges when embeddings unconfigured."""
        # Verify embeddings are unconfigured
        assert not embeddings.is_configured()

        # Create two documents and a manual relation
        doc1 = await store.create_document(
            temp_db,
            name="doc1.md",
            path="/tmp/doc1.md",
        )
        doc2 = await store.create_document(
            temp_db,
            name="doc2.md",
            path="/tmp/doc2.md",
        )

        rel = await store.create_relation(
            temp_db,
            src_id=doc1["id"],
            dst_id=doc2["id"],
            relation="related",
            weight=1.0,
        )

        # Get graph with include_auto=True
        graph = await store.get_graph(temp_db, include_auto=True)

        # Should not crash
        assert "nodes" in graph
        assert "edges" in graph

        # Should have 2 nodes
        assert len(graph["nodes"]) == 2

        # Should have 1 edge (manual)
        assert len(graph["edges"]) == 1

        # Manual edge should have source field
        assert graph["edges"][0]["source"] == "manual"
        assert graph["edges"][0]["relation"] == "related"

        # No auto edges should be present
        assert all(edge["source"] != "auto" for edge in graph["edges"])


class TestBinaryFileHandling:
    """Binary/unknown files must be archived, not read as text (regression)."""

    @pytest.mark.asyncio
    async def test_binary_file_not_read_as_text(self, temp_db):
        """A binary 'file' kind (e.g. .pptx) must NOT be read as text.

        Regression: extract_document_text used to read every non-pdf/image file
        as utf-8 (errors=ignore), turning a multi-MB binary into mojibake that
        exploded into tens of thousands of junk chunks (and as many embedding
        calls when vectors are configured).
        """
        import os
        from cyrene.knowledge.ingest import index_document
        with TemporaryDirectory() as tmpdir:
            bf = Path(tmpdir) / "deck.pptx"
            # PK zip header + NUL bytes => detected as binary (pptx/docx/zip are zips)
            bf.write_bytes(b"PK\x03\x04" + b"\x00" * 64 + os.urandom(80000))
            doc = await store.create_document(temp_db, name="deck.pptx", path=str(bf), kind="file")
            await index_document(temp_db, doc["id"])
            d = await store.get_document(temp_db, doc["id"])
            assert d["status"] == "indexed"
            assert d["chunk_count"] == 0  # archived only — no junk chunks
            chunks = await store.get_chunks(temp_db, doc["id"])
            assert len(chunks) == 0
