import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
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
from contextvars import ContextVar

from cyrene.search import deep_search
from cyrene.short_term import touch_entry, get_context, clear_old_entries, load_entries, save_entries
from cyrene import debug
from cyrene.subagent import (
    register as _reg_subagent,
    mark_done as _mark_subagent_done,
    wait_for_others as _subagent_wait_for_others,
    get_context as _get_subagent_context,
    is_alive,
    clear as _clear_subagents,
)

logger = logging.getLogger(__name__)

# 当前 agent ID，用于 send_agent_message 识别发送者
_current_agent_id: ContextVar[str] = ContextVar("_current_agent_id", default="main")
# 当前调用者类型，用于 debug 日志
_caller_type: ContextVar[str] = ContextVar("_caller_type", default="main_agent")
_agent_lock = asyncio.Lock()
_MAX_HISTORY_MESSAGES = 40
_MAX_TOOL_ROUNDS = 12
_MAX_TOOL_OUTPUT_CHARS = 12000

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_MAIN_AGENT_PROMPT = """You are a capable AI assistant. Get things done efficiently.

Rules:
- Respond clearly and directly
- You have many tools available — use them when helpful
- You can write files, search the web, run code, etc.
- Be efficient and accurate
- When a task is complete, call the `quit` tool
"""

_CHAT_FILTER_PROMPT = """You are a character voice translator. Your ONLY job is to rewrite assistant text using a character's voice.

Below you may receive a personality profile (SOUL.md) describing how to speak. Use it to match the character's: verbal tics, catchphrases, sentence patterns, tone, and vocabulary.

If no profile is given, use a casual friendly tone.

Rules:
- Keep ALL essential information; nothing can be lost
- Remove any formatting, markdown, lists, bullet points from the original
- Use the character's specific speech patterns
- Never add information that wasn't in the original
"""

