import asyncio
import sys

from cyrene.agent import clear_session_id, run_agent
from cyrene.config import ASSISTANT_NAME, DB_PATH, DATA_DIR, STORE_DIR, WORKSPACE_DIR
from cyrene.db import init_db
from cyrene.memory import ensure_workspace


class LocalBot:
    async def send_message(self, chat_id: int, text: str) -> None:
        print(f"[send_message:{chat_id}] {text}")


async def _prepare_runtime() -> None:
    for directory in (WORKSPACE_DIR, STORE_DIR, DATA_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    await init_db(str(DB_PATH))
    ensure_workspace()


async def _run_once(prompt: str) -> None:
    await _prepare_runtime()
    response = await run_agent(prompt, LocalBot(), 0, str(DB_PATH))
    print(response)


async def _repl() -> None:
    await _prepare_runtime()
    bot = LocalBot()
    print(f"{ASSISTANT_NAME} local CLI. Type /clear to reset memory, /exit to quit.")
    while True:
        try:
            prompt = input("> ").strip()
        except EOFError:
            break
        if not prompt:
            continue
        if prompt == "/exit":
            break
        if prompt == "/clear":
            clear_session_id()
            print("Session cleared.")
            continue
        response = await run_agent(prompt, bot, 0, str(DB_PATH))
        print(response)


def main() -> None:
    if len(sys.argv) > 1:
        asyncio.run(_run_once(" ".join(sys.argv[1:])))
    else:
        asyncio.run(_repl())


if __name__ == "__main__":
    main()
