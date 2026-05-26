"""WeChat iLink Bot channel for Cyrene.

Usage in ``server.py``::

    from cyrene.channels.wechat import setup_wechat

    @app.on_event("startup")
    async def _start_wechat():
        await setup_wechat(app, db_path)
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .auth import WeChatAuth, WeChatAuthError
from .bot import WeChatUpdater
from .client import WeChatClient, WeChatConfig
from .web import register_wechat_routes

__all__ = [
    "setup_wechat",
    "get_current_client",
    "WeChatAuth",
    "WeChatAuthError",
    "WeChatClient",
    "WeChatConfig",
    "WeChatUpdater",
]

logger = logging.getLogger(__name__)

# Module-level reference so other modules (e.g. scheduler) can send
# proactive WeChat notifications without coupling to the FastAPI app.
_current_client: WeChatClient | None = None


def get_current_client() -> WeChatClient | None:
    """Return the currently active WeChatClient, or ``None``."""
    return _current_client


async def setup_wechat(app: FastAPI, db_path: str) -> None:
    """Initialise the WeChat channel.

    Registers ``/api/wechat/*`` routes regardless of token presence.
    If ``WECHAT_BOT_TOKEN`` is already set in ``.env``, also starts the
    long-polling background task immediately.
    After a QR-login from the UI, the token is written to ``.env`` and
    the user calls ``POST /api/wechat/start`` — no restart needed.
    """
    from cyrene.config import WECHAT_BOT_TOKEN

    global _current_client

    # Routes and shared state are needed even without a token (for QR login)
    register_wechat_routes(app)
    app.state.wechat_db_path = str(db_path)

    if WECHAT_BOT_TOKEN:
        config = WeChatConfig(bot_token=WECHAT_BOT_TOKEN)
        client = WeChatClient(config)
        updater = WeChatUpdater(client, str(db_path))

        _current_client = client
        app.state.wechat_updater = updater
        await updater.start()
        logger.info("WeChat channel started (token found in .env)")
    else:
        logger.debug("WECHAT_BOT_TOKEN not set — WeChat channel disabled")
