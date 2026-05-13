"""Route handlers for the Cyrene Web UI."""

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from cyrene.agent import clear_session_id, run_agent
from cyrene.config import DB_PATH, SOUL_PATH, STATE_FILE
from cyrene.conversations import archive_exchange
from cyrene.scheduler import reset_lottery
from cyrene.short_term import load_entries

logger = logging.getLogger(__name__)

_bot: Any = None
_db_path: str = ""
_templates: Jinja2Templates | None = None
_CHAT_ID = -1

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def register_routes(app, bot: Any, db_path: str) -> None:
    global _bot, _db_path, _templates
    _bot = bot
    _db_path = db_path
    _templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    router = APIRouter()

    # ---- Pages ----

    @router.get("/", response_class=HTMLResponse)
    async def chat_page(request: Request):
        return _templates.TemplateResponse(request, "chat.html", {"page": "chat"})

    @router.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        stats = await _collect_stats()
        return _templates.TemplateResponse(request, "status.html", {"page": "status", **stats})

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return _templates.TemplateResponse(
            request,
            "settings.html",
            {"page": "settings", "soul_content": _read_soul()},
        )

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

    @router.get("/api/subagents")
    async def api_subagents():
        from cyrene.subagent import get_snapshot
        return await get_snapshot()

    @router.post("/api/chat/clear")
    async def api_clear_session():
        await clear_session_id()
        return {"ok": True}

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

    # ---- Status API ----

    @router.get("/api/status")
    async def api_status():
        return await _collect_stats()

    # ---- Settings API ----

    @router.get("/api/settings/soul")
    async def api_get_soul():
        return {"content": _read_soul()}

    @router.put("/api/settings/soul")
    async def api_update_soul(request: Request):
        body = await request.json()
        SOUL_PATH.write_text(body.get("content", ""), encoding="utf-8")
        return {"ok": True}

    app.include_router(router)


# ---- Helpers ----


def _parse_conversation_archive() -> list[dict]:
    """Parse today's conversation archive file into message dicts."""
    from cyrene.conversations import CONVERSATIONS_DIR
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = CONVERSATIONS_DIR / f"{today}.md"
    if not filepath.exists():
        return []

    content = filepath.read_text(encoding="utf-8")
    messages = []
    current_user = None
    current_ape_lines = []
    in_ape = False

    for line in content.split("\n"):
        if line.startswith("**User**: "):
            if current_user and current_ape_lines:
                messages.append({"role": "user", "content": current_user})
                messages.append({"role": "assistant", "content": "\n".join(current_ape_lines).strip()})
            current_user = line[len("**User**: "):].strip()
            current_ape_lines = []
            in_ape = False
        elif line.startswith("**Ape**: "):
            in_ape = True
            current_ape_lines = [line[len("**Ape**: "):]]
        elif in_ape:
            if line.strip() == "---":
                if current_user and current_ape_lines:
                    messages.append({"role": "user", "content": current_user})
                    messages.append({"role": "assistant", "content": "\n".join(current_ape_lines).strip()})
                current_user = None
                current_ape_lines = []
                in_ape = False
            else:
                current_ape_lines.append(line)

    # Last entry if file doesn't end with ---
    if current_user and current_ape_lines:
        messages.append({"role": "user", "content": current_user})
        messages.append({"role": "assistant", "content": "\n".join(current_ape_lines).strip()})

    return messages


def _load_messages() -> list[dict]:
    # First try conversation archive (authoritative history)
    archive_msgs = _parse_conversation_archive()
    if archive_msgs:
        return archive_msgs

    # Fallback to state.json (current session)
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


def _read_soul() -> str:
    try:
        if SOUL_PATH.exists():
            return SOUL_PATH.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


async def _collect_stats() -> dict:
    from cyrene import db as cy_db
    from cyrene.config import OPENAI_BASE_URL, OPENAI_MODEL

    st_entries = load_entries()
    session_msgs = []
    if STATE_FILE.exists():
        try:
            session_msgs = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("messages", [])
        except Exception:
            pass
    try:
        tasks = await cy_db.get_all_tasks(_db_path)
    except Exception:
        tasks = []
    return {
        "model": OPENAI_MODEL,
        "base_url": OPENAI_BASE_URL,
        "short_term_entries": len(st_entries),
        "session_messages": len(session_msgs),
        "scheduled_tasks": len(tasks),
        "soul_exists": SOUL_PATH.exists(),
    }
