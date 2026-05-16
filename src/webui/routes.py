"""Route handlers for the Cyrene Web UI (SPA backend)."""

import getpass
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from cyrene import debug
from cyrene.agent import clear_session_id, get_live_rounds, get_session_labels, interrupt_active_run, queue_round_guidance, run_agent
from cyrene.config import (
    ASSISTANT_NAME,
    DATA_DIR,
    DB_PATH,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    SOUL_PATH,
    STATE_FILE,
    WORKSPACE_DIR,
)
from cyrene.conversations import CONVERSATIONS_DIR, archive_exchange
from cyrene.scheduler import reset_lottery
from cyrene.shells import list_shells as list_live_shells
from cyrene.short_term import load_entries

logger = logging.getLogger(__name__)

_bot: Any = None
_db_path: str = ""
_CHAT_ID = -1

_STATIC_DIR = Path(__file__).parent / "static"
_APP_DIR = _STATIC_DIR / "app"

_SERVER_STARTED_AT = time.time()


def register_routes(app, bot: Any, db_path: str) -> None:
    global _bot, _db_path
    _bot = bot
    _db_path = db_path

    router = APIRouter()

    # ---- SPA root ----

    @router.get("/", response_class=HTMLResponse)
    async def spa_root():
        return FileResponse(_APP_DIR / "index.html")

    # ---- UI bootstrap data ----

    @router.get("/api/ui-data")
    async def api_ui_data():
        return await _build_ui_data()

    # ---- Chat API ----

    @router.post("/api/chat")
    async def api_chat(request: Request):
        body = await request.json()
        message = (body.get("message") or "").strip()
        guide_round_id = str(body.get("guide_round_id") or "").strip()
        if not message:
            return JSONResponse({"error": "empty message"}, status_code=400)

        reset_lottery()
        if guide_round_id:
            try:
                item = await queue_round_guidance(guide_round_id, message, _bot, _CHAT_ID, _db_path)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            return {
                "response": f"Queued guidance for {guide_round_id}. It will run after the current main-agent output finishes.",
                "queued": True,
                "guide_round_id": guide_round_id,
                "guide_request_id": item.get("id", ""),
            }

        response = await run_agent(message, _bot, _CHAT_ID, _db_path)
        labels = get_session_labels()
        await archive_exchange(
            message,
            response,
            _CHAT_ID,
            session_title=labels.get("session_title", ""),
            round_title=labels.get("round_title", ""),
            round_id=labels.get("round_id", ""),
        )
        return {"response": response}

    @router.get("/api/chat/history")
    async def api_chat_history():
        return {"messages": _load_messages()}

    @router.post("/api/chat/interrupt")
    async def api_interrupt_chat():
        return {"ok": True, "interrupted": interrupt_active_run()}

    @router.post("/api/chat/clear")
    async def api_clear_session():
        await clear_session_id()
        return {"ok": True}

    @router.get("/api/subagents")
    async def api_subagents():
        from cyrene.subagent import _registry  # noqa: WPS437
        items = []
        for agent_id, info in _registry.items():
            items.append({
                "id": agent_id,
                "name": agent_id,
                "task": info.get("task", ""),
                "status": info.get("status", "running"),
                "result": info.get("result", ""),
            })
        return {"subagents": items}

    @router.get("/api/rounds/live")
    async def api_live_rounds():
        return {"rounds": get_live_rounds()}

    # ---- SSE ----

    @router.get("/api/events")
    async def api_events(request: Request):
        from cyrene.debug import subscribe

        async def event_stream():
            async for event in subscribe():
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---- Sessions API ----

    @router.get("/api/sessions")
    async def api_sessions():
        return {"sessions": _build_sessions()}

    @router.post("/api/sessions")
    async def api_create_session():
        """Start a new session by clearing current state.

        Compresses the existing conversation into short-term memory first
        (handled inside clear_session_id), then wipes state.json so the
        next message starts a fresh context window.
        """
        await clear_session_id()
        return {"ok": True, "sessions": _build_sessions()}

    @router.delete("/api/sessions/{session_id}")
    async def api_delete_session(session_id: str):
        """Delete a session.

        - run_live: same as create (clear current state).
        - day_YYYY-MM-DD: deletes the corresponding archive file.
        """
        if session_id == "run_live":
            await clear_session_id()
            return {"ok": True, "sessions": _build_sessions()}

        if session_id.startswith("day_"):
            date_str = session_id[len("day_"):]
            filepath = CONVERSATIONS_DIR / f"{date_str}.md"
            if not filepath.exists():
                return JSONResponse({"error": "session not found"}, status_code=404)
            try:
                filepath.unlink()
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            return {"ok": True, "sessions": _build_sessions()}

        return JSONResponse({"error": "unknown session id"}, status_code=400)

    # ---- Status API ----

    @router.get("/api/status")
    async def api_status():
        return await _build_status()

    # ---- Skills API ----

    @router.get("/api/skills")
    async def api_skills():
        return {"skills": _build_skills()}

    # ---- Settings API ----

    @router.get("/api/settings/soul")
    async def api_get_soul():
        return {"content": _read_soul()}

    @router.put("/api/settings/soul")
    async def api_update_soul(request: Request):
        body = await request.json()
        SOUL_PATH.write_text(body.get("content", ""), encoding="utf-8")
        return {"ok": True}

    @router.get("/api/settings/config")
    async def api_get_config():
        return _build_config()

    app.include_router(router)


# ---------------------------------------------------------------------------
# UI data builders
# ---------------------------------------------------------------------------


async def _build_ui_data() -> dict:
    """Assemble the full DATA payload the SPA expects."""
    sessions = _build_sessions()
    if not sessions:
        sessions = [_empty_session()]
    return {
        "user": _build_user(),
        "assistantName": ASSISTANT_NAME,
        "sessions": sessions,
        "status": await _build_status(),
        "skills": _build_skills(),
        "settings": _build_settings_meta(),
    }


def _build_user() -> dict:
    """User identity from environment or workspace owner."""
    name = _resolve_local_username()
    handle = re.sub(r"[^a-z0-9._-]+", "", name.lower().replace(" ", "")) or "user"
    parts = [part for part in re.split(r"[\s._-]+", name) if part]
    initials = "".join(part[0].upper() for part in parts[:2]) or name[:2].upper() or "U"
    return {"name": name, "handle": handle, "initials": initials}


