"""FastAPI app factory and WebBot adapter for the scheduler."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from cyrene.config import WEB_PORT

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


class WebBot:
    """Bot adapter for the scheduler in web-only mode.

    Implements send_message() so the scheduler, heartbeat, and steward
    can deliver proactive messages without a Telegram bot.
    """

    def __init__(self) -> None:
        self._pending: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self._pending.append({
            "chat_id": chat_id,
            "text": text,
            "timestamp": datetime.now().isoformat(),
        })

    def pop_pending(self, chat_id: int) -> list[dict[str, Any]]:
        matched = [m for m in self._pending if m["chat_id"] == chat_id]
        self._pending = [m for m in self._pending if m["chat_id"] != chat_id]
        return matched


def create_app(bot: Any, db_path: str) -> FastAPI:
    from webui.routes import register_routes

    app = FastAPI(title="Cyrene")
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    register_routes(app, bot, db_path)
    return app


async def run_web(bot: Any, db_path: str) -> None:
    app = create_app(bot, db_path)
    config = uvicorn.Config(app, host="0.0.0.0", port=WEB_PORT, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)
    logger.info("Web UI at http://0.0.0.0:%d", WEB_PORT)
    await server.serve()
