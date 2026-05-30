"""Git integration tools — status, diff, log, commit, branch."""

import asyncio
import json

from cyrene.config import WORKSPACE_DIR


async def _run_git(args: list[str], timeout: float = 30.0) -> dict:
    """Run a git command and return {stdout, stderr, returncode}."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(WORKSPACE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "returncode": proc.returncode or 0,
        }
    except asyncio.TimeoutError:
        return {"error": "git command timed out"}
    except FileNotFoundError:
        return {"error": "git not available"}
    except Exception as e:
        return {"error": str(e)}


def _parse_status(porcelain: str) -> list[dict]:
    """Parse git status --porcelain output.

    Returns entries with the full XY status code, e.g. "M " (staged modify),
    " M" (unstaged modify), "??" (untracked), "MM" (both staged and unstaged).
    """
    results = []
    for line in porcelain.strip().split("\n"):
        if not line.strip():
            continue
        if len(line) < 4:
            continue
        xy = line[:2]
        path = line[3:].strip()
        # Handle rename: "R  old -> new"
        if " -> " in path:
            parts = path.split(" -> ")
            path = parts[-1]
        staged = xy[0] != " "
        results.append({"path": path, "status": xy, "staged": staged})
    return results


def _parse_log(oneline: str) -> list[dict]:
    """Parse git log --oneline output."""
    results = []
    for line in oneline.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        results.append({
            "hash": parts[0],
            "message": parts[1] if len(parts) > 1 else "",
        })
    return results


# ── Tool handlers ──

async def _tool_git_status(args: dict, bot=None, chat_id=None, db_path=None, notify_state=None) -> str:
    result = await _run_git(["status", "--porcelain"])
    if result.get("error"):
        return json.dumps({"error": result["error"]}, ensure_ascii=False)
    files = _parse_status(result["stdout"])
    return json.dumps({
        "status": "ok",
        "files": files,
        "changed_count": len(files),
        "is_clean": len(files) == 0,
    }, ensure_ascii=False)


async def _tool_git_diff(args: dict, bot=None, chat_id=None, db_path=None, notify_state=None) -> str:
    cmd = ["diff"]
    if args.get("staged"):
        cmd.append("--staged")
    path = args.get("path", "")
    if path:
        cmd.append("--")
        cmd.append(path)
    result = await _run_git(cmd, timeout=60.0)
    if result.get("error"):
        return json.dumps({"error": result["error"]}, ensure_ascii=False)
    diff_text = result["stdout"]
    return json.dumps({
        "status": "ok",
        "diff": diff_text,
        "has_changes": bool(diff_text.strip()),
    }, ensure_ascii=False)


async def _tool_git_log(args: dict, bot=None, chat_id=None, db_path=None, notify_state=None) -> str:
    count = int(args.get("count", 10))
    result = await _run_git(["log", "--oneline", f"-n{count}"])
    if result.get("error"):
        return json.dumps({"error": result["error"]}, ensure_ascii=False)
    commits = _parse_log(result["stdout"])
    return json.dumps({
        "status": "ok",
        "commits": commits,
        "count": len(commits),
    }, ensure_ascii=False)


async def _tool_git_commit(args: dict, bot=None, chat_id=None, db_path=None, notify_state=None) -> str:
    """Stage and commit changes. Requires user confirmation via scope elevation."""
    message = str(args.get("message", ""))
    files = args.get("files", [])

    if not message:
        return json.dumps({"error": "Commit message is required"}, ensure_ascii=False)

    from cyrene.tools import _request_scope_elevation

    # Ask user for confirmation
    files_hint = ", ".join(files) if files else "all changes"
    elevation_result = await _request_scope_elevation(
        tool_name="GitCommit",
        path_hint=files_hint,
        operation=f"commit: {message}",
        reason=f"Commit {files_hint} with message: {message}",
        permission_kind="git_commit",
        options=["allow_once"],
    )
    status = json.loads(elevation_result)
    if str(status.get("status", "")).strip() == "awaiting_user":
        return elevation_result

    # Stage files first (if specific files given, add only those)
    if files:
        add_result = await _run_git(["add", "--"] + list(files))
    else:
        add_result = await _run_git(["add", "-A"])
    if add_result.get("error"):
        return json.dumps({"error": "Failed to stage files: " + add_result["error"]}, ensure_ascii=False)

    cmd = ["commit", "-m", message]
    result = await _run_git(cmd)
    if result.get("error"):
        return json.dumps({"error": result["error"]}, ensure_ascii=False)
    return json.dumps({
        "status": "ok",
        "output": result["stdout"] or result["stderr"],
    }, ensure_ascii=False)


async def _tool_git_branch(args: dict, bot=None, chat_id=None, db_path=None, notify_state=None) -> str:
    new_branch = args.get("create", "")
    if new_branch:
        result = await _run_git(["branch", new_branch])
        if result.get("error"):
            return json.dumps({"error": result["error"]}, ensure_ascii=False)
        return json.dumps({
            "status": "ok",
            "created": new_branch,
            "output": result["stdout"] or result["stderr"],
        }, ensure_ascii=False)

    # List branches
    result = await _run_git(["branch"])
    if result.get("error"):
        return json.dumps({"error": result["error"]}, ensure_ascii=False)
    branches = []
    for line in result["stdout"].strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        current = line.startswith("*")
        name = line.lstrip("* ").strip()
        branches.append({"name": name, "current": current})
    return json.dumps({
        "status": "ok",
        "branches": branches,
    }, ensure_ascii=False)


# ── Tool definitions ──

GIT_STATUS_DEF = {
    "type": "function",
    "function": {
        "name": "GitStatus",
        "description": "Show the working tree status. Returns a list of changed files with their status (M=modified, A=added, D=deleted, ??=untracked) and whether each is staged.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

GIT_DIFF_DEF = {
    "type": "function",
    "function": {
        "name": "GitDiff",
        "description": "Show changes in the working tree. Use staged=True to see staged changes. Use path to limit to a specific file.",
        "parameters": {
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "description": "Show staged changes instead of working tree changes."},
                "path": {"type": "string", "description": "Limit diff to a specific file path."},
            },
            "required": [],
        },
    },
}

GIT_LOG_DEF = {
    "type": "function",
    "function": {
        "name": "GitLog",
        "description": "Show recent commit history. Returns commit hashes and messages.",
        "parameters": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of commits to show (default: 10)."},
            },
            "required": [],
        },
    },
}

GIT_COMMIT_DEF = {
    "type": "function",
    "function": {
        "name": "GitCommit",
        "description": "Stage and commit changes. Requires user confirmation before committing. Use this to save work with a descriptive message.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message."},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of specific files to commit (stages all changes if omitted).",
                },
            },
            "required": ["message"],
        },
    },
}

GIT_BRANCH_DEF = {
    "type": "function",
    "function": {
        "name": "GitBranch",
        "description": "List local branches or create a new one. Pass create='name' to create a new branch.",
        "parameters": {
            "type": "object",
            "properties": {
                "create": {"type": "string", "description": "Name of a new branch to create. Omit to list existing branches."},
            },
            "required": [],
        },
    },
}


def register_to(tool_defs: list, tool_handlers: dict) -> None:
    tool_defs.append(GIT_STATUS_DEF)
    tool_handlers["GitStatus"] = _tool_git_status
    tool_defs.append(GIT_DIFF_DEF)
    tool_handlers["GitDiff"] = _tool_git_diff
    tool_defs.append(GIT_LOG_DEF)
    tool_handlers["GitLog"] = _tool_git_log
    tool_defs.append(GIT_COMMIT_DEF)
    tool_handlers["GitCommit"] = _tool_git_commit
    tool_defs.append(GIT_BRANCH_DEF)
    tool_handlers["GitBranch"] = _tool_git_branch
