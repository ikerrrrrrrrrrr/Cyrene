"""自动模式：审核 agent —— 对主 agent 的提权请求自主裁决，从不打扰用户。

由 ``tool_legacy._request_scope_elevation`` 在 ``_permission_mode == "auto"`` 时调用。
裁决倾向：与用户请求一致且非破坏性 → 批准；高风险（递归删除、写系统目录、
workspace 之外、命令替换等无法静态验证的 shell）→ 拒绝并给出安全建议。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_REVIEW_TOOL_DEFS = [{
    "type": "function",
    "function": {
        "name": "decide",
        "description": "对该提权请求做出裁决。必须调用且只调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "approve": {"type": "boolean", "description": "是否批准此操作"},
                "rationale": {"type": "string", "description": "一句话理由（中文）。如拒绝，简述风险与更安全的替代做法。"},
            },
            "required": ["approve", "rationale"],
        },
    },
}]

_SYSTEM = (
    "你是 Cyrene 的安全审核员。主 agent 正在为用户完成任务，碰到了一个超出默认安全范围的操作，"
    "需要你裁决是否放行。你必须自主决定，绝不能把问题抛回给用户。\n\n"
    "放行原则：\n"
    "- 操作明显服务于用户的请求、且非破坏性 → approve。\n"
    "- 高风险操作要谨慎 deny：删除多个文件 / 递归删除（rm -rf）、写入系统目录或 workspace 之外的敏感位置、"
    "包含命令替换（$() 或反引号）等无法静态验证路径的 shell、可能造成不可逆后果的命令。\n"
    "- 当 deny 时，请在 rationale 里简述风险，主 agent 会据此改用更安全的做法。\n"
    "只调用 decide 工具返回结果。"
)


async def review_elevation(
    *,
    tool_name: str,
    operation: str,
    path_hint: str = "",
    reason: str = "",
) -> tuple[bool, str]:
    """返回 (approved, rationale)。LLM 失败或未给裁决时，出于安全默认拒绝。"""
    import cyrene.agent.state as _state

    user_request = str(_state._active_main_round_public_prompt or "").strip()
    parts = [f"工具：{tool_name}", f"操作：{operation}"]
    if path_hint:
        parts.append(f"目标路径：{path_hint}")
    if reason:
        parts.append(f"原因：{reason}")
    if user_request:
        parts.append(f"\n用户的原始请求：\n{user_request[:1200]}")
    user_msg = "请裁决以下提权请求：\n" + "\n".join(parts)

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    try:
        response = await _state._call_llm(
            messages,
            tools=_REVIEW_TOOL_DEFS,
            max_tokens=600,
            secondary=True,
            thinking="disabled",
        )
    except Exception:
        logger.warning("auto-review LLM call failed; denying by default", exc_info=True)
        return (False, "审核 agent 调用失败，出于安全默认拒绝。")

    for tc in (response.get("tool_calls") or []):
        if str(tc.get("function", {}).get("name") or "").strip() != "decide":
            continue
        try:
            args = json.loads(tc.get("function", {}).get("arguments") or "{}")
        except Exception:
            args = {}
        approved = bool(args.get("approve"))
        rationale = str(args.get("rationale") or "").strip()
        return (approved, rationale or ("已批准。" if approved else "出于安全拒绝。"))

    logger.warning("auto-review returned no decide() call; denying by default")
    return (False, "审核 agent 未给出明确裁决，出于安全默认拒绝。")


__all__ = ["review_elevation"]
