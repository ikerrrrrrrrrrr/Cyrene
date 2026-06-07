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
from contextvars import ContextVar
import random
from datetime import datetime, timezone
from typing import Any

from cyrene import debug
from cyrene.config import DATA_DIR

logger = logging.getLogger(__name__)

# 状态常量
RUNNING = "running"          # 正在干活
WAITING = "waiting"          # 活干完了，等别人消息
RESUMED = "resumed"          # 等待期间收到新消息，继续干活
DONE = "done"                # 真正完成
TIMEOUT = "timeout"          # 超时退出
_MAX_WAITING_RESULT_CHARS = 6000
_MAX_FINAL_RESULT_CHARS = 16000
_MAX_COLLECT_RESULT_CHARS = 12000
_MAX_SUMMARY_MESSAGE_CHARS = 2400
_MAX_SUMMARY_TOTAL_CHARS = 48000

_NO_LIMIT = 1_000_000_000
_SUMMARY_AGENT_PREFIX = "agent_summary_"

def _is_deep_research() -> bool:
    try:
        from cyrene.agent.state import _deep_research_mode
        return _deep_research_mode.get()
    except Exception:
        return False

def _limit(val: int) -> int:
    return _NO_LIMIT if _is_deep_research() else val

# 全局注册表
_registry: dict[str, dict] = {}
_lock = asyncio.Lock()
_direct_message_mode: ContextVar[bool] = ContextVar("_direct_message_mode", default=False)

# 已生成子 agent 的 asyncio 任务，用于中断时取消
_subagent_tasks: dict[str, asyncio.Task] = {}


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


async def register(agent_id: str, task: str, round_id: str = "", role: str = "") -> None:
    """注册一个子 agent。

    *role* 可选，目前支持 "moderator"（主持人）/ "participant"（参与者），
    用于多 agent 讨论时区分谁负责开场、谁负责等待发言。
    """
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
        if role:
            _registry[agent_id]["role"] = role
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
                        _registry[agent_id]["result"] = result[:_limit(_MAX_FINAL_RESULT_CHARS)]
                    else:
                        _registry[agent_id]["result"] = (existing + "\n---\n" + result)[:_limit(_MAX_FINAL_RESULT_CHARS)]
                else:
                    _registry[agent_id]["result"] = result[:_limit(_MAX_FINAL_RESULT_CHARS)]
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


async def get_role(agent_id: str) -> str:
    """获取 agent 的讨论角色（moderator / participant / 空）。"""
    async with _lock:
        entry = _registry.get(agent_id)
        return str(entry.get("role", "")) if entry else ""


async def round_has_moderator(round_id: str = "", exclude: str = "") -> bool:
    """本轮是否存在主持人（除 *exclude* 之外）。"""
    async with _lock:
        return any(
            info.get("role") == "moderator" and aid != exclude
            for aid, info in _registry.items()
            if _matches_round(info, round_id)
        )


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
                _registry[agent_id]["result"] = result[:_limit(_MAX_WAITING_RESULT_CHARS)]
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

    每 2 秒检查一次（加随机抖动避免惊群效应）：
    - inbox 有新消息 → 短暂等待 0.5s 允许批量投递，然后返回消息内容（回去继续干活）
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
            # 短暂等待让批量消息有机会全部到达
            await asyncio.sleep(0.5)
            if mark_read_func:
                maybe_awaitable = mark_read_func(agent_id)
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
            return new_msgs
        if await all_willing_to_quit(round_id=round_id):
            return ""
        interval = 2 + random.uniform(-0.3, 0.3)
        await asyncio.sleep(interval)
        waited += interval
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
            role_tag = {"moderator": "（主持人）", "participant": "（参与者）"}.get(info.get("role", ""), "")
            lines.append(f"  {marker}{aid}{role_tag}: {info['task'][:50]} [{st}]")
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
            task = str(info.get("task", "") or "").strip()
            status = str(info.get("status", "") or "").strip()
            result = info.get("result", "")
            if result:
                lines.append(
                    f"[{aid}] task: {task or '—'}\n"
                    f"status: {status or 'unknown'}\n"
                    f"result:\n{str(result)[:_limit(_MAX_COLLECT_RESULT_CHARS)]}"
                )
            else:
                lines.append(
                    f"[{aid}] task: {task or '—'}\n"
                    f"status: {status or 'unknown'}\n"
                    "result:\n无结果"
                )
        return "\n\n".join(lines) if lines else "无 subagent 结果。"


