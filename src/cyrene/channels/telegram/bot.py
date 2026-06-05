import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from cyrene.agent import (
    _AWAITING_USER_SENTINEL,
    answer_pending_question,
    clear_session_id,
    get_pending_question,
    get_session_labels,
    run_agent,
)
from cyrene.agent.state import _conversation_source
from cyrene.conversations import archive_exchange
from cyrene.config import ASSISTANT_NAME, DB_PATH, OWNER_ID, TELEGRAM_BOT_TOKEN
from cyrene.scheduler import reset_lottery, setup_scheduler

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_LENGTH = 4096


def _format_pending_question(question: dict) -> str:
    """Format a pending question as a Telegram message."""
    text = str(question.get("text", "")).strip()
    options = question.get("options", []) or []

    if options:
        lines = [text, ""]
        for i, opt in enumerate(options, start=1):
            label = str(opt.get("label", opt) if isinstance(opt, dict) else opt).strip()
            lines.append(f"{i}. {label}")
        if question.get("allow_custom", True):
            lines.append("")
            lines.append("（也可以直接输入您的回答）")
    else:
        lines = [text]

    return "\n".join(lines)


def _is_owner(update: Update) -> bool:
    return OWNER_ID is not None and update.effective_user is not None and update.effective_user.id == OWNER_ID


async def _start(update: Update, context) -> None:
    if not _is_owner(update):
        return
    await update.message.reply_text(
        f"Hi! I'm {ASSISTANT_NAME}, your personal AI assistant. Send me a message to get started.\n\n"
        "Commands:\n"
        "/clear - Reset conversation session"
    )


async def _clear(update: Update, context) -> None:
    if not _is_owner(update):
        return
    await clear_session_id()
    await update.message.reply_text("Session cleared. Starting fresh!")


async def _send_response(update: Update, bot, chat_id: int, response: str) -> None:
    """Send a response, splitting it if it exceeds Telegram's limit."""
    for i in range(0, len(response), _TELEGRAM_MAX_LENGTH):
        chunk = response[i : i + _TELEGRAM_MAX_LENGTH]
        await update.message.reply_text(chunk)


async def _handle_message(update: Update, context) -> None:
    if not _is_owner(update) or not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text

    # User initiative resets the proactive lottery impulse
    reset_lottery()

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    _conversation_source.set("telegram")

    # If there is a pending question, route this message as the answer
    pending = get_pending_question()
    if pending and str(pending.get("id", "")).strip():
        question_id = str(pending["id"]).strip()
        # Map numeric replies to option labels
        options = pending.get("options") or []
        answer_text = user_text.strip()
        if options and answer_text.isdigit():
            idx = int(answer_text) - 1
            if 0 <= idx < len(options):
                opt = options[idx]
                answer_text = str(opt.get("label", opt) if isinstance(opt, dict) else opt).strip()
        try:
            response = await answer_pending_question(
                question_id, answer_text, context.bot, chat_id, str(DB_PATH)
            )
        except Exception as exc:
            logger.warning("answer_pending_question failed: %s", exc)
            response = f"处理回答时出错：{exc}"
    else:
        response = await run_agent(user_text, context.bot, chat_id, str(DB_PATH))

    # If the agent is now waiting for user input, send the question
    if response == _AWAITING_USER_SENTINEL:
        new_pending = get_pending_question()
        if new_pending:
            question_text = _format_pending_question(new_pending)
            await update.message.reply_text(question_text)
        return

    # Archive to conversations/ for long-term memory
    labels = get_session_labels()
    await archive_exchange(
        user_text,
        response,
        chat_id,
        session_title=labels.get("session_title", ""),
        round_title=labels.get("round_title", ""),
        round_id=labels.get("round_id", ""),
    )

    await _send_response(update, context.bot, chat_id, response)


async def _post_init(application: Application) -> None:
    scheduler = setup_scheduler(application.bot, str(DB_PATH))
    scheduler.start()
    logger.info("Scheduler started")


def setup_bot() -> Application:
    if not TELEGRAM_BOT_TOKEN or OWNER_ID is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and OWNER_ID must be set to run the Telegram bot.")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("clear", _clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))
    return app
