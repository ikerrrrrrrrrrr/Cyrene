"""Diff computation API."""
import asyncio
import difflib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from cyrene.config import WORKSPACE_DIR

router = APIRouter()


class DiffBody(BaseModel):
    mode: str = "text"  # "text" or "file"
    left: str = ""
    right: str = ""


class GitDiffBody(BaseModel):
    path: str = ""
    staged: bool = False


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


@router.post("/git-diff")
async def compute_git_diff(body: GitDiffBody):
    """Compute git diff for the current workspace or a specific path."""
    cmd = ["git", "diff"]
    if body.staged:
        cmd.append("--staged")
    if body.path:
        from cyrene.tools import _resolve_workspace_path
        try:
            resolved = _resolve_workspace_path(body.path)
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))
        try:
            rel = str(resolved.resolve().relative_to(WORKSPACE_DIR.resolve()))
        except ValueError:
            rel = body.path
        cmd.extend(["--", rel])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(WORKSPACE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="git diff timed out")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="git not available")
    if proc.returncode not in (0, 1):
        raise HTTPException(status_code=400, detail=stderr.decode("utf-8", errors="replace") or "git diff failed")
    diff = stdout.decode("utf-8", errors="replace")
    if body.path and not body.staged and not diff.strip():
        # `git diff -- path` does not include untracked files. For the chat
        # change summary, synthesize a normal unified diff for new text files.
        resolved = resolved if "resolved" in locals() else (WORKSPACE_DIR / body.path).resolve()
        try:
            rel = str(resolved.relative_to(WORKSPACE_DIR.resolve()))
        except ValueError:
            rel = body.path
        if resolved.is_file():
            tracked_proc = await asyncio.create_subprocess_exec(
                "git",
                "ls-files",
                "--error-unmatch",
                "--",
                rel,
                cwd=str(WORKSPACE_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await tracked_proc.communicate()
            if tracked_proc.returncode != 0:
                try:
                    right_text = resolved.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    right_text = ""
                if right_text:
                    diff = _compute_unified_diff("", right_text, left_label="/dev/null", right_label=f"b/{rel}")
    return {"diff": diff, "has_changes": bool(diff.strip()), "path": body.path, "staged": body.staged}