async def build_deep_research_source(round_id: str = "") -> str:
    """Collect only subagent research RESULTS for the final Phase 3 report.

    Unlike build_round_summary_transcript, this does NOT include:
    - Subagent internal transcripts (tool calls, reasoning, messages)
    - Inter-agent communication messages
    - Agent IDs, status labels, or process metadata

    Output is pure research material, formatted as clean sections the main
    agent can directly incorporate into the final report.
    """
    entries = await _registry_entries_for_round(round_id=round_id)
    if not entries:
        return "No research material available."

    # Sort by creation time for consistent ordering
    entries.sort(key=lambda item: str(item[1].get("created_at") or ""))

    sections: list[str] = []
    for index, (agent_id, info) in enumerate(entries, start=1):
        task = str(info.get("task") or "").strip()
        result = str(info.get("result") or "").strip()
        if not result:
            continue

        section = (
            f"## Research Topic {index}: {task or 'Untitled'}\n\n"
            f"{result}"
        )
        sections.append(section)

    if not sections:
        return "No research material available."

    return "\n\n---\n\n".join(sections)


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


def _flow_message_copy(message: dict[str, Any]) -> dict[str, Any]:
    """Return a compact, JSON-safe message copy for flow persistence."""
    role = str(message.get("role", "") or "").strip()
    entry: dict[str, Any] = {"role": role}

    if "content" in message:
        content = message.get("content")
        if role == "system":
            entry["content"] = str(content or "")[:200]
        else:
            entry["content"] = content
    if message.get("reasoning_content"):
        entry["reasoning_content"] = message.get("reasoning_content")
    if message.get("tool_call_id"):
        entry["tool_call_id"] = str(message.get("tool_call_id") or "")
    if message.get("usage"):
        entry["usage"] = message.get("usage")

    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        compact_calls: list[dict[str, Any]] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
            compact_fn = {
                "name": str(fn.get("name") or "").strip(),
                "arguments": fn.get("arguments", ""),
            }
            compact_calls.append({
                "id": str(tc.get("id") or ""),
                "type": tc.get("type", "function"),
                "function": compact_fn,
            })
        if compact_calls:
            entry["tool_calls"] = compact_calls

    return entry


async def build_flow_snapshot(round_id: str) -> dict[str, Any]:
    """Persist the minimum completed-round subagent data needed by the WebUI."""
    entries = await _registry_entries_for_round(round_id=round_id)
    agent_ids = {agent_id for agent_id, _ in entries}
    comm_messages = _round_comm_messages(agent_ids, round_id=round_id)

    snapshot_agents: dict[str, dict[str, Any]] = {}
    for agent_id, info in entries:
        snapshot_agents[agent_id] = {
            "task": info.get("task", ""),
            "status": info.get("status", ""),
            "result": info.get("result", ""),
            "messages": [
                _flow_message_copy(message)
                for message in (info.get("messages") or [])
                if isinstance(message, dict)
            ],
            "created_at": info.get("created_at"),
            "updated_at": info.get("updated_at"),
            "round_id": str(info.get("round_id", "") or round_id),
        }

    compact_comm_messages = [
        {
            "message_id": str(item.get("message_id") or ""),
            "from": str(item.get("from") or ""),
            "to": str(item.get("to") or ""),
            "type": str(item.get("type") or "chat"),
            "content": str(item.get("content") or ""),
            "timestamp": item.get("timestamp"),
            "round_id": str(item.get("round_id") or round_id),
        }
        for item in comm_messages
        if isinstance(item, dict)
    ]

    return {
        "round_id": round_id,
        "summary_agent_id": _summary_agent_id(round_id),
        "agents": snapshot_agents,
        "comm_messages": compact_comm_messages,
    }


def _summary_agent_id(round_id: str) -> str:
    suffix = str(round_id or "").removeprefix("round_").strip() or "adhoc"
    suffix = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in suffix)
    return f"{_SUMMARY_AGENT_PREFIX}{suffix[:32]}"


def _truncate_summary_text(text: str, limit: int | None = None) -> str:
    if limit is None:
        limit = _limit(_MAX_SUMMARY_MESSAGE_CHARS)
    source = str(text or "")
    if len(source) <= limit:
        return source
    return source[:limit] + "\n...[truncated]..."


def _render_summary_message(message: dict[str, Any]) -> str:
    role = str(message.get("role", "") or "").strip() or "unknown"
    if role == "system":
        return ""

    chunks: list[str] = []
    content = str(message.get("content") or "").strip()
    reasoning = str(message.get("reasoning_content") or "").strip()
    if role == "assistant" and reasoning:
        chunks.append(f"[reasoning]\n{_truncate_summary_text(reasoning)}")
    if content:
        chunks.append(_truncate_summary_text(content))

    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = str(fn.get("name") or "tool").strip()
        args = str(fn.get("arguments") or "").strip()
        rendered = f"[tool_call] {name}"
        if args:
            rendered += f"\nargs: {_truncate_summary_text(args, 600)}"
        chunks.append(rendered)

    if role == "tool" and not chunks:
        chunks.append(_truncate_summary_text(str(message.get("content") or "")))

    if not chunks:
        return ""
    return f"{role}:\n" + "\n".join(chunks)


