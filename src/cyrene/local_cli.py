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


def _show_help():
    print()
    print("=" * 40)
    print("  Cyrene 帮助菜单")
    print("=" * 40)
    print("  1) 重新注入人格（重新运行设置向导）")
    print("  2) 清除对话上下文（session）")
    print("  3) 重置人格（恢复默认 SOUL.md）")
    print("  4) 检查系统状态")
    print("  0) 返回对话")
    print("=" * 40)


async def _handle_menu():
    while True:
        choice = input("\n选择操作 (0-4): ").strip()

        if choice == "0":
            print("返回对话。")
            return

        elif choice == "1":
            from cyrene.setup import init_setup_flag, mark_setup_done, run_setup
            init_setup_flag()
            print("\n--- 重新注入人格 ---")
            await run_setup()
            print("人格设置完成。输入 /h 可以重新设置。")
            return

        elif choice == "2":
            await clear_session_id()
            print("✅ 对话上下文已清除。")
            return

        elif choice == "3":
            from cyrene.soul import get_soul_path, ensure_soul
            from cyrene.short_term import save_entries
            soul_path = get_soul_path()
            if soul_path.exists():
                soul_path.unlink()
            ensure_soul()
            save_entries([])  # 同时清空短期记忆
            print("✅ SOUL.md 已重置为默认。短期记忆已清空。")
            return

        elif choice == "4":
            from cyrene.config import OPENAI_MODEL, OPENAI_BASE_URL
            from cyrene.soul import get_soul_path, read_soul
            from cyrene.short_term import load_entries
            print("\n--- 系统状态 ---")
            print(f"  模型: {OPENAI_MODEL}")
            print(f"  地址: {OPENAI_BASE_URL}")
            soul_path = get_soul_path()
            print(f"  SOUL.md: {'存在' if soul_path.exists() else '不存在'} ({soul_path})")
            if soul_path.exists():
                soul_content = read_soul()
                print(f"  人格内容: {len(soul_content)} 字符")
            st_entries = load_entries()
            print(f"  短期记忆: {len(st_entries)} 条")
            from cyrene.config import STATE_FILE
            if STATE_FILE.exists():
                import json
                msgs = json.loads(STATE_FILE.read_text()).get("messages", [])
                print(f"  当前 session: {len(msgs)} 条消息")
            else:
                print("  当前 session: 空")
            print("------------------")
            return

        else:
            print("无效选择，请输入 0-4。")


async def _cli_loop() -> None:
    print(f"{ASSISTANT_NAME} CLI mode. '/h' for menu, '/clear' to reset session, 'quit' to exit.")
    while True:
        try:
            user_input = input("\nYou: ").strip()
            if not user_input:
                continue
            if user_input.lower() == "quit":
                break
            if user_input.lower() == "/h":
                _show_help()
                await _handle_menu()
                continue
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


def _run_web_mode() -> None:
    """Start web UI mode (python -m cyrene.local_cli --web)."""
    import sys as _sys
    if "--verbose" in _sys.argv:
        import cyrene.debug as _debug
        _debug.VERBOSE = True
        _debug.init_debug_log()

    import asyncio
    from cyrene.debug import enable_event_bus
    from cyrene.scheduler import setup_scheduler
    from webui.server import run_web, WebBot

    async def _start():
        for d in (WORKSPACE_DIR, STORE_DIR, DATA_DIR, INBOX_DIR):
            d.mkdir(parents=True, exist_ok=True)
        await init_db(str(DB_PATH))
        ensure_soul()
        ensure_inbox("cyrene")
        init_short_term(DATA_DIR)
        enable_event_bus()

        bot = WebBot()
        scheduler = setup_scheduler(bot, str(DB_PATH))
        scheduler.start()
        print(f"{ASSISTANT_NAME} Web UI starting...")

        try:
            await run_web(bot, str(DB_PATH))
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            scheduler.shutdown()

    asyncio.run(_start())


def main() -> None:
    import sys
    if "--web" in sys.argv:
        _run_web_mode()
        return
    if "--verbose" in sys.argv:
        import cyrene.debug as _debug
        _debug.VERBOSE = True
        _debug.init_debug_log()
        lp = _debug.get_log_path()
        if lp:
            print(f"Debug log: {lp}")

    asyncio.run(_prepare_cli())

    # 人格设置向导（首次启动时运行）
    from cyrene.setup import init_setup_flag, is_setup_done, run_setup
    init_setup_flag()
    if not is_setup_done():
        asyncio.run(run_setup())

    asyncio.run(_cli_loop())


if __name__ == "__main__":
    main()
