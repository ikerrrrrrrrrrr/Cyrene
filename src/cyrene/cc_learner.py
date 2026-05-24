"""Learns lightweight user patterns from Claude Code transcript JSONL files."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cyrene.short_term import touch_entry
from cyrene.soul import apply_soul_update, read_soul

logger = logging.getLogger(__name__)

_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")


def analyze_session(jsonl_path: Path) -> dict[str, Any]:
    """Return a non-mutating analysis snapshot for one Claude transcript."""
    entries = _read_jsonl(jsonl_path)
    tools = extract_tool_usage_pattern(entries)
    style = analyze_user_messages(entries)
    cadence = analyze_user_cadence(entries)
    corrections = analyze_correction_pattern(entries)
    summary = summarize_learning(
        {
            "tools": tools,
            "style": style,
            "cadence": cadence,
            "corrections": corrections,
        }
    )
    return {
        "jsonl_path": str(jsonl_path),
        "tools": tools,
        "style": style,
        "cadence": cadence,
        "corrections": corrections,
        "summary": summary,
    }


def learn_from_session(jsonl_path: Path) -> dict[str, Any]:
    """Analyze a transcript, persist compact preferences, and return the snapshot."""
    result = analyze_session(jsonl_path)
    _persist_short_term_patterns(result)
    _persist_soul_patterns(result)
    result["persisted"] = True
    return result


def extract_tool_usage_pattern(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Collect common tool-use sequences after each user request."""
    tools_after_user: list[list[str]] = []
    current_tools: list[str] = []

    for entry in entries:
        if _extract_user_text(entry):
            if current_tools:
                tools_after_user.append(current_tools)
            current_tools = []

        message = entry.get("message", {})
        if message.get("role") != "assistant":
            continue

        for block in _content_blocks(message.get("content")):
            if block.get("type") == "tool_use":
                name = str(block.get("name") or "").strip()
                if name:
                    current_tools.append(name)

    if current_tools:
        tools_after_user.append(current_tools)

    all_tools = [tool for group in tools_after_user for tool in group]
    tool_counts = Counter(all_tools)
    transitions = Counter()
    for group in tools_after_user:
        for index in range(len(group) - 1):
            transitions[f"{group[index]} -> {group[index + 1]}"] += 1

    return {
        "total_exchanges": len(tools_after_user),
        "top_tools": tool_counts.most_common(5),
        "common_sequences": transitions.most_common(5),
        "avg_tools_per_request": round((len(all_tools) / len(tools_after_user)) if tools_after_user else 0, 2),
    }


