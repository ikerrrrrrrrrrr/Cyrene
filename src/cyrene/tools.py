"""
Tool definitions and handlers for the Cyrene agent.

All tool handler functions, tool definitions (TOOL_DEFS), tool handler registry
(TOOL_HANDLERS), tool execution dispatch (_execute_tool), and helper functions
(_resolve_workspace_path, _json_result).

NOTE: _tool_quit and the "quit" entry in TOOL_HANDLERS are kept in agent.py
to avoid circular imports. agent.py adds "quit" to TOOL_HANDLERS after import.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from croniter import croniter

from cyrene import db
from cyrene.config import (
    DATA_DIR,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    STATE_FILE,
    WORKSPACE_DIR,
)
from cyrene.llm import _truncate
from cyrene.search import deep_search
from cyrene.shells import close_shell as _close_shell_session
from cyrene.shells import list_shells as _list_shell_sessions
from cyrene.shells import send_shell as _send_shell_session
from cyrene.shells import start_shell as _start_shell_session
from cyrene.subagent import register as _reg_subagent, can_receive, _run_subagent, _spawn_subagent_task
from cyrene.inbox import send_message as _send_inbox

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_workspace_path(path_str: str) -> Path:
    candidate = Path(path_str)
    path = candidate if candidate.is_absolute() else WORKSPACE_DIR / candidate
    resolved = path.resolve()
    workspace = WORKSPACE_DIR.resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError(f"Path escapes workspace: {path_str}")
    return resolved


def _json_result(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _tool_send_message(args: dict[str, Any], bot: Any, chat_id: int, _db_path: str, notify_state: dict[str, bool] | None) -> str:
    text = str(args.get("text", ""))
    if bot is not None:
        await bot.send_message(chat_id=chat_id, text=text)
    if notify_state is not None:
        notify_state["sent"] = True
    return "Message sent."


async def _tool_schedule_task(args: dict[str, Any], _bot: Any, chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    stype = str(args["schedule_type"])
    svalue = str(args["schedule_value"])
    now = datetime.now(timezone.utc)

    if stype == "cron":
        next_run = croniter(svalue, now).get_next(datetime).isoformat()
    elif stype == "interval":
        next_run = (now + timedelta(milliseconds=int(svalue))).isoformat()
    elif stype == "once":
        next_run = svalue
    else:
        raise ValueError(f"Unknown schedule_type: {stype}")

    task_id = await db.create_task(db_path, chat_id, str(args["prompt"]), stype, svalue, next_run)
    return f"Task {task_id} scheduled. Next run: {next_run}"


async def _tool_list_tasks(_args: dict[str, Any], _bot: Any, _chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    tasks = await db.get_all_tasks(db_path)
    if not tasks:
        return "No scheduled tasks."
    lines = [f"- [{t['id']}] {t['status']} | {t['schedule_type']}({t['schedule_value']}) | {t['prompt'][:60]}" for t in tasks]
    return "\n".join(lines)


async def _tool_pause_task(args: dict[str, Any], _bot: Any, _chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    task_id = str(args["task_id"])
    ok = await db.update_task_status(db_path, task_id, "paused")
    return f"Task {task_id} paused." if ok else f"Task {task_id} not found."


async def _tool_resume_task(args: dict[str, Any], _bot: Any, _chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    task_id = str(args["task_id"])
    ok = await db.update_task_status(db_path, task_id, "active")
    return f"Task {task_id} resumed." if ok else f"Task {task_id} not found."


async def _tool_cancel_task(args: dict[str, Any], _bot: Any, _chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    task_id = str(args["task_id"])
    ok = await db.delete_task(db_path, task_id)
    return f"Task {task_id} cancelled." if ok else f"Task {task_id} not found."


async def _tool_read(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    path = _resolve_workspace_path(str(args["path"]))
    return _truncate(path.read_text(encoding="utf-8"))


async def _tool_write(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    path = _resolve_workspace_path(str(args["path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(args.get("content", "")), encoding="utf-8")
    return f"Wrote {path}"


async def _tool_edit(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    path = _resolve_workspace_path(str(args["path"]))
    old_string = str(args["old_string"])
    new_string = str(args["new_string"])
    replace_all = bool(args.get("replace_all", False))

    content = path.read_text(encoding="utf-8")
    occurrences = content.count(old_string)
    if occurrences == 0:
        raise ValueError("old_string not found")
    if occurrences > 1 and not replace_all:
        raise ValueError("old_string matched multiple times; set replace_all=true")

    updated = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    path.write_text(updated, encoding="utf-8")
    replaced = occurrences if replace_all else 1
    return f"Edited {path}. Replacements: {replaced}"


async def _tool_glob(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    pattern = str(args["pattern"])
    matches = sorted(str(path.relative_to(WORKSPACE_DIR)) for path in WORKSPACE_DIR.glob(pattern))
    return "\n".join(matches[:200]) if matches else "No matches."


async def _tool_grep(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    pattern = re.compile(str(args["pattern"]))
    search_root = _resolve_workspace_path(str(args.get("path", ".")))
    glob_pattern = str(args.get("glob", "**/*"))
    lines: list[str] = []

    for path in search_root.glob(glob_pattern):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for index, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line):
                rel = path.relative_to(WORKSPACE_DIR)
                lines.append(f"{rel}:{index}:{line}")
                if len(lines) >= 200:
                    return "\n".join(lines)
    return "\n".join(lines) if lines else "No matches."


async def _tool_bash(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    command = str(args["command"])
    timeout_ms = int(args.get("timeout_ms", 120000))
    timeout_sec = timeout_ms / 1000
    shell = os.environ.get("SHELL") or "/bin/sh"
    proc = await asyncio.create_subprocess_exec(
        shell,
        "-lc",
        command,
        cwd=str(WORKSPACE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    from cyrene.agent import _interrupt_event

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def _read(stream: asyncio.StreamReader | None, chunks: list[bytes]) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            chunks.append(chunk)

    reads = asyncio.gather(_read(proc.stdout, stdout_chunks), _read(proc.stderr, stderr_chunks))
    import time as _time
    deadline = _time.monotonic() + timeout_sec

    try:
        while True:
            if reads.done():
                break
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                proc.kill()
                reads.cancel()
                try:
                    await reads
                except (asyncio.CancelledError, Exception):
                    pass
                raise ValueError(f"Command timed out after {timeout_ms} ms")
            if _interrupt_event.is_set():
                proc.kill()
                reads.cancel()
                try:
                    await reads
                except (asyncio.CancelledError, Exception):
                    pass
                payload = {
                    "exit_code": -1,
                    "stdout": _truncate(b"".join(stdout_chunks).decode("utf-8", errors="replace")),
                    "stderr": "Command interrupted by new user message.",
                }
                return _json_result(payload)
            try:
                await asyncio.wait_for(asyncio.shield(reads), timeout=min(1, remaining))
            except asyncio.TimeoutError:
                pass

        await proc.wait()
    except ValueError:
        raise
    except Exception:
        proc.kill()
        raise

    payload = {
        "exit_code": proc.returncode,
        "stdout": _truncate(b"".join(stdout_chunks).decode("utf-8", errors="replace")),
        "stderr": _truncate(b"".join(stderr_chunks).decode("utf-8", errors="replace")),
    }
    return _json_result(payload)


async def _tool_webfetch(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    url = str(args["url"])
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
    return _truncate(response.text)


async def _tool_websearch(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    query = str(args.get("query", ""))
    if not query:
        return "No query provided."

    # 超过 15 个字符的复杂查询走深度搜索，简单的直接搜索
    if len(query) > 15:
        result = await deep_search(query)
        return result

    # 短查询走 DuckDuckGo 搜索，失败时 fallback 到 Bing
    url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
        html = response.text
        matches = re.findall(r'<a[^>]*class="result__a"[^>]*href="(.*?)"[^>]*>(.*?)</a>', html, re.S)
        if matches:
            results = []
            for href, title in matches[:10]:
                clean_title = re.sub(r"<.*?>", "", title).strip()
                results.append(f"- {clean_title}\n  {href}")
            return "\n".join(results)
    except Exception:
        pass

    # DuckDuckGo 失败，尝试 Bing
    try:
        bing_url = f"https://www.bing.com/search?q={quote(query)}&setmkt=en-US"
        bing_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Edg/131.0.0.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            bresp = await client.get(bing_url, headers=bing_headers)
            bresp.raise_for_status()
        bhtml = bresp.text
        blocks = re.findall(r'<li\s+class="b_algo"[^>]*>([\s\S]*?)</li>', bhtml, re.DOTALL)
        bresults = []
        for block in blocks[:10]:
            hm = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', block, re.DOTALL)
            if hm:
                bt = re.sub(r'<[^>]+>', '', hm.group(2)).strip()
                bu = hm.group(1)
                if bt and not bu.startswith('/'):
                    bresults.append(f"- {bt}\n  {bu}")
        if bresults:
            return "\n".join(bresults)
    except Exception:
        pass

    return "No results."


async def _tool_send_agent_message(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Send a message to another sub-agent via inbox."""
    target = str(args.get("to", ""))
    content = str(args.get("content", ""))
    if not target or not content:
        return "Error: both 'to' and 'content' are required."
    from cyrene.agent import _current_agent_id, _current_round_id
    current_round_id = _current_round_id.get()
    if not await can_receive(target, round_id=current_round_id):
        if target.lower() in {"main", "main_agent", "cyrene", "danny", "host", "coordinator", "parent"}:
            return "Main agent does not receive inbox messages. Put your final conclusion in your next quit response; the parent agent will collect it automatically."
        if current_round_id:
            return f"Cannot deliver: agent '{target}' is not available in the current round ({current_round_id})."
        return f"Cannot deliver: agent '{target}' is not available (finished or timed out)."
    from_agent = _current_agent_id.get()
    await _send_inbox(from_agent, target, "chat", content, round_id=current_round_id)
    return f"Message sent to {target}."


