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
import inspect
import json
import logging
from datetime import datetime, timezone
from typing import Any

from cyrene import debug

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


def _matches_round(entry: dict[str, Any], round_id: str = "") -> bool:
    """Return True when *entry* belongs to the requested round filter."""
    if not round_id:
        return True
    return str(entry.get("round_id", "")) == round_id


async def _publish_registry_event(agent_id: str) -> None:
    """Publish the latest subagent snapshot for live UI updates."""
    async with _lock:
        entry = dict(_registry.get(agent_id, {}))
    if not entry:
        return
    await debug.publish_event({
        "type": "subagent_update",
        "agent_id": agent_id,
        "task": entry.get("task", ""),
        "status": entry.get("status", ""),
        "result_preview": str(entry.get("result", "") or "")[:200],
        "message_count": len(entry.get("messages", [])),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "round_id": entry.get("round_id", ""),
    })


async def register(agent_id: str, task: str, round_id: str = "") -> None:
    """注册一个子 agent。"""
    from cyrene.inbox import clear_inbox

    await clear_inbox(agent_id)
    async with _lock:
        now = datetime.now(timezone.utc).isoformat()
        _registry[agent_id] = {
            "task": task,
            "status": RUNNING,
            "result": "",
            "messages": [],
            "created_at": now,
            "updated_at": now,
        }
        if round_id:
            _registry[agent_id]["round_id"] = round_id
    await _publish_registry_event(agent_id)