_EXECUTION_SYSTEM_PROMPT = """You are a capable execution agent. Your job is to complete tasks using tools.

Rules:
- Use tools to complete the task efficiently
- Read/Write/Edit files, run Bash commands, search the web as needed
- Return the RESULT of what you did, not a conversation
- Be concise in tool usage
- When done, call the `quit` tool
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = _MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _resolve_workspace_path(path_str: str) -> Path:
    candidate = Path(path_str)
    path = candidate if candidate.is_absolute() else WORKSPACE_DIR / candidate
    resolved = path.resolve()
    workspace = WORKSPACE_DIR.resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError(f"Path escapes workspace: {path_str}")
    return resolved


def _load_session_messages() -> list[dict[str, Any]]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read state file")
        return []
    messages = data.get("messages", [])
    return messages if isinstance(messages, list) else []


async def _save_session_messages(messages: list[dict[str, Any]]) -> None:
    """保存 session 消息。如果超过上限，触发后台压缩。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    trimmed = messages[-_MAX_HISTORY_MESSAGES:]
    STATE_FILE.write_text(json.dumps({"messages": trimmed}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 如果原始消息超过阈值，后台压缩
    if len(messages) > _MAX_HISTORY_MESSAGES + 5:
        asyncio.create_task(_compress_old_messages(messages))


async def _compress_old_messages(all_messages: list[dict]) -> None:
    """
    压缩最早的一部分消息到短期记忆。
    在后台运行，不阻塞对话。
    """
    # 取前 20 条用户+助理消息
    to_compress = [m for m in all_messages[:20] if m["role"] in ("user", "assistant")]
    if not to_compress:
        return

    # 格式化成文本
    lines = []
    for m in to_compress:
        role = "User" if m["role"] == "user" else "Cyrene"
        content = m.get("content", "")[:200]
        lines.append(f"{role}: {content}")
    text = "\n".join(lines)

    # LLM 调用压缩
    prompt = f"""Extract key information from this conversation. Focus on:
1. Facts about the user (job, preferences, habits)
2. Emotional patterns or recurring topics
3. Action items or decisions made

For each finding, classify as: fact | pattern | preference | emotion

Conversation:
{text}

Output format (one per line, no explanations):
[fact] user works at a tech company
[emotion] user was frustrated about a project deadline
[preference] user likes casual short replies
"""

    try:
        response = await _call_llm([
            {"role": "system", "content": "You extract structured memories from conversations. Be concise."},
            {"role": "user", "content": prompt}
        ], tools=None)
        compressed = _assistant_text(response) or ""
    except Exception:
        logger.warning("Memory compression failed", exc_info=True)
        return

    # 解析并写入短期记忆
    for line in compressed.split("\n"):
        line = line.strip()
        if not line or not line.startswith("["):
            continue
        try:
            closing = line.index("]")
            entry_type = line[1:closing]
            content = line[closing + 1:].strip()
            if content and len(content) > 3:
                touch_entry(content, {
                    "content": content,
                    "type": entry_type,
                    "emotional_valence": -2 if "frustrat" in content.lower() or "stress" in content.lower() or "angry" in content.lower()
                    else 2 if "happy" in content.lower() or "love" in content.lower() or "excit" in content.lower()
                    else 0,
                })
        except (ValueError, IndexError):
            continue


async def clear_session_id() -> None:
    """Clear session, subagent registry, and compress conversation to short-term memory before discarding."""
    await _clear_subagents()
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
            if msgs:
                await _compress_old_messages(msgs)
        except Exception:
            pass
        STATE_FILE.unlink()
    # 不清短期记忆。它用于在 session 重置后注入上下文。


def _json_result(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _tool_send_message(args: dict[str, Any], bot: Any, chat_id: int, notify_state: dict[str, bool] | None) -> str:
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
    proc = await asyncio.create_subprocess_exec(
        "powershell",
        "-NoProfile",
        "-Command",
        command,
        cwd=str(WORKSPACE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms / 1000)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise ValueError(f"Command timed out after {timeout_ms} ms")

    payload = {
        "exit_code": proc.returncode,
        "stdout": _truncate(stdout.decode("utf-8", errors="replace")),
        "stderr": _truncate(stderr.decode("utf-8", errors="replace")),
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


async def _tool_quit(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    return "Interaction ended."


async def _tool_send_agent_message(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Send a message to another sub-agent via inbox."""
    target = str(args.get("to", ""))
    content = str(args.get("content", ""))
    if not target or not content:
        return "Error: both 'to' and 'content' are required."
    if not await is_alive(target):
        return f"Cannot deliver: agent '{target}' is no longer alive."
    from_agent = _current_agent_id.get()
    from cyrene.inbox import send_message as _send_inbox
    _send_inbox(from_agent, target, "chat", content)
    return f"Message sent to {target}."


async def _tool_spawn_subagent(args: dict[str, Any], bot: Any, chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Spawn a sub-agent to handle a specific task."""
    agent_id = str(args.get("agent_id", ""))
    task = str(args.get("task", ""))
    if not agent_id or not task:
        return "Error: agent_id and task are required."
    await _reg_subagent(agent_id, task)
    asyncio.create_task(_run_subagent(agent_id, task, bot, chat_id, db_path))
    return f"Sub-agent '{agent_id}' spawned. Task: {task[:80]}"


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
            "description": "Run a PowerShell command in the workspace.",
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
            "description": "Spawn a sub-agent that has the same abilities as you (search, code, files, etc.). It runs independently in its own loop. Use this INSTEAD of writing a Python script to simulate, because sub-agents can actually search the web, run real commands, read real files, and communicate with each other via send_agent_message. Good for: parallel research, multi-perspective analysis, debate, complex multi-step tasks that need real tool access.",
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
]


TOOL_HANDLERS: dict[str, Any] = {
    "send_telegram": _tool_send_message,
    "send_agent_message": _tool_send_agent_message,
    "spawn_subagent": _tool_spawn_subagent,
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
    "WebFetch": _tool_webfetch,
    "WebSearch": _tool_websearch,
    "quit": _tool_quit,
}


async def _execute_tool(name: str, arguments: dict[str, Any], bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None) -> str:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")
    _t0 = __import__("time").monotonic()
    result = await handler(arguments, bot, chat_id, db_path, notify_state)
    if debug.VERBOSE:
        debug.log_tool_call(_caller_type.get(), name, arguments, result, (__import__("time").monotonic() - _t0) * 1000)
    return result


def _assistant_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            return content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        text = "".join(parts)
        if text.strip():
            return text
    # Fallback: use reasoning_content if content is empty (Qwen-style models)
    reasoning = message.get("reasoning_content")
    if reasoning and isinstance(reasoning, str):
        return reasoning.strip()
    return ""


# ---------------------------------------------------------------------------
# LLM call (accepts tools as parameter)
# ---------------------------------------------------------------------------


async def _call_llm(messages: list[dict], tools: list | None = None) -> dict:
    _t0 = __import__("time").monotonic()
    _phase = "phase1" if tools is _LIGHT_TOOL_DEFS else ("phase2" if tools else "no_tools")
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "max_tokens": 32000,
    }
    if "deepseek" in OPENAI_MODEL:
        payload["thinking"] = {"type": "enabled"}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers = {"Content-Type": "application/json"}
    if OPENAI_API_KEY and OPENAI_API_KEY.lower() not in ("lmstudio", "dummy", ""):
        headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"

    transport = httpx.AsyncHTTPTransport(retries=1)
    async with httpx.AsyncClient(transport=transport, timeout=120.0) as client:
        resp = await client.post(
            f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        if resp.status_code != 200:
            logger.error("LLM API error %s: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        if debug.VERBOSE:
            debug.log_llm_call(_caller_type.get(), _phase, messages, tools, msg, (__import__("time").monotonic() - _t0) * 1000)
        return msg


# ---------------------------------------------------------------------------
# Main agent (assistant tone + full tools + session persistence)
# ---------------------------------------------------------------------------


# 轻量 tool：只有 use_tools + quit，用于第一阶段判断是否进重循环
_LIGHT_TOOL_DEFS = [
    {"type": "function", "function": {"name": "use_tools", "description": "Call this when the user asks you to DO something (file ops, search, code, web, spawn_subagent, etc.). Not needed for chat only. IMPORTANT: set task to the user's EXACT original message, do not rewrite it.", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}},
    {"type": "function", "function": {"name": "quit", "description": "Call this when the interaction is done.", "parameters": {"type": "object", "properties": {}}}},
]


async def _run_main_agent(user_message: str, history: list, bot: Any, chat_id: int, db_path: str) -> str:
    """主 Agent：先轻量判断是否需工具，再决定是否进重循环。"""
    _caller_type.set("main_agent")
    messages = [{"role": "system", "content": _MAIN_AGENT_PROMPT}, *history, {"role": "user", "content": user_message}]

    # Phase 1: 轻量调用，无完整工具列表，只有 use_tools + quit
    response = await _call_llm(messages, tools=_LIGHT_TOOL_DEFS)
    tool_calls = response.get("tool_calls") or []

    # 如果 LLM 调了 use_tools → 进入重循环（含全部工具）
    use_tools_call = None
    for tc in tool_calls:
        name = tc.get("function", {}).get("name")
        if name == "use_tools":
            use_tools_call = tc
        elif name == "quit":
            return _assistant_text(response).strip() or "Done."

    if use_tools_call:
        # Phase 2: 重循环 — 全部工具。使用原始用户消息，不用 LLM 编的 task
        messages = [{"role": "system", "content": _MAIN_AGENT_PROMPT}, *history, {"role": "user", "content": user_message}]

        for _ in range(_MAX_TOOL_ROUNDS):
            response = await _call_llm(messages, tools=TOOL_DEFS)
            entry: dict = {"role": "assistant", "content": response.get("content") or ""}
            if response.get("reasoning_content"):
                entry["reasoning_content"] = response["reasoning_content"]
            if response.get("tool_calls"):
                entry["tool_calls"] = response["tool_calls"]
            messages.append(entry)

            tcs = response.get("tool_calls") or []
            if any(t.get("function", {}).get("name") == "quit" for t in tcs):
                return _assistant_text(response).strip() or "Done."
            if not tcs:
                return _assistant_text(response).strip() or "Done."

            for t in tcs:
                try:
                    args = json.loads(t["function"].get("arguments") or "{}")
                    result = await _execute_tool(t["function"]["name"], args, bot, chat_id, db_path, None)
                except Exception as e:
                    result = f"Tool failed: {e}"
                messages.append({"role": "tool", "tool_call_id": t["id"], "content": _truncate(result)})
        return "Stopped after hitting the tool loop limit."

    # Phase 1 结束：纯聊天，无工具需要
    return _assistant_text(response).strip() or "Done."

    # 保存 session（只保存 user/assistant/tool 消息，不包括 system）
    session_msgs = [m for m in messages[1:] if m["role"] != "system"]
    await _save_session_messages(session_msgs)

    return final_text


async def _run_chat_filter(text: str, soul_context: str = "") -> str:
    """根据 SOUL.md 人格设定，将助理腔翻译成角色语气。轻量 LLM 调用，无工具。"""
    if not text or len(text) < 10:
        return text

    _caller_type.set("chat_filter")
    import time as _time
    _t0 = _time.monotonic()
    system_prompt = _CHAT_FILTER_PROMPT
    if soul_context:
        system_prompt = f"{_CHAT_FILTER_PROMPT}\n\n参考以下人格设定，用该角色的语气和说话方式改写：\n{soul_context}"

    try:
        response = await _call_llm([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ], tools=None)
        result = _assistant_text(response) or text
        result = re.sub(r'[\U0001F300-\U0010FFFF]', '', result).strip()
        debug.log_chat_filter(text, result, (_time.monotonic() - _t0) * 1000)
        return result
    except Exception:
        return text  # 失败时 fallback 到原文


# ---------------------------------------------------------------------------
# Execution agent (internal, all tools)
# ---------------------------------------------------------------------------


async def _run_execution_agent(task: str, bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> str:
    _caller_type.set("execution_agent")
    """Execution agent with all tools. Used internally by chat agent."""
    messages = [
        {"role": "system", "content": _EXECUTION_SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    for _ in range(_MAX_TOOL_ROUNDS):
        response = await _call_llm(messages, tools=TOOL_DEFS)

        assistant_entry: dict[str, Any] = {"role": "assistant"}
        if response.get("content"):
            assistant_entry["content"] = response["content"]
        else:
            assistant_entry["content"] = ""
        if response.get("tool_calls"):
            assistant_entry["tool_calls"] = response["tool_calls"]
        if response.get("reasoning_content"):
            assistant_entry["reasoning_content"] = response["reasoning_content"]
        messages.append(assistant_entry)

        tool_calls = response.get("tool_calls") or []

        # Check for quit
        if any(tc.get("function", {}).get("name") == "quit" for tc in tool_calls):
            break

        if not tool_calls:
            return _assistant_text(response) or "Done."

        for tc in tool_calls:
            call_id = tc["id"]
            fn = tc["function"]
            name = fn["name"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
                result = await _execute_tool(name, args, bot, chat_id, db_path, notify_state)
            except Exception as e:
                result = f"Tool {name} failed: {e}"
            messages.append({"role": "tool", "tool_call_id": call_id, "content": _truncate(result)})

    return "Done."


# ---------------------------------------------------------------------------
# Sub-agent
# ---------------------------------------------------------------------------


async def _run_subagent(agent_id: str, task: str, bot: Any, chat_id: int, db_path: str) -> str:
    _caller_type.set(f"subagent_{agent_id}")
    """Run a sub-agent in its own loop.

    Has its own agent loop, inbox checking, and full tool access.
    Communicates with other agents via inbox.
    """
    from cyrene.inbox import get_inbox_context as _get_inbox

    subagent_prompt = f"""You are a sub-agent, ID: {agent_id}. Your job is to complete the assigned task.

You can:
- Use tools (files, search, bash, etc.)
- Communicate with other agents via the send_agent_message tool
- Check who else is active via the context at the top

When you finish the task or need help, call quit.
"""

    messages = [
        {"role": "system", "content": subagent_prompt},
        {"role": "user", "content": task},
    ]

    final_text = ""
    try:
        for _ in range(_MAX_TOOL_ROUNDS):
            # 每次 LLM 调用前注入注册表和 inbox
            registry_ctx = await _get_subagent_context(exclude=agent_id)
            inbox_text = _get_inbox(agent_id)
            inbox_ctx = ""
            if inbox_text:
                inbox_ctx = f"\n[收件箱]\n{inbox_text}\n"

            system_content = subagent_prompt
            extras = []
            if registry_ctx:
                extras.append(registry_ctx)
            if inbox_ctx:
                extras.append(inbox_ctx)
            if extras:
                system_content = subagent_prompt + "\n\n" + "\n".join(extras)
            messages[0] = {"role": "system", "content": system_content}

            response = await _call_llm(messages, tools=TOOL_DEFS)

            entry: dict = {"role": "assistant", "content": response.get("content") or ""}
            if response.get("reasoning_content"):
                entry["reasoning_content"] = response["reasoning_content"]
            if response.get("tool_calls"):
                entry["tool_calls"] = response["tool_calls"]
            messages.append(entry)

            tcs = response.get("tool_calls") or []

            # 检测 quit 或纯文本（活干完了）
            should_exit = any(t.get("function", {}).get("name") == "quit" for t in tcs) or not tcs
            if should_exit:
                final_text = _assistant_text(response).strip() or "Done."
                # 标记 willing_to_quit，等别人（每 5 秒检查 inbox）
                from cyrene.inbox import get_inbox_context as _inbox_ctx
                inbox_msg = await _subagent_wait_for_others(agent_id, _inbox_ctx)
                if inbox_msg == "":
                    break  # 全部 finished，正常退出
                elif inbox_msg == "timeout":
                    break  # 超时，强制退出
                else:
                    # 有新消息，继续干活
                    messages.append({"role": "user", "content": f"[等待期间收到新消息]\n{inbox_msg}"})
                    continue

            for tc in tcs:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                    token = _current_agent_id.set(agent_id)
                    try:
                        result = await _execute_tool(name, args, bot, chat_id, db_path, None)
                    finally:
                        _current_agent_id.reset(token)
                except Exception as e:
                    result = f"Tool {name} failed: {e}"
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": _truncate(result)})
        else:
            final_text = "Sub-agent hit loop limit."
    except Exception as e:
        logger.exception("Sub-agent %s crashed", agent_id)
        final_text = f"Sub-agent crashed: {e}"

    await _mark_subagent_done(agent_id, final_text)
    return final_text


# ---------------------------------------------------------------------------
# Chat agent (entry point)
# ---------------------------------------------------------------------------


async def run_agent(user_message: str, bot: Any, chat_id: int, db_path: str) -> str:
    """Main entry point. Main agent (assistant tone + full tools) -> Chat filter (friend-style)."""
    async with _agent_lock:
        return await _run_chat_agent(user_message, bot, chat_id, db_path)


async def _run_chat_agent(user_message: str, bot: Any, chat_id: int, db_path: str) -> str:
    """Coordinator: main agent -> chat filter."""
    history = _load_session_messages()

    # 如果 history 为空（session 被重置），注入短期记忆
    if not history:
        st = get_context(max_chars=5000)
        if st:
            history = [{"role": "system", "content": "[Restored context]\n" + st}]

    # 读取 SOUL.md人格设定（仅给 Chat Filter 使用，不污染主 Agent）
    from cyrene.soul import read_shallow_memory
    soul_context = read_shallow_memory()[:3000] if read_shallow_memory() else ""

    # ====== Step 1: 主 Agent（助理语气 + 全部工具，不关心人格）=======
    main_text = await _run_main_agent(user_message, history, bot, chat_id, db_path)

    # ====== Step 2: Chat Filter 根据 SOUL.md 翻译成角色语气 =======
    if main_text and main_text != "Done.":
        friend_text = await _run_chat_filter(main_text, soul_context)
    else:
        friend_text = main_text or "Done."

    return friend_text


# ---------------------------------------------------------------------------
# Backward-compatible public API
# ---------------------------------------------------------------------------


async def run_task_agent(prompt: str, bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> str:
    """Alias for execution agent (no session). Used by scheduler."""
    return await _run_execution_agent(prompt, bot, chat_id, db_path, notify_state=notify_state)


async def run_heartbeat_agent(prompt: str, bot: Any, chat_id: int, db_path: str) -> str:
    """Alias for execution agent (no session). Used by heartbeat."""
    return await _run_execution_agent(prompt, bot, chat_id, db_path)


async def run_steward_agent(conversation_text: str, soulmd_content: str, bot: Any, chat_id: int, db_path: str) -> str:
    """Steward Agent call. Reads recent conversation + current SOUL.md, outputs modification instructions.
    Uses a different system prompt and no session persistence.
    """
    steward_prompt = f"""You are a memory steward. Your job is to update Cyrene's SOUL.md based on recent conversations.

Read the recent conversation and current SOUL.md, then output:
- APPEND: what new information to add
- ERASE: what old information to remove
- MERGE: what to consolidate
- Or SKIP if nothing important

SOUL.md:
{soulmd_content}

Recent conversation:
{conversation_text}

Output only the modifications needed, one per line, prefixed with APPEND/ERASE/MERGE/SKIP."""

    return await _run_execution_agent(steward_prompt, bot, chat_id, db_path)
