import asyncio
import json
import logging
import re
from typing import Any

import httpx

from contextvars import ContextVar

from cyrene.config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, DATA_DIR, STATE_FILE
from cyrene.short_term import get_context, touch_entry
from cyrene import debug
from cyrene.llm import _assistant_text, _truncate
from cyrene.tools import TOOL_DEFS, TOOL_HANDLERS, _execute_tool
from cyrene.subagent import (
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
# 后台 compressor 任务，防止被事件循环 GC
_pending_compressors: set[asyncio.Task] = set()

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_MAIN_AGENT_PROMPT = """You are a capable AI assistant. Get things done efficiently.

## Values
- **Ownership**: Take responsibility end-to-end. Do not stop at analysis — implement, verify, and confirm.
- **Honesty over deference**: If something is wrong or risky, say so directly. Do not fabricate results.
- **Clarity > Speed**: When a decision has non-obvious consequences, pause and explain. For routine tasks, just do it.

## Communication
- Respond clearly and directly. No conversational interjections ("Got it", "Sure", "Great question").
- No emoji. Never.
- While working, give brief progress updates (1-2 sentences). After completion, give a concise final answer.
- Final answer: prefer 1-2 short paragraphs. Use lists only when the content is inherently list-shaped. Keep it flat.

## Tools
- Use tools when helpful: files, search, web, code, sub-agents, etc.
- When a task is complete, call the `quit` tool.
"""

_CHAT_FILTER_PROMPT = """You are a character voice translator. Your ONLY job is to rewrite assistant text using a character's voice.

Below you may receive a personality profile (SOUL.md) describing how to speak. Use it to match the character's: verbal tics, catchphrases, sentence patterns, tone, and vocabulary.

If no profile is given, use a casual friendly tone.

Rules:
- Keep ALL essential information; nothing can be lost.
- Preserve code blocks, file paths, error messages, URLs, and numbered references as-is. Do not rewrite technical content.
- Only rewrite conversational text into the character's voice.
- Use the character's specific speech patterns from the personality profile.
- Never add information that wasn't in the original.
- Never use emoji unless the character's profile explicitly demonstrates that they use them.
"""

_EXECUTION_SYSTEM_PROMPT = """You are a capable execution agent. Your job is to complete tasks using tools.

Rules:
- Use tools to complete the task efficiently.
- Read/Write/Edit files, run Bash commands, search the web as needed.
- Return the RESULT of what you did, not a conversation.
- Be concise in tool usage.
- When done, call the `quit` tool.
- Do not fabricate results. If a tool fails or returns nothing useful, state that clearly.
"""

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


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
    if len(messages) >= _MAX_HISTORY_MESSAGES + 5:
        task = asyncio.create_task(_compress_old_messages(messages))
        _pending_compressors.add(task)
        task.add_done_callback(_pending_compressors.discard)


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


# ---------------------------------------------------------------------------
# Tool: quit (stays here to avoid circular imports — added to TOOL_HANDLERS below)
# ---------------------------------------------------------------------------


async def _tool_quit(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    return "Interaction ended."


# Add quit handler to the shared TOOL_HANDLERS dict (from tools.py)
TOOL_HANDLERS["quit"] = _tool_quit


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
                await _save_session_messages([m for m in messages[1:] if m["role"] != "system"])
                return _assistant_text(response).strip() or "Done."
            if not tcs:
                await _save_session_messages([m for m in messages[1:] if m["role"] != "system"])
                return _assistant_text(response).strip() or "Done."

            spawned = False
            for t in tcs:
                try:
                    args = json.loads(t["function"].get("arguments") or "{}")
                    result = await _execute_tool(t["function"]["name"], args, bot, chat_id, db_path, None)
                except Exception as e:
                    result = f"Tool failed: {e}"
                messages.append({"role": "tool", "tool_call_id": t["id"], "content": _truncate(result)})
                if t.get("function", {}).get("name") == "spawn_subagent":
                    spawned = True

            # 调用了 spawn_subagent → 进入监控模式，不调 LLM，等 subagent 全部安静
            if spawned:
                from cyrene.subagent import all_quiescent as _sub_all_quiet, all_done as _sub_all_done, collect_results as _sub_collect
                for _ in range(120):  # max 10 min
                    await asyncio.sleep(5)
                    if await _sub_all_quiet():
                        break
                # 等 quiescent 后，收集结果
                await asyncio.sleep(2)  # 给 subagent 一点时间写 registry
                summary = await _sub_collect()
                # 用 LLM 综合结果
                synthesis = await _call_llm([
                    {"role": "system", "content": "You are a research synthesizer. Combine the following expert findings into a clear, structured answer. Preserve all factual claims and cite sources when provided."},
                    {"role": "user", "content": f"Task: {user_message}\n\nExpert findings:\n{summary}"}
                ], tools=None)
                final_text = _assistant_text(synthesis) or summary
                await _save_session_messages([m for m in messages[1:] if m["role"] != "system"])
                return final_text

        await _save_session_messages([m for m in messages[1:] if m["role"] != "system"])
        return "Stopped after hitting the tool loop limit."

    # Phase 1 结束：纯聊天，无工具需要
    session_msgs = [m for m in messages[1:] if m["role"] != "system"]
    await _save_session_messages(session_msgs)
    return _assistant_text(response).strip() or "Done."


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
