"""Diff computation API."""
import difflib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class DiffBody(BaseModel):
    mode: str = "text"  # "text" or "file"
    left: str = ""
    right: str = ""


def _compute_unified_diff(left_text: str, right_text: str, left_label: str = "a", right_label: str = "b") -> str:
    lines_a = left_text.splitlines(keepends=True)
    lines_b = right_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        lines_a, lines_b,
        fromfile=left_label,
        tofile=right_label,
    )
    return "".join(diff)


@router.post("/diff")
async def compute_diff(body: DiffBody):
    """Compute a unified diff between two texts or two files."""
    left_text = body.left
    right_text = body.right

    if body.mode == "file":
        from cyrene.tools import _resolve_workspace_path
        try:
            left_path = _resolve_workspace_path(body.left)
            right_path = _resolve_workspace_path(body.right)
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))

        if not left_path.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {body.left}")
        if not right_path.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {body.right}")

        try:
            left_text = left_path.read_text(encoding="utf-8")
            right_text = right_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Files must be UTF-8 text")

    diff = _compute_unified_diff(left_text, right_text, left_label=body.left, right_label=body.right)
    return {"diff": diff, "has_changes": bool(diff.strip())}
