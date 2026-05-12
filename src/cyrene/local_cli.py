import asyncio
import logging

from cyrene.agent import clear_session_id, run_agent
from cyrene.config import ASSISTANT_NAME, DB_PATH, DATA_DIR, INBOX_DIR, STORE_DIR, WORKSPACE_DIR
from cyrene.db import init_db
from cyrene.inbox import ensure_inbox
from cyrene.short_term import init_short_term
from cyrene.soul import ensure_soul

logger = logging.getLogger(__name__)


async def _prepare_cli() -> None:
    """初始化（同 __main__ 但不需要 bot）"""
    for d in (WORKSPACE_DIR, STORE_DIR, DATA_DIR, INBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)
    await init_db(str(DB_PATH))
    ensure_soul()
    ensure_inbox("cyrene")
    init_short_term(DATA_DIR)


async def _cli_loop() -> None:
    print(f"{ASSISTANT_NAME} CLI mode. Type 'quit' to exit, '/clear' to reset session.")
    while True:
        try:
            user_input = input("\nYou: ").strip()
            if not user_input:
                continue
            if user_input.lower() == "quit":
                break
            if user_input.lower() == "/clear":
                await clear_session_id()
                print("Session cleared.")
                continue

            response = await run_agent(user_input, None, 0, str(DB_PATH))
            print(f"\n{ASSISTANT_NAME}: {response}")
        except KeyboardInterrupt:
            break
        except Exception:
            logger.exception("Error in CLI loop")


def main() -> None:
    asyncio.run(_prepare_cli())

    # 人格设置向导（首次启动时运行）
    from cyrene.setup import init_setup_flag, is_setup_done, run_setup
    init_setup_flag()
    if not is_setup_done():
        asyncio.run(run_setup())

    asyncio.run(_cli_loop())


if __name__ == "__main__":
    main()
