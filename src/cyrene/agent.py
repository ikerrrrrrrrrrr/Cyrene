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
from cyrene.search import deep_search
from cyrene.short_term import touch_entry, get_context, clear_old_entries, load_entries, save_entries

logger = logging.getLogger(__name__)
_agent_lock = asyncio.Lock()
_MAX_HISTORY_MESSAGES = 40
_MAX_TOOL_ROUNDS = 12
_MAX_TOOL_OUTPUT_CHARS = 12000

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_MAIN_AGENT_PROMPT = """You are Cyrene, a capable AI assistant. Your job is to help Alice get things done.

Rules:
- Respond clearly and directly
- You have many tools available — use them when helpful
- You can write files, search the web, run code, etc.
- Be efficient and accurate
- When a task is complete, call the `quit` tool
"""

_CHAT_FILTER_PROMPT = """You are a friend-style translator. Your ONLY job is to rewrite assistant text as casual friend chat.

Rules:
- Keep the essential information; the user needs to know what happened
- Remove all formatting, markdown, lists, bullet points
- No emoji unless the original had clear emotional tone
- Never add information that wasn't in the original
- Never say "how can I help" or "how may I assist"

Rewrite the following:
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
    """Clear session and compress conversation to short-term memory before discarding."""
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

    # 短查询走原来的 DuckDuckGo 搜索
    url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
    html = response.text
    matches = re.findall(r'<a[^>]*class="result__a"[^>]*href="(.*?)"[^>]*>(.*?)</a>', html, re.S)
    results: list[str] = []
    for href, title in matches[:10]:
        clean_title = re.sub(r"<.*?>", "", title).strip()
        results.append(f"- {clean_title}\n  {href}")
    return "\n".join(results) if results else "No results."


async def _tool_quit(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    return "Interaction ended."


# ---------------------------------------------------------------------------
# Tool definitions and dispatch
# ---------------------------------------------------------------------------

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send a message to the user on Telegram or the local console.",
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
]


TOOL_HANDLERS: dict[str, Any] = {
    "send_message": _tool_send_message,
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
    return await handler(arguments, bot, chat_id, db_path, notify_state)


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
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "max_tokens": 8192,  # 推理模型需要足够 token 完成思考+输出
    }
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
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]


# ---------------------------------------------------------------------------
# Main agent (assistant tone + full tools + session persistence)
# ---------------------------------------------------------------------------


async def _run_main_agent(user_message: str, history: list, bot: Any, chat_id: int, db_path: str, personality_block: str = "") -> str:
    """主 Agent：助理语气 + 全部工具 + session 持久化。"""
    system_content = _MAIN_AGENT_PROMPT
    if personality_block:
        system_content += "\n\n[人格设定]\n" + personality_block
    messages = [{"role": "system", "content": system_content}, *history, {"role": "user", "content": user_message}]

    final_text = ""
    for _ in range(_MAX_TOOL_ROUNDS):
        response = await _call_llm(messages, tools=TOOL_DEFS)

        assistant_entry: dict = {"role": "assistant", "content": response.get("content") or ""}
        if response.get("tool_calls"):
            assistant_entry["tool_calls"] = response["tool_calls"]
        messages.append(assistant_entry)

        tool_calls = response.get("tool_calls") or []

        # quit tool
        if any(tc.get("function", {}).get("name") == "quit" for tc in tool_calls):
            final_text = _assistant_text(response).strip() or "Done."
            for tc in tool_calls:
                if tc.get("function", {}).get("name") == "quit":
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "Interaction ended."})
            break

        if not tool_calls:
            final_text = _assistant_text(response).strip() or ""
            if final_text:
                break
            continue  # 空内容重试

        for tc in tool_calls:
            call_id = tc["id"]
            fn = tc["function"]
            name = fn["name"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
                result = await _execute_tool(name, args, bot, chat_id, db_path, None)
            except Exception as e:
                result = f"Tool {name} failed: {e}"
            messages.append({"role": "tool", "tool_call_id": call_id, "content": _truncate(result)})
    else:
        final_text = "Stopped after hitting the tool loop limit."

    # 保存 session（只保存 user/assistant/tool 消息，不包括 system）
    session_msgs = [m for m in messages[1:] if m["role"] != "system"]
    await _save_session_messages(session_msgs)

    return final_text


async def _run_chat_filter(text: str) -> str:
    """翻译助理腔 → 朋友语气。轻量 LLM 调用，无工具。"""
    if not text or len(text) < 10:
        return text
    try:
        response = await _call_llm([
            {"role": "system", "content": _CHAT_FILTER_PROMPT},
            {"role": "user", "content": text}
        ], tools=None)
        result = _assistant_text(response) or text
        # Strip emojis as a safety net (models don't always follow instructions)
        result = re.sub(r'[\U0001F300-\U0010FFFF]', '', result).strip()
        return result
    except Exception:
        return text  # 失败时 fallback 到原文


# ---------------------------------------------------------------------------
# Execution agent (internal, all tools)
# ---------------------------------------------------------------------------


async def _run_execution_agent(task: str, bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> str:
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

    # 注入 SOUL.md 人格到系统 prompt（始终生效）
    from cyrene.soul import read_shallow_memory
    soul = read_shallow_memory()
    personality_block = ""
    if soul:
        personality_block = soul[:2000]

    # ====== Step 1: 主 Agent（助理语气 + 全部工具）=======
    main_text = await _run_main_agent(user_message, history, bot, chat_id, db_path, personality_block)

    # ====== Step 2: Chat Filter 翻译成朋友语气 =======
    if main_text and main_text != "Done.":
        friend_text = await _run_chat_filter(main_text)
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
