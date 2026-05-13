"""Web UI entry point — ``python -m webui``

Initializes the runtime, starts the scheduler, and launches the web server.
"""

import asyncio
import logging

from cyrene.config import DB_PATH, DATA_DIR, STORE_DIR, WORKSPACE_DIR, INBOX_DIR
from cyrene.db import init_db
from cyrene.inbox import ensure_inbox
from cyrene.short_term import init_short_term
from cyrene.soul import ensure_soul
from cyrene.scheduler import setup_scheduler

from webui.server import run_web, WebBot

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def main():
    # 1. Prepare runtime directories and files
    for d in (WORKSPACE_DIR, STORE_DIR, DATA_DIR, INBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)

    await init_db(str(DB_PATH))
    ensure_soul()
    ensure_inbox("cyrene")
    init_short_term(DATA_DIR)

    # 2. Create WebBot adapter for the scheduler
    bot = WebBot()
    logger.info("Web UI mode: using WebBot adapter for scheduler")

    # 3. Start scheduler (heartbeat, lottery, steward, tasks)
    scheduler = setup_scheduler(bot, str(DB_PATH))
    scheduler.start()
    logger.info("Scheduler started")

    # 4. Start web server (blocks until interrupted)
    try:
        await run_web(bot, str(DB_PATH))
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
