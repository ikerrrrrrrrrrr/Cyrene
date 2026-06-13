"""Workspace-scoped memory API for the new Workbench UI.

This module is intentionally INDEPENDENT from the legacy memory page
(``/api/memory`` in ``routes.py`` + ``compiled/memory.js``), which the old
``--agent`` UI uses. It exposes a parallel set of endpoints under
``/api/workbench/memory/*`` so the two UIs never share request code.

Per-workspace isolation: every request carries a ``workspace`` query param
(the Workbench project id). It resolves to its own
``store/wb_memory_<workspace>.json`` file, so each workspace/project owns a
separate memory store. A missing/blank workspace falls back to ``default``.
Cross-workspace memory is intentionally NOT implemented yet.

Each memory item is a structured entry adapted into the rich model the
Workbench memory page shows (category / tags / source / confidence /
citations). The storage format is forward/backward compatible: extra fields
are additive and unknown fields are preserved on round-trips.
"""

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cyrene.config import STORE_DIR
from cyrene.io_utils import atomic_write_json, read_json_safe

logger = logging.getLogger(__name__)

# ── classification vocab ─────────────────────────────────────────────────
# The five memory categories surfaced in the sidebar, in display order.
_CATEGORY_LABELS: dict[str, str] = {
    "preference": "个人偏好",
    "project": "项目背景",
    "habit": "工作习惯",
    "fact": "事实信息",
    "conversation": "对话记忆",
}
_CATEGORY_ORDER = ["preference", "project", "habit", "fact", "conversation"]

# Map a legacy/free-form entry ``type`` onto a Workbench category so memories
# captured by the agent (which only tags ``fact`` / ``preference`` / …) still
# land in a sensible bucket.
_TYPE_TO_CATEGORY: dict[str, str] = {
    "preference": "preference",
    "pref": "preference",
    "fact": "fact",
    "project": "project",
    "background": "project",
    "habit": "habit",
    "routine": "habit",
    "conversation": "conversation",
    "chat": "conversation",
    "event": "conversation",
    "emotion": "conversation",
}

_SOURCE_LABELS: dict[str, str] = {
    "conversation": "对话",
    "knowledge": "知识库",
    "manual": "手动添加",
    "agent": "Agent 记录",
    "other": "其他",
}
_SOURCE_ORDER = ["conversation", "knowledge", "manual", "agent", "other"]

# Memory categories worth injecting into an agent run. "conversation" (idle
# chatter distilled from talk) is excluded — high noise, low task value.
_INJECT_CATEGORIES = {"preference", "project", "habit", "fact"}

_CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}


def _safe_workspace_id(workspace_id: str | None) -> str:
    """Sanitize a workspace id into a filesystem-safe key (defaults to 'default')."""
    raw = str(workspace_id or "").strip()
    if not raw:
        return "default"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    return cleaned or "default"


def _resolve_workspace_id(workspace_id: str | None) -> str:
    """Map a Workbench project id to its storage key when possible."""
    wid = _safe_workspace_id(workspace_id)
    try:
        from webui import routes as R

        payload = R._read_workbench_store()
        project = R._workbench_find_project(payload, str(workspace_id or "").strip())
        if project:
            return R._workbench_project_data_key(project)
    except Exception:
        pass
    return wid


def _memory_path(workspace_id: str | None) -> Path:
    """Resolve a workspace to its per-workspace memory JSON file."""
    return STORE_DIR / f"wb_memory_{_resolve_workspace_id(workspace_id)}.json"


def _load(workspace_id: str | None) -> list[dict]:
    if _resolve_workspace_id(workspace_id) == "default":
        from cyrene.short_term import load_entries

        return load_entries()
    data = read_json_safe(_memory_path(workspace_id))
    return data if isinstance(data, list) else []


def _save(workspace_id: str | None, entries: list[dict]) -> None:
    if _resolve_workspace_id(workspace_id) == "default":
        from cyrene.short_term import save_entries

        save_entries(entries)
        return
    atomic_write_json(_memory_path(workspace_id), entries)


