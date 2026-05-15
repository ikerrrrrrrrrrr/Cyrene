"""Route handlers for the Cyrene Web UI (SPA backend)."""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from cyrene.agent import clear_session_id, run_agent
from cyrene.config import (
    ASSISTANT_NAME,
    DB_PATH,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    SOUL_PATH,
    STATE_FILE,
    WORKSPACE_DIR,
)
from cyrene.conversations import CONVERSATIONS_DIR, archive_exchange
from cyrene.scheduler import reset_lottery
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
        if not message:
            return JSONResponse({"error": "empty message"}, status_code=400)

        reset_lottery()
        response = await run_agent(message, _bot, _CHAT_ID, _db_path)
        await archive_exchange(message, response, _CHAT_ID)
        return {"response": response}

    @router.get("/api/chat/history")
    async def api_chat_history():
        return {"messages": _load_messages()}

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
    import os
    name = os.environ.get("USER") or os.environ.get("USERNAME") or "you"
    handle = name.lower().replace(" ", "")
    initials = "".join(p[0].upper() for p in name.split()[:2]) or name[:2].upper()
    return {"name": name, "handle": handle, "initials": initials}


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
    archive_sessions = _build_archive_sessions()
    sessions.extend(archive_sessions)

    return sessions


def _build_current_session() -> dict | None:
    """Build a session object from state.json + live subagents.

    Always returns a run_live entry — when state.json is missing or empty,
    returns an empty placeholder so the Chat page shows a clean "start a new
    conversation" view instead of falling back to an old archive.
    """
    raw_msgs: list[dict] = []
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            raw_msgs = data.get("messages", []) or []
        except Exception:
            raw_msgs = []

    messages = _convert_messages(raw_msgs) if raw_msgs else []

    from cyrene.subagent import _registry  # noqa: WPS437
    subagents = []
    for agent_id, info in _registry.items():
        status = info.get("status", "running")
        ui_status = {"running": "running", "waiting": "queued", "resumed": "running",
                     "done": "done", "timeout": "err"}.get(status, status)
        subagents.append({
            "id": agent_id,
            "name": agent_id,
            "status": ui_status,
            "task": info.get("task", ""),
            "tokens": 0,
            "elapsed": "—",
            "progress": 1.0 if status == "done" else 0.5,
        })

    started_at = datetime.fromtimestamp(_SERVER_STARTED_AT, tz=timezone.utc).strftime("%H:%M")
    duration = _format_duration(time.time() - _SERVER_STARTED_AT)
    last_msg = messages[-1] if messages else None

    is_empty = not messages
    if subagents and any(s["status"] == "running" for s in subagents):
        live_status = "running"
    elif is_empty:
        live_status = "queued"  # nothing happening yet — fresh session
    else:
        live_status = "done"

    return {
        "id": "run_live",
        "title": "new session" if is_empty else "current session",
        "status": live_status,
        "started": started_at,
        "dur": duration,
        "preview": (last_msg["body"][:80] + "…") if last_msg and last_msg.get("body") else "—",
        "model": OPENAI_MODEL,
        "summary": {"tokens": "—", "spend": "—", "toolCalls": _count_tool_calls(raw_msgs)},
        "chat": {
            "contextChips": [
                {"icon": "🧠", "label": "SOUL.md"},
                {"icon": "📁", "label": "workspace"},
            ],
            "messages": messages,
        },
        "shells": [],
        "subagents": subagents,
        "flow": _build_live_flow(messages, subagents),
    }


def _build_archive_sessions() -> list[dict]:
    """Build session entries from conversation archives (one per day)."""
    if not CONVERSATIONS_DIR.exists():
        return []

    sessions = []
    files = sorted(CONVERSATIONS_DIR.glob("*.md"), reverse=True)
    for filepath in files[:10]:  # cap at 10 most recent days
        date_str = filepath.stem
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception:
            continue
        messages = _parse_archive_file(content)
        if not messages:
            continue

        last_user = next((m for m in messages if m["role"] == "user"), None)
        title = (last_user["body"][:60] + ("…" if len(last_user["body"]) > 60 else "")) if last_user else date_str
        preview = messages[-1].get("body", "")[:80] if messages else ""

        sessions.append({
            "id": f"day_{date_str}",
            "title": title,
            "status": "done",
            "started": date_str,
            "dur": "—",
            "preview": preview,
            "model": OPENAI_MODEL,
            "summary": {
                "tokens": f"{len(messages)} msgs",
                "spend": "—",
                "toolCalls": 0,
            },
            "chat": {
                "contextChips": [{"icon": "📅", "label": date_str}],
                "messages": messages,
            },
            "shells": [],
            "subagents": [],
            "flow": _build_simple_flow(messages),
        })
    return sessions


