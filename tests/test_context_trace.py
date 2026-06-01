from cyrene.context_trace import (
    attach_context,
    context_block,
    strip_context_metadata,
    summarize_context_trace,
)


def test_context_metadata_is_stripped_before_payload() -> None:
    message = attach_context(
        {"role": "user", "content": "hello"},
        context_block("user.current.raw", "user", source="test", reason="unit test", content="hello"),
    )

    stripped = strip_context_metadata(message)

    assert "_ctx" not in stripped
    assert stripped == {"role": "user", "content": "hello"}


def test_context_trace_uses_explicit_blocks() -> None:
    message = attach_context(
        {"role": "user", "content": "hello"},
        context_block("user.current.raw", "user", source="test", reason="unit test", content="hello"),
    )

    trace = summarize_context_trace([message])

    assert trace["included"][0]["id"] == "user.current.raw"
    assert trace["included"][0]["type"] == "user"
    assert trace["message_map"][0]["block_ids"] == ["user.current.raw"]


def test_context_trace_infers_blocks_for_plain_messages() -> None:
    trace = summarize_context_trace([{"role": "tool", "tool_call_id": "call_123", "content": "result"}])

    assert trace["included"][0]["id"] == "tool.result.call_123"
    assert trace["included"][0]["type"] == "tool_result"
