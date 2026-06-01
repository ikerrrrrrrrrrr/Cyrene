"""Document ingestion pipeline for the knowledge base.

Handles text extraction, chunking, embedding, and indexing.
"""

import re
import aiosqlite
from pathlib import Path

from cyrene.attachments import (
    is_pdf_path,
    is_image_path,
    _vision_analysis,
)
from cyrene.call_llm import _approx_token_count
from cyrene.knowledge import store, embeddings


async def extract_document_text(path: Path, kind: str) -> str:
    """Extract full text from a document based on its kind.

    - pdf: Use pypdf to extract full text from all pages
    - image: Use vision analysis to describe image
    - code/text: Read file with UTF-8 (ignoring errors)
    - other: Return empty string

    Strategy: attempt extraction for all kinds; use empty string as fallback.
    """
    if not isinstance(path, Path):
        path = Path(path)

    if not path.exists():
        return ""

    try:
        if kind == "pdf" or is_pdf_path(path):
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            pages = []
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    pages.append("")
            return "\n\n".join(part.strip() for part in pages if part and part.strip())

        if kind == "image" or is_image_path(path):
            try:
                result = await _vision_analysis(path, prompt="")
                return result.get("vision_text", "").strip()
            except Exception:
                return ""

        # code, text, and default: read as text
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def chunk_text(
    text: str,
    target_chars: int = 800,
    overlap: int = 120,
) -> list[tuple[str, int, int]]:
    """Chunk text into overlapping segments preferring paragraph/sentence boundaries.

    Returns list of (text, char_start, char_end) tuples. Offsets refer to the
    normalized text. Short text (< target_chars) yields a single chunk.
    """
    if not text or not text.strip():
        return []

    # Normalize spaces/tabs and collapse 3+ blank lines, but PRESERVE paragraph breaks
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    n = len(text)
    if n == 0:
        return []

    chunks: list[tuple[str, int, int]] = []
    char_pos = 0
    step = max(1, target_chars - overlap)

    while char_pos < n:
        chunk_end = min(char_pos + target_chars, n)

        # Only look for a nicer boundary when this is NOT the final window
        if chunk_end < n:
            search_start = max(char_pos + 1, chunk_end - 200)
            para_break = text.rfind("\n\n", search_start, chunk_end)
            if para_break != -1 and para_break > char_pos:
                chunk_end = para_break + 2
            else:
                last = None
                for m in re.finditer(r"[.!?。！？]\s|[.!?。！？]$", text[search_start:chunk_end]):
                    last = m
                if last is not None:
                    cand = search_start + last.end()
                    if cand > char_pos:
                        chunk_end = cand

        piece = text[char_pos:chunk_end].strip()
        if piece:
            chunks.append((piece, char_pos, chunk_end))

        if chunk_end >= n:
            break

        next_pos = chunk_end - overlap
        if next_pos <= char_pos:
            next_pos = char_pos + step
        char_pos = next_pos

    return chunks


async def index_document(db_path: str, doc_id: str) -> None:
    """Index a document: extract text, chunk, embed, and update database.

    Sets status to parsing -> indexed or error.
    Gracefully handles missing embeddings configuration (uses FTS only).
    """
    try:
        # Get document
        doc = await store.get_document(db_path, doc_id)
        if not doc:
            return

        # Set status to parsing
        await store.update_document(db_path, doc_id, status="parsing")

        # Extract text
        path = Path(doc["path"])
        text = await extract_document_text(path, doc["kind"])

        # Chunk text
        chunks_raw = chunk_text(text)

        # Prepare chunks for storage
        chunks_to_store = []
        for ordinal, (chunk_text_str, char_start, char_end) in enumerate(chunks_raw):
            chunk_dict = {
                "id": None,  # Will be generated
                "ordinal": ordinal,
                "content": chunk_text_str,
                "char_start": char_start,
                "char_end": char_end,
                "token_count": _approx_token_count(chunk_text_str),
                "embedding": None,
                "embedding_dim": 0,
                "embedding_model": "",
            }
            chunks_to_store.append(chunk_dict)

        # Embed if configured
        if embeddings.is_configured() and chunks_to_store:
            try:
                texts_to_embed = [c["content"] for c in chunks_to_store]
                embedded_vectors = await embeddings.embed_texts(texts_to_embed)

                for chunk_dict, vector in zip(chunks_to_store, embedded_vectors):
                    chunk_dict["embedding"] = embeddings.pack_vector(vector)
                    chunk_dict["embedding_dim"] = len(vector)
                    chunk_dict["embedding_model"] = embeddings._model()
            except Exception:
                # Gracefully degrade: proceed without embeddings
                pass

        # Replace chunks
        await store.replace_chunks(db_path, doc_id, chunks_to_store)

        # Update document metadata
        summary = text[: min(300, len(text))] if text else ""
        await store.update_document(
            db_path,
            doc_id,
            status="indexed",
            char_count=len(text),
            chunk_count=len(chunks_to_store),
            summary=summary,
            indexed_at=store._now(),
        )

    except Exception as e:
        # Set status to error
        await store.update_document(
            db_path,
            doc_id,
            status="error",
            error=str(e),
        )


async def reindex_document(db_path: str, doc_id: str) -> None:
    """Reindex a document by calling index_document."""
    await index_document(db_path, doc_id)


async def process_pending(db_path: str, *, limit: int | None = None) -> None:
    """Process pending documents sequentially.

    Indexes up to `limit` pending documents. Failures are marked as error without retry.
    Sequential processing avoids overwhelming vision/embedding APIs.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM kb_documents WHERE status = ? ORDER BY created_at ASC LIMIT ?",
            ("pending", limit or 999999),
        )
        rows = await cursor.fetchall()

    for row in rows:
        doc_id = row["id"]
        await index_document(db_path, doc_id)
