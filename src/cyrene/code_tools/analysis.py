"""Code analysis tools — linting, formatting, and code review."""

import asyncio
import ast
import json
import tempfile
from pathlib import Path

# ── Ruff helpers ──

async def _run_ruff_check(path: str) -> list[dict]:
    """Run ruff check on a path and return structured results."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ruff", "check", "--output-format=json", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0 and not stdout.strip():
            return []
        try:
            return json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return [{"error": "ruff parse error", "detail": stdout.decode("utf-8", errors="replace")[:500]}]
    except FileNotFoundError:
        return [{"error": "ruff not installed"}]
    except Exception as e:
        return [{"error": str(e)}]


async def _run_ruff_format(path: str, check_only: bool = False) -> dict:
    """Run ruff format on a path."""
    args = ["ruff", "format"]
    if check_only:
        args.append("--check")
        args.append("--diff")
    args.append(path)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        stdout_s = stdout.decode("utf-8", errors="replace")
        stderr_s = stderr.decode("utf-8", errors="replace")
        return {
            "changed": proc.returncode != 0 if check_only else False,
            "diff": stdout_s if check_only else "",
            "output": stdout_s if not check_only else "",
            "error": stderr_s if proc.returncode != 0 and not check_only else "",
        }
    except FileNotFoundError:
        return {"error": "ruff not installed"}
    except Exception as e:
        return {"error": str(e)}


# ── AST analysis ──

def analyze_structure(path: str) -> dict:
    """Analyze Python file structure: functions, classes, complexity, imports."""
    from cyrene.tools import _resolve_workspace_path
    resolved = _resolve_workspace_path(path)
    try:
        source = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"error": f"Cannot read file: {path}"}

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"error": f"Syntax error: {e}"}

    functions = []
    classes = []
    imports = []
    total_lines = len(source.splitlines())

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_lines = node.end_lineno - node.lineno + 1 if node.end_lineno else 0
            functions.append({
                "name": node.name,
                "line": node.lineno,
                "lines": func_lines,
                "args": len(node.args.args),
                "is_async": isinstance(node, ast.AsyncFunctionDef),
            })
        elif isinstance(node, ast.ClassDef):
            cls_lines = node.end_lineno - node.lineno + 1 if node.end_lineno else 0
            methods = [
                n.name for n in ast.iter_child_nodes(node)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "lines": cls_lines,
                "methods": len(methods),
            })
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    # Find long functions (> 50 lines)
    long_funcs = [f for f in functions if f["lines"] > 50]

    return {
        "file": path,
        "total_lines": total_lines,
        "functions": functions,
        "classes": classes,
        "imports": sorted(set(imports)),
        "function_count": len(functions),
        "class_count": len(classes),
        "long_functions": long_funcs,
    }


# ── Tool handlers ──

async def _tool_lint_code(args: dict, bot=None, chat_id=None, db_path=None, notify_state=None) -> str:
    from cyrene.tools import _resolve_workspace_path
    path = str(args.get("path", "."))
    try:
        resolved = _resolve_workspace_path(path)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    results = await _run_ruff_check(str(resolved))
    return json.dumps({"status": "ok", "file": str(resolved), "issues": results}, ensure_ascii=False)


async def _tool_format_code(args: dict, bot=None, chat_id=None, db_path=None, notify_state=None) -> str:
    from cyrene.tools import _resolve_workspace_path
    path = str(args.get("path", "."))
    check_only = bool(args.get("check_only", False))
    try:
        resolved = _resolve_workspace_path(path)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    result = await _run_ruff_format(str(resolved), check_only=check_only)
    return json.dumps({"status": "ok", "file": str(resolved), **result}, ensure_ascii=False)


async def _tool_code_review(args: dict, bot=None, chat_id=None, db_path=None, notify_state=None) -> str:
    from cyrene.tools import _resolve_workspace_path
    path = str(args.get("path", "."))
    try:
        resolved = _resolve_workspace_path(path)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    # Run lint, format check, and structure analysis in parallel
    lint_task = _run_ruff_check(str(resolved))
    format_task = _run_ruff_format(str(resolved), check_only=True)

    lint_results, format_results = await asyncio.gather(lint_task, format_task)

    # Structure analysis (sync, but fast)
    structure = analyze_structure(str(resolved))

    suggestions = []
    if lint_results:
        suggestions.append(f"Found {len(lint_results)} lint issue(s)")
    if format_results.get("changed"):
        suggestions.append("Code needs formatting (ruff format)")
    if structure.get("long_functions"):
        names = [f["name"] for f in structure["long_functions"]]
        suggestions.append(f"Long functions (>50 lines): {', '.join(names)}")

    return json.dumps({
        "status": "ok",
        "file": str(resolved),
        "lint_issues": lint_results,
        "format_diff": format_results.get("diff", ""),
        "needs_formatting": format_results.get("changed", False),
        "structure": structure,
        "suggestions": suggestions,
    }, ensure_ascii=False)


# ── Tool definitions ──

LINT_CODE_DEF = {
    "type": "function",
    "function": {
        "name": "LintCode",
        "description": "Run the Ruff linter on a file or directory. Returns structured lint results with file, line number, error code, and message.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File or directory path to lint (relative to workspace).",
                },
            },
            "required": ["path"],
        },
    },
}

FORMAT_CODE_DEF = {
    "type": "function",
    "function": {
        "name": "FormatCode",
        "description": "Run the Ruff formatter on a file or directory. Use check_only=True to see what would change without actually modifying files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File or directory path to format.",
                },
                "check_only": {
                    "type": "boolean",
                    "description": "If true, only check what would be formatted without making changes (default: false).",
                },
            },
            "required": ["path"],
        },
    },
}

CODE_REVIEW_DEF = {
    "type": "function",
    "function": {
        "name": "CodeReview",
        "description": "Perform a comprehensive code review: runs linter, format check, and structural analysis (functions, classes, complexity, imports). Returns a report with issues and suggestions.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to review.",
                },
            },
            "required": ["path"],
        },
    },
}


def register_to(tool_defs: list, tool_handlers: dict) -> None:
    tool_defs.append(LINT_CODE_DEF)
    tool_handlers["LintCode"] = _tool_lint_code
    tool_defs.append(FORMAT_CODE_DEF)
    tool_handlers["FormatCode"] = _tool_format_code
    tool_defs.append(CODE_REVIEW_DEF)
    tool_handlers["CodeReview"] = _tool_code_review
