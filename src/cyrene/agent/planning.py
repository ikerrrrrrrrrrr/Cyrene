"""计划模式：把用户请求拆解成「步骤 → 详细任务」，展示在右侧「计划」tab，
并用 ask_user 请用户确认（同意并开始 / 拒绝 / 修改）。同意后按默认模式执行。

触发途径：
  1. 用户在输入框选「计划模式」 → coordinator._run_chat_agent 在执行前调用 run_plan_flow。
  2. agent 自发调用 enter_plan_mode 工具（tool_impl/enter_plan_mode.py）。
确认回答由 guidance._handle_plan_confirmation_answer 处理。
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

_PLAN_TOOL_DEFS = [{
    "type": "function",
    "function": {
        "name": "submit_plan",
        "description": "提交对用户请求的结构化拆解计划。必须调用且只调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "计划标题（简短，中文）"},
                "summary": {"type": "string", "description": "一两句话概述总体思路"},
                "steps": {
                    "type": "array",
                    "description": "多个有序步骤；每个步骤再拆成若干具体、可操作的任务",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "步骤标题"},
                            "tasks": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "该步骤下的详细任务（具体到可以照着做）",
                            },
                        },
                        "required": ["title", "tasks"],
                    },
                },
            },
            "required": ["title", "steps"],
        },
    },
}]

_PLAN_SYSTEM = (
    "你正处于「计划模式」。现在不要执行任何实际操作，只做规划。\n"
    "把用户的请求拆解成清晰的执行计划：先给出多个有序步骤，再把每个步骤拆解成具体、可操作的任务。\n"
    "步骤应覆盖完成请求的完整路径；任务要具体到可以照着做。\n"
    "完成后只调用 submit_plan 提交结构化计划，不要输出其他文字、不要调用其他工具。"
)


def _normalize_plan(args: dict[str, Any]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for s in (args.get("steps") or []):
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()
        tasks = [str(t).strip() for t in (s.get("tasks") or []) if str(t).strip()]
        if title or tasks:
            steps.append({"title": title or "步骤", "tasks": tasks})
    return {
        "title": str(args.get("title") or "执行计划").strip() or "执行计划",
        "summary": str(args.get("summary") or "").strip(),
        "steps": steps,
    }


def _plan_to_text(plan: dict[str, Any]) -> str:
    """把计划渲染成纯文本，用于注入「同意后」的执行指令。"""
    lines = [f"# {plan.get('title', '执行计划')}"]
    if plan.get("summary"):
        lines.append(str(plan["summary"]))
    for i, step in enumerate(plan.get("steps") or [], 1):
        lines.append(f"\n步骤 {i}：{step.get('title', '')}")
        for j, task in enumerate(step.get("tasks") or [], 1):
            lines.append(f"  {i}.{j} {task}")
    return "\n".join(lines)


async def generate_plan(
    user_message: str,
    history: list[dict[str, Any]] | None = None,
    modification: str = "",
) -> dict[str, Any]:
    """调用 LLM 生成结构化计划。失败时返回带空步骤的降级计划。"""
    import cyrene.agent.state as _state

    parts = [f"用户的请求：\n{user_message}"]
    if modification:
        parts.append(
            f"\n用户对上一版计划的修改意见：\n{modification}\n请在新计划中采纳这些意见。"
        )
    messages = [
        {"role": "system", "content": _PLAN_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]
    response = await _state._call_llm(
        messages,
        tools=_PLAN_TOOL_DEFS,
        max_tokens=4000,
        thinking="disabled",
    )
    for tc in (response.get("tool_calls") or []):
        if str(tc.get("function", {}).get("name") or "").strip() != "submit_plan":
            continue
        try:
            args = json.loads(tc.get("function", {}).get("arguments") or "{}")
        except Exception:
            args = {}
        plan = _normalize_plan(args)
        if plan["steps"]:
            return plan
    # 降级：未给出结构化计划
    from cyrene.llm import _assistant_text
    text = str(_assistant_text(response) or "").strip()
    return {"title": "执行计划", "summary": text[:400], "steps": []}


async def run_plan_flow(
    *,
    user_message: str,
    history: list[dict[str, Any]] | None = None,
    round_id: str,
    public_user_message: str | None = None,
    public_attachments: list[dict[str, Any]] | None = None,
    client_request_id: str = "",
    persist_user_message: bool = True,
    modification: str = "",
) -> str:
    """生成计划 → 推送「计划」tab 事件 → ask_user 三选项 → 返回 awaiting_user。"""
    import cyrene.agent.state as _state
    from cyrene.agent.state import _publish_runtime_event
    from cyrene.agent.session import (
        _append_session_message,
        _upsert_pending_question,
        get_session_labels,
    )
    from cyrene.tool_legacy import _json_result

    # 1. 持久化用户可见消息（新轮：原请求；修改：本次修改意见），让聊天里能看到
    if persist_user_message:
        visible = modification or (
            public_user_message if public_user_message is not None else user_message
        )
        user_entry: dict[str, Any] = {
            "role": "user",
            "content": str(visible),
            "message_id": f"user_{uuid4().hex}",
        }
        if round_id:
            user_entry["round_id"] = round_id
        if client_request_id:
            user_entry["client_request_id"] = client_request_id
        if public_attachments and not modification:
            user_entry["attachments"] = [dict(x) for x in public_attachments if isinstance(x, dict)]
        await _append_session_message(user_entry)

    # 2. 生成计划
    await _publish_runtime_event({
        "type": "phase_transition",
        "from": "chat", "to": "planning",
        "detail": "计划模式：正在拆解任务…",
        "round_id": round_id,
    })
    try:
        plan = await generate_plan(user_message, history, modification)
    except Exception:
        logger.warning("generate_plan failed", exc_info=True)
        plan = {"title": "计划生成失败", "summary": "无法生成计划，请重试或换种描述。", "steps": []}

    # 3. 推送 plan 事件 → 前端「计划」tab
    await _publish_runtime_event({
        "type": "plan",
        "status": "proposed",
        "plan": plan,
        "round_id": round_id,
        "client_request_id": client_request_id,
    })

    # 4. ask_user 三选项（plan_confirmation 不属于权限提权，会作为带选项的普通问题显示）
    labels = get_session_labels(round_id)
    question = await _upsert_pending_question({
        "text": (
            "我已经把这个请求拆解成了计划（见右侧「计划」标签）。\n"
            "是否同意并开始执行？如需调整，直接输入你的修改意见即可。"
        ),
        "round_id": round_id,
        "round_title": labels.get("round_title", ""),
        "client_request_id": client_request_id,
        "options": ["同意并开始", "拒绝"],
        "allow_custom": True,
        "meta": {"kind": "plan_confirmation", "plan": plan, "user_message": user_message},
    })
    # 返回 awaiting_user JSON（与 ask_user 一致）：作为工具结果时 agent loop 会暂停；
    # 从 _run_chat_agent 拦截调用时，由 coordinator 归一化为 _AWAITING_USER_SENTINEL。
    return _json_result({
        "status": "awaiting_user",
        "question_id": question.get("id", ""),
        "option_count": len(question.get("options", []) or []),
    })


__all__ = ["generate_plan", "run_plan_flow", "_plan_to_text"]