def analyze_user_messages(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract user-authored prompts and infer a few stylistic preferences."""
    messages = _user_messages(entries)
    if not messages:
        return {
            "message_count": 0,
            "avg_length": 0,
            "chinese_ratio": 0,
            "directive_count": 0,
            "question_count": 0,
            "top_starting_words": [],
            "common_tasks": [],
        }

    avg_length = round(sum(len(message) for message in messages) / len(messages))
    chinese_count = sum(1 for message in messages if _CHINESE_RE.search(message))
    starting_words = Counter()
    directive_count = 0
    question_count = 0

    for message in messages:
        first = _first_token(message)
        if first:
            starting_words[first] += 1
        if message.strip().endswith(("?", "？")):
            question_count += 1
        else:
            directive_count += 1

    return {
        "message_count": len(messages),
        "avg_length": avg_length,
        "chinese_ratio": round(chinese_count / len(messages), 2),
        "directive_count": directive_count,
        "question_count": question_count,
        "top_starting_words": starting_words.most_common(8),
        "common_tasks": _classify_tasks(messages),
    }


def analyze_user_cadence(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimate the user's operating rhythm from prompt timestamps."""
    timestamps: list[datetime] = []
    for entry in entries:
        if entry.get("type") != "user":
            continue
        if not _extract_user_text(entry):
            continue
        dt = _parse_timestamp(entry.get("timestamp"))
        if dt is not None:
            timestamps.append(dt)

    if len(timestamps) < 2:
        return {"avg_gap_seconds": 0, "medianish_gap_seconds": 0}

    gaps: list[int] = []
    for index in range(1, len(timestamps)):
        gaps.append(max(0, int((timestamps[index] - timestamps[index - 1]).total_seconds())))
    if not gaps:
        return {"avg_gap_seconds": 0, "medianish_gap_seconds": 0}
    ordered = sorted(gaps)
    mid = ordered[len(ordered) // 2]
    return {
        "avg_gap_seconds": round(sum(gaps) / len(gaps)),
        "medianish_gap_seconds": mid,
    }


def analyze_correction_pattern(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Look for messages that appear to correct or redirect Claude Code."""
    correction_terms = ("不对", "不行", "错", "重来", "改一下", "重新", "fix", "wrong", "retry", "instead")
    correction_hits = 0
    total = 0
    for message in _user_messages(entries):
        lowered = message.lower()
        total += 1
        if any(term in lowered for term in correction_terms):
            correction_hits += 1
    return {
        "correction_count": correction_hits,
        "correction_ratio": round((correction_hits / total) if total else 0, 2),
    }


def summarize_learning(result: dict[str, Any]) -> dict[str, Any]:
    """Build a concise UI summary from analysis output."""
    tools = result.get("tools", {})
    style = result.get("style", {})
    cadence = result.get("cadence", {})
    corrections = result.get("corrections", {})

    highlights: list[str] = []
    if style.get("chinese_ratio", 0) >= 0.5:
        highlights.append("偏好用中文和 Claude Code 交流")
    elif style.get("message_count", 0) > 0:
        highlights.append("偏好用英文或中英混合交流")

    if style.get("directive_count", 0) > style.get("question_count", 0):
        highlights.append("偏好直接指令式表达")
    elif style.get("question_count", 0) > 0:
        highlights.append("经常用问题式表达来引导任务")

    top_tools = [item[0] for item in tools.get("top_tools", [])[:3] if item and item[0]]
    if top_tools:
        highlights.append("常用工具顺序偏好: " + " / ".join(top_tools))

    top_tasks = [item[0] for item in style.get("common_tasks", [])[:2] if item and item[0]]
    if top_tasks:
        highlights.append("高频任务类型: " + "、".join(top_tasks))

    avg_gap = int(cadence.get("avg_gap_seconds") or 0)
    if avg_gap:
        highlights.append(f"平均操作节奏约 {avg_gap} 秒一轮输入")

    if float(corrections.get("correction_ratio") or 0) >= 0.25:
        highlights.append("会较频繁地纠偏和重定向 Claude Code")

    return {
        "highlights": highlights[:5],
        "top_tools": top_tools,
        "top_tasks": top_tasks,
    }


def _persist_short_term_patterns(result: dict[str, Any]) -> None:
    tools = result.get("tools", {})
    style = result.get("style", {})
    summary = result.get("summary", {})

    top_tools = tools.get("top_tools", [])
    if top_tools:
        tool_name = str(top_tools[0][0])
        touch_entry(
            f"user-cc-top-tool-{tool_name}",
            {
                "content": f"用户在 Claude Code 中最常用 {tool_name}",
                "type": "pattern",
                "emotional_valence": 0,
            },
        )

    top_tasks = summary.get("top_tasks", [])
    if top_tasks:
        touch_entry(
            f"user-cc-top-task-{top_tasks[0]}",
            {
                "content": f"用户在 Claude Code 里常做 {top_tasks[0]} 类任务",
                "type": "pattern",
                "emotional_valence": 0,
            },
        )

    if style.get("chinese_ratio", 0) >= 0.5:
        touch_entry(
            "user-prefers-chinese-cc",
            {
                "content": "用户在 Claude Code 中偏好用中文提问",
                "type": "preference",
                "emotional_valence": 0,
            },
        )

    if style.get("directive_count", 0) > style.get("question_count", 0):
        touch_entry(
            "user-prefers-directive-cc",
            {
                "content": "用户在 Claude Code 中偏好直接指令式表达",
                "type": "preference",
                "emotional_valence": 0,
            },
        )


def _persist_soul_patterns(result: dict[str, Any]) -> None:
    summary = result.get("summary", {})
    highlights = [str(item).strip() for item in summary.get("highlights", []) if str(item).strip()]
    if not highlights:
        return

    existing = read_soul()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for highlight in highlights:
        if highlight in existing:
            continue
        apply_soul_update(f"APPEND PATTERN:USER :: ({today}) {highlight}")


def _classify_tasks(messages: list[str]) -> list[tuple[str, int]]:
    categories = {
        "代码重构": ("重构", "refactor", "提取", "拆", "整理"),
        "修 bug": ("bug", "fix", "报错", "error", "修", "错"),
        "写新功能": ("新增", "添加", "实现", "写", "创建", "add"),
        "代码理解": ("解释", "看看", "查", "是什么", "怎么", "explain"),
        "测试": ("测试", "test", "spec", "用例"),
        "配置/部署": ("部署", "deploy", "配置", "config", ".env"),
    }
    counts: Counter[str] = Counter()
    for message in messages:
        lowered = message.lower()
        for category, keywords in categories.items():
            if any(keyword in lowered for keyword in keywords):
                counts[category] += 1
    return counts.most_common(5)


def _user_messages(entries: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for entry in entries:
        text = _extract_user_text(entry)
        if text:
            messages.append(text)
    return messages


def _extract_user_text(entry: dict[str, Any]) -> str:
    if entry.get("type") != "user":
        return ""
    message = entry.get("message", {})
    if message.get("role") != "user":
        return ""

    texts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        cleaned = content.strip()
        if cleaned:
            texts.append(cleaned)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = str(block.get("text") or "").strip()
            if text:
                texts.append(text)
    return "\n".join(texts).strip()


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _first_token(message: str) -> str:
    first = str(message).strip().split(maxsplit=1)[0] if str(message).strip() else ""
    return first.rstrip("，。：:,.!?！？")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    entries.append(data)
    except Exception:
        logger.exception("Failed reading Claude transcript: %s", path)
    return entries


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None
