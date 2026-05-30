"""File read/write API for the code editor."""
from pathlib import Path

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

router = APIRouter()

MIME_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".json": "json",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".sql": "sql",
    ".sh": "shell",
    ".bash": "shell",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".txt": "text",
}


def _language_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return MIME_MAP.get(suffix, "text")


class FileWriteBody(BaseModel):
    path: str
    content: str


@router.get("/file")
async def read_file(path: str = Query(...)):
    """Read a file from the workspace."""
    from cyrene.tools import _resolve_workspace_path
    try:
        resolved = _resolve_workspace_path(path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {path}")
    try:
        content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not UTF-8 text")
    return {
        "content": content,
        "language": _language_for_path(str(resolved)),
        "size": resolved.stat().st_size,
        "path": str(resolved),
    }


@router.put("/file")
async def write_file(body: FileWriteBody):
    """Write a file to the workspace."""
    from cyrene.tools import _resolve_workspace_write_target
    try:
        resolved = _resolve_workspace_write_target(body.path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(body.content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok", "path": str(resolved), "size": resolved.stat().st_size}