def _today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _entry_category(entry: dict) -> str:
    cat = str(entry.get("category") or "").strip().lower()
    if cat in _CATEGORY_LABELS:
        return cat
    t = str(entry.get("type") or "").strip().lower()
    return _TYPE_TO_CATEGORY.get(t, "conversation")


def _entry_source(entry: dict) -> str:
    src = str(entry.get("source") or "").strip().lower()
    return src if src in _SOURCE_LABELS else "conversation"


def _entry_confidence(entry: dict) -> str:
    conf = str(entry.get("confidence") or "").strip().lower()
    if conf in _CONFIDENCE_LABELS:
        return conf
    # Derive from how often the memory has been reinforced — mirrors the
    # short-term retention heuristic (>=3 mentions == high confidence).
    mc = int(entry.get("mention_count") or 1)
    if mc >= 3:
        return "high"
    if mc == 2:
        return "medium"
    return "low"


def _entry_id(entry: dict) -> str:
    eid = str(entry.get("id") or "").strip()
    if eid:
        return eid
    content = str(entry.get("content") or "")
    return "mem_" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]


def _serialize(entry: dict) -> dict:
    cat = _entry_category(entry)
    src = _entry_source(entry)
    conf = _entry_confidence(entry)
    tags = entry.get("tags")
    if not isinstance(tags, list):
        tags = []
    return {
        "id": _entry_id(entry),
        "content": str(entry.get("content") or ""),
        "category": cat,
        "category_label": _CATEGORY_LABELS[cat],
        "source": src,
        "source_label": _SOURCE_LABELS[src],
        "confidence": conf,
        "confidence_label": _CONFIDENCE_LABELS[conf],
        "tags": [str(t) for t in tags],
        "citation_count": int(entry.get("mention_count") or 1),
        "created_at": str(entry.get("first_seen") or ""),
        "updated_at": str(entry.get("last_mentioned") or entry.get("first_seen") or ""),
        "citations": entry.get("citations") if isinstance(entry.get("citations"), list) else [],
        "emotional_valence": entry.get("emotional_valence", 0),
    }