async def _tool_spawn_subagent(args: dict[str, Any], bot: Any, chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Spawn a sub-agent to handle a specific task."""
    agent_id = str(args.get("agent_id", ""))
    task = str(args.get("task", ""))
    if not agent_id or not task:
        return "Error: agent_id and task are required."
    from cyrene.agent import _current_round_id
    await _reg_subagent(agent_id, task, round_id=_current_round_id.get())
    _spawn_subagent_task(_run_subagent(agent_id, task, bot, chat_id, db_path), agent_id)
    return f"Sub-agent '{agent_id}' spawned. Task: {task[:80]}"


async def _tool_query_round(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Query live round status for the main agent."""
    from cyrene.agent import query_live_rounds

    return query_live_rounds(round_id=str(args.get("round_id", "")).strip())


async def _tool_start_shell(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.agent import _current_round_id

    snap = await _start_shell_session(
        command=str(args.get("command", "") or ""),
        cwd=str(args.get("cwd", ".") or "."),
        title=str(args.get("title", "") or ""),
        round_id=_current_round_id.get(),
    )
    return _json_result({
        "shell_id": snap.get("id", ""),
        "status": snap.get("status", ""),
        "cwd": snap.get("cwd", "."),
        "title": snap.get("title", "independent shell"),
    })


async def _tool_send_shell(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    snap = await _send_shell_session(
        str(args.get("shell_id", "")),
        str(args.get("command", "")),
        wait_ms=int(args.get("wait_ms", 700) or 700),
    )
    return _json_result({
        "shell_id": snap.get("id", ""),
        "status": snap.get("status", ""),
        "elapsed": snap.get("elapsed", "—"),
        "lines": snap.get("lines", [])[-20:],
    })


async def _tool_list_shells(_args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    shells = _list_shell_sessions(include_exited=False)
    if not shells:
        return "No independent shells are currently running."
    return _json_result([
        {
            "shell_id": item.get("id", ""),
            "title": item.get("title", "independent shell"),
            "cwd": item.get("cwd", "."),
            "status": item.get("status", ""),
            "elapsed": item.get("elapsed", "—"),
        }
        for item in shells
    ])


async def _tool_close_shell(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    snap = await _close_shell_session(str(args.get("shell_id", "")))
    return _json_result({
        "shell_id": snap.get("id", ""),
        "status": snap.get("status", ""),
        "elapsed": snap.get("elapsed", "—"),
    })


# ---------------------------------------------------------------------------
# Tool definitions and dispatch
# ---------------------------------------------------------------------------

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "send_telegram",
            "description": "Send a Telegram message to the user. NOT for agent-to-agent communication — use send_agent_message instead.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": "Schedule a task. schedule_type must be cron, interval, or once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "schedule_type": {"type": "string"},
                    "schedule_value": {"type": "string"},
                },
                "required": ["prompt", "schedule_type", "schedule_value"],
            },
        },
    },
    {
        "type": "function",
        "function": {"name": "list_tasks", "description": "List all scheduled tasks.", "parameters": {"type": "object", "properties": {}}},
    },
    {
        "type": "function",
        "function": {
            "name": "pause_task",
            "description": "Pause a scheduled task.",
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_task",
            "description": "Resume a paused scheduled task.",
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_task",
            "description": "Cancel and delete a scheduled task.",
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read a UTF-8 text file from the workspace.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Write",
            "description": "Write a UTF-8 text file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Edit",
            "description": "Replace an exact string in a text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Glob",
            "description": "Find files in the workspace using a glob pattern.",
            "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": "Search file contents by regex pattern inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Run a shell command in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "timeout_ms": {"type": "integer"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "StartShell",
            "description": "Start an independent persistent shell session for long-running work. Use this when you need a shell that stays alive and should keep appearing in the UI shell list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string"},
                    "title": {"type": "string"},
                    "command": {"type": "string", "description": "Optional initial command to run immediately after the shell starts"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "SendShell",
            "description": "Send a command to an existing persistent shell session and wait briefly for new output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "shell_id": {"type": "string"},
                    "command": {"type": "string"},
                    "wait_ms": {"type": "integer"},
                },
                "required": ["shell_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ListShells",
            "description": "List currently running independent persistent shell sessions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "CloseShell",
            "description": "Terminate an independent persistent shell session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "shell_id": {"type": "string"},
                },
                "required": ["shell_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "WebFetch",
            "description": "Fetch a URL and return the response text.",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "WebSearch",
            "description": "Search the web and return the top result links.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "quit",
            "description": "Call this when the task is complete and the interaction should end.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_agent_message",
            "description": "Send a message to another sub-agent via inbox. Use this to communicate with other sub-agents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Target agent ID"},
                    "content": {"type": "string", "description": "Message content"},
                },
                "required": ["to", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": "Spawn a sub-agent. A sub-agent has independent full tool access and can communicate with other agents via send_agent_message. The parent agent automatically collects each sub-agent's final result from its quit text, so do not invent a separate coordinator agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Unique ID for the sub-agent"},
                    "task": {"type": "string", "description": "The task for the sub-agent to complete"},
                },
                "required": ["agent_id", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_round",
            "description": "Inspect currently live rounds and their progress. Use this when the user asks how a background round is going or wants the status of a still-running discussion.",
            "parameters": {
                "type": "object",
                "properties": {
                    "round_id": {"type": "string", "description": "Optional specific live round id to inspect"},
                },
            },
        },
    },
]


