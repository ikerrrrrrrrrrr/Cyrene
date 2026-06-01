import json

from cyrene import debug
from cyrene import context_identity
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


def test_context_identity_is_runtime_only_and_stripped_from_payload() -> None:
    tokens = context_identity.begin_request("user", "hello identity", round_id="round_1")
    try:
        message = attach_context(
            {"role": "user", "content": "hello"},
            context_block("user.current.raw", "user", source="test", reason="unit test", content="hello"),
        )
        trace = summarize_context_trace([message])
    finally:
        context_identity.reset_request(tokens)

    assert trace["request_id"].startswith("req.user.")
    assert trace["included"][0]["cid"].startswith("cid.user.user.current.raw")
    assert trace["included"][0]["source_node_id"].startswith("node.source.user.")
    assert "_ctx" not in strip_context_metadata(message)
    assert "cid" not in strip_context_metadata(message)


def test_tool_result_cid_matches_tool_event_formula() -> None:
    tokens = context_identity.begin_request("user", "tool identity", round_id="round_1")
    try:
        args = {"query": "weather"}
        result = "tool result"
        block = context_block(
            "tool.result.websearch.call_123",
            "tool_result",
            source="tool:websearch",
            reason="tool output returned to LLM",
            content=result,
            metadata={"tool_name": "websearch", "tool_call_id": "call_123", "tool_args": args},
        )
    finally:
        context_identity.reset_request(tokens)

    assert block["cid"] == context_identity.tool_result_cid("websearch", "call_123", args, result)


def test_runtime_identity_exists_without_verbose_logging(monkeypatch, tmp_path) -> None:
    log_path = tmp_path / "debug.jsonl"
    monkeypatch.setattr(debug, "VERBOSE", False)
    monkeypatch.setattr(debug, "_log_file", log_path)
    tokens = context_identity.begin_request("user", "runtime identity", round_id="round_1")
    try:
        message = attach_context(
            {"role": "user", "content": "hello"},
            context_block("user.current.raw", "user", source="test", reason="unit test", content="hello"),
        )
        trace = summarize_context_trace([message])
        debug.log_llm_call("main_agent", "phase1", [message], [], {"content": "ok"}, 1.0)
    finally:
        context_identity.reset_request(tokens)

    assert trace["request_id"].startswith("req.user.")
    assert trace["included"][0]["cid"].startswith("cid.user.")
    assert not log_path.exists()


def test_verbose_logging_persists_identity_graph(monkeypatch, tmp_path) -> None:
    log_path = tmp_path / "debug.jsonl"
    monkeypatch.setattr(debug, "VERBOSE", True)
    monkeypatch.setattr(debug, "_log_file", log_path)
    tokens = context_identity.begin_request("user", "persist identity", round_id="round_1")
    try:
        message = attach_context(
            {"role": "user", "content": "hello"},
            context_block("user.current.raw", "user", source="test", reason="unit test", content="hello"),
        )
        debug.log_llm_call(
            "main_agent",
            "phase1",
            [message],
            [{"function": {"name": "websearch"}}],
            {"content": "ok"},
            1.0,
        )
    finally:
        context_identity.reset_request(tokens)

    entry = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert entry["request_id"].startswith("req.user.")
    assert entry["identity_graph"]["node_type"] == "llm"
    assert entry["identity_graph"]["source_nodes"]
    assert "_ctx" not in entry["messages"][0]
