"""Web UI entry point — python -m cyrene.webui"""

import asyncio
import logging

from cyrene.config import (
    DB_PATH, DATA_DIR, INBOX_DIR, STORE_DIR, WORKSPACE_DIR,
    SEARXNG_AUTO_START, SEARXNG_HOST, SEARXNG_PORT,
)
from cyrene.db import init_db
from cyrene.inbox import ensure_inbox
from cyrene.short_term import init_short_term
from cyrene.soul import ensure_soul
from cyrene.debug import enable_event_bus
from cyrene.scheduler import setup_scheduler
from webui.server import run_web, WebBot

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def main() -> None:
    for d in (WORKSPACE_DIR, STORE_DIR, DATA_DIR, INBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)

    await init_db(str(DB_PATH))
    ensure_soul()
    ensure_inbox("cyrene")
    init_short_term(DATA_DIR)
    enable_event_bus()

    if SEARXNG_AUTO_START:
        from cyrene.searxng_manager import start_searxng
        try:
            url = await start_searxng(SEARXNG_PORT, SEARXNG_HOST)
            logger.info("SearXNG auto-started at %s", url)
        except Exception as exc:
            logger.warning("SearXNG auto-start failed: %s", exc)

    bot = WebBot()
    scheduler = setup_scheduler(bot, str(DB_PATH))
    scheduler.start()
    logger.info("Scheduler started")

    try:
        await run_web(bot, str(DB_PATH))
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        from cyrene.searxng_manager import stop_searxng
        stop_searxng()