# TOOL_HANDLERS without "quit" — agent.py adds it after import to avoid circular import.
TOOL_HANDLERS: dict[str, Any] = {
    "send_telegram": _tool_send_message,
    "send_agent_message": _tool_send_agent_message,
    "spawn_subagent": _tool_spawn_subagent,
    "query_round": _tool_query_round,
    "schedule_task": _tool_schedule_task,
    "list_tasks": _tool_list_tasks,
    "pause_task": _tool_pause_task,
    "resume_task": _tool_resume_task,
    "cancel_task": _tool_cancel_task,
    "Read": _tool_read,
    "Write": _tool_write,
    "Edit": _tool_edit,
    "Glob": _tool_glob,
    "Grep": _tool_grep,
    "Bash": _tool_bash,
    "StartShell": _tool_start_shell,
    "SendShell": _tool_send_shell,
    "ListShells": _tool_list_shells,
    "CloseShell": _tool_close_shell,
    "WebFetch": _tool_webfetch,
    "WebSearch": _tool_websearch,
}


async def _execute_tool(name: str, arguments: dict[str, Any], bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None) -> str:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")
    _t0 = __import__("time").monotonic()
    result = await handler(arguments, bot, chat_id, db_path, notify_state)
    from cyrene import debug
    if debug.VERBOSE:
        from cyrene.agent import _caller_type
        debug.log_tool_call(_caller_type.get(), name, arguments, result, (__import__("time").monotonic() - _t0) * 1000)
    from cyrene.agent import _caller_type, _current_round_id
    await debug.publish_event({
        "type": "tool_call", "caller": _caller_type.get(), "tool": name, "args": arguments,
        "result_preview": str(result)[:200],
        "round_id": _current_round_id.get(),
    })
    return result
