"""
Subagent registry and lifecycle management.

每个子 agent 在注册表中有一条记录：
  agent_id -> {"task": str, "status": "alive" | "done", "result": str}

注册表用于：
1. 发送 inbox 消息前检查对方是否还活着
2. 注入到每个 agent 的 context 中，让大家知道谁在干什么
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# 全局注册表
_registry: dict[str, dict] = {}
_lock = asyncio.Lock()


async def register(agent_id: str, task: str) -> None:
    """注册一个子 agent。"""
    async with _lock:
        _registry[agent_id] = {"task": task, "status": "alive", "result": ""}


async def mark_done(agent_id: str, result: str = "") -> None:
    """标记 agent 已完成。"""
    async with _lock:
        if agent_id in _registry:
            _registry[agent_id]["status"] = "done"
            _registry[agent_id]["result"] = result[:200]


async def get_status(agent_id: str) -> str | None:
    """获取 agent 状态：alive / done / None（不存在）。"""
    async with _lock:
        entry = _registry.get(agent_id)
        if entry is None:
            return None
        return entry["status"]


async def is_alive(agent_id: str) -> bool:
    """检查 agent 是否存活。"""
    st = await get_status(agent_id)
    return st == "alive"


async def get_context(exclude: str = "") -> str:
    """格式化注册表为文本，注入 agent context。"""
    async with _lock:
        if not _registry:
            return ""
        lines = ["[活跃子 agent]"]
        for aid, info in _registry.items():
            marker = "-> " if aid == exclude else "  "
            st = "工作中" if info["status"] == "alive" else "已完成"
            lines.append(f"  {marker}{aid}: {info['task'][:50]} [{st}]")
        return "\n".join(lines)


async def clear() -> None:
    """清除注册表（新 session 时调用）。"""
    async with _lock:
        _registry.clear()
