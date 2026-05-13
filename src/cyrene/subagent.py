"""
Subagent registry, lifecycle management, and sub-agent execution loop.

每个子 agent 在注册表中有一条记录：
  agent_id -> {"task": str, "status": "running" | "waiting" | "resumed" | "done" | "timeout", "result": str}

状态机：
  RUNNING → WAITING → (收到新消息 → RESUMED → RUNNING) | (全部 done → DONE) | (超时 → TIMEOUT)

注册表用于：
1. 发送 inbox 消息前检查对方是否还活着
2. 注入到每个 agent 的 context 中，让大家知道谁在干什么

_run_subagent 原本在 agent.py 中，移到此处避免 tools.py 与 agent.py 之间的循环依赖。
"""

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 状态常量
RUNNING = "running"          # 正在干活
WAITING = "waiting"          # 活干完了，等别人消息
RESUMED = "resumed"          # 等待期间收到新消息，继续干活
DONE = "done"                # 真正完成
TIMEOUT = "timeout"          # 超时退出

# 全局注册表
_registry: dict[str, dict] = {}
_lock = asyncio.Lock()


async def register(agent_id: str, task: str) -> None:
    """注册一个子 agent。"""
    async with _lock:
        _registry[agent_id] = {"task": task, "status": RUNNING, "result": ""}


async def mark_done(agent_id: str, result: str = "") -> None:
    """标记 agent 已完成。"""
    async with _lock:
        if agent_id in _registry:
            _registry[agent_id]["status"] = DONE
            _registry[agent_id]["result"] = result[:1000]


async def set_waiting(agent_id: str) -> None:
    """标记 agent 活干完了，等待其他人。"""
    async with _lock:
        if agent_id in _registry and _registry[agent_id]["status"] == RUNNING:
            _registry[agent_id]["status"] = WAITING


async def set_resumed(agent_id: str) -> None:
    """标记 agent 在等待期间收到新消息，恢复工作。"""
    async with _lock:
        if agent_id in _registry and _registry[agent_id]["status"] == WAITING:
            _registry[agent_id]["status"] = RESUMED


async def can_receive(agent_id: str) -> bool:
    """检查 agent 是否能接收消息。running/waiting/resumed 都可以。"""
    async with _lock:
        entry = _registry.get(agent_id)
        if entry is None:
            return False
        return entry["status"] in (RUNNING, WAITING, RESUMED)


async def all_quiescent() -> bool:
    """所有 agent 都进入了 waiting/done（没有 running 的）。"""
    async with _lock:
        return not any(info["status"] == RUNNING for info in _registry.values())


async def all_done() -> bool:
    """所有 agent 都真正完成了（没有 running/waiting/resumed 的）。"""
    async with _lock:
        return not any(info["status"] in (RUNNING, WAITING, RESUMED) for info in _registry.values())


async def wait_for_others(agent_id: str, inbox_check_func, max_wait: int = 600) -> str:
    """Subagent 干完活后调用：标记 waiting，等其他人。

    每 5 秒检查一次：
    - 所有 agent 都 done → 返回 ""（正常退出）
    - inbox 有新消息 → 返回消息内容（回去继续干活）
    - 超时 → 返回 "timeout"
    """
    await set_waiting(agent_id)
    waited = 0
    while waited < max_wait:
        if await all_done():
            return ""
        await asyncio.sleep(5)
        waited += 5
        new_msgs = inbox_check_func(agent_id)
        if new_msgs:
            return new_msgs
    return "timeout"


async def get_status(agent_id: str) -> str | None:
    """获取 agent 状态：running / waiting / resumed / done / timeout / None。"""
    async with _lock:
        entry = _registry.get(agent_id)
        if entry is None:
            return None
        return entry["status"]


async def get_context(exclude: str = "") -> str:
    """格式化注册表为文本，注入 agent context。"""
    async with _lock:
        if not _registry:
            return ""
        lines = ["[活跃子 agent]"]
        for aid, info in _registry.items():
            marker = "-> " if aid == exclude else "  "
            st = {"running": "工作中", "waiting": "活干完了等大家", "resumed": "恢复工作", "done": "已完成", "timeout": "超时"}.get(info["status"], info["status"])
            lines.append(f"  {marker}{aid}: {info['task'][:50]} [{st}]")
        return "\n".join(lines)


async def clear() -> None:
    """清除注册表（新 session 时调用）。"""
    async with _lock:
        _registry.clear()


async def collect_results() -> str:
    """收集所有 subagent 的结果，格式化为文本。"""
    async with _lock:
        lines = []
        for aid, info in _registry.items():
            result = info.get("result", "")
            if result:
                lines.append(f"[{aid}]: {result[:1000]}")
            else:
                lines.append(f"[{aid}]: 无结果")
        return "\n\n".join(lines) if lines else "无 subagent 结果。"


# ---------------------------------------------------------------------------
# Sub-agent execution loop (moved from agent.py)
# ---------------------------------------------------------------------------


async def _run_subagent(agent_id: str, task: str, bot: Any, chat_id: int, db_path: str) -> str:
    """Run a sub-agent in its own loop.

    Has its own agent loop, inbox checking, and full tool access.
    Communicates with other agents via inbox.

    Uses lazy imports from agent.py to avoid circular dependencies.
    """
    from cyrene.agent import _call_llm, _caller_type, _current_agent_id, _MAX_TOOL_ROUNDS
    from cyrene.llm import _assistant_text, _truncate
    from cyrene.tools import TOOL_DEFS, _execute_tool

    _caller_type.set(f"subagent_{agent_id}")
    from cyrene.inbox import get_inbox_context as _get_inbox

    subagent_prompt = f"""You are a sub-agent, ID: {agent_id}. Your job is to complete the assigned task.

You can:
- Use tools (files, search, bash, etc.)
- Communicate with other agents via the send_agent_message tool
- Check who else is active via the context at the top

Rules:
- Search max 3 times. If still no useful results, use your existing knowledge and tag the output with `[fallback to model knowledge]`.
- When you call quit, include your findings or analysis in the text. Do not quit empty-handed.
"""

    messages = [
        {"role": "system", "content": subagent_prompt},
        {"role": "user", "content": task},
    ]

    final_text = ""
    try:
        for _ in range(_MAX_TOOL_ROUNDS):
            # 每次 LLM 调用前注入注册表和 inbox
            registry_ctx = await get_context(exclude=agent_id)
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
                inbox_msg = await wait_for_others(agent_id, _inbox_ctx)
                if inbox_msg == "":
                    break  # 全部 finished，正常退出
                elif inbox_msg == "timeout":
                    break  # 超时，强制退出
                else:
                    # 有新消息，标记 RESUMED，继续干活
                    await set_resumed(agent_id)
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

    await mark_done(agent_id, final_text)
    return final_text
