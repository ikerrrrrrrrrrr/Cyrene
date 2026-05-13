"""All route handlers for the Cyrene Web UI."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from cyrene.agent import clear_session_id, run_agent
from cyrene.config import DB_PATH, SOUL_PATH, STATE_FILE
from cyrene.conversations import archive_exchange
from cyrene.scheduler import reset_lottery
from cyrene.short_term import load_entries

logger = logging.getLogger(__name__)

# Shared resources, set by register_routes()
_bot: Any = None
_db_path: str = ""
_env: Environment | None = None

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_CHAT_ID = -1  # Web UI uses a fixed chat_id to avoid Telegram collisions


def register_routes(app, bot: Any, db_path: str) -> None:
    """Attach all route handlers to the FastAPI app."""
    global _bot, _db_path, _env
    _bot = bot
    _db_path = db_path

    _env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )

    router = APIRouter()

    # ---- Pages (server-rendered HTML) ---------------------------------------

    @router.get("/", response_class=HTMLResponse)
    async def chat_page():
        """Chat page: message list + input form."""
        messages = _load_session_for_template()
        tpl = _env.get_template("chat.html")
        return tpl.render(messages=messages)

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page():
        """Settings page: SOUL.md viewer/editor + session controls."""
        soul_content = _read_soul()
        tpl = _env.get_template("settings.html")
        return tpl.render(soul_content=soul_content)

    @router.get("/status", response_class=HTMLResponse)
    async def status_page():
        """System status page."""
        stats = _collect_stats()
        tpl = _env.get_template("status.html")
        return tpl.render(**stats)

    # ---- API endpoints ------------------------------------------------------

    @router.post("/api/chat")
    async def api_chat(request: Request, message: str = Form(...)):
        """Send a message to the agent and get a response.

        When called via HTMX (``HX-Request`` header present), returns an HTML
        fragment with both the user message and assistant response bubbles.
        Otherwise returns JSON ``{"response": "..."}``.
        """
        reset_lottery()
        response = await run_agent(message, _bot, _CHAT_ID, _db_path)
        await archive_exchange(message, response, _CHAT_ID)

        if request.headers.get("HX-Request"):
            # HTMX: render message bubbles as HTML fragment
            now_str = datetime.now().strftime("%H:%M")
            user_html = _render("chat_message.html", {
                "msg": {"role": "user", "content": message, "time": now_str},
            })
            asst_html = _render("chat_message.html", {
                "msg": {"role": "assistant", "content": response, "time": now_str},
            })
            return HTMLResponse(user_html + asst_html)
        return {"response": response}

    @router.post("/api/clear-session")
    async def api_clear_session():
        """Clear the conversation session and redirect to chat page."""
        await clear_session_id()
        return Response(status_code=200, headers={"HX-Redirect": "/"})

    @router.post("/api/soul")
    async def api_save_soul(content: str = Form(...)):
        """Save edited SOUL.md content."""
        SOUL_PATH.write_text(content, encoding="utf-8")
        return HTMLResponse('<div class="alert success">Saved ✓</div>')

    @router.get("/api/status")
    async def api_status():
        """Return system status as JSON (used by status page and HTMX polling)."""
        return _collect_stats()

    @router.get("/api/events")
    async def api_events(request: Request):
        """SSE endpoint — 推送 agent 实时事件。"""
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

    # ---- Attach router to app -----------------------------------------------

    app.include_router(router)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(template_name: str, context: dict) -> str:
    """Render a Jinja2 template with *context*."""
    tpl = _env.get_template(template_name)
    return tpl.render(context)


def _load_session_for_template() -> list[dict]:
    """Load session messages and format them for the chat template."""
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        msgs = data.get("messages", [])
    except Exception:
        logger.exception("Failed to load state file")
        return []

    result = []
    for m in msgs:
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content", "")
        if not content or not content.strip():
            continue
        result.append({
            "role": "user" if role == "user" else "assistant",
            "content": content,
            "time": "",  # state.json doesn't store per-message timestamps
        })
    return result


def _read_soul() -> str:
    """Read SOUL.md, return empty string on error."""
    try:
        if SOUL_PATH.exists():
            return SOUL_PATH.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Failed to read SOUL.md")
    return ""


def _collect_stats() -> dict:
    """Gather system statistics for the status page/api."""
    from cyrene.config import OPENAI_MODEL, OPENAI_BASE_URL

    st_entries = load_entries()
    session_msgs = []
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            session_msgs = data.get("messages", [])
        except Exception:
            pass

    from cyrene import db
    import asyncio

    try:
        tasks = asyncio.run(db.get_all_tasks(_db_path))
    except Exception:
        tasks = []

    soul_exists = SOUL_PATH.exists()
    soul_size = len(_read_soul()) if soul_exists else 0

    return {
        "model": OPENAI_MODEL,
        "base_url": OPENAI_BASE_URL,
        "short_term_entries": len(st_entries),
        "session_messages": len(session_msgs),
        "scheduled_tasks": len(tasks),
        "soul_exists": soul_exists,
        "soul_size": soul_size,
    }
