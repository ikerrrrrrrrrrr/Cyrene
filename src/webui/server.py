"""FastAPI app factory, WebBot adapter, and uvicorn runner."""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from cyrene.config import WEB_PORT

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# WebBot adapter — replaces Telegram bot for the scheduler
# ---------------------------------------------------------------------------


class WebBot:
    """Bot adapter for the scheduler in web-only mode.

    Implements ``send_message(chat_id, text)`` so the scheduler, heartbeat,
    and steward can deliver proactive messages.  Instead of sending to
    Telegram, messages are buffered in memory for the web UI to poll.
    """

    def __init__(self) -> None:
        self._pending: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        """Store a proactive message (non-blocking)."""
        logger.info("WebBot proactive message for chat %s: %s", chat_id, text[:80])
        self._pending.append({
            "chat_id": chat_id,
            "text": text,
            "timestamp": datetime.now().isoformat(),
        })

    def pop_pending(self, chat_id: int) -> list[dict[str, Any]]:
        """Return and clear pending messages for *chat_id*."""
        matched: list[dict] = []
        remaining: list[dict] = []
        for msg in self._pending:
            if msg["chat_id"] == chat_id:
                matched.append(msg)
            else:
                remaining.append(msg)
        self._pending = remaining
        return matched


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def create_app(bot: Any, db_path: str):
    """Build and return a configured FastAPI application."""
    from webui.routes import register_routes

    app = FastAPI(title="Cyrene Web")

    # Mount static files
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Register all routes, passing shared resources
    register_routes(app, bot, db_path)

    return app


# ---------------------------------------------------------------------------
# Uvicorn runner
# ---------------------------------------------------------------------------


async def run_web(bot: Any, db_path: str) -> None:
    """Start the uvicorn server on the current event loop.

    Designed to be awaited via ``asyncio.create_task`` so it can coexist
    with other async services (e.g. the Telegram bot).
    """
    app = create_app(bot, db_path)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=WEB_PORT,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    logger.info("Web UI starting on http://0.0.0.0:%d", WEB_PORT)
    await server.serve()
