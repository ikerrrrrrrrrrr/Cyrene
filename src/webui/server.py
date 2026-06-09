"""FastAPI app factory and WebBot adapter for the scheduler."""

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

_STATIC_DIR = Path(__file__).parent / "static"
_WORKBENCH_UI_DIR = Path(__file__).parent.parent / "workbench-webui"


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


def create_app(bot: Any, db_path: str, instance_id: str = "", ui_mode: str = "workbench") -> FastAPI:
    from cyrene.channels.wechat import setup_wechat as _setup_wechat
    from webui.routes import register_routes

    from webui.auth import LocalAuthMiddleware

    app = FastAPI(title="Cyrene")
    app.add_middleware(LocalAuthMiddleware)
    app.state.instance_id = instance_id
    app.state.ui_mode = ui_mode
    app.mount("/static/workbench-ui", StaticFiles(directory=str(_WORKBENCH_UI_DIR)), name="workbench-ui")
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/api/instance-id")
    async def api_instance_id() -> dict[str, str]:
        return {"instance_id": str(app.state.instance_id or "")}

    register_routes(app, bot, db_path)

    @app.on_event("startup")
    async def _start_wechat() -> None:
        try:
            await _setup_wechat(app, db_path)
        except Exception:
            logger.warning("WeChat bot setup failed — check your config / proxy setup")

    @app.on_event("startup")
    async def _migrate_knowledge_db() -> None:
        try:
            from cyrene.config import migrate_knowledge_to_workspace_db
            result = await migrate_knowledge_to_workspace_db()
            if result["migrated"]:
                logger.info("Knowledge base migrated: %s", result["reason"])
        except Exception:
            logger.warning("Knowledge base migration failed (non-fatal)")

    @app.on_event("startup")
    async def _sync_knowledge_catalog() -> None:
        try:
            from cyrene.config import get_knowledge_db_path
            from cyrene.db import init_knowledge_db
            from cyrene.knowledge import store, ingest
            _kb_db_path = str(get_knowledge_db_path())
            await init_knowledge_db(_kb_db_path)
            await store.sync_filesystem(_kb_db_path)
            asyncio.create_task(ingest.process_pending(_kb_db_path))
        except Exception:
            logger.warning("Knowledge catalog sync failed — check your knowledge base")

    @app.on_event("shutdown")
    async def _close_browser_session() -> None:
        try:
            from cyrene.browser import close_session
            await close_session()
        except Exception:
            logger.warning("Browser session shutdown failed")

    return app


async def run_web(bot: Any, db_path: str, port: int = WEB_PORT, instance_id: str = "", ui_mode: str = "workbench") -> None:
    app = create_app(bot, db_path, instance_id=instance_id, ui_mode=ui_mode)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)
    logger.info("Web UI at http://0.0.0.0:%d", port)
    await server.serve()