def _parse_archive_file(content: str) -> list[dict]:
    """Parse a conversations/YYYY-MM-DD.md file into UI-formatted messages."""
    messages: list[dict] = []
    pattern = re.compile(
        r"##\s*(\S+\s+UTC)\s*\n+\*\*User\*\*:\s*(.*?)\n+\*\*[^*]+\*\*:\s*(.*?)(?=\n+---|\Z)",
        re.DOTALL,
    )
    for idx, m in enumerate(pattern.finditer(content)):
        ts, user_body, assistant_body = m.group(1), m.group(2).strip(), m.group(3).strip()
        messages.append({
            "id": f"m{idx}u",
            "role": "user",
            "time": ts,
            "body": user_body,
        })
        messages.append({
            "id": f"m{idx}a",
            "role": "agent",
            "time": ts,
            "body": assistant_body,
        })
    return messages


def _convert_messages(raw_msgs: list[dict]) -> list[dict]:
    """Convert state.json raw messages → UI message format."""
    out = []
    for i, m in enumerate(raw_msgs):
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        ui_role = "user" if role == "user" else "agent"
        ui_msg = {"id": f"m{i}", "role": ui_role, "time": "—", "body": content}
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
    """Minimal flow for archive sessions — just user + agent + output."""
    last_user = next((m for m in messages if m["role"] == "user"), None)
    last_agent = next((m for m in reversed(messages) if m["role"] == "agent"), None)
    nodes = [
        {
            "id": "n_user", "kind": "input", "x": 40, "y": 80,
            "title": "user request", "status": "done",
            "detail": {
                "role": "User",
                "text": last_user["body"] if last_user else "",
                "tokens": 0,
                "time": last_user["time"] if last_user else "—",
            },
        },
        {
            "id": "n_main", "kind": "main", "x": 320, "y": 70,
            "title": f"main agent · {ASSISTANT_NAME}",
            "subtitle": "session",
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
            "id": "n_out", "kind": "output", "x": 660, "y": 90,
            "title": "response", "status": "done",
            "detail": {
                "kind": "Output",
                "content": (last_agent["body"][:600] if last_agent else "—"),
            },
        },
    ]
    edges = [
        {"from": "n_user", "to": "n_main"},
        {"from": "n_main", "to": "n_out"},
    ]
    return {"nodes": nodes, "edges": edges}


def _build_live_flow(messages: list[dict], subagents: list[dict]) -> dict:
    """Build a richer flow for the current session, including subagents."""
    last_user = next((m for m in messages if m["role"] == "user"), None)
    nodes = [
        {
            "id": "n_user", "kind": "input", "x": 40, "y": 80,
            "title": "user request", "status": "done",
            "detail": {
                "role": "User",
                "text": last_user["body"] if last_user else "—",
                "tokens": 0,
                "time": last_user["time"] if last_user else "—",
            },
        },
        {
            "id": "n_main", "kind": "main", "x": 320, "y": 70,
            "title": f"main agent · {ASSISTANT_NAME}",
            "subtitle": "orchestrator",
            "status": "running" if subagents else "done",
            "model": OPENAI_MODEL,
            "detail": {
                "systemPrompt": (
                    f"You are {ASSISTANT_NAME}. Two-phase loop: lightweight tool decision, "
                    "then full tool loop with subagent spawn. Chat filter applies SOUL.md voice."
                ),
                "reasoning": "Live session — see chat for current turn.",
                "tokensIn": 0, "tokensOut": 0,
                "model": OPENAI_MODEL, "temp": 0.2,
            },
        },
    ]
    edges = [{"from": "n_user", "to": "n_main", "kind": "active"}]

    # Add subagents
    for i, sa in enumerate(subagents):
        nid = f"n_sa_{i}"
        nodes.append({
            "id": nid, "kind": "subagent",
            "x": 660, "y": 40 + i * 140,
            "title": f"subagent · {sa['name']}",
            "subtitle": sa["task"][:30],
            "status": sa["status"],
            "detail": {
                "name": sa["name"],
                "task": sa["task"],
                "parent": "main agent",
                "spawnedAt": "—",
                "tokensIn": 0,
                "tokensOut": 0,
                "model": OPENAI_MODEL,
            },
        })
        edges.append({
            "from": "n_main", "to": nid,
            "kind": "active" if sa["status"] == "running" else None,
        })

    return {"nodes": nodes, "edges": edges}


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

    workers = [{
        "id": "main", "role": "orchestrator", "status": "running",
        "host": "local", "uptime": _format_duration(time.time() - _SERVER_STARTED_AT),
        "tokens": "—", "spend": "—",
    }]
    for aid, info in _registry.items():
        workers.append({
            "id": aid,
            "role": "subagent",
            "status": info.get("status", "running"),
            "host": "local",
            "uptime": "—",
            "tokens": "—",
            "spend": "—",
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
    archive_msgs = _parse_conversation_archive()
    if archive_msgs:
        return archive_msgs
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        msgs = data.get("messages", [])
    except Exception:
        return []
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