async def save_messages(agent_id: str, messages: list) -> None:
    """Save subagent conversation messages to the registry."""
    async with _lock:
        if agent_id in _registry:
            _registry[agent_id]["messages"] = messages
            _registry[agent_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    await _publish_registry_event(agent_id)


async def mark_done(agent_id: str, result: str = "") -> None:
    """标记 agent 已完成。

    Result 会累加而非覆盖 —— 这样被唤醒的 agent 跑完第二轮再次 mark_done 时，
    新的内容会被追加在已有结果之后，不会丢掉初次执行的结论。
    """
    async with _lock:
        if agent_id in _registry:
            _registry[agent_id]["status"] = DONE
            _registry[agent_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
            existing = _registry[agent_id].get("result", "") or ""
            if result and result != existing:
                if existing:
                    # 如果 existing 是 result 的前缀（说明是 set_waiting 截断的版本），
                    # 直接用完整 result，避免重复拼接。
                    if result.startswith(existing):
                        _registry[agent_id]["result"] = result[:2000]
                    else:
                        _registry[agent_id]["result"] = (existing + "\n---\n" + result)[:2000]
                else:
                    _registry[agent_id]["result"] = result[:2000]
    await _publish_registry_event(agent_id)


async def reactivate(agent_id: str) -> bool:
    """把 DONE/TIMEOUT 的 agent 状态改回 RESUMED，准备被重新启动。

    返回 True 表示成功改了状态；如果 agent 不存在或已经在跑，返回 False。
    """
    async with _lock:
        entry = _registry.get(agent_id)
        if entry is None:
            return False
        if entry["status"] in (DONE, TIMEOUT):
            entry["status"] = RESUMED
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            should_publish = True
        else:
            should_publish = False
    if should_publish:
        await _publish_registry_event(agent_id)
        return True
    return False


async def get_raw_messages(agent_id: str) -> list:
    """获取 agent 的完整消息历史（含 system prompt、tool_calls 原始参数）。

    与 get_snapshot 不同 —— snapshot 是给 WebUI 用的，会精简内容；
    这里返回的是可以直接喂给 LLM 续跑的原始 messages 列表。
    """
    async with _lock:
        entry = _registry.get(agent_id)
        if entry is None:
            return []
        return list(entry.get("messages", []))


async def get_task(agent_id: str) -> str:
    """获取 agent 的原始任务（被唤醒时用于恢复 context）。"""
    async with _lock:
        entry = _registry.get(agent_id)
        return entry["task"] if entry else ""


async def get_round_id(agent_id: str) -> str:
    """获取 agent 所属轮次 ID。"""
    async with _lock:
        entry = _registry.get(agent_id)
        return str(entry.get("round_id", "")) if entry else ""


async def set_waiting(agent_id: str, result: str = "") -> None:
    """标记 agent 活干完了，等待其他人。

    可选地把当前阶段的 result 写入 registry —— 这样主 agent 即便提前 collect，
    也能拿到真实内容，而不是空字符串。
    """
    async with _lock:
        if agent_id in _registry and _registry[agent_id]["status"] in (RUNNING, RESUMED):
            _registry[agent_id]["status"] = WAITING
            _registry[agent_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
            if result:
                _registry[agent_id]["result"] = result[:1000]
    await _publish_registry_event(agent_id)


async def set_resumed(agent_id: str) -> None:
    """标记 agent 在等待期间收到新消息，恢复工作。"""
    async with _lock:
        if agent_id in _registry and _registry[agent_id]["status"] == WAITING:
            _registry[agent_id]["status"] = RESUMED
            _registry[agent_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    await _publish_registry_event(agent_id)


async def set_running(agent_id: str) -> None:
    """标记 agent 已进入活跃执行态。"""
    async with _lock:
        if agent_id in _registry and _registry[agent_id]["status"] != RUNNING:
            _registry[agent_id]["status"] = RUNNING
            _registry[agent_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    await _publish_registry_event(agent_id)


async def can_receive(agent_id: str, round_id: str = "") -> bool:
    """检查 agent 是否能接收消息。

    任何已注册的 agent 都能收 —— 即使是 DONE/TIMEOUT 的也可以，
    主 agent 监控循环会负责唤醒它们处理新消息。
    """
    async with _lock:
        entry = _registry.get(agent_id)
        return entry is not None and _matches_round(entry, round_id)


async def all_quiescent(round_id: str = "") -> bool:
    """所有 agent 都进入了 waiting/done（没有 running 的）。"""
    async with _lock:
        infos = [info for info in _registry.values() if _matches_round(info, round_id)]
        return not any(info["status"] == RUNNING for info in infos)


async def all_done(round_id: str = "") -> bool:
    """所有 agent 都真正完成了（没有 running/waiting/resumed 的）。"""
    async with _lock:
        infos = [info for info in _registry.values() if _matches_round(info, round_id)]
        return not any(info["status"] in (RUNNING, WAITING, RESUMED) for info in infos)


async def all_willing_to_quit(round_id: str = "") -> bool:
    """没有 agent 还在主动干活 —— 全部都在 WAITING/DONE/TIMEOUT。

    用于 wait_for_others 的解锁判断：当所有人都进入 WAITING（想退出但还在等别人）时，
    应该让大家一起退出，而不是互相等待。
    """
    async with _lock:
        infos = [info for info in _registry.values() if _matches_round(info, round_id)]
        return not any(info["status"] in (RUNNING, RESUMED) for info in infos)


async def wait_for_others(agent_id: str, inbox_check_func, mark_read_func=None, max_wait: int = 600, result: str = "") -> str:
    """Subagent 干完活后调用：标记 waiting（带 result），等其他人。

    每 5 秒检查一次：
    - inbox 有新消息 → 返回消息内容（回去继续干活）
    - 所有 agent 都不在干活 (RUNNING/RESUMED) → 返回 ""（一起退出）
    - 超时 → 返回 "timeout"

    先检查 inbox 再检查全局退出条件，避免在有人发来消息时直接退出。
    """
    round_id = await get_round_id(agent_id)
    await set_waiting(agent_id, result=result)
    waited = 0
    while waited < max_wait:
        new_msgs = inbox_check_func(agent_id)
        if new_msgs:
            if mark_read_func:
                maybe_awaitable = mark_read_func(agent_id)
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
            return new_msgs
        if await all_willing_to_quit(round_id=round_id):
            return ""
        await asyncio.sleep(5)
        waited += 5
    return "timeout"


async def get_status(agent_id: str) -> str | None:
    """获取 agent 状态：running / waiting / resumed / done / timeout / None。"""
    async with _lock:
        entry = _registry.get(agent_id)
        if entry is None:
            return None
        return entry["status"]


async def get_context(exclude: str = "", round_id: str = "") -> str:
    """格式化注册表为文本，注入 agent context。"""
    async with _lock:
        entries = [
            (aid, info)
            for aid, info in _registry.items()
            if _matches_round(info, round_id)
        ]
        if not entries:
            return ""
        lines = ["[活跃子 agent]"]
        for aid, info in entries:
            marker = "-> " if aid == exclude else "  "
            st = {"running": "工作中", "waiting": "活干完了等大家", "resumed": "恢复工作", "done": "已完成", "timeout": "超时"}.get(info["status"], info["status"])
            lines.append(f"  {marker}{aid}: {info['task'][:50]} [{st}]")
        return "\n".join(lines)


async def clear(round_id: str | None = None) -> None:
    """清除注册表（新 session 时调用）。

    当提供 *round_id* 时，只删除该轮次的 subagent。
    """
    async with _lock:
        if not round_id:
            _registry.clear()
            return
        doomed = [aid for aid, info in _registry.items() if _matches_round(info, round_id)]
        for aid in doomed:
            _registry.pop(aid, None)


async def collect_results(round_id: str = "") -> str:
    """收集所有 subagent 的结果，格式化为文本。"""
    async with _lock:
        lines = []
        for aid, info in _registry.items():
            if not _matches_round(info, round_id):
                continue
            result = info.get("result", "")
            if result:
                lines.append(f"[{aid}]: {result[:1000]}")
            else:
                lines.append(f"[{aid}]: 无结果")
        return "\n\n".join(lines) if lines else "无 subagent 结果。"


async def get_snapshot(round_id: str = "") -> dict:
    """Return a JSON-safe snapshot of all subagents for the WebUI."""
    async with _lock:
        snapshot = {}
        for aid, info in _registry.items():
            if not _matches_round(info, round_id):
                continue
            msgs = []
            for m in info.get("messages", []):
                role = m.get("role", "")
                content = m.get("content", "")
                if role == "system":
                    content = content[:200]  # trim system prompts
                entry = {"role": role, "content": content}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [
                        {"name": tc["function"]["name"]}
                        for tc in m["tool_calls"]
                    ]
                msgs.append(entry)
            snapshot[aid] = {
                "task": info.get("task", ""),
                "status": info.get("status", ""),
                "result": info.get("result", ""),
                "messages": msgs,
            }
        return snapshot


# ---------------------------------------------------------------------------
# Sub-agent execution loop (moved from agent.py)
# ---------------------------------------------------------------------------


def _spawn_subagent_task(coro, agent_id: str) -> asyncio.Task:
    """Create a fire-and-forget asyncio task with error logging.

    If the coroutine raises before its internal try/except, the exception
    would otherwise be silently lost.
    """
    task = asyncio.create_task(coro)
    task.add_done_callback(lambda t: _log_task_exception(t, agent_id))
    return task


def _log_task_exception(task: asyncio.Task, agent_id: str) -> None:
    try:
        task.result()
    except Exception:
        logger.exception("Sub-agent %s task crashed before internal try/except", agent_id)


async def _run_subagent(
    agent_id: str,
    task: str,
    bot: Any,
    chat_id: int,
    db_path: str,
    resume_messages: list | None = None,
) -> str:
    """Run a sub-agent in its own loop.

    Has its own agent loop, inbox checking, and full tool access.
    Communicates with other agents via inbox.

    If *resume_messages* is provided, the agent picks up from that history
    instead of starting fresh — used when a DONE agent is woken up to
    process new inbox messages.

    Uses lazy imports from agent.py to avoid circular dependencies.
    """
    from cyrene.agent import _call_llm, _caller_type, _current_agent_id, _current_round_id, _MAX_TOOL_ROUNDS
    from cyrene.llm import _assistant_text, _truncate
    from cyrene.tools import TOOL_DEFS, _execute_tool

    _caller_type.set(f"subagent_{agent_id}")
    round_id = await get_round_id(agent_id)
    round_token = _current_round_id.set(round_id) if round_id else None
    from cyrene.inbox import get_inbox_context as _get_inbox, mark_all_read as _mark_inbox_read

    subagent_prompt = f"""You are a sub-agent, ID: {agent_id}. Your job is to complete the assigned task.

You can:
- Use tools (files, search, bash, etc.)
- Communicate with other agents via the send_agent_message tool
- Check who else is active via the context at the top

Rules:
- Search max 3 times. If still no useful results, use your existing knowledge and tag the output with `[fallback to model knowledge]`.
- When you call quit, include your findings or analysis in the text. Do not quit empty-handed.
- Your final quit text is automatically collected by the parent agent. Do not invent a separate coordinator or try to send the final answer to a non-existent agent such as "main" or "danny".
- When you choose people to message, only use agent IDs that appear in the current `[活跃子 agent]` list or in your inbox messages. If an ID is not currently visible there, treat it as unavailable.
"""

    if resume_messages:
        # 被唤醒：从已有历史续跑，注入一条提示让 LLM 知道发生了什么
        messages = list(resume_messages)
        messages.append({"role": "user", "content": "[你已被唤醒 — inbox 中有新消息需要处理。处理完后再决定是否 quit。]"})
    else:
        messages = [
            {"role": "system", "content": subagent_prompt},
            {"role": "user", "content": task},
        ]

    await set_running(agent_id)

    final_text = ""
    empty_quit_count = 0
    _MAX_EMPTY_QUIT_RETRIES = 2

    async def _save_if_registered() -> None:
        """Keep registry messages resumable after any local history mutation."""
        await save_messages(agent_id, messages)

    try:
        for _ in range(_MAX_TOOL_ROUNDS):
            # 每次 LLM 调用前注入注册表和 inbox 作为独立消息，保持 messages[0] 稳定
            registry_ctx = await get_context(exclude=agent_id, round_id=round_id)
            inbox_text = _get_inbox(agent_id)

            # 移除上一轮的旧上下文消息（以特定前缀开头的用户消息）
            messages = [m for m in messages if not (
                m.get("role") == "user" and (
                    str(m.get("content", "")).startswith("[活跃子 agent]") or
                    str(m.get("content", "")).startswith("[收件箱]")
                )
            )]
            # 注入新上下文
            if registry_ctx:
                messages.append({"role": "user", "content": registry_ctx})
            if inbox_text:
                messages.append({"role": "user", "content": f"[收件箱]\n{inbox_text}"})
                # 注入后立即标记为已读 —— 避免下一轮重复展示同一批消息
                await _mark_inbox_read(agent_id)

            response = await _call_llm(messages, tools=TOOL_DEFS)

            entry: dict = {"role": "assistant", "content": response.get("content") or ""}
            if response.get("reasoning_content"):
                entry["reasoning_content"] = response["reasoning_content"]
            if response.get("tool_calls"):
                entry["tool_calls"] = response["tool_calls"]
            if response.get("usage"):
                entry["usage"] = response["usage"]
            messages.append(entry)

            # Save messages to registry for WebUI display
            await _save_if_registered()

            tcs = response.get("tool_calls") or []

            # 检测 quit 或纯文本（活干完了）
            should_exit = any(t.get("function", {}).get("name") == "quit" for t in tcs) or not tcs
            if should_exit:
                for tc in tcs:
                    if tc.get("function", {}).get("name") == "quit":
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": "Interaction ended.",
                        })
                if tcs:
                    await _save_if_registered()

                final_text = _assistant_text(response).strip() or "Done."

                # 验证 quit 是否附带了有效结果
                if final_text in ("Done.", "Interaction ended.", ""):
                    empty_quit_count += 1
                    if empty_quit_count >= _MAX_EMPTY_QUIT_RETRIES:
                        # 多次空 quit，强制退出避免死循环。结果写入 registry。
                        final_text = "[未输出有效结果 — quit 多次为空]"
                        break
                    from cyrene.inbox import send_message as _send_feedback
                    await _send_feedback("validator", agent_id, "quit_quality",
                        f"[quit 质量反馈] 你的 quit 没有附带任何结果。请重新 quit，并在 quit 的文字中说明：做了哪些工作、找到了什么信息、或者为什么没找到。即使没有找到结果也要说明原因。")
                    await set_resumed(agent_id)
                    messages.append({"role": "user", "content": f"[系统] quit 质量检查未通过 (重试 {empty_quit_count}/{_MAX_EMPTY_QUIT_RETRIES})。"})
                    await _save_if_registered()
                    continue

                # 标记 willing_to_quit（带 result），等别人（每 5 秒检查 inbox）
                from cyrene.inbox import get_inbox_context as _inbox_ctx
                inbox_msg = await wait_for_others(agent_id, _inbox_ctx, mark_read_func=_mark_inbox_read, result=final_text)
                if inbox_msg == "":
                    break  # 全部 finished，正常退出
                elif inbox_msg == "timeout":
                    break  # 超时，强制退出
                else:
                    # 有新消息，标记 RESUMED，继续干活
                    await set_resumed(agent_id)
                    empty_quit_count = 0  # 收到新消息，重置空 quit 计数
                    messages.append({"role": "user", "content": f"[等待期间收到新消息]\n{inbox_msg}"})
                    await _save_if_registered()
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
            if tcs:
                await _save_if_registered()
        else:
            final_text = "Sub-agent hit loop limit."
    except Exception as e:
        logger.exception("Sub-agent %s crashed", agent_id)
        final_text = f"Sub-agent crashed: {e}"
    finally:
        if round_token is not None:
            _current_round_id.reset(round_token)

    await mark_done(agent_id, final_text)
    return final_text
