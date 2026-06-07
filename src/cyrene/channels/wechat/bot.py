"""WeChat message handler and long-polling loop.

Follows the same pattern as ``channels/telegram/bot.py`` — receives messages
via long-polling, dispatches to ``run_agent()``, splits long responses.
"""

import asyncio
import logging

from cyrene.channels.wechat.client import (
    WECHAT_MAX_LENGTH,
    WeChatAuthError,
    WeChatClient,
)

logger = logging.getLogger(__name__)


class WeChatUpdater:
    """Background long-polling loop that receives WeChat messages."""

    def __init__(self, client: WeChatClient, db_path: str):
        self._client = client
        self._db_path = db_path
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("WeChat polling started")

    async def stop(self) -> None:
        """Stop the background polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("WeChat polling stopped")

    async def _poll_loop(self) -> None:
        """Core polling loop: call get_updates, dispatch messages."""
        backoff = 1
        while self._running:
            try:
                msgs = await self._client.get_updates()
                backoff = 1  # reset on success

                for msg in msgs:
                    sender = msg.get("from_user_id", "")
                    ctx_token = msg.get("context_token", "")
                    text = _extract_text(msg)
                    file_items = _extract_file_items(msg)

                    if text or file_items:
                        self._client._config.context_tokens[sender] = ctx_token
                        # Log task errors so they don't get silently swallowed
                        task = asyncio.create_task(
                            _handle_message(text, sender, self._client, self._db_path, file_items=file_items)
                        )
                        task.add_done_callback(lambda t: t.exception() and logger.error("WeChat message handler error", exc_info=t.exception()))

            except WeChatAuthError:
                logger.error("WeChat token expired — polling stopped, re-login required")
                self._running = False
                # TODO: publish SSE event for Web UI notification
                break
            except Exception:
                logger.debug("WeChat poll error, backing off", exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def _extract_text(msg: dict) -> str:
    """Extract the text content from a WeChat message's item_list."""
    for item in msg.get("item_list", []):
        if item.get("type") == 1:  # TEXT
            return item.get("text_item", {}).get("text", "")
    return ""


def _extract_file_items(msg: dict) -> list[dict]:
    """Return any image (type 2) or file (type 4) items from item_list."""
    return [
        item for item in msg.get("item_list", [])
        if item.get("type") in (2, 4)
    ]


def _format_pending_question_wechat(question: dict) -> str:
    """Format a pending question as a WeChat text message."""
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


async def _handle_message(
    text: str,
    sender: str,
    client: WeChatClient,
    db_path: str,
    *,
    file_items: list[dict] | None = None,
) -> None:
    """Process a single incoming message (text, image, or file).

    1. Auto-register the first sender as the owner.
    2. Ignore non-owner messages (single-user mode).
    3. Download any attached files, augment the message, run the agent.
    """
    from cyrene.agent import (
        _AWAITING_USER_SENTINEL,
        answer_pending_question,
        get_pending_question,
        get_session_labels,
        run_agent,
    )
    from cyrene.agent.state import _conversation_source
    from cyrene.conversations import archive_exchange
    from cyrene.scheduler import reset_lottery

    config = client._config

    # Auto-detect owner on first message
    if not config.owner_wxid:
        config.owner_wxid = sender
        try:
            from cyrene.config import write_env_keys
            write_env_keys({"WECHAT_OWNER_ID": sender})
            logger.info("WeChat owner auto-set to %s", sender)
        except Exception:
            logger.exception("Failed to persist WECHAT_OWNER_ID")

    # Single-user mode: ignore non-owner
    if sender != config.owner_wxid:
        logger.debug("Ignoring message from non-owner %s", sender)
        return

    reset_lottery()
    await client.send_typing(sender)

    _conversation_source.set("wechat")

    # Download any attached files and build the attachment context block
    normalized_attachments: list[dict] = []
    if file_items:
        import mimetypes
        from cyrene.attachments import UPLOADS_DIR, attachment_kind_from_meta, build_public_attachment_payload
        for item in file_items:
            result = await client.download_incoming_item(item, UPLOADS_DIR)
            if result:
                local_path, filename = result
                content_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
                kind = attachment_kind_from_meta(content_type, filename)
                att = {
                    "id": local_path.name,
                    "name": filename,
                    "path": str(local_path),
                    "content_type": content_type,
                    "kind": kind,
                    "size": local_path.stat().st_size,
                }
                normalized_attachments.append(att)

    original_text = text
    if normalized_attachments:
        if not text:
            text = "[Attachment upload]"
        att_lines = [
            "",
            "[Uploaded attachments]",
            "The user uploaded the following files into the local workspace-accessible runtime data directory.",
            "Before answering anything about these files, you MUST inspect the relevant attachment with AnalyzeAttachment.",
            "Do not answer from the filename, extension, or metadata alone.",
            "After AnalyzeAttachment returns extracted content, use that extracted content to answer the user.",
        ]
        for att in normalized_attachments:
            att_lines.append(f'- {att["name"]} ({att["content_type"]}): {att["path"]}')
        text = text + "\n".join(att_lines)

    public_attachments = (
        [build_public_attachment_payload(att) for att in normalized_attachments]
        if normalized_attachments else None
    )

    # If there is a pending question, route this message as the answer
    pending = get_pending_question()
    if pending and str(pending.get("id", "")).strip():
        question_id = str(pending["id"]).strip()
        options = pending.get("options") or []
        answer_text = original_text.strip()
        # Map numeric replies to option labels
        if options and answer_text.isdigit():
            idx = int(answer_text) - 1
            if 0 <= idx < len(options):
                opt = options[idx]
                answer_text = str(opt.get("label", opt) if isinstance(opt, dict) else opt).strip()
        try:
            response = await answer_pending_question(
                question_id, answer_text, client, sender, db_path
            )
        except Exception as exc:
            logger.warning("answer_pending_question failed: %s", exc)
            response = f"处理回答时出错：{exc}"
    else:
        response = await run_agent(
            text, client, sender, db_path,
            public_user_message=original_text if normalized_attachments else None,
            public_attachments=public_attachments,
        )

    # If the agent is now waiting for user input, send the question
    if response == _AWAITING_USER_SENTINEL:
        new_pending = get_pending_question()
        if new_pending:
            question_text = _format_pending_question_wechat(new_pending)
            await client.send_message(sender, question_text)
        return

    labels = get_session_labels()
    await archive_exchange(
        original_text,
        response,
        sender,
        session_title=labels.get("session_title", ""),
        round_title=labels.get("round_title", ""),
        round_id=labels.get("round_id", ""),
    )

    # Split long messages at WeChat's character limit
    for i in range(0, len(response), WECHAT_MAX_LENGTH):
        await client.send_message(sender, response[i : i + WECHAT_MAX_LENGTH])