async def _registry_entries_for_round(round_id: str = "", exclude_ids: set[str] | None = None) -> list[tuple[str, dict[str, Any]]]:
    blocked = exclude_ids or set()
    async with _lock:
        entries = [
            (aid, dict(info))
            for aid, info in _registry.items()
            if aid not in blocked and _matches_round(info, round_id)
        ]
    entries.sort(key=lambda item: str(item[1].get("created_at") or ""))
    return entries


def _round_comm_messages(agent_ids: set[str], round_id: str = "") -> list[dict[str, Any]]:
    inbox_root = DATA_DIR / "inbox"
    if not agent_ids or not inbox_root.exists():
        return []

    messages: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for msg_file in inbox_root.glob("*/*.json"):
        try:
            payload = json.loads(msg_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if round_id and str(payload.get("round_id", "")).strip() != round_id:
            continue
        from_agent = str(payload.get("from", "")).strip()
        to_agent = str(payload.get("to", "")).strip()
        if from_agent not in agent_ids or to_agent not in agent_ids:
            continue
        message_id = str(payload.get("message_id") or msg_file.stem).strip()
        if message_id in seen_ids:
            continue
        seen_ids.add(message_id)
        messages.append(payload)
    messages.sort(key=lambda item: str(item.get("timestamp") or ""))
    return messages


async def build_group_chat_messages(round_id: str) -> dict[str, Any]:
    """Build group-chat-formatted messages for a given round.

    Extracts:
    - ``send_agent_message`` / ``broadcast_agent_message`` tool calls from
      each subagent's message history, formatted as chat entries with
      ``@recipient`` / ``@所有人`` prepended to the body.
    - Each subagent's final ``result`` (when non-trivial).
    - User messages from inbox files (``from == "user"``).

    Falls back to ``subagent_flow_snapshot`` in saved session messages when
    the live ``_registry`` has no entries for *round_id* (e.g. after the
    round has completed and the registry was cleared).

    Returns ``{"messages": [...], "agents": [...]}`` sorted chronologically.
    """
    async with _lock:
        entries = [
            (aid, dict(info))
            for aid, info in _registry.items()
            if _matches_round(info, round_id) and aid != "main"
        ]

    # Fallback: live registry may have been cleared — reconstruct from
    # subagent_flow_snapshot embedded in the saved session messages.
    if not entries:
        from cyrene.agent.state import STATE_FILE

        if STATE_FILE and STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                raw_msgs = data.get("messages", []) if isinstance(data, dict) else []
                for msg in reversed(raw_msgs):
                    snapshot = msg.get("subagent_flow_snapshot")
                    if not isinstance(snapshot, dict):
                        continue
                    if str(snapshot.get("round_id", "")).strip() != round_id:
                        continue
                    agents_data = snapshot.get("agents") or {}
                    comm_msgs = snapshot.get("comm_messages") or []
                    agents_list = []
                    msg_list = []
                    for agent_id, info in agents_data.items():
                        if not isinstance(info, dict):
                            continue
                        agents_list.append({
                            "id": agent_id,
                            "task": str(info.get("task", "") or "").strip(),
                            "status": str(info.get("status", "") or "done").strip(),
                        })
                        result = str(info.get("result", "") or "").strip()
                        if result and result not in ("Done.", "", "无结果"):
                            msg_list.append({
                                "id": f"{agent_id}_result",
                                "type": "agent_result",
                                "from": agent_id,
                                "to": "",
                                "content": result,
                                "timestamp": str(info.get("updated_at") or info.get("created_at") or ""),
                                "round_id": round_id,
                            })
                    for comm in comm_msgs:
                        if not isinstance(comm, dict):
                            continue
                        content = str(comm.get("content", "") or "").strip()
                        if not content:
                            continue
                        target = str(comm.get("to", "") or "").strip()
                        is_broadcast = str(comm.get("type", "") or "").strip() == "broadcast"
                        display = f"@{'所有人' if is_broadcast or not target else target} {content}"
                        from_agent = str(comm.get("from", "") or "").strip()
                        msg_list.append({
                            "id": str(comm.get("message_id", "") or f"{from_agent}_{comm.get('timestamp', '')}"),
                            "type": "agent_broadcast" if is_broadcast else "agent_send",
                            "from": from_agent,
                            "to": target or "all",
                            "content": display,
                            "timestamp": str(comm.get("timestamp", "") or ""),
                            "round_id": round_id,
                        })
                    msg_list.sort(key=lambda m: str(m.get("timestamp") or ""))
                    return {"messages": msg_list, "agents": agents_list}
            except Exception:
                logger.warning("Failed to load flow snapshot for round %s", round_id, exc_info=True)

    agents_list: list[dict[str, str]] = []
    messages: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()

    for agent_id, info in entries:
        task = str(info.get("task", "") or "").strip()
        status = str(info.get("status", "") or "unknown").strip()
        agents_list.append({"id": agent_id, "task": task, "status": status})

        agent_created = str(info.get("created_at") or now)
        agent_msgs = info.get("messages") or []

        # 1. Extract send_agent_message / broadcast_agent_message tool calls
        for msg_idx, msg in enumerate(agent_msgs):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant":
                continue
            for tc in (msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function", {})
                if not isinstance(fn, dict):
                    continue
                name = str(fn.get("name", "") or "").strip()
                if name not in ("send_agent_message", "broadcast_agent_message", "send_message_to_user"):
                    continue
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    continue
                # send_message_to_user uses "text"; others use "content"
                content = str(args.get("content", "") or args.get("text", "") or "").strip()
                if not content:
                    continue

                is_broadcast = name == "broadcast_agent_message"
                is_user_reply = name == "send_message_to_user"
                if is_user_reply:
                    target = "user"
                elif is_broadcast:
                    target = "all"
                else:
                    target = str(args.get("to", "") or "").strip()
                display_content = f"@{target} {content}" if target else content

                # Generate a per-message timestamp from agent_created + offset
                ts = agent_created  # same-timestamp batch; sort stable within agent

                messages.append({
                    "id": f"{agent_id}_{tc.get('id', f'msg_{msg_idx}')}",
                    "type": "agent_broadcast" if is_broadcast else "agent_send",
                    "from": agent_id,
                    "to": target,
                    "content": display_content,
                    "timestamp": ts,
                    "round_id": round_id,
                })

        # 2. Extract subagent result (non-trivial only)
        result = str(info.get("result", "") or "").strip()
        result_ts = str(info.get("updated_at") or agent_created)
        if result and result not in ("Done.", "", "无结果"):
            messages.append({
                "id": f"{agent_id}_result",
                "type": "agent_result",
                "from": agent_id,
                "to": "",
                "content": result,
                "timestamp": result_ts,
                "round_id": round_id,
            })

    # 3. Read inbox messages from user
    inbox_root = DATA_DIR / "inbox"
    agent_ids = {aid for aid, _ in entries}
    if inbox_root.exists():
        for inbox_dir in sorted(inbox_root.iterdir()):
            if not inbox_dir.is_dir():
                continue
            agent_id = inbox_dir.name
            if agent_id not in agent_ids:
                continue
            for msg_file in sorted(inbox_dir.glob("msg_*.json")):
                try:
                    payload = json.loads(msg_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                if str(payload.get("from", "") or "").strip() != "user":
                    continue
                if round_id and str(payload.get("round_id", "") or "").strip() != round_id:
                    continue
                content = str(payload.get("content", "") or "").strip()
                if not content:
                    continue
                messages.append({
                    "id": str(payload.get("message_id", msg_file.stem)),
                    "type": "user_message",
                    "from": "user",
                    "to": agent_id,
                    "content": content,
                    "timestamp": str(payload.get("timestamp", "") or ""),
                    "round_id": round_id,
                })

    # 4. Dedup by (from + content) — avoids duplicate user messages from broadcast to multiple agents
    seen_keys: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for m in messages:
        key = (str(m.get("from", "") or "").strip(), str(m.get("content", "") or "").strip())
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(m)

    # 5. Sort by timestamp (empty timestamps sort last within their group)
    deduped.sort(key=lambda m: str(m.get("timestamp") or ""))

    return {"messages": deduped, "agents": agents_list}


async def build_round_summary_transcript(round_id: str, exclude_ids: set[str] | None = None) -> str:
    entries = await _registry_entries_for_round(round_id=round_id, exclude_ids=exclude_ids)
    if not entries:
        return "No peer subagent transcript was captured for this round."

    sections: list[str] = []
    total_chars = 0
    agent_ids = {agent_id for agent_id, _ in entries}
    for agent_id, info in entries:
        rendered_messages = [
            block
            for block in (_render_summary_message(message) for message in (info.get("messages") or []))
            if block
        ]
        section = (
            f"## {agent_id}\n"
            f"task: {str(info.get('task') or '').strip() or '—'}\n"
            f"status: {str(info.get('status') or '').strip() or 'unknown'}\n"
            f"result:\n{_truncate_summary_text(str(info.get('result') or ''), _limit(5000)) or '—'}\n\n"
            f"transcript:\n" + ("\n\n".join(rendered_messages) if rendered_messages else "—")
        )
        summary_total_limit = _limit(_MAX_SUMMARY_TOTAL_CHARS)
        if total_chars + len(section) > summary_total_limit:
            remaining = summary_total_limit - total_chars
            if remaining <= 0:
                sections.append("[older peer transcript omitted]")
                break
            sections.append(_truncate_summary_text(section, remaining))
            sections.append("[older peer transcript omitted]")
            break
        sections.append(section)
        total_chars += len(section)

    comms = _round_comm_messages(agent_ids, round_id=round_id)
    if comms and total_chars < summary_total_limit:
        lines = ["## Inter-agent messages"]
        for item in comms:
            lines.append(
                f"[{item.get('timestamp', '—')}] {item.get('from', '?')} -> {item.get('to', '?')} ({item.get('type', 'chat')})\n"
                f"{_truncate_summary_text(str(item.get('content') or ''))}"
            )
        comms_block = "\n\n".join(lines)
        remaining = summary_total_limit - total_chars
        if len(comms_block) > remaining:
            comms_block = _truncate_summary_text(comms_block, remaining)
        sections.append(comms_block)

    return "\n\n".join(sections).strip() or "No peer subagent transcript was captured for this round."


async def run_summary_subagent(
    round_id: str,
    parent_task: str,
    guidance: str = "",
    round_history: list[dict[str, Any]] | None = None,
) -> str:
    """Run a dedicated summary subagent after peer subagents finish.

    In deep research mode, skip the LLM summariser and just concatenate all
    subagent transcripts directly — the main agent will synthesise from the
    full raw material without any intermediate compression.
    """
    from cyrene.agent.state import _call_llm
    from cyrene.llm import _assistant_text

    summary_agent_id = _summary_agent_id(round_id)
    transcript = await build_round_summary_transcript(round_id=round_id, exclude_ids={summary_agent_id})

    # Deep research: return raw concatenated transcript, no LLM compression
    if _is_deep_research():
        header = f"## Deep Research Raw Transcript\nParent task: {parent_task or '—'}\n\n"
        final_text = header + transcript
        await register(summary_agent_id, "Concatenate all subagent transcripts (deep research)", round_id=round_id)
        await mark_done(summary_agent_id, final_text)
        return final_text

    summary_task = "Summarize every peer subagent transcript and their communication for the main agent."

    history_lines: list[str] = []
    for msg in (round_history or [])[-12:]:
        role = str(msg.get("role", "") or "").strip()
        if role == "system":
            continue
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        history_lines.append(f"[{role}] {_truncate_summary_text(content, 800)}")
    history_block = "\n".join(history_lines) if history_lines else "—"

    await register(summary_agent_id, summary_task, round_id=round_id)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a synthesis agent. Your job is to read the materials below "
                "and produce a clear, well-organised answer that directly addresses the user's question.\n\n"
                "### How to Synthesise\n"
                "1. Read ALL the materials thoroughly. Identify: what was the user asking, "
                "what are the key findings, where do different sources agree or conflict.\n"
                "2. Write a direct answer to the user. Do NOT describe the research process or mention "
                "how the information was gathered. Write as if you personally found everything.\n"
                "3. Organise your answer for clarity:\n"
                "   - Start by directly addressing the user's question.\n"
                "   - Present findings in a logical order — group related information together.\n"
                "   - Use headings to separate major topics if the answer is long.\n"
                "   - Use bullet points or numbered lists when comparing items or listing options.\n"
                "   - End with a brief conclusion or recommendation when appropriate.\n"
                "4. Preserve ALL important data: specific numbers, concrete facts, key quotes, "
                "and important nuances from the materials. Do not over-summarise — "
                "a detailed answer is better than a vague one.\n"
                "5. When sources disagree, present both sides rather than arbitrarily picking one.\n"
                "6. Be honest about uncertainty. If information is incomplete, say so.\n\n"
                "### Forbidden\n"
                "- Do NOT reference 'subagents', 'research tracks', 'transcripts', or the process.\n"
                "- Do NOT preface your answer with meta-commentary like 'Based on the research...'.\n"
                "- Do NOT end with 'I hope this helps' or similar filler.\n"
                "- Do NOT ask the user questions.\n"
                "- Do NOT invent facts not in the materials.\n\n"
                "### Language\n"
                "Match the user's language. If the user wrote in Chinese, reply in Chinese. "
                "If in English, reply in English."
            ),
        },
        {
            "role": "user",
            "content": (
                f"User's question:\n{parent_task or '—'}\n\n"
                f"Additional guidance:\n{guidance or '—'}\n\n"
                f"Context from the conversation:\n{history_block}\n\n"
                f"Research materials:\n{transcript}"
            ),
        },
    ]
    await save_messages(summary_agent_id, messages)

    try:
        response = await _call_llm(messages, tools=None, max_tokens=32000)
        assistant_entry: dict[str, Any] = {"role": "assistant", "content": response.get("content") or ""}
        if response.get("reasoning_content"):
            assistant_entry["reasoning_content"] = response["reasoning_content"]
        if response.get("usage"):
            assistant_entry["usage"] = response["usage"]
        messages.append(assistant_entry)
        await save_messages(summary_agent_id, messages)
        final_text = _assistant_text(response).strip() or "No summary was produced."
    except Exception as exc:
        logger.exception("Summary sub-agent %s crashed", summary_agent_id)
        final_text = f"Summary sub-agent crashed: {exc}"

    await mark_done(summary_agent_id, final_text)
    return final_text


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
    task.add_done_callback(lambda t: _subagent_tasks.pop(agent_id, None))
    _subagent_tasks[agent_id] = task
    return task


