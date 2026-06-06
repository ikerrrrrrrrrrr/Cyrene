"""Tool implementation for send_file."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _json_result,
    _resolve_exportable_path,
    asyncio,
    build_public_attachment_payload,
    logger,
    register_generated_attachment,
)

TOOL_NAME = 'send_file'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_send_file(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    path_arg = str(args.get("path", "") or "").strip()
    if not path_arg:
        return "Error: 'path' is required."

    from cyrene.agent.state import _current_agent_id, _current_client_request_id, _current_round_id
    from cyrene.agent.session import append_system_message
    from cyrene.agent.message import _insert_intermediate_user_reply

    if _current_agent_id.get() != "main":
        return "Only the main agent can send a file to the WebUI."

    path = _resolve_exportable_path(path_arg)
    if not path.exists() or not path.is_file():
        return f"Error: file not found: {path}"

    text = str(args.get("text", "") or "").strip()
    registered = register_generated_attachment(str(path), display_name=str(args.get("name", "") or "").strip() or None)
    attachment = build_public_attachment_payload(registered)

    # Register in knowledge base
    try:
        from cyrene.knowledge import store, ingest
        import mimetypes
        doc_path = registered.get("path", "")
        if doc_path:
            from pathlib import Path
            import mimetypes
            doc_file = Path(doc_path)
            content_type = mimetypes.guess_type(str(doc_file))[0] or "application/octet-stream"
            from cyrene.attachments import attachment_kind_from_meta
            kind = attachment_kind_from_meta(content_type, doc_file.name)
            content_hash = store.content_hash_file(doc_file)
            doc = await store.upsert_document_by_path(
                _db_path,
                path=str(doc_file.resolve()),
                source="generated",
                name=registered.get("name", doc_file.name),
                content_type=content_type,
                kind=kind,
                size=doc_file.stat().st_size if doc_file.exists() else 0,
                metadata={"sent_to_chat": True},
                content_hash=content_hash,
            )
            if doc.get("status") in {"pending", "error"}:
                asyncio.create_task(ingest.index_document(_db_path, doc["id"]))
    except Exception as e:
        logger.debug(f"Failed to register generated file in knowledge base: {e}")

    round_id = str(_current_round_id.get() or "").strip()
    client_request_id = str(_current_client_request_id.get() or "").strip()
    if round_id:
        await _insert_intermediate_user_reply(
            text,
            round_id=round_id,
            client_request_id=client_request_id,
            attachments=[attachment],
        )
    else:
        await append_system_message(
            text,
            message_meta={"attachments": [attachment]},
        )
    if _notify_state is not None:
        _notify_state["sent"] = True
    return _json_result({
        "status": "sent",
        "attachment": attachment,
    })


handler = _tool_send_file

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_send_file"]