def _recent_added(entries: list[dict], days: int = 7) -> int:
    now = datetime.now(timezone.utc)
    count = 0
    for e in entries:
        seen = str(e.get("first_seen") or "")
        try:
            dt = datetime.strptime(seen[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if 0 <= (now - dt).days < days:
            count += 1
    return count


def _build_payload(workspace_id: str | None) -> dict:
    """Assemble the full memory state (items + sidebar aggregates) for a workspace."""
    entries = _load(workspace_id)
    memories = [_serialize(e) for e in entries]
    memories.sort(key=lambda m: m["updated_at"], reverse=True)
    total = len(memories)

    cat_counts = {c: 0 for c in _CATEGORY_ORDER}
    src_counts = {s: 0 for s in _SOURCE_ORDER}
    for m in memories:
        cat_counts[m["category"]] = cat_counts.get(m["category"], 0) + 1
        src_counts[m["source"]] = src_counts.get(m["source"], 0) + 1

    categories = [{"id": "all", "label": "全部记忆", "count": total}]
    categories += [
        {"id": c, "label": _CATEGORY_LABELS[c], "count": cat_counts[c]}
        for c in _CATEGORY_ORDER
    ]
    sources = [
        {
            "id": s,
            "label": _SOURCE_LABELS[s],
            "count": src_counts[s],
            "pct": round(src_counts[s] / total * 100) if total else 0,
        }
        for s in _SOURCE_ORDER
    ]

    overview = {
        "total": total,
        "recent_added": _recent_added(entries),
        "total_citations": sum(m["citation_count"] for m in memories),
        "last_updated": max((m["updated_at"] for m in memories), default=""),
    }
    return {
        "memories": memories,
        "categories": categories,
        "sources": sources,
        "overview": overview,
    }


def _normalize_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = re.split(r"[,，;；\s]+", value)
    else:
        return []
    out: list[str] = []
    for t in items:
        s = str(t or "").strip()
        if s and s not in out:
            out.append(s)
    return out[:12]


# ── conversation capture (agent memory → per-workspace store) ────────────
# When the workbench agent finishes a turn, an LLM pass distills durable,
# user-specific memories from the exchange and sinks them into THIS workspace's
# store (source = "conversation"). Runs fire-and-forget so it never blocks the
# reply. This is the per-workspace equivalent of the legacy global short-term
# capture, and is the only path that feeds memories automatically.

# Hold references to in-flight capture tasks so they are not garbage-collected.
_pending_captures: set[asyncio.Task] = set()

_EXTRACT_PROMPT = """\
你是一个记忆抽取器。请从下面这一轮对话中，提取「值得长期记住的、关于用户的稳定信息」。

只提取：用户的偏好、习惯、角色/身份、稳定的事实、项目背景，或明确的长期决定。
不要提取：一次性的任务细节、寒暄客套、临时的操作请求、以及助手自己说的话。
如果没有值得长期记住的内容，就返回空列表。

每条记忆的字段：
- content: 一句话，用第二人称"你"来描述用户（例如"你偏好简洁、结构化的回答"）。简洁、自包含、不含具体某次任务的临时细节。
- category: 从这五个里选一个 —— preference（个人偏好）/ project（项目背景）/ habit（工作习惯）/ fact（事实信息）/ conversation（对话记忆）
- confidence: high / medium / low（这条信息的可靠程度）

只输出 JSON，不要解释，格式如下：
{"memories": [{"content": "...", "category": "preference", "confidence": "high"}]}

[用户]
%(user)s

[助手]
%(agent)s
"""


def _parse_json_object(text: str) -> dict:
    """Best-effort parse of an LLM response into a JSON object."""
    s = str(text or "").strip()
    if not s:
        return {}
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass
    start, end = s.find("{"), s.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(s[start:end + 1])
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _similar_entry(entries: list[dict], content: str) -> dict | None:
    """Find an existing entry whose content is (near-)identical, for dedup."""
    target = content.strip().lower()
    if not target:
        return None
    for e in entries:
        existing = str(e.get("content") or "").strip().lower()
        if not existing:
            continue
        if existing == target:
            return e
        # one side substantially contains the other → treat as the same memory
        shorter, longer = sorted((existing, target), key=len)
        if shorter and shorter in longer and len(shorter) >= len(longer) * 0.7:
            return e
    return None


async def _extract_memories_llm(user_text: str, agent_text: str) -> list[dict]:
    """Ask the LLM to distill durable memories from one exchange."""
    from cyrene.agent.state import _call_llm, _caller_type
    from cyrene.llm import _assistant_text

    prompt = _EXTRACT_PROMPT % {
        "user": user_text[:1500],
        "agent": agent_text[:1500] or "（无回复）",
    }
    token = _caller_type.set("workbench_memory")
    try:
        resp = await _call_llm([{"role": "user", "content": prompt}], tools=None, max_tokens=700)
        data = _parse_json_object(_assistant_text(resp))
    finally:
        _caller_type.reset(token)
    mems = data.get("memories") if isinstance(data, dict) else None
    return mems if isinstance(mems, list) else []


async def capture_from_exchange(workspace_id: str, user_text: str, agent_text: str) -> int:
    """Distill durable memories from one turn and merge them into the store.

    Returns the number of memories newly added (existing ones are reinforced via
    ``mention_count`` rather than duplicated). Safe to call in the background.
    """
    user_text = str(user_text or "").strip()
    agent_text = str(agent_text or "").strip()
    # Skip trivial inputs and slash-commands (those are actions, not memories).
    if len(user_text) < 4 or user_text.startswith("/"):
        return 0

    extracted = await _extract_memories_llm(user_text, agent_text)
    if not extracted:
        return 0

    entries = _load(workspace_id)
    today = _today()
    added = 0
    changed = False
    for mem in extracted:
        if not isinstance(mem, dict):
            continue
        content = str(mem.get("content") or "").strip()
        if len(content) < 4:
            continue
        category = str(mem.get("category") or "").strip().lower()
        if category not in _CATEGORY_LABELS:
            category = "conversation"
        confidence = str(mem.get("confidence") or "").strip().lower()

        dup = _similar_entry(entries, content)
        if dup is not None:
            dup["last_mentioned"] = today
            dup["mention_count"] = int(dup.get("mention_count") or 1) + 1
            changed = True
            continue

        entry: dict[str, Any] = {
            "id": "mem_" + uuid.uuid4().hex[:12],
            "content": content,
            "type": category,
            "category": category,
            "source": "conversation",
            "tags": _normalize_tags(mem.get("tags")),
            "first_seen": today,
            "last_mentioned": today,
            "mention_count": 1,
            "emotional_valence": 0,
        }
        if confidence in _CONFIDENCE_LABELS:
            entry["confidence"] = confidence
        entries.append(entry)
        added += 1
        changed = True

    if changed:
        _save(workspace_id, entries)
    return added


def schedule_capture(workspace_id: str | None, user_text: str, agent_text: str) -> None:
    """Fire-and-forget :func:`capture_from_exchange` so it never blocks a reply."""
    wid = _resolve_workspace_id(workspace_id)

    async def _runner() -> None:
        try:
            count = await capture_from_exchange(wid, user_text, agent_text)
            if count:
                logger.info("Workbench memory: captured %d memory(ies) for %s", count, wid)
        except Exception:  # noqa: BLE001
            logger.debug("Workbench memory capture failed for %s", wid, exc_info=True)

    try:
        task = asyncio.create_task(_runner())
    except RuntimeError:
        # No running event loop (e.g. called from sync context) — skip silently.
        return
    _pending_captures.add(task)
    task.add_done_callback(_pending_captures.discard)


def add_agent_memory(
    workspace_id: str | None,
    content: str,
    *,
    category: str = "fact",
    tags: Any = None,
    confidence: str = "",
    source: str = "agent",
) -> dict | None:
    """Append one durable memory written by the task agent into the project store.

    Reuses the same store + dedup as conversation capture so agent-written items
    show up on the Workbench memory page AND feed back into future runs. Returns
    the serialized entry, or ``None`` when skipped (blank/too short, or a
    non-Workbench session that resolves to the global ``default`` store — which
    aliases short-term memory and must never be written here).
    """
    content = str(content or "").strip()
    if len(content) < 4:
        return None
    if _resolve_workspace_id(workspace_id) == "default":
        return None
    category = str(category or "").strip().lower()
    if category not in _CATEGORY_LABELS:
        category = "fact"
    entries = _load(workspace_id)
    today = _today()
    dup = _similar_entry(entries, content)
    if dup is not None:
        # Reinforce an existing memory rather than duplicating it.
        dup["last_mentioned"] = today
        dup["mention_count"] = int(dup.get("mention_count") or 1) + 1
        _save(workspace_id, entries)
        return _serialize(dup)
    entry: dict[str, Any] = {
        "id": "mem_" + uuid.uuid4().hex[:12],
        "content": content,
        "type": category,
        "category": category,
        "source": source if source in _SOURCE_LABELS else "agent",
        "tags": _normalize_tags(tags),
        "first_seen": today,
        "last_mentioned": today,
        "mention_count": 1,
        "emotional_valence": 0,
    }
    conf = str(confidence or "").strip().lower()
    if conf in _CONFIDENCE_LABELS:
        entry["confidence"] = conf
    entries.append(entry)
    _save(workspace_id, entries)
    return _serialize(entry)


def render_memory_for_injection(
    workspace_id: str | None,
    *,
    limit: int = 20,
    max_chars: int = 2000,
) -> str:
    """Render a project's durable memories as a compact prompt block for a run.

    Cache note: callers inject this via ``ephemeral_system`` (prompt tail), so it
    never invalidates the cached system+history prefix. ``conversation`` memories
    are skipped (noise); strongest (most reinforced, then most recent) first.
    Returns "" when there is nothing worth injecting.
    """
    if _resolve_workspace_id(workspace_id) == "default":
        return ""
    entries = _load(workspace_id)
    if not entries:
        return ""
    items: list[tuple[int, str, str, str]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        cat = _entry_category(e)
        if cat not in _INJECT_CATEGORIES:
            continue
        content = str(e.get("content") or "").strip()
        if not content:
            continue
        mc = int(e.get("mention_count") or 1)
        ts = str(e.get("last_mentioned") or e.get("first_seen") or "")
        items.append((mc, ts, cat, content))
    if not items:
        return ""
    items.sort(key=lambda x: (x[0], x[1]), reverse=True)
    lines: list[str] = []
    used = 0
    for _mc, _ts, cat, content in items[:limit]:
        line = f"- [{_CATEGORY_LABELS.get(cat, cat)}] {content}"
        if lines and used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    header = "## 项目记忆（本项目此前沉淀/记录的长期信息，执行时请参考复用、避免重复摸索；与当前任务无关则忽略）"
    return header + "\n" + "\n".join(lines)


def register_workbench_memory_routes(router: APIRouter) -> None:
    """Register workspace-scoped memory routes for the Workbench UI."""

    @router.get("/api/workbench/memory")
    async def wb_list_memory(workspace: str = "default"):
        try:
            return _build_payload(workspace)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"List failed: {e}"}, status_code=400)

    @router.post("/api/workbench/memory")
    async def wb_create_memory(request: Request, workspace: str = "default"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        content = str(body.get("content") or "").strip()
        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)

        category = str(body.get("category") or "").strip().lower()
        if category not in _CATEGORY_LABELS:
            category = "fact"
        source = str(body.get("source") or "manual").strip().lower()
        if source not in _SOURCE_LABELS:
            source = "manual"
        confidence = str(body.get("confidence") or "").strip().lower()

        today = _today()
        entry: dict[str, Any] = {
            "id": "mem_" + uuid.uuid4().hex[:12],
            "content": content,
            # Keep ``type`` in sync with category for any legacy reader.
            "type": category,
            "category": category,
            "source": source,
            "tags": _normalize_tags(body.get("tags")),
            "first_seen": today,
            "last_mentioned": today,
            "mention_count": 1,
            "emotional_valence": 0,
        }
        if confidence in _CONFIDENCE_LABELS:
            entry["confidence"] = confidence

        try:
            entries = _load(workspace)
            entries.append(entry)
            _save(workspace, entries)
            payload = _build_payload(workspace)
            payload["id"] = entry["id"]
            return payload
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"Create failed: {e}"}, status_code=400)

    @router.patch("/api/workbench/memory/{mem_id}")
    async def wb_update_memory(mem_id: str, request: Request, workspace: str = "default"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        try:
            entries = _load(workspace)
            target = None
            for e in entries:
                if _entry_id(e) == mem_id:
                    target = e
                    break
            if target is None:
                return JSONResponse({"error": "memory not found"}, status_code=404)

            # Persist the resolved id so future edits stay stable even after the
            # content (and thus its content-hash fallback id) changes.
            target["id"] = mem_id

            if "content" in body:
                content = str(body.get("content") or "").strip()
                if not content:
                    return JSONResponse({"error": "content cannot be empty"}, status_code=400)
                target["content"] = content
            if "category" in body:
                cat = str(body.get("category") or "").strip().lower()
                if cat in _CATEGORY_LABELS:
                    target["category"] = cat
                    target["type"] = cat
            if "source" in body:
                src = str(body.get("source") or "").strip().lower()
                if src in _SOURCE_LABELS:
                    target["source"] = src
            if "confidence" in body:
                conf = str(body.get("confidence") or "").strip().lower()
                if conf in _CONFIDENCE_LABELS:
                    target["confidence"] = conf
                else:
                    target.pop("confidence", None)
            if "tags" in body:
                target["tags"] = _normalize_tags(body.get("tags"))
            # An edit counts as a fresh touch — drives the "更新时间".
            target["last_mentioned"] = _today()

            _save(workspace, entries)
            return _build_payload(workspace)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"Update failed: {e}"}, status_code=400)

    @router.delete("/api/workbench/memory/{mem_id}")
    async def wb_delete_memory(mem_id: str, workspace: str = "default"):
        try:
            entries = _load(workspace)
            kept = [e for e in entries if _entry_id(e) != mem_id]
            if len(kept) == len(entries):
                return JSONResponse({"error": "memory not found"}, status_code=404)
            _save(workspace, kept)
            return _build_payload(workspace)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"Delete failed: {e}"}, status_code=400)