async def cancel_subagent_tasks(round_id: str) -> None:
    """Cancel all running subagent tasks for *round_id* and mark them done immediately.

    This is called when the user hits "stop" — subagents stop whatever they are
    doing (the asyncio task is cancelled) and their registry entry flips to
    ``done`` so the UI updates in real time and the summary phase sees a
    consistent snapshot.
    """
    cancelled_ids: list[str] = []
    async with _lock:
        for agent_id, info in list(_registry.items()):
            if not _matches_round(info, round_id):
                continue
            if agent_id.startswith(_SUMMARY_AGENT_PREFIX):
                continue
            if info.get("status") in ("done", "timeout"):
                continue
            _registry[agent_id]["status"] = DONE
            _registry[agent_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
            cancelled_ids.append(agent_id)

    for agent_id in cancelled_ids:
        await _publish_registry_event(agent_id)
        task = _subagent_tasks.get(agent_id)
        if task is not None and not task.done():
            task.cancel()

    if cancelled_ids:
        await asyncio.sleep(0.1)  # brief yield so CancelledError can propagate


def _log_task_exception(task: asyncio.Task, agent_id: str) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Sub-agent %s task crashed before internal try/except", agent_id)


async def _run_subagent(
    agent_id: str,
    task: str,
    bot: Any,
    chat_id: int,
    db_path: str,
    resume_messages: list | None = None,
    use_secondary: bool = False,
    role: str = "",
) -> str:
    """Run a sub-agent in its own loop.

    Has its own agent loop, inbox checking, and full tool access.
    Communicates with other agents via inbox.

    If *resume_messages* is provided, the agent picks up from that history
    instead of starting fresh — used when a DONE agent is woken up to
    process new inbox messages.

    Uses lazy imports from agent.py to avoid circular dependencies.
    """
    from cyrene.agent.prompts import (
        _MAIN_AGENT_PROMPT, _DEEP_RESEARCH_SUBAGENT_PROMPT,
        _DECISION_SUBAGENT_PROMPT, _LEARNING_SUBAGENT_PROMPT, _COMPARE_SUBAGENT_PROMPT,
        _WORKSPACE_SCOPE_BLOCK,
    )
    from cyrene.agent.state import (
        _deep_research_mode, _current_command,
        _call_llm, _caller_type, _current_agent_id, _current_round_id, _get_max_tool_rounds,
    )
    from cyrene.llm import _assistant_text, _truncate
    from cyrene.tools import get_active_tool_defs_for_actor, is_tool_allowed_for_actor, _execute_tool

    caller_token = _caller_type.set(f"subagent_{agent_id}")
    round_id = await get_round_id(agent_id)
    round_token = _current_round_id.set(round_id) if round_id else None
    dm_token = _direct_message_mode.set(False)
    from cyrene.inbox import get_inbox_context as _get_inbox, mark_all_read as _mark_inbox_read

    cmd = _current_command.get()
    if cmd == "help-me-decide":
        extra_prompt = _DECISION_SUBAGENT_PROMPT
    elif cmd == "learning-plan":
        extra_prompt = _LEARNING_SUBAGENT_PROMPT
    elif cmd == "deep-compare":
        extra_prompt = _COMPARE_SUBAGENT_PROMPT
    elif _deep_research_mode.get():
        extra_prompt = _DEEP_RESEARCH_SUBAGENT_PROMPT
    else:
        extra_prompt = ""
    now = datetime.now(timezone.utc).astimezone()
    temporal_context = (
        "## Current Date\n"
        f"- Current local date: {now:%Y-%m-%d} ({now:%A}).\n"
        "- Interpret relative phrases such as today, recently, this week, last week, 最近, 最近一周, 今天, 本周 relative to this date.\n"
        "- For current weather or travel recommendations, search for current forecast/current conditions. Do not invent or substitute old years unless the user explicitly asks for historical weather."
    )
    subagent_prompt = (
        _MAIN_AGENT_PROMPT
        + extra_prompt
        + """

## Sub-agent Context
- You are a sub-agent. Complete the assigned task directly.
- You can use regular work tools plus `send_agent_message` and `broadcast_agent_message` to coordinate with other sub-agents.
- If you receive a [DIRECT_MESSAGE] from the user via your inbox, this is real-time guidance from the user. The user is steering your work — take it seriously. Use `send_message_to_user` ONCE to: (1) acknowledge the guidance, (2) briefly state what you will do differently. Then immediately continue working with your adjusted approach. Do NOT argue, ask follow-up questions, or chat — act on the guidance. The tool disables after one use.
- You MUST NOT call `send_message`, `send_telegram`, `ask_user`, `spawn_subagent`, or `query_round`.
- For normal rounds, report your result via `quit` — the main agent collects it. Do NOT use `send_message_to_user` in normal rounds.
- Active sub-agents and inbox context may be injected as separate user messages before each turn.
- Your final text is collected by the parent agent. Do not invent a separate coordinator or try to send the final answer to a non-existent agent such as "main" or "danny".

## Inter-Agent Coordination
- **One person OR broadcast — never both, never multiple.** Each turn you may send at most ONE communication message, and it must be EITHER a targeted `send_agent_message` to ONE specific agent OR a `broadcast_agent_message` to ALL. Do NOT send multiple individual messages in the same turn. If something concerns everyone, broadcast once. If it concerns one peer, message them directly.
- **Avoid broadcast when possible.** Broadcast interrupts all peers and fills inboxes with noise. Default to targeted `send_agent_message` — only broadcast when EVERY peer genuinely needs the information (e.g. a shared source URL).
- **Share findings directly.** When you find something another sub-agent needs, `send_agent_message` them directly with the key info. Keep it brief — a few sentences max.
- **Ask for help.** If you're stuck or need data another agent may have, just ask via `send_agent_message`. A short question is fine.
- **Read peer messages.** When another sub-agent sends you something, take a moment to consider it. Respond briefly if needed — remember the one-person-or-broadcast rule applies to your reply too.
- **No handshake or readiness checks.** NEVER send messages like "ready", "waiting for moderator", "standing by", or "received". These waste tokens. Jump straight into substantive work or content.
- **Know when to leave.** When your task is done, call `quit` immediately. No farewells, no confirmations, no waiting for permission. If you feel you're done, you're done.
"""
    )

    if role == "moderator":
        subagent_prompt += """
## Your Role: Moderator
You are the **moderator** of this discussion. Your responsibilities:
1. **Start immediately.** Your FIRST message must announce the topic and kick off the discussion. Do NOT wait for participants to confirm readiness — they are already listening.
2. **Drive the discussion.** Call on participants by name, pose questions, redirect off-topic threads, and keep things moving.
3. **Address one participant per turn.** Each turn, talk to ONE specific participant via `send_agent_message`. Do NOT address multiple participants in the same message — if something concerns everyone, use `broadcast_agent_message` instead.
4. **Summarize and close.** When the discussion has covered enough ground, synthesize key points and wrap up.

CRITICAL: Do NOT ask "is everyone ready?" or wait for confirmations. All participants are live and listening from the moment you speak. Begin the discussion in your very first turn.
"""
    elif role == "participant":
        subagent_prompt += """
## Your Role: Participant
You are a **participant** in this discussion. Rules:
1. **No readiness announcements.** Do NOT send "ready", "waiting", "standing by", or any greeting/confirmation. These are prohibited.
2. **Respond substantively.** When the moderator or another participant addresses you, reply with actual content — arguments, evidence, opinions. Never reply with just an acknowledgment.
3. **One person per reply.** Reply to ONE agent per turn via `send_agent_message`. If your point truly concerns everyone, use `broadcast_agent_message` instead. Do not send multiple individual replies.
4. **Engage proactively.** If you have something relevant to say, speak up via `send_agent_message`. Don't wait to be called on for every point.
5. **Stay in character.** Focus on delivering value through the substance of your contributions.
"""

    subagent_prompt += "\n\n" + temporal_context + "\n\n" + _WORKSPACE_SCOPE_BLOCK

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
    tool_calls_since_checkpoint = 0
    _COORDINATION_CHECKPOINT_INTERVAL = 3

    async def _save_if_registered() -> None:
        """Keep registry messages resumable after any local history mutation."""
        await save_messages(agent_id, messages)

    try:
        max_rounds = _get_max_tool_rounds()
        for _round in range(max_rounds):
            # 每次 LLM 调用前注入注册表和 inbox 作为独立消息，保持 messages[0] 稳定
            registry_ctx = await get_context(exclude=agent_id, round_id=round_id)
            inbox_text = _get_inbox(agent_id)

            # 移除上一轮的旧上下文消息（以特定前缀开头的用户消息）
            messages = [m for m in messages if not (
                m.get("role") == "user" and (
                    str(m.get("content", "")).startswith("[活跃子 agent]") or
                    str(m.get("content", "")).startswith("[收件箱]") or
                    str(m.get("content", "")).startswith("[Coordination Checkpoint]") or
                    str(m.get("content", "")).startswith("[快到工具调用上限")
                )
            )]
            # 注入新上下文
            if registry_ctx:
                messages.append({"role": "user", "content": registry_ctx})
            if inbox_text:
                if _direct_message_mode.get():
                    # 正在处理用户引导：丢弃所有 inbox 消息（含 agent 间通信），
                    # 让 subagent 专注执行用户指令不被干扰。
                    await _mark_inbox_read(agent_id)
                else:
                    messages.append({"role": "user", "content": f"[收件箱]\n{inbox_text}"})
                    # 注入后立即标记为已读 —— 避免下一轮重复展示同一批消息
                    await _mark_inbox_read(agent_id)
                    _direct_message_mode.set("[DIRECT_MESSAGE]" in inbox_text)

            # 定期注入协调检查点，鼓励 subagent 之间主动沟通
            if tool_calls_since_checkpoint >= _COORDINATION_CHECKPOINT_INTERVAL:
                messages.append({
                    "role": "user",
                    "content": (
                        "[Coordination Checkpoint]\n"
                        "Any updates worth sharing? Talk to ONE peer via `send_agent_message`, or broadcast to ALL via `broadcast_agent_message`. Not both, not multiple.\n"
                        "Any new messages from peers? Read and respond if needed — one person or broadcast."
                    ),
                })
                tool_calls_since_checkpoint = 0

            # 快到上限时提醒 agent 收尾，让它能在截断前产出有效结果
            rounds_left = max_rounds - _round
            if rounds_left == 3:
                messages.append({
                    "role": "user",
                    "content": (
                        f"[快到工具调用上限，还剩约 {rounds_left} 轮。"
                        "请用现有信息给出最好的结果，然后调用 quit 退出。"
                        "quit 前的文本里说明：已完成什么、还差什么（如有）。"
                        "不要再启动耗时的新工具调用。]"
                    ),
                })

            response = await _call_llm(messages, tools=get_active_tool_defs_for_actor("subagent"), max_tokens=None, secondary=use_secondary)

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

                # Include all sent agent messages in the final text so that
                # creative output (poems, reviews, etc.) is preserved in the
                # registry result and shown in the final synthesis.
                sent_output: list[str] = []
                for msg in messages:
                    if msg.get("role") != "assistant":
                        continue
                    for tc in (msg.get("tool_calls") or []):
                        fn = tc.get("function", {})
                        if fn.get("name") in ("send_agent_message", "broadcast_agent_message"):
                            try:
                                args = json.loads(fn.get("arguments", "{}"))
                                content = args.get("content", "")
                                target = args.get("to", "all") if fn.get("name") == "broadcast_agent_message" else args.get("to", "?")
                                if content:
                                    sent_output.append(f"[to {target}]\n{content}")
                            except Exception:
                                pass
                agent_text = _assistant_text(response).strip() or "Done."
                if sent_output:
                    final_text = agent_text + "\n\n---\n\n" + "\n\n".join(sent_output)
                else:
                    final_text = agent_text

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
                    messages.append({"role": "user", "content": f"[等待期间收到新消息]\n{inbox_msg}"})
                    _direct_message_mode.set("[DIRECT_MESSAGE]" in str(inbox_msg))
                    await _save_if_registered()
                    continue

            fresh_inbox = False
            for tc in tcs:
                name = tc["function"]["name"]
                if not is_tool_allowed_for_actor(name, "subagent"):
                    result = f"Tool {name} is reserved for the main agent. Subagents must coordinate via send_agent_message and return their final result via quit."
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    continue
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
                # 每执行完一个工具检查 inbox，用户引导时能更快响应
                inbox_text = _get_inbox(agent_id)
                if inbox_text:
                    fresh_inbox = True
                    break
                # 如果刚执行的是通讯类工具，重置检查点计数器（已满足协调要求）
                if name in ("send_agent_message", "broadcast_agent_message"):
                    tool_calls_since_checkpoint = 0
                else:
                    tool_calls_since_checkpoint += 1
            if fresh_inbox:
                await _save_if_registered()
                continue
            if tcs:
                await _save_if_registered()
        else:
            # 警告注入后 LLM 可能已在最后几轮里给出了部分结果，提取出来
            last_content = ""
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    content = str(msg.get("content") or "").strip()
                    if content:
                        last_content = content
                        break
            if last_content:
                final_text = f"[已到工具调用上限，任务可能未完成]\n{last_content}"
            else:
                final_text = "[已到工具调用上限，任务未完成。]"
    except Exception as e:
        logger.exception("Sub-agent %s crashed", agent_id)
        final_text = f"Sub-agent crashed: {e}"
    finally:
        _caller_type.reset(caller_token)
        _direct_message_mode.reset(dm_token)
        if round_token is not None:
            _current_round_id.reset(round_token)

    await mark_done(agent_id, final_text)
    return final_text
