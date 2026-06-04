"""Prompt and rendering helpers for deep reflection packets."""

from __future__ import annotations

from typing import Any

DEEP_REFLECTION_SCHEMA = "cyrene.deep_reflection.v1"

DEEP_REFLECTION_PROMPT_V1 = """You are a clean-context context reframing worker.

You receive compressed evidence from a long-running agent attempt. Your job is not to judge tool reliability and not to summarize the whole chat. Your job is to produce a compact Deep Reflection Packet that helps the next agent step satisfy the user's actual goal without seeing the concrete failure transcript.

Rules:
- Focus on the gap between the user's goal/requirements and what the agent has achieved.
- Do not treat a single tool failure as sufficient reason for reflection.
- Preserve user requirements, explicit corrections, bad attempted paths, and useful next directions.
- Compress failed attempts aggressively. Keep what was attempted and which tools/parameters were used; do not reproduce raw tool output.
- Avoid discouraging language. Frame failed attempts as search-space constraints.
- Return strict JSON only. No markdown, no prose wrapper.

Output JSON shape:
{
  "schema": "cyrene.deep_reflection.v1",
  "objective": "string",
  "user_requirements": ["string"],
  "goal_gap": "string",
  "current_state": "string",
  "compressed_attempts": [
    {
      "attempt": "string",
      "why_bad_for_goal": "string",
      "tools": [{"name": "string", "args": {}}]
    }
  ],
  "excluded_paths": ["string"],
  "tools_used": [{"name": "string", "args": {}}],
  "promising_directions": ["string"],
  "next_step": "string",
  "open_questions": ["string"]
}"""


def render_deep_reflection_packet(packet: dict[str, Any]) -> str:
    """Render a packet as a stable LLM-visible context block."""
    def _lines(values: Any) -> list[str]:
        if not isinstance(values, list):
            values = [values] if values else []
        result: list[str] = []
        for item in values:
            text = str(item or "").strip()
            if text:
                result.append(f"- {text}")
        return result or ["- (none)"]

    attempts: list[str] = []
    for item in packet.get("compressed_attempts") or []:
        if not isinstance(item, dict):
            continue
        attempt = str(item.get("attempt") or "").strip()
        why_bad = str(item.get("why_bad_for_goal") or "").strip()
        tools = item.get("tools") if isinstance(item.get("tools"), list) else []
        tool_bits = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            args = tool.get("args")
            tool_bits.append(f"{name}({args if isinstance(args, dict) else {}})" if name else "")
        tool_text = "; tools: " + ", ".join(bit for bit in tool_bits if bit) if tool_bits else ""
        if attempt or why_bad:
            attempts.append(f"- {attempt or '(attempt)'} | why not enough: {why_bad or '(unspecified)'}{tool_text}")
    if not attempts:
        attempts = ["- (none)"]

    tools_used: list[str] = []
    for tool in packet.get("tools_used") or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        args = tool.get("args") if isinstance(tool.get("args"), dict) else {}
        tools_used.append(f"- {name}({args})")
    if not tools_used:
        tools_used = ["- (none)"]

    sections = [
        "[Deep reflection packet]",
        f"Schema: {str(packet.get('schema') or DEEP_REFLECTION_SCHEMA)}",
        "",
        "Objective:",
        str(packet.get("objective") or "(unknown)").strip(),
        "",
        "User requirements:",
        *_lines(packet.get("user_requirements")),
        "",
        "Goal gap:",
        str(packet.get("goal_gap") or "(unspecified)").strip(),
        "",
        "Current state:",
        str(packet.get("current_state") or "(unspecified)").strip(),
        "",
        "Compressed attempts that should not be repeated as-is:",
        *attempts,
        "",
        "Excluded paths:",
        *_lines(packet.get("excluded_paths")),
        "",
        "Tools used in failed/insufficient attempts:",
        *tools_used,
        "",
        "Promising directions:",
        *_lines(packet.get("promising_directions")),
        "",
        "Next step:",
        str(packet.get("next_step") or "(choose the highest-leverage next action)").strip(),
        "",
        "Open questions:",
        *_lines(packet.get("open_questions")),
    ]
    return "\n".join(sections).strip()
