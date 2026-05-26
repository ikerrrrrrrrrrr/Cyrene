"""FastAPI routes for WeChat QR login, status, and lifecycle control."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException

logger = logging.getLogger(__name__)


def register_wechat_routes(app: FastAPI) -> None:
    """Register ``/api/wechat/*`` endpoints on *app*."""

    @app.get("/api/wechat/status")
    async def wechat_status():
        """Return WeChat channel status.

        ``running`` means the long-poll background task is active.
        ``connected`` means a token is present in .env (persisted).
        """
        from cyrene.config import WECHAT_BOT_TOKEN, WECHAT_OWNER_ID

        updater = getattr(app.state, "wechat_updater", None)
        running = updater is not None and updater._running

        return {
            "running": running,
            "connected": bool(WECHAT_BOT_TOKEN),
            "owner_wxid": WECHAT_OWNER_ID or "",
        }

    @app.post("/api/wechat/qr-login")
    async def wechat_qr_login():
        """Fetch a QR code for WeChat login."""
        from .auth import WeChatAuth

        auth = WeChatAuth()
        qrcode_id, qrcode_img = await auth.get_qr_code()
        return {"qrcode_id": qrcode_id, "qrcode_img": qrcode_img}

    @app.post("/api/wechat/poll-login")
    async def wechat_poll_login(data: dict):
        """Poll QR code status; writes token to .env on success."""
        from .auth import WeChatAuth

        auth = WeChatAuth()
        token = await auth.poll_login(data.get("qrcode_id", ""), timeout=120)
        if token:
            from cyrene.config import write_env_keys

            write_env_keys({"WECHAT_BOT_TOKEN": token})
            return {"ok": True}
        return {"ok": False, "expired": True}

    @app.post("/api/wechat/start")
    async def wechat_start():
        """Start the WeChat long-polling background task.

        Requires ``WECHAT_BOT_TOKEN`` to be set (either from .env at startup
        or from a previous QR-login via ``/api/wechat/poll-login``).
        Safe to call multiple times — returns immediately if already running.
        """
        from cyrene.config import WECHAT_BOT_TOKEN

        if not WECHAT_BOT_TOKEN:
            raise HTTPException(400, "WECHAT_BOT_TOKEN not configured")

        updater = getattr(app.state, "wechat_updater", None)
        if updater is not None and updater._running:
            return {"ok": True, "already_running": True}

        db_path = getattr(app.state, "wechat_db_path", None)
        if not db_path:
            raise HTTPException(500, "wechat_db_path not initialised")

        from .bot import WeChatUpdater
        from .client import WeChatClient, WeChatConfig
        from cyrene.channels.wechat import get_current_client, set_current_client

        # Close old client before creating a new one
        old_client = get_current_client()
        if old_client is not None:
            try:
                await old_client.close()
            except Exception:
                logger.exception("Failed to close old WeChat client")

        config = WeChatConfig(bot_token=WECHAT_BOT_TOKEN)
        client = WeChatClient(config)
        updater = WeChatUpdater(client, db_path)
        set_current_client(client)

        app.state.wechat_updater = updater
        await updater.start()
        logger.info("WeChat polling started via /api/wechat/start")
        return {"ok": True}

    @app.post("/api/wechat/stop")
    async def wechat_stop():
        """Stop the WeChat long-polling background task."""
        updater = getattr(app.state, "wechat_updater", None)
        if updater is not None:
            await updater.stop()
            app.state.wechat_updater = None
            from cyrene.channels.wechat import get_current_client, set_current_client
            old = get_current_client()
            set_current_client(None)
            if old is not None:
                await old.close()
            logger.info("WeChat polling stopped via /api/wechat/stop")
            return {"ok": True}
        return {"ok": True, "already_stopped": True}