def _resolve_local_username() -> str:
    """Best-effort local account name for the current machine."""
    candidates = [
        os.environ.get("USER"),
        os.environ.get("USERNAME"),
        os.environ.get("LOGNAME"),
    ]
    try:
        candidates.append(getpass.getuser())
    except Exception:
        pass

    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()

    return "user"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def _build_sessions() -> list[dict]:
    """Build session list — current state.json + parsed conversation archives."""
    sessions: list[dict] = []

    # 1. Current active session from state.json
    current = _build_current_session()
    if current:
        sessions.append(current)

    # 2. Historical sessions from conversation archives (one per day, most recent first)
    skip_dates: set[str] = set()
    if current and current.get("chat", {}).get("messages"):
        skip_dates.add(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    archive_sessions = _build_archive_sessions(skip_dates=skip_dates)
    sessions.extend(archive_sessions)

    return sessions


def _build_summary(raw_msgs: list[dict]) -> dict:
    prompt, completion = _usage_totals(raw_msgs)
    return {
        "tokens": _format_tokens(prompt, completion),
        "spend": _calc_spend(prompt, completion),
        "toolCalls": _count_tool_calls(raw_msgs),
    }


def _build_current_session() -> dict | None:
    """Build a session object from state.json + live subagents.

    Always returns a run_live entry — when state.json is missing or empty,
    returns an empty placeholder so the Chat page shows a clean "start a new
    conversation" view instead of falling back to an old archive.
    """
    state: dict[str, Any] = {}
    raw_msgs: list[dict] = []
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state = loaded if isinstance(loaded, dict) else {}
            raw_msgs = state.get("messages", []) or []
        except Exception:
            raw_msgs = []
            state = {}

    messages = _convert_messages(raw_msgs) if raw_msgs else []
    current_round_id = _latest_round_id_from_messages(raw_msgs)
    current_round_title = next(
        (
            str(msg.get("round_title", "")).strip()
            for msg in reversed(raw_msgs)
            if str(msg.get("round_id", "")).strip() == current_round_id and msg.get("round_title")
        ),
        "",
    )

    from cyrene.subagent import _registry  # noqa: WPS437
    subagent_registry = _infer_subagent_entries(raw_msgs, _registry)
    subagents = []
    for agent_id, info in subagent_registry.items():
        status = info.get("status", "running")
        ui_status = {"running": "running", "waiting": "queued", "resumed": "running",
                     "done": "done", "timeout": "err"}.get(status, status)
        created_at = info.get("created_at")
        subagents.append({
            "id": agent_id,
            "name": agent_id,
            "status": ui_status,
            "task": info.get("task", ""),
            "roundId": str(info.get("round_id", "")).strip(),
            "tokens": len(info.get("messages", [])),
            "elapsed": _elapsed_since(created_at),
            "progress": _status_progress(status),
            "result": info.get("result", ""),
            "messageCount": len(info.get("messages", [])),
            "createdAt": _short_time(created_at),
            "updatedAt": _short_time(info.get("updated_at")),
        })

    subagents.sort(key=lambda item: (item.get("createdAt") == "—", item.get("createdAt"), item["name"]))
    live_rounds = get_live_rounds()

    started_at = datetime.fromtimestamp(_SERVER_STARTED_AT, tz=timezone.utc).strftime("%H:%M")
    duration = _format_duration(time.time() - _SERVER_STARTED_AT)
    last_msg = messages[-1] if messages else None

    is_empty = not messages
    if live_rounds and any(str(item.get("status", "")) == "running" for item in live_rounds):
        live_status = "running"
    elif live_rounds and any(int(item.get("pendingGuidance", 0) or 0) > 0 for item in live_rounds):
        live_status = "queued"
    elif is_empty:
        live_status = "queued"  # nothing happening yet — fresh session
    else:
        live_status = "done"

    return {
        "id": "run_live",
        "title": str(state.get("session_title", "")).strip() or ("new session" if is_empty else "current session"),
        "status": live_status,
        "started": started_at,
        "dur": duration,
        "preview": (last_msg["body"][:80] + "…") if last_msg and last_msg.get("body") else "—",
        "model": OPENAI_MODEL,
        "currentRoundId": current_round_id,
        "currentRoundTitle": current_round_title,
        "summary": _build_summary(raw_msgs),
        "chat": {
            "contextChips": [
                {"icon": "🧠", "label": "SOUL.md"},
                {"icon": "📁", "label": "workspace"},
            ],
            "messages": messages,
        },
        "liveRounds": live_rounds,
        "shells": list_live_shells(include_exited=False),
        "subagents": subagents,
        "flow": _build_live_flow(raw_msgs, messages, subagents, subagent_registry),
    }


def _build_archive_sessions(skip_dates: set[str] | None = None) -> list[dict]:
    """Build session entries from conversation archives (one per day)."""
    if not CONVERSATIONS_DIR.exists():
        return []

    sessions = []
    files = sorted(CONVERSATIONS_DIR.glob("*.md"), reverse=True)
    for filepath in files[:10]:  # cap at 10 most recent days
        date_str = filepath.stem
        if skip_dates and date_str in skip_dates:
            continue
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception:
            continue
        messages = _parse_archive_file(content)
        if not messages:
            continue

        last_user = next((m for m in messages if m["role"] == "user"), None)
        session_title = _parse_archive_session_title(content)
        title = session_title or ((last_user["body"][:60] + ("…" if len(last_user["body"]) > 60 else "")) if last_user else date_str)
        preview = messages[-1].get("body", "")[:80] if messages else ""
        current_round_id = next((str(m.get("round_id", "")).strip() for m in reversed(messages) if m.get("round_id")), "")
        current_round_title = next(
            (
                str(m.get("round_title", "")).strip()
                for m in reversed(messages)
                if str(m.get("round_id", "")).strip() == current_round_id and m.get("round_title")
            ),
            "",
        )

        sessions.append({
            "id": f"day_{date_str}",
            "title": title,
            "status": "done",
            "started": date_str,
            "dur": "—",
            "preview": preview,
            "model": OPENAI_MODEL,
            "currentRoundId": current_round_id,
            "currentRoundTitle": current_round_title,
            "summary": {
                "tokens": f"{len(messages)} msgs",
                "spend": "—",
                "toolCalls": 0,
            },
            "chat": {
                "contextChips": [{"icon": "📅", "label": date_str}],
                "messages": messages,
            },
            "liveRounds": [],
            "shells": [],
            "subagents": [],
            "flow": _build_simple_flow(messages),
        })
    return sessions


def _parse_archive_meta(section: str, key: str) -> str:
    match = re.search(rf"<!--\s*{re.escape(key)}:\s*(.*?)\s*-->", section)
    return match.group(1).strip() if match else ""


def _parse_archive_session_title(content: str) -> str:
    return _parse_archive_meta(content, "session_title")


def _parse_archive_file(content: str) -> list[dict]:
    """Parse a conversations/YYYY-MM-DD.md file into UI-formatted messages."""
    messages: list[dict] = []
    sections = re.split(r"\n---\s*\n", content)
    round_index = 0

    for section in sections:
        if "**User**:" not in section:
            continue
        ts_match = re.search(r"##\s*(\S+\s+UTC)", section)
        user_match = re.search(r"\*\*User\*\*:\s*(.*?)(?=\n+\*\*[^*]+\*\*:|\Z)", section, re.DOTALL)
        assistant_match = re.search(r"\n\*\*[^*]+\*\*:\s*(.*)\Z", section, re.DOTALL)
        if not ts_match or not user_match or not assistant_match:
            continue

        ts = ts_match.group(1).strip()
        user_body = user_match.group(1).strip()
        assistant_body = assistant_match.group(1).strip()
        round_id = _parse_archive_meta(section, "round_id") or f"archive_round_{round_index}"
        round_title = _parse_archive_meta(section, "round_title")

        messages.append({
            "id": f"m{round_index}u",
            "role": "user",
            "time": ts,
            "body": user_body,
            "round_id": round_id,
            "round_title": round_title,
        })
        messages.append({
            "id": f"m{round_index}a",
            "role": "agent",
            "time": ts,
            "body": assistant_body,
            "round_id": round_id,
            "round_title": round_title,
        })
        round_index += 1
    return messages


def _convert_messages(raw_msgs: list[dict]) -> list[dict]:
    """Convert state.json raw messages → UI message format."""
    out = []
    for i, m in enumerate(raw_msgs):
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        has_live_detail = bool(m.get("reasoning_content") or m.get("tool_calls"))
        if role == "user" and not content:
            continue
        if role == "assistant" and not content and not has_live_detail:
            continue
        ui_role = "user" if role == "user" else "agent"
        ui_msg = {"id": f"m{i}", "role": ui_role, "time": "—"}
        if content:
            ui_msg["body"] = content
        round_id = str(m.get("round_id", "")).strip()
        if round_id:
            ui_msg["roundId"] = round_id
        queued_guidance_id = str(m.get("queued_guidance_id", "")).strip()
        if queued_guidance_id:
            ui_msg["queuedGuidanceId"] = queued_guidance_id
        if m.get("reasoning_content"):
            ui_msg["thinking"] = m["reasoning_content"]
        if m.get("tool_calls"):
            tools = []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments", "")
                if isinstance(args, str) and len(args) > 80:
                    args = args[:80] + "…"
                tools.append({
                    "name": fn.get("name", "?"),
                    "arg": str(args)[:120],
                    "status": "done",
                    "out": "",
                })
            ui_msg["tools"] = tools
        out.append(ui_msg)
    return out


def _count_tool_calls(raw_msgs: list[dict]) -> int:
    return sum(len(m.get("tool_calls") or []) for m in raw_msgs)


def _build_simple_flow(messages: list[dict]) -> dict:
    """Archive flow grouped by conversation round, without live tool traces."""
    rounds: list[list[dict]] = []
    current: list[dict] = []
    current_round_id = ""

    for msg in messages:
        round_id = str(msg.get("round_id", "")).strip() or current_round_id or "archive_round_0"
        if current and round_id != current_round_id:
            rounds.append(current)
            current = []
        current.append(msg)
        current_round_id = round_id
    if current:
        rounds.append(current)

    nodes: list[dict] = []
    edges: list[dict] = []
    y_offset = 0
    multiple_rounds = len(rounds) > 1

    for round_index, round_msgs in enumerate(rounds or [messages]):
        prefix = f"r{round_index}_" if multiple_rounds else ""
        last_user = next((m for m in round_msgs if m["role"] == "user"), None)
        last_agent = next((m for m in reversed(round_msgs) if m["role"] == "agent"), None)
        round_title = next((str(m.get("round_title", "")).strip() for m in round_msgs if m.get("round_title")), "") or "user request"
        user_id = f"{prefix}n_user"
        main_id = f"{prefix}n_main"
        out_id = f"{prefix}n_out"

        nodes.extend([
            {
                "id": user_id, "kind": "input", "x": 40, "y": y_offset + 80,
                "title": round_title, "status": "done",
                "detail": {
                    "role": "User",
                    "text": last_user["body"] if last_user else "",
                    "tokens": 0,
                    "time": last_user["time"] if last_user else "—",
                },
            },
            {
                "id": main_id, "kind": "main", "x": 320, "y": y_offset + 70,
                "title": f"main agent · {ASSISTANT_NAME}",
                "subtitle": "archive",
                "status": "done",
                "model": OPENAI_MODEL,
                "detail": {
                    "systemPrompt": f"You are {ASSISTANT_NAME}, an AI companion. Use SOUL.md to maintain persona.",
                    "reasoning": "Loaded session from archive — no live reasoning trace.",
                    "tokensIn": 0, "tokensOut": 0,
                    "model": OPENAI_MODEL, "temp": 0.2,
                },
            },
            {
                "id": out_id, "kind": "output", "x": 660, "y": y_offset + 90,
                "title": "response", "status": "done",
                "detail": {
                    "kind": "Output",
                    "content": (last_agent["body"][:600] if last_agent else "—"),
                },
            },
        ])
        edges.extend([
            {"from": user_id, "to": main_id},
            {"from": main_id, "to": out_id},
        ])
        y_offset += 180

    return {"nodes": nodes, "edges": edges}


def _build_live_flow(raw_msgs: list[dict], messages: list[dict], subagents: list[dict], registry: dict[str, dict]) -> dict:
    """Build a richer flow for the current session, stacked by conversation round."""
    rounds = _split_raw_rounds(raw_msgs)
    recent_events = debug.get_recent_events(250)
    if not rounds and raw_msgs:
        rounds = [raw_msgs]
    if not rounds:
        synthetic_round = _synthetic_live_round(registry, recent_events)
        if synthetic_round:
            rounds = [synthetic_round]
    if not rounds:
        return {"nodes": [], "edges": []}

    rounds, active_round_index = _prune_flow_rounds(rounds)
    if not rounds:
        return {"nodes": [], "edges": []}

    nodes: list[dict] = []
    edges: list[dict] = []
    next_y = 0
    multiple_rounds = len(rounds) > 1

    for round_index, round_raw in enumerate(rounds):
        is_current_round = round_index == active_round_index
        round_messages = _convert_messages(round_raw)
        round_id = _latest_round_id_from_messages(round_raw)
        round_registry = _round_registry_for_flow(round_raw, registry if is_current_round else {})
        related_agents = _related_round_agent_names(set(round_registry), round_id=round_id)
        if is_current_round and subagents:
            candidate_subagents = [
                sa for sa in subagents
                if _subagent_matches_round(sa, round_id) and (not round_registry or sa["name"] in related_agents)
            ]
            for sa in candidate_subagents:
                entry = round_registry.setdefault(sa["name"], {
                    "task": sa.get("task", ""),
                    "status": "done",
                    "result": sa.get("result", ""),
                    "messages": [],
                    "created_at": None,
                    "updated_at": None,
                    "round_id": round_id,
                })
                entry["task"] = entry.get("task") or sa.get("task", "")
                entry["status"] = _registry_status_from_ui(sa.get("status", entry.get("status", "done")))
                entry["result"] = entry.get("result") or sa.get("result", "")
        if is_current_round and not round_registry and registry:
            round_registry = {
                agent_id: dict(info)
                for agent_id, info in registry.items()
                if not round_id or info.get("round_id") in ("", round_id)
            }
        round_subagents = _subagent_cards_from_registry(round_registry)
        round_recent_events = _events_for_round(recent_events, round_id) if is_current_round else []
        prefix = f"r{round_index}_" if multiple_rounds else ""
        round_nodes, round_edges, round_bottom = _build_live_flow_round(
            prefix=prefix,
            raw_msgs=round_raw,
            messages=round_messages,
            subagents=round_subagents,
            registry=round_registry,
            recent_events=round_recent_events,
            y_offset=next_y,
            round_id=round_id,
        )
        nodes.extend(round_nodes)
        edges.extend(round_edges)
        next_y = round_bottom + 180

    return {"nodes": nodes, "edges": edges}


def _synthetic_live_round(registry: dict[str, dict], recent_events: list[dict]) -> list[dict]:
    if not registry:
        return []
    round_id = next((str(info.get("round_id", "")).strip() for info in registry.values() if info.get("round_id")), "")
    latest_phase = next((e for e in reversed(recent_events) if e.get("type") == "phase_transition"), None)
    latest_llm = next((e for e in reversed(recent_events) if e.get("type") == "llm_call" and e.get("caller") == "main_agent"), None)
    prompt = (
        latest_phase.get("detail")
        if latest_phase and latest_phase.get("detail")
        else latest_llm.get("response")
        if latest_llm and latest_llm.get("response")
        else "Live round in progress"
    )
    entry: dict[str, Any] = {"role": "user", "content": prompt}
    if round_id:
        entry["round_id"] = round_id
    return [entry]


def _split_raw_rounds(raw_msgs: list[dict]) -> list[list[dict]]:
    rounds: list[list[dict]] = []
    current: list[dict] = []
    for msg in raw_msgs:
        if msg.get("round_id") and current:
            current_round_id = current[0].get("round_id")
            if current_round_id and msg.get("round_id") != current_round_id:
                rounds.append(current)
                current = []
        if msg.get("role") == "user":
            if current:
                rounds.append(current)
            current = [msg]
        elif current:
            current.append(msg)
    if current:
        rounds.append(current)
    return rounds


def _round_has_activity(raw_msgs: list[dict]) -> bool:
    return any(str(msg.get("role", "")) != "user" for msg in raw_msgs)


def _prune_flow_rounds(rounds: list[list[dict]]) -> tuple[list[list[dict]], int]:
    """Keep substantive rounds plus the latest pending user-only round.

    This prevents interrupted trailing user messages from stretching the flow
    into multiple empty rounds while still preserving the latest pending input.
    """
    if not rounds:
        return [], -1

    substantive_indices = [i for i, round_raw in enumerate(rounds) if _round_has_activity(round_raw)]
    if not substantive_indices:
        return [rounds[-1]], 0

    keep_indices = set(substantive_indices)
    latest_substantive = substantive_indices[-1]
    tail_pending = [
        i for i in range(latest_substantive + 1, len(rounds))
        if not _round_has_activity(rounds[i])
    ]
    if tail_pending:
        keep_indices.add(tail_pending[-1])

    pruned: list[list[dict]] = []
    index_map: dict[int, int] = {}
    for original_index, round_raw in enumerate(rounds):
        if original_index not in keep_indices:
            continue
        index_map[original_index] = len(pruned)
        pruned.append(round_raw)

    return pruned, index_map[latest_substantive]


def _round_registry_for_flow(raw_msgs: list[dict], live_registry: dict[str, dict]) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    round_id = _latest_round_id_from_messages(raw_msgs)
    for msg in raw_msgs:
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") != "spawn_subagent":
                continue
            args = _safe_json_loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                continue
            agent_id = str(args.get("agent_id") or "").strip()
            if not agent_id:
                continue
            live = dict(live_registry.get(agent_id, {}))
            if round_id and live.get("round_id") and live.get("round_id") != round_id:
                live = {}
            task = str(args.get("task") or live.get("task") or "")
            entries[agent_id] = {
                "task": task,
                "status": live.get("status", "done"),
                "result": live.get("result", ""),
                "messages": list(live.get("messages", [])),
                "created_at": live.get("created_at"),
                "updated_at": live.get("updated_at"),
                "round_id": round_id or live.get("round_id", ""),
            }
    return entries


def _related_round_agent_names(seed_ids: set[str], round_id: str = "") -> set[str]:
    if not seed_ids:
        return set()
    related = set(seed_ids)
    inbox_root = DATA_DIR / "inbox"
    if not inbox_root.exists():
        return related

    changed = True
    while changed:
        changed = False
        for msg_file in inbox_root.glob("*/*.json"):
            try:
                payload = json.loads(msg_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if round_id and str(payload.get("round_id", "")) != round_id:
                continue
            from_agent = str(payload.get("from", ""))
            to_agent = str(payload.get("to", ""))
            if from_agent in related or to_agent in related:
                size_before = len(related)
                if from_agent:
                    related.add(from_agent)
                if to_agent:
                    related.add(to_agent)
                changed = changed or len(related) != size_before
    return related


def _round_id_from_messages(raw_msgs: list[dict]) -> str:
    for msg in raw_msgs:
        round_id = str(msg.get("round_id", "")).strip()
        if round_id:
            return round_id
    return ""


def _latest_round_id_from_messages(raw_msgs: list[dict]) -> str:
    for msg in reversed(raw_msgs):
        round_id = str(msg.get("round_id", "")).strip()
        if round_id:
            return round_id
    return ""


def _events_for_round(recent_events: list[dict], round_id: str) -> list[dict]:
    if not round_id:
        return list(recent_events)
    return [
        event for event in recent_events
        if str(event.get("round_id", "")).strip() == round_id
    ]


def _subagent_matches_round(subagent: dict[str, Any], round_id: str) -> bool:
    if not round_id:
        return True
    subagent_round_id = str(subagent.get("roundId") or subagent.get("round_id") or "").strip()
    return not subagent_round_id or subagent_round_id == round_id


def _registry_status_from_ui(status: str) -> str:
    return {
        "running": "running",
        "queued": "waiting",
        "done": "done",
        "err": "timeout",
    }.get(status, status)


def _subagent_cards_from_registry(round_registry: dict[str, dict]) -> list[dict]:
    cards: list[dict] = []
    for agent_id, info in round_registry.items():
        status = info.get("status", "done")
        ui_status = {"running": "running", "waiting": "queued", "resumed": "running",
                     "done": "done", "timeout": "err"}.get(status, status)
        created_at = info.get("created_at")
        cards.append({
            "id": agent_id,
            "name": agent_id,
            "status": ui_status,
            "task": info.get("task", ""),
            "tokens": len(info.get("messages", [])),
            "elapsed": _elapsed_since(created_at),
            "progress": _status_progress(status),
            "result": info.get("result", ""),
            "messageCount": len(info.get("messages", [])),
            "createdAt": _short_time(created_at),
            "updatedAt": _short_time(info.get("updated_at")),
        })
    return cards


def _build_live_flow_round(
    prefix: str,
    raw_msgs: list[dict],
    messages: list[dict],
    subagents: list[dict],
    registry: dict[str, dict],
    recent_events: list[dict],
    y_offset: int,
    round_id: str,
) -> tuple[list[dict], list[dict], int]:
    main_x = 320
    main_y = y_offset + 70
    main_tool_x = 600
    subagent_x = 900
    subagent_tool_x = 1220
    output_x = 1540
    subagent_base_y = y_offset + 40
    subagent_gap_y = 220

    last_user = next((m for m in messages if m["role"] == "user"), None)
    latest_main_llm = next((e for e in reversed(recent_events) if e.get("type") == "llm_call" and e.get("caller") == "main_agent"), None)
    latest_phase = next((e for e in reversed(recent_events) if e.get("type") == "phase_transition"), None)
    latest_agent = next((m for m in reversed(messages) if m["role"] == "agent"), None)
    latest_assistant_raw = next((m for m in reversed(raw_msgs) if m.get("role") == "assistant"), None)
    round_title = next((str(m.get("round_title", "")).strip() for m in raw_msgs if m.get("round_title")), "") or "user request"
    main_tokens_in, main_tokens_out = _usage_totals(raw_msgs)
    main_tool_base_y = main_y + 150

    main_id = f"{prefix}n_main"
    user_id = f"{prefix}n_user"
    output_id = f"{prefix}n_out"

    nodes = [
        {
            "id": user_id, "kind": "input", "x": 40, "y": y_offset + 80,
            "title": round_title, "status": "done",
            "detail": {
                "role": "User",
                "text": last_user["body"] if last_user else "—",
                "tokens": 0,
                "time": last_user["time"] if last_user else "—",
            },
        },
        {
            "id": main_id, "kind": "main", "x": main_x, "y": main_y,
            "title": f"main agent · {ASSISTANT_NAME}",
            "subtitle": latest_phase["to"] if latest_phase and latest_phase.get("to") else "orchestrator",
            "status": "running" if any(sa["status"] == "running" for sa in subagents) else ("done" if latest_agent else "queued"),
            "model": OPENAI_MODEL,
            "detail": {
                "systemPrompt": (
                    f"You are {ASSISTANT_NAME}. Two-phase loop: lightweight tool decision, "
                    "then full tool loop with subagent spawn. Chat filter applies SOUL.md voice."
                ),
                "reasoning": (
                    latest_assistant_raw.get("reasoning_content")
                    if latest_assistant_raw and latest_assistant_raw.get("reasoning_content")
                    else latest_main_llm.get("response")
                    if latest_main_llm and latest_main_llm.get("response")
                    else latest_phase.get("detail")
                    if latest_phase and latest_phase.get("detail")
                    else "Session step completed."
                ),
                "tokensIn": main_tokens_in if main_tokens_in is not None else "—",
                "tokensOut": main_tokens_out if main_tokens_out is not None else "—",
                "model": OPENAI_MODEL, "temp": 0.2,
            },
        },
    ]
    edges = [{"from": user_id, "to": main_id, "kind": "active"}]

    tool_nodes, tool_edges = _build_tool_nodes_for_owner(
        owner_node_id=main_id,
        owner_title=f"main agent · {ASSISTANT_NAME}",
        owner_x=main_x,
        owner_y=main_y,
        raw_messages=raw_msgs,
        recent_events=recent_events,
        caller_prefix="main_agent",
        x=main_tool_x,
        base_y=main_tool_base_y,
    )
    nodes.extend(tool_nodes)
    edges.extend(tool_edges)

    agent_node_ids: dict[str, str] = {}
    subagent_bottoms: list[int] = []
    subagent_y = subagent_base_y
    for i, sa in enumerate(subagents):
        nid = f"{prefix}n_sa_{i}"
        agent_node_ids[sa["name"]] = nid
        info = registry.get(sa["name"], {})
        agent_messages = info.get("messages", [])
        latest_subassistant = next((m for m in reversed(agent_messages) if m.get("role") == "assistant"), None)
        sub_tokens_in, sub_tokens_out = _usage_totals(agent_messages)
        sub_tool_count = _count_tool_nodes_for_owner(
            raw_messages=agent_messages,
            recent_events=recent_events,
            caller_prefix=f"subagent_{sa['name']}",
        )
        nodes.append({
            "id": nid, "kind": "subagent",
            "x": subagent_x, "y": subagent_y,
            "title": f"subagent · {sa['name']}",
            "subtitle": sa["task"][:30],
            "status": sa["status"],
            "detail": {
                "name": sa["name"],
                "task": sa["task"],
                "parent": "main agent",
                "spawnedAt": sa.get("createdAt", "—"),
                "tokensIn": sub_tokens_in if sub_tokens_in is not None else "—",
                "tokensOut": sub_tokens_out if sub_tokens_out is not None else "—",
                "model": OPENAI_MODEL,
                "reasoning": latest_subassistant.get("reasoning_content") if latest_subassistant else "",
                "result": sa.get("result", ""),
            },
        })
        edges.append({
            "from": main_id, "to": nid,
            "kind": "active" if sa["status"] == "running" else None,
        })

        sub_nodes, sub_edges = _build_tool_nodes_for_owner(
            owner_node_id=nid,
            owner_title=f"subagent · {sa['name']}",
            owner_x=subagent_x,
            owner_y=subagent_y,
            raw_messages=agent_messages,
            recent_events=recent_events,
            caller_prefix=f"subagent_{sa['name']}",
            x=subagent_tool_x,
            base_y=subagent_y,
        )
        nodes.extend(sub_nodes)
        edges.extend(sub_edges)
        lane_height = _agent_lane_height(sub_tool_count)
        subagent_bottoms.append(subagent_y + lane_height)
        subagent_y += lane_height + subagent_gap_y

    edges.extend(_build_comm_edges(agent_node_ids, round_id=round_id))

    output_content = str(latest_agent.get("body") or "") if latest_agent else ""
    output_status = "done" if output_content else ("running" if subagents else "queued")
    if output_content or subagents:
        flow_bottom = max(subagent_bottoms) if subagent_bottoms else (main_tool_base_y + _agent_lane_height(max(1, len(tool_nodes))))
        output_y = y_offset + 90 if not subagents else max(y_offset + 90, int((main_y + flow_bottom) / 2) - 43)
        nodes.append({
            "id": output_id, "kind": "output", "x": output_x, "y": output_y,
            "title": "response", "status": output_status,
            "detail": {
                "kind": "Output",
                "content": output_content or "Waiting for subagent synthesis…",
            },
        })
        edges.append({
            "from": main_id,
            "to": output_id,
            "kind": "active" if output_status == "running" else None,
        })

    bottom = max((node["y"] + 86) for node in nodes) if nodes else y_offset
    return nodes, edges, bottom


def _empty_session() -> dict:
    """Placeholder when no real session exists yet."""
    return {
        "id": "run_empty",
        "title": "no active session",
        "status": "queued",
        "started": "—",
        "dur": "—",
        "preview": "Send a message to start a session.",
        "model": OPENAI_MODEL,
        "summary": {"tokens": "0", "spend": "$0.00", "toolCalls": 0},
        "chat": {
            "contextChips": [{"icon": "🧠", "label": "SOUL.md"}],
            "messages": [],
        },
        "liveRounds": [],
        "shells": [],
        "subagents": [],
        "flow": {
            "nodes": [
                {
                    "id": "n_main", "kind": "main", "x": 200, "y": 80,
                    "title": f"main agent · {ASSISTANT_NAME}",
                    "subtitle": "idle", "status": "queued",
                    "model": OPENAI_MODEL,
                    "detail": {
                        "systemPrompt": f"You are {ASSISTANT_NAME}.",
                        "reasoning": "Waiting for user input.",
                        "tokensIn": 0, "tokensOut": 0,
                        "model": OPENAI_MODEL, "temp": 0.2,
                    },
                }
            ],
            "edges": [],
        },
    }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


async def _build_status() -> dict:
    """Real status metrics for the Status page."""
    from cyrene import db as cy_db
    from cyrene.subagent import _registry  # noqa: WPS437

    st_entries = load_entries()
    session_msgs: list = []
    if STATE_FILE.exists():
        try:
            session_msgs = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("messages", [])
        except Exception:
            session_msgs = []
    try:
        tasks = await cy_db.get_all_tasks(_db_path)
    except Exception:
        tasks = []

    running_subagents = sum(1 for v in _registry.values() if v.get("status") == "running")
    total_subagents = len(_registry)

    main_prompt, main_completion = _usage_totals(session_msgs)
    workers = [{
        "id": "main", "role": "orchestrator", "status": "running",
        "host": "local", "uptime": _format_duration(time.time() - _SERVER_STARTED_AT),
        "tokens": _format_tokens(main_prompt, main_completion),
        "spend": _calc_spend(main_prompt, main_completion),
    }]
    for aid, info in _registry.items():
        sub_msgs = info.get("messages", [])
        sub_prompt, sub_completion = _usage_totals(sub_msgs)
        workers.append({
            "id": aid,
            "role": "subagent",
            "status": info.get("status", "running"),
            "host": "local",
            "uptime": "—",
            "tokens": _format_tokens(sub_prompt, sub_completion),
            "spend": _calc_spend(sub_prompt, sub_completion),
        })

    metrics = [
        {"label": "Subagents", "value": str(total_subagents), "unit": "",
         "sub": f"{running_subagents} running", "delta": "up" if running_subagents else None},
        {"label": "Session msgs", "value": str(len(session_msgs)), "unit": "",
         "sub": "context window", "delta": None},
        {"label": "Short-term", "value": str(len(st_entries)), "unit": "",
         "sub": "memory entries", "delta": None},
        {"label": "Scheduled", "value": str(len(tasks)), "unit": "",
         "sub": "tasks pending", "delta": None},
    ]

    spark = [max(1, len(session_msgs) + i) for i in range(20)]

    services = [
        {"name": OPENAI_BASE_URL, "status": "ok", "latency": "—", "note": OPENAI_MODEL},
        {"name": "SOUL.md", "status": "ok" if SOUL_PATH.exists() else "warn",
         "latency": "—", "note": "loaded" if SOUL_PATH.exists() else "missing"},
        {"name": "SQLite (scheduled)", "status": "ok", "latency": "—",
         "note": f"{len(tasks)} tasks"},
        {"name": "Conversations archive", "status": "ok" if CONVERSATIONS_DIR.exists() else "warn",
         "latency": "—", "note": str(len(list(CONVERSATIONS_DIR.glob("*.md")))) + " days"
         if CONVERSATIONS_DIR.exists() else "none"},
    ]

    logs = _read_recent_logs()

    return {
        "metrics": metrics,
        "sparkData": spark,
        "workers": workers,
        "logs": logs,
        "services": services,
        "model": OPENAI_MODEL,
        "base_url": OPENAI_BASE_URL,
        "short_term_entries": len(st_entries),
        "session_messages": len(session_msgs),
        "scheduled_tasks": len(tasks),
        "soul_exists": SOUL_PATH.exists(),
    }


def _read_recent_logs() -> list[dict]:
    """Read the most recent debug log file and convert to status log rows."""
    from cyrene.config import DATA_DIR
    if not DATA_DIR.exists():
        return _placeholder_logs()
    log_files = sorted(DATA_DIR.glob("debug_*.jsonl"), reverse=True)
    if not log_files:
        return _placeholder_logs()
    latest = log_files[0]
    rows: list[dict] = []
    try:
        with open(latest, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception:
        return _placeholder_logs()
    for line in lines[-40:]:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        kind = entry.get("type", "info")
        ts = entry.get("timestamp", "")[11:19]
        if kind == "llm_call":
            caller = entry.get("caller", "?")
            phase = entry.get("phase", "?")
            duration = entry.get("duration_ms", 0)
            rows.append({"t": ts, "lvl": "info", "msg": f"{caller} · {phase} · {duration}ms"})
        elif kind == "tool_call":
            caller = entry.get("caller", "?")
            tool = entry.get("tool", "?")
            rows.append({"t": ts, "lvl": "ok", "msg": f"{caller} → {tool}"})
        elif kind == "chat_filter":
            rows.append({"t": ts, "lvl": "info", "msg": "chat filter applied"})
        elif kind == "session_start":
            rows.append({"t": ts, "lvl": "info", "msg": "session started"})
    return list(reversed(rows[-20:]))


def _placeholder_logs() -> list[dict]:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    return [{"t": now, "lvl": "info", "msg": "no logs yet — start the agent with --verbose"}]


# ---------------------------------------------------------------------------
# Skills (Cyrene tools → skills)
# ---------------------------------------------------------------------------


def _build_skills() -> list[dict]:
    """Map Cyrene's actual tools to skills shown in the UI."""
    return [
        {
            "id": "filesystem", "name": "Filesystem", "icon": "▤",
            "desc": "Read, write, edit, and search files in workspace/.",
            "enabled": True, "installed": True, "hotkey": "F",
            "version": "1.0.0", "author": "Cyrene core",
            "invocations": 0, "successRate": 1.0, "avgDuration": "—", "lastUsed": "—",
            "tools": ["read", "write", "edit", "glob", "grep"],
            "prompt": "File ops are scoped to workspace/. Edit must match exactly once unless replace_all.",
            "tags": ["core", "io"],
            "recent": [],
        },
        {
            "id": "shell", "name": "Shell", "icon": "▣",
            "desc": "Execute shell commands. WARNING: hardcoded to powershell on macOS/Linux — needs fix.",
            "enabled": True, "installed": True, "hotkey": "B",
            "version": "0.9.0", "author": "Cyrene core",
            "invocations": 0, "successRate": 0.0, "avgDuration": "—", "lastUsed": "—",
            "tools": ["bash"],
            "prompt": "Run shell commands with a timeout. Currently broken on non-Windows.",
            "tags": ["core", "exec"],
            "recent": [],
        },
        {
            "id": "search", "name": "Web search", "icon": "◐",
            "desc": "Search engines via SearxNG (priority) + Google/Bing fallback.",
            "enabled": True, "installed": True, "hotkey": "S",
            "version": "1.0.0", "author": "Cyrene core",
            "invocations": 0, "successRate": 0.95, "avgDuration": "—", "lastUsed": "—",
            "tools": ["web_search", "web_fetch"],
            "prompt": "Prefer SearxNG. Falls back across engines on rate limits.",
            "tags": ["core", "web"],
            "recent": [],
        },
        {
            "id": "subagent", "name": "Sub-agents", "icon": "✸",
            "desc": "Spawn parallel agents with inbox communication. Lifecycle: running→waiting→resumed→done.",
            "enabled": True, "installed": True, "hotkey": "A",
            "version": "1.0.0", "author": "Cyrene core",
            "invocations": 0, "successRate": 1.0, "avgDuration": "—", "lastUsed": "—",
            "tools": ["spawn_subagent", "send_agent_message"],
            "prompt": "Spawn for parallelizable work. Each subagent has its own inbox.",
            "tags": ["core", "orchestration"],
            "recent": [],
        },
        {
            "id": "scheduler", "name": "Scheduler", "icon": "◷",
            "desc": "Schedule cron/interval/once tasks. Stored in SQLite. Heartbeat + lottery for proactive messages.",
            "enabled": True, "installed": True, "hotkey": "T",
            "version": "1.0.0", "author": "Cyrene core",
            "invocations": 0, "successRate": 1.0, "avgDuration": "—", "lastUsed": "—",
            "tools": ["schedule_task", "list_tasks", "pause_task", "resume_task", "cancel_task"],
            "prompt": "Schedule recurring tasks. Lottery sends proactive messages on probability tick.",
            "tags": ["core", "automation"],
            "recent": [],
        },
        {
            "id": "soul", "name": "SOUL memory", "icon": "✱",
            "desc": "Three-layer memory: context window (40) → short-term (compressed) → SOUL.md (long-term).",
            "enabled": True, "installed": True,
            "version": "1.0.0", "author": "Cyrene core",
            "invocations": 0, "successRate": 1.0, "avgDuration": "—", "lastUsed": "—",
            "tools": ["read_soul", "steward_agent"],
            "prompt": "Steward agent updates SOUL.md every 30min via APPEND/ERASE/MERGE commands.",
            "tags": ["core", "memory"],
            "recent": [],
        },
        {
            "id": "telegram", "name": "Telegram bot", "icon": "✈",
            "desc": "Optional Telegram interface. Requires TELEGRAM_BOT_TOKEN + OWNER_ID env vars.",
            "enabled": False, "installed": False,
            "version": "1.0.0", "author": "Cyrene core",
            "tools": ["send_message"], "tags": ["interface"],
        },
    ]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _build_settings_meta() -> dict:
    return {
        "sections": [
            {"id": "general", "label": "General"},
            {"id": "models", "label": "Models"},
            {"id": "agents", "label": "Agents"},
            {"id": "tools", "label": "Tools"},
            {"id": "keys", "label": "API keys"},
            {"id": "appearance", "label": "Appearance"},
            {"id": "danger", "label": "Danger zone"},
        ],
        "models": [
            {"id": "current", "name": OPENAI_MODEL, "desc": "Currently active",
             "ctx": "—", "price": "—"},
            {"id": "haiku45", "name": "claude-haiku-4-5", "desc": "Fast, capable",
             "ctx": "200k", "price": "$0.25 / $1.25"},
            {"id": "sonnet45", "name": "claude-sonnet-4-5", "desc": "Heavy reasoning",
             "ctx": "200k", "price": "$3.00 / $15.00"},
            {"id": "deepseek-chat", "name": "deepseek-chat", "desc": "DeepSeek default",
             "ctx": "64k", "price": "low"},
        ],
    }


def _build_config() -> dict:
    return {
        "model": OPENAI_MODEL,
        "base_url": OPENAI_BASE_URL,
        "assistant_name": ASSISTANT_NAME,
        "soul_path": str(SOUL_PATH),
        "workspace_dir": str(WORKSPACE_DIR),
        "soul_content": _read_soul(),
    }


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _load_messages() -> list[dict]:
    msgs = _load_state_messages()
    if msgs:
        result = []
        for m in msgs:
            role = m.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content", "")
            if not content or not content.strip():
                continue
            result.append({"role": role, "content": content})
        if result:
            return result

    archive_msgs = _parse_conversation_archive()
    if archive_msgs:
        return archive_msgs

    return []


def _load_state_messages() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("messages", []) or []
    except Exception:
        return []


def _infer_subagent_entries(raw_msgs: list[dict], registry: dict[str, dict]) -> dict[str, dict]:
    entries: dict[str, dict] = {
        agent_id: dict(info)
        for agent_id, info in registry.items()
    }
    for entry in entries.values():
        entry.setdefault("messages", [])

    spawned: dict[str, dict[str, str]] = {}
    for msg in raw_msgs:
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") != "spawn_subagent":
                continue
            args = _safe_json_loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                continue
            agent_id = str(args.get("agent_id") or "").strip()
            if not agent_id:
                continue
            spawned[agent_id] = {
                "task": str(args.get("task") or ""),
                "round_id": str(msg.get("round_id", "")).strip(),
            }

    for agent_id, meta in spawned.items():
        entry = entries.setdefault(agent_id, {})
        meta_round_id = str(meta.get("round_id", "")).strip()
        existing_round_id = str(entry.get("round_id", "")).strip()
        if meta_round_id and existing_round_id and meta_round_id != existing_round_id:
            # Treat a reused agent ID in a later round as a fresh live subagent.
            entry["task"] = meta["task"] or entry.get("task", "")
            entry["round_id"] = meta_round_id
            entry["status"] = "running"
            entry["result"] = ""
            entry["messages"] = []
            entry["created_at"] = None
            entry["updated_at"] = None
            continue
        entry.setdefault("task", meta["task"])
        entry.setdefault("round_id", meta_round_id)
        entry.setdefault("status", "done")
        entry.setdefault("result", "")
        entry.setdefault("messages", [])
        entry.setdefault("created_at", None)
        entry.setdefault("updated_at", None)

    inbox_meta = _scan_inbox_agents()
    for agent_id, meta in inbox_meta.items():
        entry = entries.setdefault(agent_id, {})
        entry.setdefault("task", spawned.get(agent_id, {}).get("task", "Discuss with other subagents"))
        entry.setdefault("status", "done")
        entry.setdefault("result", "")
        if not entry.get("messages"):
            entry["messages"] = [{}] * int(meta.get("message_count") or 0)
        if meta.get("created_at") and not entry.get("created_at"):
            entry["created_at"] = meta["created_at"]
        if meta.get("updated_at") and not entry.get("updated_at"):
            entry["updated_at"] = meta["updated_at"]
        if meta.get("round_id") and not entry.get("round_id"):
            entry["round_id"] = meta["round_id"]

    return entries
    result = []
    for m in msgs:
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content", "")
        if not content or not content.strip():
            continue
        result.append({"role": role, "content": content})
    return result


def _parse_conversation_archive() -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = CONVERSATIONS_DIR / f"{today}.md"
    if not filepath.exists():
        return []
    content = filepath.read_text(encoding="utf-8")
    messages = []
    current_user = None
    current_lines: list[str] = []
    in_assistant = False
    for line in content.split("\n"):
        if line.startswith("**User**: "):
            if current_user and current_lines:
                messages.append({"role": "user", "content": current_user})
                messages.append({"role": "assistant", "content": "\n".join(current_lines).strip()})
            current_user = line[len("**User**: "):].strip()
            current_lines = []
            in_assistant = False
        elif line.startswith("**") and "**: " in line and not line.startswith("**User**"):
            in_assistant = True
            idx = line.index("**: ")
            current_lines = [line[idx + len("**: "):]]
        elif in_assistant:
            if line.strip() == "---":
                if current_user and current_lines:
                    messages.append({"role": "user", "content": current_user})
                    messages.append({"role": "assistant", "content": "\n".join(current_lines).strip()})
                current_user = None
                current_lines = []
                in_assistant = False
            else:
                current_lines.append(line)
    if current_user and current_lines:
        messages.append({"role": "user", "content": current_user})
        messages.append({"role": "assistant", "content": "\n".join(current_lines).strip()})
    return messages


def _read_soul() -> str:
    try:
        if SOUL_PATH.exists():
            return SOUL_PATH.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m:02d}:{s:02d}"


def _status_progress(status: str) -> float:
    return {
        "running": 0.45,
        "resumed": 0.65,
        "waiting": 0.82,
        "done": 1.0,
        "timeout": 1.0,
    }.get(status, 0.5)


def _short_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")
    except Exception:
        return "—"


def _elapsed_since(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        return _format_duration((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return "—"


def _safe_json_loads(value: str) -> dict[str, Any] | list[Any] | None:
    try:
        return json.loads(value)
    except Exception:
        return None


def _summarize_text(value: str, limit: int = 96) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _tool_output_map(raw_messages: list[dict]) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for msg in raw_messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            outputs[str(msg["tool_call_id"])] = str(msg.get("content") or "")
    return outputs


def _usage_totals(raw_messages: list[dict]) -> tuple[int | None, int | None]:
    prompt_total = 0
    completion_total = 0
    found = False
    for msg in raw_messages:
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if isinstance(prompt, int):
            prompt_total += prompt
            found = True
        if isinstance(completion, int):
            completion_total += completion
            found = True
    if not found:
        return None, None
    return prompt_total, completion_total


def _format_tokens(prompt_tokens: int | None, completion_tokens: int | None) -> str:
    if prompt_tokens is None and completion_tokens is None:
        return "—"
    parts: list[str] = []
    if prompt_tokens is not None:
        parts.append(f"{_fmt_tok(prompt_tokens)} in")
    if completion_tokens is not None:
        parts.append(f"{_fmt_tok(completion_tokens)} out")
    return " / ".join(parts) if parts else "—"


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _model_pricing() -> tuple[float, float] | None:
    """Return (input_price_per_1M, output_price_per_1M) for known models, or None."""
    model_lower = OPENAI_MODEL.lower()
    if "opus-4" in model_lower or "claude-opus-4" in model_lower:
        return (15.0, 75.0)
    if "sonnet-4" in model_lower or "claude-sonnet-4" in model_lower:
        return (3.0, 15.0)
    if "haiku-4" in model_lower or "claude-haiku-4" in model_lower:
        return (0.25, 1.25)
    if "deepseek" in model_lower:
        return (0.14, 0.28)
    return None


def _calc_spend(prompt_tokens: int | None, completion_tokens: int | None) -> str:
    if prompt_tokens is None and completion_tokens is None:
        return "—"
    pricing = _model_pricing()
    if pricing is None:
        return "—"
    in_price, out_price = pricing
    cost = 0.0
    if prompt_tokens is not None:
        cost += (prompt_tokens / 1_000_000) * in_price
    if completion_tokens is not None:
        cost += (completion_tokens / 1_000_000) * out_price
    if cost < 0.01:
        return "<$0.01"
    return f"${cost:.2f}"


def _build_shells_from_messages(raw_msgs: list[dict]) -> list[dict]:
    """Extract bash/shell tool calls from raw messages and build shell entries."""
    shells: list[dict] = []
    tool_results: dict[str, str] = {}
    for msg in raw_msgs:
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            tool_results[str(msg["tool_call_id"])] = str(msg.get("content") or "")

    shell_index = 0
    for msg in raw_msgs:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            if name.lower() not in ("bash", "shell", "cmd", "terminal"):
                continue
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {}
            cmd = args.get("command") or args.get("cmd") or json.dumps(args)
            cwd = args.get("cwd") or args.get("workdir") or "workspace/"
            result = tool_results.get(str(tc.get("id")), "")
            lines: list[dict] = [
                {"kind": "shell-prompt", "text": f"$ {cmd}"},
            ]
            if result:
                for line in result.strip().split("\n")[:30]:
                    lines.append({"kind": "shell-out", "text": line})
            else:
                lines.append({"kind": "shell-out", "text": "(running…)"})

            shells.append({
                "id": f"shell_{shell_index}",
                "cwd": cwd,
                "pid": "—",
                "lines": lines,
            })
            shell_index += 1

    return shells


def _build_tool_nodes_for_owner(
    owner_node_id: str,
    owner_title: str,
    owner_x: int,
    owner_y: int,
    raw_messages: list[dict],
    recent_events: list[dict],
    caller_prefix: str,
    x: int,
    base_y: int,
) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    tool_outputs = _tool_output_map(raw_messages)
    tool_index = 0

    for msg_index, msg in enumerate(raw_messages):
        tool_calls = msg.get("tool_calls") or []
        for call_index, tc in enumerate(tool_calls):
            fn = tc.get("function", {})
            raw_args = fn.get("arguments") or "{}"
            parsed_args = _safe_json_loads(raw_args) if isinstance(raw_args, str) else raw_args
            output = tool_outputs.get(str(tc.get("id")), "")
            status = "done" if output else "running"
            nid = f"{owner_node_id}_tool_{msg_index}_{call_index}"
            nodes.append({
                "id": nid,
                "kind": "tool",
                "x": x,
                "y": base_y + tool_index * 112,
                "title": fn.get("name", "tool"),
                "subtitle": _summarize_text(str(raw_args), 36) if raw_args else "",
                "status": status,
                "detail": {
                    "name": fn.get("name", "tool"),
                    "owner": owner_title,
                    "input": parsed_args if parsed_args is not None else raw_args,
                    "output": output or "Running…",
                    "duration": "—",
                },
            })
            edges.append({
                "from": owner_node_id,
                "to": nid,
                "kind": "active" if status == "running" else None,
            })
            tool_index += 1

    overlay_events = [
        event for event in recent_events
        if event.get("type") == "tool_call" and str(event.get("caller", "")).startswith(caller_prefix)
    ][-6:]
    for event_index, event in enumerate(overlay_events):
        key = f"{event.get('tool')}::{json.dumps(event.get('args', {}), ensure_ascii=False, sort_keys=True)}"
        if any(node["detail"].get("name") == event.get("tool") and json.dumps(node["detail"].get("input", {}), ensure_ascii=False, sort_keys=True) == json.dumps(event.get("args", {}), ensure_ascii=False, sort_keys=True) for node in nodes):
            continue
        nid = f"{owner_node_id}_live_tool_{event_index}"
        nodes.append({
            "id": nid,
            "kind": "tool",
            "x": x,
            "y": base_y + tool_index * 112,
            "title": event.get("tool", "tool"),
            "subtitle": _summarize_text(json.dumps(event.get("args", {}), ensure_ascii=False), 36),
            "status": "running",
            "detail": {
                "name": event.get("tool", "tool"),
                "owner": owner_title,
                "input": event.get("args", {}),
                "output": event.get("result_preview", "Running…"),
                "duration": "live",
                "eventKey": key,
            },
        })
        edges.append({"from": owner_node_id, "to": nid, "kind": "active"})
        tool_index += 1

    return nodes, edges


def _count_tool_nodes_for_owner(
    raw_messages: list[dict],
    recent_events: list[dict],
    caller_prefix: str,
) -> int:
    count = sum(len(msg.get("tool_calls") or []) for msg in raw_messages)
    message_keys = {
        (
            tc.get("function", {}).get("name", "tool"),
            json.dumps(
                _safe_json_loads(tc.get("function", {}).get("arguments") or "{}")
                if isinstance(tc.get("function", {}).get("arguments"), str)
                else (tc.get("function", {}).get("arguments") or {}),
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        for msg in raw_messages
        for tc in (msg.get("tool_calls") or [])
    }
    overlay_events = [
        event for event in recent_events
        if event.get("type") == "tool_call" and str(event.get("caller", "")).startswith(caller_prefix)
    ][-6:]
    overlay_count = 0
    for event in overlay_events:
        event_key = (
            event.get("tool", "tool"),
            json.dumps(event.get("args", {}), ensure_ascii=False, sort_keys=True),
        )
        if event_key in message_keys:
            continue
        overlay_count += 1
    return count + overlay_count


def _agent_lane_height(tool_count: int) -> int:
    base_height = 86
    if tool_count <= 0:
        return base_height
    return max(base_height, base_height + (tool_count - 1) * 112)


def _build_comm_edges(agent_node_ids: dict[str, str], round_id: str = "") -> list[dict]:
    edges: list[dict] = []
    if not agent_node_ids:
        return edges
    seen: set[tuple[str, str, str]] = set()
    for agent_name, target_node_id in agent_node_ids.items():
        inbox_dir = DATA_DIR / "inbox" / agent_name
        if not inbox_dir.exists():
            continue
        for msg_file in sorted(inbox_dir.glob("msg_*.json")):
            try:
                payload = json.loads(msg_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            from_agent = str(payload.get("from", ""))
            to_agent = str(payload.get("to", ""))
            if round_id and str(payload.get("round_id", "")) != round_id:
                continue
            if from_agent not in agent_node_ids or to_agent not in agent_node_ids:
                continue
            edge_key = (from_agent, to_agent, str(payload.get("message_id", msg_file.stem)))
            if edge_key in seen:
                continue
            seen.add(edge_key)
            body = str(payload.get("content", ""))
            edges.append({
                "from": agent_node_ids[from_agent],
                "to": agent_node_ids[to_agent],
                "kind": "comm",
                "label": payload.get("type", "chat"),
                "message": {
                    "time": _short_time(payload.get("timestamp")),
                    "summary": _summarize_text(body, 90),
                    "body": body,
                },
            })
    return edges


def _scan_inbox_agents() -> dict[str, dict[str, Any]]:
    agents: dict[str, dict[str, Any]] = {}
    inbox_root = DATA_DIR / "inbox"
    if not inbox_root.exists():
        return agents

    for inbox_dir in sorted(path for path in inbox_root.iterdir() if path.is_dir()):
        agent_id = inbox_dir.name
        timestamps: list[str] = []
        round_ids: list[str] = []
        msg_count = 0
        for msg_file in sorted(inbox_dir.glob("msg_*.json")):
            try:
                payload = json.loads(msg_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            msg_count += 1
            timestamp = payload.get("timestamp")
            if isinstance(timestamp, str) and timestamp:
                timestamps.append(timestamp)
            round_id = str(payload.get("round_id", "")).strip()
            if round_id:
                round_ids.append(round_id)

        if msg_count == 0:
            continue

        timestamps.sort()
        agents[agent_id] = {
            "message_count": msg_count,
            "created_at": timestamps[0] if timestamps else None,
            "updated_at": timestamps[-1] if timestamps else None,
            "round_id": round_ids[-1] if round_ids else "",
        }

    return agents
