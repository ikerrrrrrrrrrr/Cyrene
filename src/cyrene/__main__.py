import asyncio
import logging

from cyrene.bot import setup_bot
from cyrene.config import (
    ASSISTANT_NAME, DATA_DIR, DB_PATH, INBOX_DIR,
    SOUL_PATH, STORE_DIR, WORKSPACE_DIR,
)
from cyrene.db import init_db
from cyrene.inbox import ensure_inbox
from cyrene.short_term import init_short_term
from cyrene.soul import ensure_soul

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def _prepare_runtime() -> None:
    """初始化运行时所需的目录和文件"""
    # 创建目录
    for d in (WORKSPACE_DIR, STORE_DIR, DATA_DIR, INBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)

    # 初始化数据库
    await init_db(str(DB_PATH))
    logger.info("Database initialized at %s", DB_PATH)

    # 创建 SOUL.md（如果不存在）
    ensure_soul()
    logger.info("SOUL.md ready at %s", SOUL_PATH)

    # 创建默认 inbox
    ensure_inbox("cyrene")
    logger.info("Inbox ready at %s", INBOX_DIR)

    # 初始化短期记忆
    init_short_term(DATA_DIR)
    logger.info("Short-term memory initialized at %s", DATA_DIR / "short_term.json")

    # 人格设置检测（Telegram 模式跳过交互，提示用户先运行 CLI）
    from cyrene.setup import init_setup_flag, is_setup_done
    init_setup_flag()
    if not is_setup_done():
        logger.warning("首次启动检测到未设置人格。请先运行 CLI 模式完成设置：")
        logger.warning("  python -m cyrene.local_cli")


def _run_bot() -> None:
    app = setup_bot()
    logger.info("%s is starting...", ASSISTANT_NAME)
    app.run_polling()


def main() -> None:
    asyncio.run(_prepare_runtime())
    _run_bot()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception:
        logger.exception("Fatal error")
