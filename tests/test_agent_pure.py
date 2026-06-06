"""Pure-function tests for the agent/ subpackage — zero mocking needed.

All functions tested here are pure data-transformation helpers.  They must
remain stable after the agent.py → agent/ split.
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Patch missing PIL dependency before any cyrene import
sys.modules.setdefault("PIL", MagicMock())
sys.modules["PIL"].__version__ = "0"
sys.modules["PIL"].Image = MagicMock()


# ===========================================================================
# report_export_filename  (modules/deep_research.py)
# ===========================================================================

def test_report_export_filename_basic():
    from cyrene.modules.deep_research import report_export_filename
    assert report_export_filename("round_12345") == "round_12345.pdf"


def test_report_export_filename_sanitized():
    from cyrene.modules.deep_research import report_export_filename
    result = report_export_filename("round_abc/def:ghi")
    assert "/" not in result
    assert result.endswith(".pdf")


def test_report_export_filename_fallback():
    from cyrene.modules.deep_research import report_export_filename
    result = report_export_filename("", "my-report")
    assert result == "my-report.pdf"


# ===========================================================================
# report_title_from_text  (modules/deep_research.py)
# ===========================================================================

def test_report_title_from_heading():
    from cyrene.modules.deep_research import report_title_from_text
    text = "# My Research Report\n\nSome content."
    assert report_title_from_text(text) == "My Research Report"


def test_report_title_from_first_line():
    from cyrene.modules.deep_research import report_title_from_text
    text = "Just a plain line\nSecond line"
    title = report_title_from_text(text)
    assert title == "Just a plain line"


def test_report_title_fallback():
    from cyrene.modules.deep_research import report_title_from_text
    assert report_title_from_text("") == "Deep Research Report"
    assert report_title_from_text(None) == "Deep Research Report"


# ===========================================================================
# _fallback_label  (agent/message.py)
# ===========================================================================

def test_fallback_label_truncates():
    from cyrene.agent.message import _fallback_label
    long_text = "a" * 100
    assert len(_fallback_label(long_text)) == 48


def test_fallback_label_strips_punctuation():
    from cyrene.agent.message import _fallback_label
    assert _fallback_label("  [Hello] ", limit=10) == "Hello"


def test_fallback_label_empty():
    from cyrene.agent.message import _fallback_label
    assert _fallback_label("", limit=10) == "Untitled"
    assert _fallback_label(None, limit=10) == "Untitled"


# ===========================================================================
# Round timestamp helpers  (agent/message.py)
# ===========================================================================

def test_round_epoch_ms_valid():
    from cyrene.agent.message import _round_epoch_ms
    assert _round_epoch_ms("round_1700000000000") == 1700000000000


def test_round_epoch_ms_invalid():
    from cyrene.agent.message import _round_epoch_ms
    assert _round_epoch_ms("round_abc") is None
    assert _round_epoch_ms("") is None
    assert _round_epoch_ms(None) is None


def test_round_started_iso_valid():
    from cyrene.agent.message import _round_started_iso
    result = _round_started_iso("round_1700000000000")
    assert result is not None
    assert "2023-11-14" in result


def test_round_started_iso_invalid():
    from cyrene.agent.message import _round_started_iso
    assert _round_started_iso("") is None
    assert _round_started_iso("bad") is None


def test_round_title_prefers_entry_title():
    from cyrene.agent.message import _round_title_from_entry
    entry = {"title": "My Custom Title", "last_user": "ignored"}
    assert _round_title_from_entry(entry) == "My Custom Title"


def test_round_title_falls_back():
    from cyrene.agent.message import _round_title_from_entry
    entry = {"last_user": "User said something"}
    title = _round_title_from_entry(entry)
    assert "User said" in title


# ===========================================================================
# Message identity  (agent/message.py)
# ===========================================================================

def test_ensure_message_identity_adds_ids():
    from cyrene.agent.message import _ensure_message_identity
    messages = [{"role": "user", "content": "hi"}]
    result = _ensure_message_identity(messages)
    assert len(result) == 1
    assert result[0]["message_id"].startswith("msg_")


def test_ensure_message_identity_preserves_existing():
    from cyrene.agent.message import _ensure_message_identity
    messages = [{"role": "user", "content": "hi", "message_id": "msg_existing"}]
    result = _ensure_message_identity(messages)
    assert result[0]["message_id"] == "msg_existing"


# ===========================================================================
# Dedup / merge  (agent/message.py)
# ===========================================================================

def test_dedupe_keeps_latest_at_original_position():
    from cyrene.agent.message import _dedupe_messages_by_id
    messages = [
        {"message_id": "m1", "content": "first"},
        {"message_id": "m2", "content": "second"},
        {"message_id": "m1", "content": "updated"},
    ]
    result = _dedupe_messages_by_id(messages)
    assert len(result) == 2
    assert result[0]["message_id"] == "m1"
    assert result[0]["content"] == "updated"
    assert result[1]["message_id"] == "m2"


def test_merge_incoming_replaces_existing():
    from cyrene.agent.message import _merge_message_sequence
    existing = [
        {"message_id": "m1", "content": "old", "round_id": "r1"},
        {"message_id": "m2", "content": "keep", "round_id": "r1"},
    ]
    incoming = [
        {"message_id": "m1", "content": "new", "round_id": "r1"},
    ]
    result = _merge_message_sequence(existing, incoming)
    assert len(result) == 2
    assert result[0]["content"] == "new"
    assert result[1]["content"] == "keep"


def test_merge_appends_new_messages():
    from cyrene.agent.message import _merge_message_sequence
    existing = [{"message_id": "m1", "content": "a"}]
    incoming = [{"message_id": "m2", "content": "b"}]
    result = _merge_message_sequence(existing, incoming)
    assert len(result) == 2
    assert result[1]["message_id"] == "m2"


def test_live_message_equivalence_uses_tool_call_identity():
    from cyrene.agent.session import _messages_equivalent

    left = {
        "role": "assistant",
        "message_id": "m1",
        "round_id": "round_1",
        "content": "checking",
        "tool_calls": [{"id": "call_1", "function": {"name": "WebSearch"}}],
    }
    right = {
        "role": "assistant",
        "message_id": "m2",
        "round_id": "round_1",
        "content": "checking",
        "tool_calls": [{"id": "call_1", "function": {"name": "WebSearch"}}],
    }

    assert _messages_equivalent(left, right)


def test_merge_live_block_dedupes_repeated_tool_call_batches():
    from cyrene.agent.session import _merge_live_block

    existing = [
        {
            "role": "assistant",
            "message_id": "m1",
            "round_id": "round_1",
            "content": "checking",
            "tool_calls": [{"id": "call_1", "function": {"name": "WebSearch"}}],
        },
        {"role": "tool", "message_id": "t1", "round_id": "round_1", "tool_call_id": "call_1", "content": "old"},
    ]
    incoming = [
        {
            "role": "assistant",
            "message_id": "m2",
            "round_id": "round_1",
            "content": "checking",
            "tool_calls": [{"id": "call_1", "function": {"name": "WebSearch"}}],
        },
        {"role": "tool", "message_id": "t2", "round_id": "round_1", "tool_call_id": "call_1", "content": "new"},
    ]

    result = _merge_live_block(existing, incoming)

    assert len(result) == 2
    assert result[0]["message_id"] == "m2"
    assert result[1]["message_id"] == "t2"
    assert result[1]["content"] == "new"


# ===========================================================================
# JSON extraction  (agent/message.py)
# ===========================================================================

def test_extract_json_object_plain():
    from cyrene.agent.message import _extract_json_object
    assert _extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_fenced():
    from cyrene.agent.message import _extract_json_object
    result = _extract_json_object('```json\n{"a": 1}\n```')
    assert result == {"a": 1}


def test_extract_json_object_invalid():
    from cyrene.agent.message import _extract_json_object
    assert _extract_json_object("not json") == {}


# ===========================================================================
# Tool result helpers  (agent/message.py)
# ===========================================================================

def test_tool_result_requests_input():
    from cyrene.agent.message import _tool_result_requests_user_input
    assert _tool_result_requests_user_input('{"status": "awaiting_user"}')


def test_tool_result_not_requesting():
    from cyrene.agent.message import _tool_result_requests_user_input
    assert not _tool_result_requests_user_input('{"status": "ok"}')
    assert not _tool_result_requests_user_input("")


# ===========================================================================
# Replaceable live message  (agent/message.py)
# ===========================================================================

def test_is_replaceable_matches_round():
    from cyrene.agent.message import _is_replaceable_live_message
    entry = {"round_id": "round_123", "content": "hi"}
    assert _is_replaceable_live_message(entry, "round_123")


def test_is_replaceable_wrong_round():
    from cyrene.agent.message import _is_replaceable_live_message
    entry = {"round_id": "round_123", "content": "hi"}
    assert not _is_replaceable_live_message(entry, "round_456")


def test_is_replaceable_guidance_not_replaced():
    from cyrene.agent.message import _is_replaceable_live_message
    entry = {"round_id": "round_123", "content": "hi", "queued_guidance_id": "guidance_1"}
    assert not _is_replaceable_live_message(entry, "round_123")


# ===========================================================================
# Message suffix after persisted prefix  (agent/message.py)
# ===========================================================================

def test_suffix_by_message_id():
    from cyrene.agent.message import _message_suffix_after_persisted_prefix
    base = [{"message_id": "m1"}, {"message_id": "m2"}]
    messages = [{"message_id": "m1"}, {"message_id": "m2"}, {"message_id": "m3", "content": "new"}]
    suffix = _message_suffix_after_persisted_prefix(messages, base, 0)
    assert len(suffix) == 1
    assert suffix[0]["message_id"] == "m3"


def test_suffix_fallback_prefix_len():
    from cyrene.agent.message import _message_suffix_after_persisted_prefix
    base = [{"role": "user"}, {"role": "assistant"}]
    messages = [{"role": "user"}, {"role": "assistant"}, {"role": "user", "content": "new"}]
    suffix = _message_suffix_after_persisted_prefix(messages, base, 2)
    assert len(suffix) == 1
    assert suffix[0]["content"] == "new"


# ===========================================================================
# extract_new_references  (modules/deep_research.py)
# ===========================================================================

def test_extract_new_references_with_heading():
    from cyrene.modules.deep_research import extract_new_references
    text = "Some body text.\n\n## New References\n[1] https://example.com/a\n[2] https://example.com/b"
    body, refs = extract_new_references(text)
    assert "Some body" in body
    assert len(refs) == 2
    assert "[1]" in refs[0]
    assert "[2]" in refs[1]


def test_extract_new_references_chinese_heading():
    from cyrene.modules.deep_research import extract_new_references
    text = "正文内容。\n\n## 参考文献\n[1] https://example.com/c"
    body, refs = extract_new_references(text)
    assert "正文" in body
    assert len(refs) == 1


def test_extract_new_references_orphan_fallback():
    from cyrene.modules.deep_research import extract_new_references
    text = "Some body text.\n[1] https://example.com/x\n[2] https://example.com/y"
    body, refs = extract_new_references(text)
    assert len(refs) >= 1
    assert "[1]" in refs[0]


def test_extract_new_references_no_refs():
    from cyrene.modules.deep_research import extract_new_references
    text = "Just body text, no references."
    body, refs = extract_new_references(text)
    assert body == "Just body text, no references."
    assert refs == []


# ===========================================================================
# strip_stray_references  (modules/deep_research.py)
# ===========================================================================

def test_strip_stray_references_removes_ref_block():
    from cyrene.modules.deep_research import strip_stray_references
    text = "Some content.\n## References\n[1] example.com\nMore content."
    result = strip_stray_references(text)
    assert "Some content." in result
    assert "## References" not in result
    assert "[1]" not in result
    assert "More content." in result


def test_strip_stray_references_no_ref_block():
    from cyrene.modules.deep_research import strip_stray_references
    text = "Clean content without references."
    result = strip_stray_references(text)
    assert result == "Clean content without references."


# ===========================================================================
# deduplicate_references  (modules/deep_research.py)
# ===========================================================================

def test_deduplicate_references_by_url():
    from cyrene.modules.deep_research import deduplicate_references
    entries = [
        "[1] https://example.com/a",
        "[2] https://example.com/b",
        "[3] https://example.com/a",
    ]
    deduped, mapping = deduplicate_references(entries)
    assert len(deduped) == 2
    assert mapping[3] == 1


def test_deduplicate_references_no_duplicates():
    from cyrene.modules.deep_research import deduplicate_references
    entries = [
        "[1] https://example.com/a",
        "[2] https://example.com/b",
    ]
    deduped, mapping = deduplicate_references(entries)
    assert len(deduped) == 2
    assert mapping == {1: 1, 2: 2}


# ===========================================================================
# fill_missing_references  (modules/deep_research.py)
# ===========================================================================

def test_fill_missing_references_adds_placeholder():
    from cyrene.modules.deep_research import fill_missing_references
    body = "See [1] and [3] for details."
    refs = ["[1] Source A"]
    result = fill_missing_references(body, refs)
    assert len(result) == 2


def test_fill_missing_references_all_present():
    from cyrene.modules.deep_research import fill_missing_references
    body = "See [1] for details."
    refs = ["[1] Source A"]
    result = fill_missing_references(body, refs)
    assert len(result) == 1
    assert result == refs


# ===========================================================================
# renumber_citations  (modules/deep_research.py)
# ===========================================================================

def test_renumber_citations():
    from cyrene.modules.deep_research import renumber_citations
    text = "See [1] and [3] for details."
    mapping = {1: 1, 3: 2}
    result = renumber_citations(text, mapping)
    assert "[1]" in result
    assert "[2]" in result
    assert "[3]" not in result


# ===========================================================================
# assemble_report  (modules/deep_research.py)
# ===========================================================================

def test_assemble_report_basic():
    from cyrene.modules.deep_research import assemble_report
    sections = ["## Intro\nContent here.", "## Analysis\nMore content."]
    refs = ["[1] https://example.com"]
    outline = {"title": "Test Report"}
    report = assemble_report(sections, refs, outline)
    assert "# Test Report" in report
    assert "## Intro" in report
    assert "## 参考文献" in report
    assert "[1]" in report


def test_assemble_report_with_dedup_mapping():
    from cyrene.modules.deep_research import assemble_report
    sections = ["## Intro\nSee [2] for details."]
    refs = ["[1] Source A"]
    outline = {"title": "Report"}
    mapping = {2: 1}
    report = assemble_report(sections, refs, outline, dedup_mapping=mapping)
    assert "[2]" not in report
    assert "[1]" in report


# ===========================================================================
# parse_length_preference  (modules/deep_research.py)
# ===========================================================================

def test_parse_length_short():
    from cyrene.modules.deep_research import parse_length_preference
    msgs = [{"role": "user", "content": "给我一个短报告，10页左右"}]
    assert parse_length_preference(msgs) == "short"


def test_parse_length_long():
    from cyrene.modules.deep_research import parse_length_preference
    msgs = [{"role": "user", "content": "写一个长报告，30页"}]
    assert parse_length_preference(msgs) == "long"


def test_parse_length_medium_default():
    from cyrene.modules.deep_research import parse_length_preference
    msgs = [{"role": "user", "content": "Just a normal question"}]
    assert parse_length_preference(msgs) == "medium"


def test_parse_length_prefers_latest():
    from cyrene.modules.deep_research import parse_length_preference
    msgs = [
        {"role": "user", "content": "一个短报告"},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": "算了写长一点，30页"},
    ]
    assert parse_length_preference(msgs) == "long"
