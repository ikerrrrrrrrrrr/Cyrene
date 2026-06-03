import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Patch missing deps before any cyrene import
sys.modules.setdefault("PIL", MagicMock())
sys.modules["PIL"].Image = MagicMock()
sys.modules.setdefault("pypdf", MagicMock())


def _patch_call_llm(monkeypatch, fake):
    """Patch _call_llm in all sub-modules that import it at module level."""
    from cyrene.agent import state as _s, agent as _a, coordinator as _c, guidance as _g, session as _se
    for _mod in (_s, _a, _c, _g, _se):
        if hasattr(_mod, '_call_llm'):
            monkeypatch.setattr(_mod, '_call_llm', fake)


def _patch_call_llm_stream(monkeypatch, fake):
    from cyrene.agent import guidance as _g
    monkeypatch.setattr(_g, '_call_llm_stream', fake)


def _patch_save_session(monkeypatch, fake):
    """Patch _save_session_messages in all sub-modules that import it."""
    from cyrene.agent import agent as _a, session as _se
    for _mod in (_a, _se):
        if hasattr(_mod, '_save_session_messages'):
            monkeypatch.setattr(_mod, '_save_session_messages', fake)


def _patch_append_session(monkeypatch, fake):
    from cyrene.agent import agent as _a, session as _se
    for _mod in (_a, _se):
        if hasattr(_mod, '_append_session_message'):
            monkeypatch.setattr(_mod, '_append_session_message', fake)


def _patch_execute_tool(monkeypatch, fake):
    """Patch _execute_tool in all sub-modules that import it."""
    from cyrene.agent import agent as _a, coordinator as _c
    for _mod in (_a, _c):
        if hasattr(_mod, '_execute_tool'):
            monkeypatch.setattr(_mod, '_execute_tool', fake)


def _patch_state_file(monkeypatch, path):
    from cyrene.agent import state as _s
    monkeypatch.setattr(_s, 'STATE_FILE', path)


def _patch_data_dir(monkeypatch, path):
    from cyrene.agent import state as _s
    monkeypatch.setattr(_s, 'DATA_DIR', path)


def _patch_runtime_context(monkeypatch, *, get_context=None, get_memory_context=None):
    from cyrene import agent
    from cyrene.agent import coordinator as _c
    if get_context is not None:
        monkeypatch.setattr(agent, "get_context", get_context)
        monkeypatch.setattr(_c, "get_context", get_context)
    if get_memory_context is not None:
        monkeypatch.setattr(agent, "get_memory_context", get_memory_context)
        monkeypatch.setattr(_c, "get_memory_context", get_memory_context)


async def test_execution_agent_returns_quit_text(monkeypatch):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    async def fake_call_llm(messages, tools=None, max_tokens=32000):
        return {
            "content": "scheduled task completed",
            "tool_calls": [{"id": "q1", "function": {"name": "quit", "arguments": "{}"}}],
        }

    _patch_call_llm(monkeypatch, fake_call_llm)
    result = await agent._run_execution_agent("do something", None, 0, "db.sqlite3")
    assert result == "scheduled task completed"


def test_get_memory_context_includes_short_term_by_default(tmp_path, monkeypatch):
    from cyrene import memory
    from cyrene import short_term

    short_term.init_short_term(tmp_path)
    short_term.save_entries([
        {
            "content": "user prefers concise replies",
            "type": "preference",
            "first_seen": "2026-05-18",
            "last_mentioned": "2026-05-19",
            "mention_count": 2,
            "emotional_valence": 0,
        }
    ])
    monkeypatch.setattr(memory, "read_shallow_memory", lambda: "## SELF:IDENTITY\n- test memory")
    context = memory.get_memory_context()

    assert "SELF:IDENTITY" in context
    assert "Short-term cross-session memory" in context
    assert "user prefers concise replies" in context


def test_get_memory_context_can_skip_short_term(tmp_path, monkeypatch):
    from cyrene import memory
    from cyrene import short_term

    short_term.init_short_term(tmp_path)
    short_term.save_entries([
        {
            "content": "user likes jasmine tea",
            "type": "fact",
            "first_seen": "2026-05-18",
            "last_mentioned": "2026-05-19",
            "mention_count": 1,
            "emotional_valence": 0,
        }
    ])
    monkeypatch.setattr(memory, "read_shallow_memory", lambda: "## SELF:BELIEFS\n- test belief")
    context = memory.get_memory_context(include_short_term=False)

    assert "SELF:BELIEFS" in context
    assert "Short-term cross-session memory" not in context
    assert "user likes jasmine tea" not in context


def test_agent_module_reexports_memory_helpers():
    from cyrene import agent, memory, short_term

    assert agent.get_context is short_term.get_context
    assert agent.get_memory_context is memory.get_memory_context


async def test_execute_tool_awaits_event_publish(monkeypatch):
    from cyrene import tools

    seen = {"published": False}

    async def fake_handler(arguments, bot, chat_id, db_path, notify_state):
        return "ok"

    async def fake_publish_event(event):
        seen["published"] = True
        seen["event"] = event

    monkeypatch.setitem(tools.TOOL_HANDLERS, "__test_tool__", fake_handler)

    from cyrene import debug

    monkeypatch.setattr(debug, "publish_event", fake_publish_event)
    result = await tools._execute_tool("__test_tool__", {}, None, 0, "db.sqlite3", None)

    assert result == "ok"
    assert seen["published"] is True
    assert seen["event"]["type"] == "tool_call"

    tools.TOOL_HANDLERS.pop("__test_tool__", None)


async def test_subagent_cannot_send_user_visible_message(monkeypatch):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import tools

    called = {"append": False}

    async def fake_append_system_message(*args, **kwargs):
        called["append"] = True
        return {}

    monkeypatch.setattr(_agent_session, "append_system_message", fake_append_system_message)

    token = agent._current_agent_id.set("agent_worker")
    try:
        result = await tools._tool_send_user_message({"text": "hello from subagent"}, None, 0, "db.sqlite3", None)
    finally:
        agent._current_agent_id.reset(token)

    assert "Only the main agent can send a user-visible WebUI message" in result
    assert called["append"] is False


def test_subagent_tool_defs_hide_main_only_tools():
    from cyrene import tools

    main_defs = {item["function"]["name"] for item in tools.get_active_tool_defs_for_actor("main")}
    sub_defs = {item["function"]["name"] for item in tools.get_active_tool_defs_for_actor("subagent")}

    assert "send_message" in main_defs
    assert "spawn_subagent" in main_defs
    assert "send_message" not in sub_defs
    assert "spawn_subagent" not in sub_defs
    assert "ask_user" not in sub_defs
    assert "send_agent_message" in sub_defs


async def test_recall_memory_tool_returns_archived_matches_and_persisted_memory(tmp_path, monkeypatch):
    from cyrene import conversations
    from cyrene import short_term
    from cyrene import tools

    conversations_dir = tmp_path / "conversations"
    conversations_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", conversations_dir)

    (conversations_dir / "2026-05-19.md").write_text(
        "# Conversations - 2026-05-19\n\n"
        "<!-- session_title: 第一场 -->\n\n"
        "## 09:00:00 UTC\n\n"
        "<!-- archive_session_id: session_alpha -->\n"
        "<!-- session_title: 第一场 -->\n"
        "<!-- round_id: round_1 -->\n"
        "<!-- round_title: 设计角色 -->\n\n"
        "**User**: 先聊角色设定\n\n"
        "**Ape**: 角色偏冷静理性。\n\n"
        "---\n\n"
        "## 10:00:00 UTC\n\n"
        "<!-- archive_session_id: session_beta -->\n"
        "<!-- session_title: 第二场 -->\n"
        "<!-- round_id: round_2 -->\n"
        "<!-- round_title: 偏好总结 -->\n\n"
        "**User**: 记住我偏好简洁回答\n\n"
        "**Ape**: 已记录你偏好简洁回答。\n\n"
        "---\n",
        encoding="utf-8",
    )

    short_term.init_short_term(tmp_path)
    short_term.save_entries([
        {
            "content": "user prefers concise replies",
            "type": "preference",
            "first_seen": "2026-05-19",
            "last_mentioned": "2026-05-19",
            "mention_count": 1,
            "emotional_valence": 0,
        }
    ])
    monkeypatch.setattr(tools, "read_shallow_memory", lambda: "## RELATIONSHIP:USER\n- Trust level: warm")

    result = await tools._tool_recall_memory(
        {"session_id": "archive_2026-05-19_session_beta", "limit": 2},
        None,
        0,
        "db.sqlite3",
        None,
    )
    payload = json.loads(result)

    assert payload["matches"][0]["archive_session_id"] == "session_beta"
    assert payload["matches"][0]["session_title"] == "第二场"
    assert payload["matches"][0]["assistant"] == "已记录你偏好简洁回答。"
    assert "user prefers concise replies" in payload["short_term_memory"]
    assert "Trust level: warm" in payload["soul_memory"]


async def test_run_chat_agent_avoids_duplicate_short_term_memory_in_system_prompt(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    seen: dict[str, Any] = {}

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(_agent_session, "_refresh_session_labels", AsyncMock())
    _patch_runtime_context(monkeypatch, get_context=lambda max_chars=5000: "[Previous context:]\n- remembers tea")

    def fake_get_memory_context(include_short_term: bool = True):
        seen["include_short_term"] = include_short_term
        return "## Memory Context\n- stable trait"

    async def fake_run_main_agent(user_message, history, bot, chat_id, db_path, system_prompt="", client_request_id="", persist_user_message=True, lang="", **kwargs):
        seen["history"] = history
        seen["system_prompt"] = system_prompt
        return "ok"

    _patch_runtime_context(monkeypatch, get_memory_context=fake_get_memory_context)
    monkeypatch.setattr(_agent_core, "_run_main_agent", fake_run_main_agent)

    result = await agent._run_chat_agent("hello", None, 0, "db.sqlite3")

    assert result == "ok"
    assert seen["include_short_term"] is False
    assert seen["history"][0]["content"].startswith("[Restored context]")
    assert "stable trait" in seen["system_prompt"]


async def test_run_chat_agent_schedules_session_label_refresh_without_blocking_reply(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene import behavior_learning
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    scheduled: list[tuple[str, str]] = []

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    _patch_runtime_context(monkeypatch, get_context=lambda max_chars=5000: "")

    async def fake_run_main_agent(user_message, history, bot, chat_id, db_path, system_prompt="", **kwargs):
        return "ok"

    monkeypatch.setattr(_agent_core, "_run_main_agent", fake_run_main_agent)
    monkeypatch.setattr(_agent_coordinator, "_schedule_session_label_refresh", lambda message, round_id: scheduled.append((message, round_id)))
    monkeypatch.setattr(behavior_learning, "begin_turn", AsyncMock(return_value=None))

    result = await asyncio.wait_for(agent._run_chat_agent("hello", None, 0, "db.sqlite3"), timeout=0.1)

    assert result == "ok"
    assert len(scheduled) == 1
    assert scheduled[0][0] == "hello"
    assert scheduled[0][1].startswith("round_")


async def test_call_llm_falls_back_to_next_model_candidate(monkeypatch):
    from cyrene import call_llm as cll

    attempts: list[tuple[str, str]] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, Any], request: httpx.Request):
            self.status_code = status_code
            self._payload = payload
            self.request = request

        def json(self):
            return self._payload

        def raise_for_status(self):
            raise httpx.HTTPStatusError("upstream failure", request=self.request, response=httpx.Response(self.status_code, request=self.request))

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, endpoint, json=None, headers=None):
            attempts.append((str(json.get("model") or ""), endpoint))
            request = httpx.Request("POST", endpoint)
            if json.get("model") == "primary-model":
                return FakeResponse(503, {}, request)
            return FakeResponse(
                200,
                {
                    "choices": [{"message": {"role": "assistant", "content": "fallback ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
                request,
            )

    monkeypatch.setattr(
        cll,
        "get_models",
        lambda: [
            {"id": "candidate-1", "model": "primary-model", "base_url": "https://primary.example/v1", "api_key": "primary-key"},
            {"id": "candidate-2", "model": "fallback-model", "base_url": "https://fallback.example/v1", "api_key": "fallback-key"},
        ],
    )
    monkeypatch.setattr(cll.httpx, "AsyncClient", lambda *args, **kwargs: FakeClient())
    monkeypatch.setenv("OPENAI_MODEL", "primary-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://primary.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "primary-key")

    message = await cll.call_llm([{"role": "user", "content": "hello"}], max_tokens=32)

    assert message["content"] == "fallback ok"
    assert attempts == [
        ("primary-model", "https://primary.example/v1/chat/completions"),
        ("fallback-model", "https://fallback.example/v1/chat/completions"),
    ]


async def test_call_llm_stream_falls_back_to_next_model_candidate(monkeypatch):
    from cyrene import call_llm as cll

    attempts: list[tuple[str, str]] = []
    emitted: list[dict[str, Any]] = []

    class FakeStreamResponse:
        def __init__(self, status_code: int, lines: list[str], request: httpx.Request):
            self.status_code = status_code
            self._lines = lines
            self.request = request

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_lines(self):
            for line in self._lines:
                yield line

        def raise_for_status(self):
            raise httpx.HTTPStatusError("upstream failure", request=self.request, response=httpx.Response(self.status_code, request=self.request))

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, endpoint, json=None, headers=None):
            attempts.append((str(json.get("model") or ""), endpoint))
            request = httpx.Request(method, endpoint)
            if json.get("model") == "primary-model":
                return FakeStreamResponse(503, [], request)
            return FakeStreamResponse(
                200,
                [
                    'data: {"choices":[{"delta":{"content":"hello "}}]}',
                    'data: {"choices":[{"delta":{"content":"world"}}],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}',
                    "data: [DONE]",
                ],
                request,
            )

    monkeypatch.setattr(
        cll,
        "get_models",
        lambda: [
            {"id": "candidate-1", "model": "primary-model", "base_url": "https://primary.example/v1", "api_key": "primary-key"},
            {"id": "candidate-2", "model": "fallback-model", "base_url": "https://fallback.example/v1", "api_key": "fallback-key"},
        ],
    )
    monkeypatch.setattr(cll.httpx, "AsyncClient", lambda *args, **kwargs: FakeClient())
    monkeypatch.setenv("OPENAI_MODEL", "primary-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://primary.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "primary-key")

    async def _capture(event):
        emitted.append(event)

    message = await cll.call_llm(
        [{"role": "user", "content": "hello"}],
        max_tokens=32,
        stream=True,
        stream_callback=_capture,
    )

    assert message["content"] == "hello world"
    assert message["usage"]["total_tokens"] == 3
    assert attempts == [
        ("primary-model", "https://primary.example/v1/chat/completions"),
        ("fallback-model", "https://fallback.example/v1/chat/completions"),
    ]
    assert emitted[0]["type"] == "reply_start"
    assert emitted[-1]["type"] == "reply_done"


def test_normalize_dsml_tool_calls_converts_textual_fallback():
    from cyrene import call_llm as cll

    message = {
        "role": "assistant",
        "content": (
            '<｜｜DSML｜｜tool_calls>\n'
            '<｜｜DSML｜｜invoke name="WebSearch">\n'
            '<｜｜DSML｜｜parameter name="query" string="true">AoA prediction</｜｜DSML｜｜parameter>\n'
            '</｜｜DSML｜｜invoke>\n'
            '<｜｜DSML｜｜invoke name="quit"/>\n'
            '</｜｜DSML｜｜tool_calls>'
        ),
    }
    tools = [
        {"type": "function", "function": {"name": "WebSearch"}},
        {"type": "function", "function": {"name": "quit"}},
    ]

    normalized = cll._normalize_dsml_tool_calls(message, tools)

    assert normalized["content"] == ""
    assert [call["function"]["name"] for call in normalized["tool_calls"]] == ["WebSearch", "quit"]
    assert json.loads(normalized["tool_calls"][0]["function"]["arguments"]) == {"query": "AoA prediction"}
    assert json.loads(normalized["tool_calls"][1]["function"]["arguments"]) == {}


def test_normalize_dsml_tool_calls_rejects_unknown_tools():
    from cyrene import call_llm as cll

    message = {
        "role": "assistant",
        "content": (
            '<｜｜DSML｜｜tool_calls>'
            '<｜｜DSML｜｜invoke name="UnknownTool"/>'
            '</｜｜DSML｜｜tool_calls>'
        ),
    }

    assert cll._normalize_dsml_tool_calls(message, [{"type": "function", "function": {"name": "WebSearch"}}]) == message


def test_retry_safe_guide_round_id_drops_completed_round_target():
    from webui import routes

    assert routes._retry_safe_guide_round_id("round_old", retry=True) == ""
    assert routes._retry_safe_guide_round_id(" round_live ", retry=False) == "round_live"


async def test_call_llm_secondary_concurrency_counter(monkeypatch):
    from cyrene import call_llm as cll

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "secondary ok"}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, endpoint, json=None, headers=None):
            return FakeResponse()

    monkeypatch.setattr(cll.httpx, "AsyncClient", lambda *args, **kwargs: FakeClient())
    monkeypatch.setattr(cll, "_secondary_in_flight", 0)

    message = await cll.call_llm(
        [{"role": "user", "content": "hello"}],
        candidates=[{
            "id": "secondary",
            "model": "secondary-model",
            "api_key": "",
            "endpoints": ["https://secondary.example/v1/chat/completions"],
            "max_concurrency": 1,
        }],
        publish_events=False,
        record_usage=False,
    )

    assert message["content"] == "secondary ok"
    assert cll._secondary_in_flight == 0


async def test_run_vision_chat_uses_vision_candidates_after_primary_failure(monkeypatch):
    from cyrene import attachments as att
    from cyrene import call_llm as cll

    attempts: list[tuple[str, str]] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, Any], request: httpx.Request):
            self.status_code = status_code
            self._payload = payload
            self.request = request

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code < 400:
                return
            raise httpx.HTTPStatusError("vision unsupported", request=self.request, response=httpx.Response(self.status_code, request=self.request))

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, endpoint, json=None, headers=None):
            attempts.append((str(json.get("model") or ""), endpoint))
            request = httpx.Request("POST", endpoint)
            if json.get("model") == "primary-model":
                return FakeResponse(400, {}, request)
            return FakeResponse(
                200,
                {"choices": [{"message": {"content": "vision fallback ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                request,
            )

    monkeypatch.setattr(
        cll,
        "get_models",
        lambda: [{"id": "candidate-1", "model": "primary-model", "base_url": "https://primary.example/v1", "api_key": "primary-key"}],
    )
    monkeypatch.setattr(
        cll,
        "get_vision_models",
        lambda: [{"id": "vision-1", "model": "vision-model", "base_url": "https://vision.example/v1", "api_key": "vision-key"}],
    )
    monkeypatch.setattr(cll.httpx, "AsyncClient", lambda *args, **kwargs: FakeClient())
    monkeypatch.setenv("OPENAI_MODEL", "primary-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://primary.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "primary-key")

    payload = await att.run_vision_chat(
        [{"type": "text", "text": "describe"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}],
        content_prompt="describe",
    )

    assert payload["vision_text"] == "vision fallback ok"
    assert payload["vision_model"] == "vision-model"
    assert attempts == [
        ("primary-model", "https://primary.example/v1/chat/completions"),
        ("vision-model", "https://vision.example/v1/chat/completions"),
    ]


async def test_chat_with_uploaded_images_falls_back_to_vision_model(monkeypatch, tmp_path):
    from webui import routes

    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"fake-image")

    request = httpx.Request("POST", "https://primary.example/v1/chat/completions")
    response = httpx.Response(400, request=request)

    async def fake_call_llm(messages, tools=None, max_tokens=None):
        raise httpx.HTTPStatusError("image unsupported", request=request, response=response)

    async def fake_run_vision_chat(content, content_prompt=""):
        return {"vision_text": "vision route ok"}

    monkeypatch.setattr(routes, "_call_llm", fake_call_llm)
    monkeypatch.setattr(routes, "run_vision_chat", fake_run_vision_chat)
    monkeypatch.setattr(routes, "format_httpx_error", lambda exc: "image unsupported")

    result = await routes._chat_with_uploaded_images(
        "",
        [{"path": str(image_path), "content_type": "image/png"}],
    )

    assert result == "vision route ok"


async def test_save_session_messages_emits_session_update(tmp_path, monkeypatch):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import debug

    seen = []

    async def fake_publish_event(event):
        seen.append(event)

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(debug, "publish_event", fake_publish_event)

    await agent._save_session_messages([
        {"role": "user", "content": "hi", "round_id": "round_1"},
        {"role": "assistant", "content": "hello", "round_id": "round_1"},
    ])

    assert seen
    assert seen[-1]["type"] == "session_update"
    assert seen[-1]["message_count"] == 2
    assert seen[-1]["last_role"] == "assistant"
    assert seen[-1]["round_id"] == "round_1"


async def test_proactive_round_hides_internal_prompt_and_initial_detail(tmp_path, monkeypatch):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import debug

    events = []

    async def fake_publish_event(event):
        events.append(dict(event))

    async def fake_call_llm(messages, tools=None, max_tokens=32000):
        return {
            "content": "最近你之前提到的那件事怎么样了？如果你想，我可以继续帮你拆一下。",
            "tool_calls": [],
        }

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(_agent_session, "_refresh_session_labels", AsyncMock())
    monkeypatch.setattr(agent, "get_memory_context", lambda include_short_term=True: "")
    _patch_call_llm(monkeypatch, fake_call_llm)
    monkeypatch.setattr(debug, "publish_event", fake_publish_event)

    result = await agent._run_chat_agent(
        "internal proactive instruction",
        None,
        0,
        "db.sqlite3",
        persist_user_message=False,
        public_prompt="",
        refresh_labels=False,
        hide_initial_detail=True,
        assistant_message_meta={"proactive": True, "system_initiated": True},
    )

    assert "如果你想" in result

    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    messages = saved["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == result
    assert messages[0]["proactive"] is True
    assert messages[0]["system_initiated"] is True

    phase_events = [event for event in events if event.get("type") == "phase_transition"]
    assert phase_events
    assert phase_events[0]["from"] == "phase1_decision"
    assert phase_events[0]["to"] == "chat_only"
    assert "detail" not in phase_events[0]


def test_build_live_flow_round_skips_input_for_system_initiated_messages():
    from webui import routes

    raw_msgs = [
        {
            "role": "assistant",
            "content": "最近你之前提到的项目推进得怎么样了？",
            "round_id": "round_1",
            "message_id": "msg_1",
            "system_initiated": True,
            "proactive": True,
        }
    ]
    messages = routes._convert_messages(raw_msgs)
    nodes, edges, _bottom = routes._build_live_flow_round(
        prefix="r1_",
        raw_msgs=raw_msgs,
        messages=messages,
        subagents=[],
        registry={},
        recent_events=[{"type": "phase_transition", "to": "chat_only"}],
        y_offset=0,
        round_id="round_1",
    )

    assert not any(node["kind"] == "input" for node in nodes)
    assert any(node["kind"] == "main" for node in nodes)
    assert any(node["kind"] == "output" for node in nodes)
    assert not any(edge.get("from") == "r1_n_user" for edge in edges)


async def test_heartbeat_proactive_check_uses_main_agent_loop(monkeypatch):
    from cyrene import scheduler

    seen = {}

    monkeypatch.setattr(scheduler, "OWNER_ID", 7)
    monkeypatch.setattr(scheduler, "_load_lottery_state", lambda: None)
    monkeypatch.setattr(scheduler, "_save_lottery_state", lambda: None)
    monkeypatch.setattr(scheduler, "_is_daytime", lambda: True)
    monkeypatch.setattr(scheduler, "_silence_hours", lambda: 96.0)

    async def fake_context(_db_path=""):
        return "## Recent memories about the user\n- user is preparing a launch"

    async def fake_run_heartbeat_agent(prompt, bot, chat_id, db_path):
        seen["prompt"] = prompt
        seen["chat_id"] = chat_id
        seen["db_path"] = db_path
        return "user-facing proactive message"

    monkeypatch.setattr(scheduler, "_assemble_proactive_context", fake_context)
    monkeypatch.setattr(scheduler, "run_heartbeat_agent", fake_run_heartbeat_agent)

    await scheduler._heartbeat_proactive_check(bot=None, db_path="db.sqlite3")

    assert seen["chat_id"] == 7
    assert seen["db_path"] == "db.sqlite3"
    assert "scheduler-initiated proactive check-in" in seen["prompt"]
    assert "Recent memories about the user" in seen["prompt"]
    assert "If you speak, the final reply will be shown directly to the user" in seen["prompt"]


async def test_heartbeat_proactive_check_stays_silent_when_agent_skips(monkeypatch):
    from cyrene import scheduler

    seen = {"notified": False}

    monkeypatch.setattr(scheduler, "OWNER_ID", 7)
    monkeypatch.setattr(scheduler, "_load_lottery_state", lambda: None)
    monkeypatch.setattr(scheduler, "_save_lottery_state", lambda: None)
    monkeypatch.setattr(scheduler, "_is_daytime", lambda: True)
    monkeypatch.setattr(scheduler, "_silence_hours", lambda: 96.0)

    async def fake_context(_db_path):
        return "## Recent conversation\n- user already closed the loop"

    async def fake_run_heartbeat_agent(prompt, bot, chat_id, db_path):
        seen["prompt"] = prompt
        return ""

    async def fake_notify(*args, **kwargs):
        seen["notified"] = True

    monkeypatch.setattr(scheduler, "_assemble_proactive_context", fake_context)
    monkeypatch.setattr(scheduler, "run_heartbeat_agent", fake_run_heartbeat_agent)
    monkeypatch.setattr(scheduler, "notify", fake_notify)

    await scheduler._heartbeat_proactive_check(bot=None, db_path="db.sqlite3")

    assert seen["notified"] is False
    assert "scheduler-initiated proactive check-in" in seen["prompt"]
    assert "do not interrupt" in seen["prompt"].lower()


async def test_execute_task_fallback_persists_webui_reminder(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import debug
    from cyrene import scheduler

    seen = []

    async def fake_publish_event(event):
        seen.append(event)

    async def fake_run_task_agent(prompt, bot, chat_id, db_path, notify_state=None):
        return "task finished without explicit message"

    async def fake_log_task_run(*args, **kwargs):
        return None

    async def fake_update_task_after_run(*args, **kwargs):
        return None

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
    monkeypatch.setattr(debug, "publish_event", fake_publish_event)
    monkeypatch.setattr(scheduler, "run_task_agent", fake_run_task_agent)
    monkeypatch.setattr(scheduler.db, "log_task_run", fake_log_task_run)
    monkeypatch.setattr(scheduler.db, "update_task_after_run", fake_update_task_after_run)

    agent.STATE_FILE.write_text(json.dumps({"messages": []}, ensure_ascii=False), encoding="utf-8")

    await scheduler._execute_task(
        {
            "id": "task_1",
            "chat_id": 7,
            "prompt": "提醒我喝水",
            "schedule_type": "once",
            "schedule_value": "2026-05-20T10:18:00+00:00",
        },
        bot=None,
        db_path="db.sqlite3",
    )

    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert saved[-1]["content"] == "Reminder: 提醒我喝水"
    assert saved[-1]["system_initiated"] is True
    assert saved[-1]["scheduled"] is True
    assert any(event.get("type") == "assistant_message" and event.get("scheduled") is True for event in seen)


def test_format_httpx_error_includes_request_response_and_cause():
    import httpx
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(502, request=request, text='{"error":"upstream exploded"}')
    cause = ConnectionError("socket closed")
    exc = httpx.HTTPStatusError("Bad Gateway", request=request, response=response)
    exc.__cause__ = cause

    detail = agent.format_httpx_error(exc)

    assert "HTTPStatusError" in detail
    assert "request=POST https://example.test/v1/chat/completions" in detail
    assert "status=502" in detail
    assert 'body={"error":"upstream exploded"}' in detail
    assert "cause=ConnectionError: socket closed" in detail


async def test_send_agent_message_redirects_main_alias():
    from cyrene import tools

    result = await tools._tool_send_agent_message(
        {"to": "danny", "content": "final answer"},
        None,
        0,
        "db.sqlite3",
        None,
    )

    assert "main-agent inbox is reserved for user guidance" in result
    assert "quit response" in result


async def test_send_agent_message_rejects_cross_round_target():
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import subagent
    from cyrene import tools

    await subagent.clear()
    await subagent.register("alice", "task A", round_id="round_old")
    round_token = agent._current_round_id.set("round_new")
    try:
        result = await tools._tool_send_agent_message(
            {"to": "alice", "content": "status?"},
            None,
            0,
            "db.sqlite3",
            None,
        )
    finally:
        agent._current_round_id.reset(round_token)

    assert "current round" in result
    assert "round_new" in result


async def test_send_message_tool_persists_intermediate_reply(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import debug
    from cyrene import tools

    seen = []

    async def fake_publish_event(event):
        seen.append(event)

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(debug, "publish_event", fake_publish_event)

    agent.STATE_FILE.write_text(json.dumps({
        "messages": [
            {"role": "user", "content": "do the work", "round_id": "round_1", "client_request_id": "req_1"},
        ]
    }, ensure_ascii=False), encoding="utf-8")

    round_token = agent._current_round_id.set("round_1")
    request_token = agent._current_client_request_id.set("req_1")
    pending_token = agent._pending_intermediate_user_replies.set([])
    sender_token = agent._current_agent_id.set("main")
    try:
        result = await tools._tool_send_user_message(
            {"text": "先给你一个中途结论：方向是对的，我继续细化。"},
            None,
            0,
            "db.sqlite3",
            None,
        )
    finally:
        agent._current_agent_id.reset(sender_token)
        agent._pending_intermediate_user_replies.reset(pending_token)
        agent._current_client_request_id.reset(request_token)
        agent._current_round_id.reset(round_token)

    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert result == "Mid-run message sent to the user."
    assert saved[-1]["role"] == "assistant"
    assert saved[-1]["content"].startswith("先给你一个中途结论")
    assert saved[-1]["round_id"] == "round_1"
    assert saved[-1]["client_request_id"] == "req_1"
    assert saved[-1]["intermediate_reply"] is True
    assert any(event.get("type") == "assistant_message" and event.get("intermediate") is True for event in seen)


async def test_send_message_tool_from_scheduler_persists_system_message(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import debug
    from cyrene import tools

    seen = []

    async def fake_publish_event(event):
        seen.append(event)

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(tools, "DATA_DIR", tmp_path)
    monkeypatch.setattr(debug, "publish_event", fake_publish_event)

    agent.STATE_FILE.write_text(json.dumps({"messages": []}, ensure_ascii=False), encoding="utf-8")

    sender_token = agent._current_agent_id.set("scheduler")
    try:
        notify_state = {"sent": False}
        result = await tools._tool_send_user_message(
            {"text": "这是调度任务消息"},
            None,
            0,
            "db.sqlite3",
            notify_state,
        )
    finally:
        agent._current_agent_id.reset(sender_token)

    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert result == "Scheduled message sent to the user."
    assert notify_state["sent"] is True
    assert saved[-1]["role"] == "assistant"
    assert saved[-1]["content"] == "这是调度任务消息"
    assert saved[-1]["system_initiated"] is True
    assert saved[-1]["scheduled"] is True
    assert any(event.get("type") == "assistant_message" and event.get("scheduled") is True for event in seen)


async def test_schedule_task_once_normalizes_naive_local_time_to_utc(monkeypatch):
    from datetime import datetime, timezone
    from cyrene import tools

    seen = {}

    async def fake_create_task(db_path, chat_id, prompt, schedule_type, schedule_value, next_run, permission_mode="workspace_only"):
        seen["db_path"] = db_path
        seen["chat_id"] = chat_id
        seen["prompt"] = prompt
        seen["schedule_type"] = schedule_type
        seen["schedule_value"] = schedule_value
        seen["next_run"] = next_run
        seen["permission_mode"] = permission_mode
        return "task_local"

    class _FakeLocalNow(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls(2026, 5, 20, 19, 33, 35, tzinfo=timezone.utc).astimezone()
            return cls(2026, 5, 20, 11, 33, 35, tzinfo=tz)

    monkeypatch.setattr(tools.db, "create_task", fake_create_task)
    monkeypatch.setattr(tools, "datetime", _FakeLocalNow)

    result = await tools._tool_schedule_task(
        {
            "prompt": "send_message(\"2分钟到了\")",
            "schedule_type": "once",
            "schedule_value": "2026-05-20T19:35:35",
        },
        None,
        -1,
        "db.sqlite3",
        None,
    )

    assert result == "Task task_local scheduled. Next run: 2026-05-20T11:35:35+00:00 权限模式：workspace_only"
    assert seen["schedule_value"] == "2026-05-20T11:35:35+00:00"
    assert seen["next_run"] == "2026-05-20T11:35:35+00:00"
    assert seen["permission_mode"] == "workspace_only"


async def test_ask_user_tool_persists_pending_question(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import debug
    from cyrene import tools

    seen = []

    async def fake_publish_event(event):
        seen.append(event)

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(debug, "publish_event", fake_publish_event)

    agent.STATE_FILE.write_text(json.dumps({
        "messages": [
            {"role": "user", "content": "帮我订行程", "round_id": "round_1", "round_title": "订行程"},
        ]
    }, ensure_ascii=False), encoding="utf-8")

    round_token = agent._current_round_id.set("round_1")
    request_token = agent._current_client_request_id.set("req_ask_1")
    sender_token = agent._current_agent_id.set("main")
    try:
        result = await tools._tool_ask_user(
            {"text": "你想去北京还是上海？", "options": ["北京", "上海"]},
            None,
            0,
            "db.sqlite3",
            None,
        )
    finally:
        agent._current_agent_id.reset(sender_token)
        agent._current_client_request_id.reset(request_token)
        agent._current_round_id.reset(round_token)

    payload = json.loads(result)
    state = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))
    pending = state["pending_question"]
    saved = state["messages"]

    assert payload["status"] == "awaiting_user"
    assert pending["text"] == "你想去北京还是上海？"
    assert pending["round_id"] == "round_1"
    assert pending["client_request_id"] == "req_ask_1"
    assert [item["label"] for item in pending["options"]] == ["北京", "上海"]
    assert saved[-1]["role"] == "assistant"
    assert saved[-1]["content"] == "你想去北京还是上海？"
    assert saved[-1]["question_prompt"] is True
    assert saved[-1]["question_id"] == pending["id"]
    assert any(event.get("type") == "user_question" and event.get("question_id") == pending["id"] for event in seen)


async def test_ask_user_wait_state_does_not_persist_assistant_trace(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(_agent_session, "_refresh_session_labels", AsyncMock())

    async def fake_call_llm(messages, tools=None, max_tokens=32000):
        if tools is _agent_state._LIGHT_TOOL_DEFS:
            return {
                "content": "我应该先问清楚。",
                "tool_calls": [
                    {
                        "id": "ask_1",
                        "function": {
                            "name": "ask_user",
                            "arguments": json.dumps({
                                "text": "你更想看攻略还是代码？",
                                "options": ["攻略", "代码"],
                            }, ensure_ascii=False),
                        },
                    }
                ],
            }
        raise AssertionError("Unexpected heavy tool loop")

    async def fake_execute_tool(name, arguments, bot, chat_id, db_path, notify_state):
        assert name == "ask_user"
        await agent._upsert_pending_question({
            "text": str(arguments.get("text", "")),
            "options": list(arguments.get("options", [])),
            "round_id": agent._current_round_id.get(),
            "client_request_id": agent._current_client_request_id.get(),
        })
        return json.dumps({
            "status": "awaiting_user",
            "question_id": "question_fake",
            "option_count": 2,
        }, ensure_ascii=False)

    _patch_call_llm(monkeypatch, fake_call_llm)
    _patch_execute_tool(monkeypatch, fake_execute_tool)

    result = await agent._run_chat_agent("帮我继续", None, 0, "db.sqlite3", client_request_id="req_wait")
    state = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))
    messages = state["messages"]

    assert result == agent._AWAITING_USER_SENTINEL
    assert [msg["role"] for msg in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "帮我继续"
    assert messages[1]["question_prompt"] is True
    assert messages[1]["content"] == "你更想看攻略还是代码？"
    assert "tool_calls" not in messages[1]
    assert state["pending_question"]["text"] == "你更想看攻略还是代码？"


async def test_answer_pending_question_resumes_same_round(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    seen = {}

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    agent.STATE_FILE.write_text(json.dumps({
        "messages": [
            {"role": "user", "content": "做一个旅游计划", "round_id": "round_1", "message_id": "u1"},
            {"role": "assistant", "content": "你更偏向城市还是自然？", "round_id": "round_1", "question_prompt": True, "question_id": "question_1", "message_id": "a1"},
            {"role": "user", "content": "别的轮次", "round_id": "round_2", "message_id": "u2"},
        ],
        "pending_question": {
            "id": "question_1",
            "text": "你更偏向城市还是自然？",
            "round_id": "round_1",
            "round_title": "旅游计划",
            "client_request_id": "req_ask_1",
            "allow_custom": True,
            "options": [{"id": "option_1", "label": "城市"}, {"id": "option_2", "label": "自然"}],
            "asked_at": "2026-05-19T03:00:00+00:00",
            "meta": {"command": "deep-research"},
        },
    }, ensure_ascii=False), encoding="utf-8")

    async def fake_run_chat_agent(
        user_message,
        bot,
        chat_id,
        db_path,
        ephemeral_system="",
        forced_round_id="",
        history_override=None,
        persist_base_messages=None,
        persist_insert_at=None,
        client_request_id="",
        persist_user_message=True,
        public_prompt=None,
        refresh_labels=True,
        hide_initial_detail=False,
        assistant_message_meta=None,
        lang="",
        command="",
    ):
        seen["user_message"] = user_message
        seen["ephemeral_system"] = ephemeral_system
        seen["forced_round_id"] = forced_round_id
        seen["history_override"] = history_override
        seen["persist_base_messages"] = persist_base_messages
        seen["persist_insert_at"] = persist_insert_at
        seen["client_request_id"] = client_request_id
        seen["persist_user_message"] = persist_user_message
        seen["command"] = command
        return "继续完成后的最终答案"

    monkeypatch.setattr(_agent_coordinator, "_run_chat_agent", fake_run_chat_agent)

    result = await agent.answer_pending_question(
        "question_1",
        "我更偏向城市",
        None,
        0,
        "db.sqlite3",
        client_request_id="req_answer_1",
    )

    state = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))

    assert result == "继续完成后的最终答案"
    assert "pending_question" not in state
    assert seen["user_message"] == "我更偏向城市"
    assert "answers your earlier clarification question" in seen["ephemeral_system"]
    assert seen["forced_round_id"] == "round_1"
    assert [msg["content"] for msg in seen["history_override"]] == ["做一个旅游计划", "你更偏向城市还是自然？"]
    assert [msg["content"] for msg in seen["persist_base_messages"]] == ["做一个旅游计划", "你更偏向城市还是自然？", "别的轮次"]
    assert seen["persist_insert_at"] == 2
    assert seen["client_request_id"] == "req_answer_1"
    assert seen["persist_user_message"] is True
    assert seen["command"] == "deep-research"


def test_build_current_session_exposes_pending_question(monkeypatch, tmp_path):
    from webui import routes

    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(routes, "_SERVER_STARTED_AT", 0)
    monkeypatch.setattr(routes, "get_live_rounds", lambda: [])
    monkeypatch.setattr(routes, "list_live_shells", lambda include_exited=False: [])

    routes.STATE_FILE.write_text(json.dumps({
        "session_title": "当前会话",
        "messages": [
            {"role": "user", "content": "帮我订机票", "round_id": "round_1", "message_id": "u1"},
            {"role": "assistant", "content": "你是要单程还是往返？", "round_id": "round_1", "question_prompt": True, "question_id": "question_1", "message_id": "a1"},
        ],
        "pending_question": {
            "id": "question_1",
            "text": "你是要单程还是往返？",
            "round_id": "round_1",
            "round_title": "订机票",
            "client_request_id": "req_ask_1",
            "allow_custom": True,
            "options": [{"id": "option_1", "label": "单程"}, {"id": "option_2", "label": "往返"}],
            "asked_at": "2026-05-19T03:00:00+00:00",
        },
    }, ensure_ascii=False), encoding="utf-8")

    session = routes._build_current_session()

    assert session["status"] == "queued"
    assert session["pendingQuestion"]["id"] == "question_1"
    assert session["pendingQuestion"]["text"] == "你是要单程还是往返？"
    assert [item["label"] for item in session["pendingQuestion"]["options"]] == ["单程", "往返"]
    assert session["chat"]["messages"][-1]["questionPrompt"] is True


def test_reply_stream_chunks_reconstructs_original_text():
    from webui import routes

    text = "第一段先说重点。\n\n第二段补充更多细节，而且这一段稍微长一点，方便验证分块逻辑。"
    chunks = routes._reply_stream_chunks(text, target_chars=12)

    assert chunks
    assert len(chunks) > 1
    assert "".join(chunks) == text


async def test_stream_reply_payload_emits_ndjson_events():
    from webui import routes

    response = await routes._stream_reply_payload("你好，世界")
    body = b""
    async for chunk in response.body_iterator:
        body += chunk.encode("utf-8") if isinstance(chunk, str) else chunk

    events = [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]

    assert events[0]["type"] == "reply_start"
    assert any(event["type"] == "reply_delta" for event in events)
    assert events[-1] == {"type": "reply_done", "response": "你好，世界"}


async def test_run_main_agent_chat_only_streams_final_reply(monkeypatch):
    from cyrene import agent
    from cyrene import behavior_learning
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    saved = {}
    streamed = []

    async def fake_call_llm(messages, tools=None, max_tokens=32000):
        return {"content": "internal draft"}

    async def fake_call_llm_stream(messages, max_tokens=32000):
        await agent._emit_reply_stream_event({"type": "reply_start"})
        await agent._emit_reply_stream_event({"type": "reply_delta", "delta": "真实"})
        await agent._emit_reply_stream_event({"type": "reply_delta", "delta": "流式"})
        await agent._emit_reply_stream_event({"type": "reply_done", "response": "真实流式"})
        return {"content": "真实流式"}

    async def fake_save_session_messages(messages):
        saved["messages"] = list(messages)

    _patch_call_llm(monkeypatch, fake_call_llm)
    _patch_call_llm_stream(monkeypatch, fake_call_llm_stream)
    _patch_save_session(monkeypatch, fake_save_session_messages)
    _patch_append_session(monkeypatch, AsyncMock())
    monkeypatch.setattr(_agent_state, "_publish_runtime_event", AsyncMock())
    monkeypatch.setattr(behavior_learning, "try_route_and_execute_skill", AsyncMock(return_value=None))

    async def collect(event):
        streamed.append(event)

    token = agent._reply_stream_writer.set(collect)
    round_token = agent._current_round_id.set("round_stream")
    try:
        result = await agent._run_main_agent(
            "直接聊聊天",
            [],
            None,
            0,
            "db.sqlite3",
            system_prompt="system",
            client_request_id="req_stream",
        )
    finally:
        agent._current_round_id.reset(round_token)
        agent._reply_stream_writer.reset(token)

    assert result == "真实流式"
    assert [event["type"] for event in streamed] == ["reply_start", "reply_delta", "reply_delta", "reply_done"]
    assert saved["messages"][-1]["content"] == "真实流式"
    assert saved["messages"][-1]["client_request_id"] == "req_stream"


async def test_stream_agent_reply_forwards_live_events_before_completion(monkeypatch):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from webui import routes

    seen = {"archived": None}

    async def fake_archive_exchange(*args, **kwargs):
        seen["archived"] = (args, kwargs)

    async def fake_run():
        writer = agent._reply_stream_writer.get()
        assert writer is not None
        await writer({"type": "reply_start"})
        await writer({"type": "reply_delta", "delta": "先到"})
        await asyncio.sleep(0)
        await writer({"type": "reply_done", "response": "先到后完"})
        return "先到后完"

    monkeypatch.setattr(routes, "archive_exchange", fake_archive_exchange)
    monkeypatch.setattr(routes, "get_session_labels", lambda: {
        "session_title": "session",
        "round_title": "round",
        "round_id": "round_1",
        "archive_session_id": "session_1",
    })

    response = routes._stream_agent_reply(fake_run, "用户消息")
    body = b""
    async for chunk in response.body_iterator:
        body += chunk.encode("utf-8") if isinstance(chunk, str) else chunk

    events = [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]

    assert [event["type"] for event in events] == ["reply_start", "reply_delta", "reply_done"]
    assert seen["archived"] is not None


def test_flush_intermediate_replies_keeps_messages_for_later_saves():
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    base_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task", "message_id": "u1"},
    ]
    pending = [{
        "role": "assistant",
        "content": "working on it",
        "message_id": "a_mid",
        "intermediate_reply": True,
    }]
    token = agent._pending_intermediate_user_replies.set(pending)
    try:
        agent._flush_intermediate_user_replies(base_messages)
    finally:
        agent._pending_intermediate_user_replies.reset(token)

    assert base_messages[-1]["message_id"] == "a_mid"
    assert base_messages[-1]["intermediate_reply"] is True


async def test_query_round_tool_reports_live_round():
    from cyrene import subagent
    from cyrene import tools

    await subagent.clear()
    await subagent.register("alice", "research topic", round_id="round_1")

    result = await tools._tool_query_round({"round_id": "round_1"}, None, 0, "db.sqlite3", None)

    assert "round_1" in result
    assert "research topic" in result


async def test_queue_round_guidance_drains_main_inbox_without_subagents(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import debug
    from cyrene import inbox
    import cyrene.conversations as conversations

    ack_text = "收到这条引导了，我会按新的方向继续这一轮。"

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")
    agent.STATE_FILE.write_text(
        json.dumps({
            "session_title": "session label",
            "messages": [
                {"role": "user", "content": "round one question", "round_id": "round_1", "round_title": "round one"},
                {"role": "assistant", "content": "round one reply", "round_id": "round_1", "round_title": "round one"},
                {"role": "user", "content": "other round question", "round_id": "round_2", "round_title": "round two"},
                {"role": "assistant", "content": "other round reply", "round_id": "round_2", "round_title": "round two"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    seen = {}
    monkeypatch.setattr(
        _agent_guidance,
        "get_live_rounds",
        lambda: [{"id": "round_1", "status": "running", "title": "round one", "pendingGuidance": 0, "runningSubagents": 0, "subagentCount": 0}],
    )

    async def fake_run_chat_agent(
        user_message,
        bot,
        chat_id,
        db_path,
        ephemeral_system="",
        forced_round_id="",
        history_override=None,
        persist_base_messages=None,
        persist_insert_at=None,
        client_request_id="",
        persist_user_message=True,
        public_prompt=None,
        refresh_labels=True,
        hide_initial_detail=False,
        assistant_message_meta=None,
        lang="",
    ):
        seen["user_message"] = user_message
        seen["ephemeral_system"] = ephemeral_system
        seen["forced_round_id"] = forced_round_id
        seen["history_override"] = history_override
        seen["persist_base_messages"] = persist_base_messages
        seen["persist_insert_at"] = persist_insert_at
        seen["client_request_id"] = client_request_id
        seen["persist_user_message"] = persist_user_message
        seen["assistant_message_meta"] = assistant_message_meta
        return "guided reply"

    async def fake_archive_exchange(user_message, assistant_response, chat_id, session_title="", round_title="", round_id="", archive_session_id=""):
        seen["archived"] = (user_message, assistant_response, session_title, round_title, round_id)

    monkeypatch.setattr(_agent_coordinator, "_run_chat_agent", fake_run_chat_agent)
    monkeypatch.setattr(_agent_guidance, "_generate_guidance_ack", AsyncMock(return_value=ack_text))
    monkeypatch.setattr(conversations, "archive_exchange", fake_archive_exchange)
    events = []
    monkeypatch.setattr(debug, "publish_event", lambda event: events.append(event) or asyncio.sleep(0))

    item = await agent.queue_round_guidance("round_1", "please continue with logistics", None, 0, "db.sqlite3", client_request_id="req_1")
    await asyncio.sleep(0.05)
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert item["target_round_id"] == "round_1"
    assert seen["user_message"] == "please continue with logistics"
    assert "main-agent inbox" in seen["ephemeral_system"]
    assert seen["forced_round_id"] == "round_1"
    assert [msg["content"] for msg in seen["history_override"]] == ["round one question", "round one reply"]
    assert [msg["content"] for msg in seen["persist_base_messages"]] == [
        "round one question",
        "round one reply",
        "other round question",
        "other round reply",
        "please continue with logistics",
        ack_text,
    ]
    assert seen["persist_insert_at"] == 6
    assert seen["client_request_id"] == "req_1"
    assert seen["persist_user_message"] is False
    assert seen["assistant_message_meta"] == {"in_reply_to_guidance_id": item["id"]}
    assert seen["archived"][0] == "please continue with logistics"
    assert seen["archived"][2:] == ("session label", "round one", "round_1")
    assert saved[4]["content"] == "please continue with logistics"
    assert saved[4]["queued_guidance_id"] == item["id"]
    assert saved[5]["content"] == ack_text
    assert saved[5]["guidance_ack_for_guidance_id"] == item["id"]
    assert inbox.get_unread_count(agent._MAIN_INBOX_AGENT_ID) == 0
    assert any(
        event.get("type") == "guidance_acknowledged"
        and event.get("client_request_id") == "req_1"
        and event.get("ack_text") == ack_text
        for event in events
    )


async def test_queue_round_guidance_persists_user_message_immediately(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import subagent
    from cyrene import inbox

    await subagent.clear()
    await subagent.register("alice", "research topic", round_id="round_1")

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")
    agent.STATE_FILE.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "round one question", "round_id": "round_1", "round_title": "round one"},
                {"role": "assistant", "content": "round one reply", "round_id": "round_1", "round_title": "round one"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(_agent_guidance, "_ensure_main_inbox_worker", lambda *_args, **_kwargs: None)

    item = await agent.queue_round_guidance("round_1", "queued follow-up", None, 0, "db.sqlite3", client_request_id="req_queued")
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert saved[-1]["role"] == "user"
    assert saved[-1]["content"] == "queued follow-up"
    assert saved[-1]["round_id"] == "round_1"
    assert saved[-1]["round_title"] == "round one"
    assert saved[-1]["client_request_id"] == "req_queued"
    assert saved[-1]["queued_guidance_id"] == item["id"]
    assert item["id"].startswith("msg_")
    assert inbox.get_unread_count(agent._MAIN_INBOX_AGENT_ID) == 1


async def test_main_inbox_guidance_relays_to_subagents_and_inserts_reply(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import behavior_learning
    from cyrene import debug
    from cyrene import inbox
    from cyrene import subagent
    import cyrene.conversations as conversations

    ack_text = "收到，我先把这一轮的结论按你这条要求展开。"

    await subagent.clear()
    await subagent.register("alice", "research topic", round_id="round_1")

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")
    agent.STATE_FILE.write_text(
        json.dumps({
            "session_title": "session label",
            "messages": [
                {"role": "user", "content": "round one question", "round_id": "round_1", "round_title": "round one"},
                {"role": "assistant", "content": "round one reply", "round_id": "round_1", "round_title": "round one"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    seen = {}

    async def fake_fan_out(round_id, content, bot, chat_id, db_path):
        seen["fanout"] = (round_id, content)
        return ["alice"]

    async def fake_wait(round_id, bot, chat_id, db_path):
        seen["wait"] = round_id
        return False, "[alice] task: research topic\nstatus: done\nresult:\nDetailed finding"

    async def fake_summary_subagent(round_id, parent_task="", guidance="", round_history=None):
        seen["synth"] = (parent_task, round_id, guidance, [m["content"] for m in (round_history or [])])
        return "expanded reply"

    async def fake_flow_snapshot(_round_id):
        return {}

    async def fake_archive_exchange(user_message, assistant_response, chat_id, session_title="", round_title="", round_id="", archive_session_id=""):
        seen["archived"] = (user_message, assistant_response, session_title, round_title, round_id)

    async def fail_run_chat_agent(*_args, **_kwargs):
        raise AssertionError("_run_chat_agent should not run when the round already has subagents")

    monkeypatch.setattr(_agent_guidance, "_fan_out_guidance_to_subagents", fake_fan_out)
    monkeypatch.setattr(_agent_guidance, "_wait_for_subagent_round", fake_wait)
    monkeypatch.setattr(subagent, "run_summary_subagent", fake_summary_subagent)
    monkeypatch.setattr(subagent, "build_flow_snapshot", fake_flow_snapshot)
    monkeypatch.setattr(_agent_coordinator, "_run_chat_agent", fail_run_chat_agent)
    monkeypatch.setattr(_agent_guidance, "_generate_guidance_ack", AsyncMock(return_value=ack_text))
    monkeypatch.setattr(behavior_learning, "try_route_and_execute_skill", AsyncMock(return_value=None))
    monkeypatch.setattr(conversations, "archive_exchange", fake_archive_exchange)
    events = []
    monkeypatch.setattr(debug, "publish_event", lambda event: events.append(event) or asyncio.sleep(0))

    item = await agent.queue_round_guidance("round_1", "please expand section B", None, 0, "db.sqlite3", client_request_id="req_sub")
    await asyncio.sleep(0.05)
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert item["target_round_id"] == "round_1"
    assert seen["fanout"] == ("round_1", "please expand section B")
    assert seen["wait"] == "round_1"
    assert seen["synth"][0] == "round one question"
    assert seen["synth"][1] == "round_1"
    assert seen["synth"][2] == "please expand section B"
    assert saved[-3]["content"] == "please expand section B"
    assert saved[-3]["queued_guidance_id"] == item["id"]
    assert saved[-2]["content"] == ack_text
    assert saved[-2]["guidance_ack_for_guidance_id"] == item["id"]
    assert saved[-1]["content"] == "expanded reply"
    assert saved[-1]["client_request_id"] == "req_sub"
    assert saved[-1]["in_reply_to_guidance_id"] == item["id"]
    assert seen["archived"] == ("please expand section B", "expanded reply", "session label", "round one", "round_1")
    assert any(
        event.get("type") == "guidance_acknowledged"
        and event.get("client_request_id") == "req_sub"
        and event.get("ack_text") == ack_text
        for event in events
    )
    assert any(
        event.get("type") == "chat_message" and event.get("client_request_id") == "req_sub"
        for event in events
    )


async def test_main_inbox_guidance_failure_inserts_error_reply(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import debug
    from cyrene import inbox
    import cyrene.conversations as conversations

    ack_text = "收到，我会按这个补充要求继续处理。"

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")
    agent.STATE_FILE.write_text(
        json.dumps({
            "session_title": "session label",
            "messages": [
                {"role": "user", "content": "round one question", "round_id": "round_1", "round_title": "round one"},
                {"role": "assistant", "content": "round one reply", "round_id": "round_1", "round_title": "round one"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        _agent_guidance,
        "get_live_rounds",
        lambda: [{"id": "round_1", "status": "running", "title": "round one", "pendingGuidance": 0, "runningSubagents": 0, "subagentCount": 0}],
    )

    seen = {}

    async def boom_run_chat_agent(*_args, **_kwargs):
        raise RuntimeError("boom")

    async def fake_archive_exchange(user_message, assistant_response, chat_id, session_title="", round_title="", round_id="", archive_session_id=""):
        seen["archived"] = (user_message, assistant_response, session_title, round_title, round_id)

    monkeypatch.setattr(_agent_coordinator, "_run_chat_agent", boom_run_chat_agent)
    monkeypatch.setattr(_agent_guidance, "_generate_guidance_ack", AsyncMock(return_value=ack_text))
    monkeypatch.setattr(conversations, "archive_exchange", fake_archive_exchange)
    events = []
    monkeypatch.setattr(debug, "publish_event", lambda event: events.append(event) or asyncio.sleep(0))

    item = await agent.queue_round_guidance("round_1", "please retry with details", None, 0, "db.sqlite3", client_request_id="req_fail")
    await asyncio.sleep(0.05)
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert saved[-3]["content"] == "please retry with details"
    assert saved[-3]["queued_guidance_id"] == item["id"]
    assert saved[-2]["content"] == ack_text
    assert saved[-2]["guidance_ack_for_guidance_id"] == item["id"]
    assert saved[-1]["role"] == "assistant"
    assert "Guidance could not be applied because an internal error occurred" in saved[-1]["content"]
    assert saved[-1]["client_request_id"] == "req_fail"
    assert saved[-1]["in_reply_to_guidance_id"] == item["id"]
    assert seen["archived"][0] == "please retry with details"
    assert seen["archived"][2:] == ("session label", "round one", "round_1")
    assert any(
        event.get("type") == "guidance_acknowledged"
        and event.get("client_request_id") == "req_fail"
        and event.get("ack_text") == ack_text
        for event in events
    )
    assert any(
        event.get("type") == "chat_message" and event.get("client_request_id") == "req_fail"
        for event in events
    )


async def test_main_inbox_guidance_continuation_keeps_ack_before_final_reply(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import behavior_learning
    from cyrene import debug
    from cyrene import inbox
    import cyrene.conversations as conversations

    ack_text = "明白，我按你的新要求重做这一轮的回复。"

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(_agent_session, "_refresh_session_labels", AsyncMock())
    monkeypatch.setattr(agent, "get_context", lambda max_chars=5000: "")
    monkeypatch.setattr(agent, "get_memory_context", lambda: "")
    agent.STATE_FILE.write_text(
        json.dumps({
            "session_title": "session label",
            "messages": [
                {"role": "user", "content": "round one question", "round_id": "round_1", "round_title": "round one"},
                {"role": "assistant", "content": "round one reply", "round_id": "round_1", "round_title": "round one"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        _agent_guidance,
        "get_live_rounds",
        lambda: [{"id": "round_1", "status": "running", "title": "round one", "pendingGuidance": 0, "runningSubagents": 0, "subagentCount": 0}],
    )

    async def fake_call_llm(messages, tools=None, max_tokens=32000):
        return {
            "content": "adjusted final reply",
            "reasoning_content": "guided reasoning",
            "tool_calls": [],
        }

    async def fake_archive_exchange(user_message, assistant_response, chat_id, session_title="", round_title="", round_id="", archive_session_id=""):
        return None

    _patch_call_llm(monkeypatch, fake_call_llm)
    monkeypatch.setattr(_agent_guidance, "_generate_guidance_ack", AsyncMock(return_value=ack_text))
    monkeypatch.setattr(behavior_learning, "try_route_and_execute_skill", AsyncMock(return_value=None))
    monkeypatch.setattr(conversations, "archive_exchange", fake_archive_exchange)
    events = []
    monkeypatch.setattr(debug, "publish_event", lambda event: events.append(event) or asyncio.sleep(0))

    item = await agent.queue_round_guidance("round_1", "please adjust the answer", None, 0, "db.sqlite3", client_request_id="req_guided")
    await asyncio.sleep(0.05)
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    guided_users = [msg for msg in saved if msg.get("role") == "user" and msg.get("client_request_id") == "req_guided"]
    ack_index = next(i for i, msg in enumerate(saved) if msg.get("guidance_ack_for_guidance_id") == item["id"])
    reply_index = next(i for i, msg in enumerate(saved) if msg.get("role") == "assistant" and msg.get("client_request_id") == "req_guided")

    assert len(guided_users) == 1
    assert guided_users[0]["content"] == "please adjust the answer"
    assert guided_users[0]["queued_guidance_id"] == item["id"]
    assert saved[ack_index]["content"] == ack_text
    assert ack_index == 3
    assert saved[reply_index]["content"] == "adjusted final reply"
    assert reply_index == 4
    assert saved[reply_index]["reasoning_content"] == "guided reasoning"
    assert saved[reply_index]["round_id"] == "round_1"
    assert saved[reply_index]["in_reply_to_guidance_id"] == item["id"]
    assert any(
        event.get("type") == "guidance_acknowledged"
        and event.get("client_request_id") == "req_guided"
        and event.get("ack_text") == ack_text
        for event in events
    )
    assert any(
        event.get("type") == "chat_message"
        and event.get("client_request_id") == "req_guided"
        for event in events
    )


async def test_run_chat_agent_persists_client_request_ids(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import debug

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(_agent_session, "_refresh_session_labels", AsyncMock())
    monkeypatch.setattr(agent, "get_context", lambda max_chars=5000: "")
    monkeypatch.setattr(agent, "get_memory_context", lambda: "")
    events = []
    monkeypatch.setattr(debug, "publish_event", lambda event: events.append(event) or asyncio.sleep(0))

    async def fake_run_main_agent(user_message, history, bot, chat_id, db_path, system_prompt="", client_request_id="", persist_user_message=True, lang="", **kwargs):
        round_id = agent._current_round_id.get()
        await agent._save_session_messages([
            *history,
            {"role": "user", "content": user_message, "round_id": round_id, "client_request_id": client_request_id},
            {"role": "assistant", "content": "raw reply", "round_id": round_id, "client_request_id": client_request_id},
        ])
        return "raw reply"

    monkeypatch.setattr(_agent_core, "_run_main_agent", fake_run_main_agent)

    result = await agent._run_chat_agent("current request", None, 0, "db.sqlite3", client_request_id="req_live")
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert result == "raw reply"
    assert saved[-2]["client_request_id"] == "req_live"
    assert saved[-1]["client_request_id"] == "req_live"
    assert saved[-2]["message_id"].startswith("msg_")
    assert saved[-1]["message_id"].startswith("msg_")
    assert any(
        event.get("type") == "chat_message" and event.get("client_request_id") == "req_live"
        for event in events
    )


async def test_run_chat_agent_history_override_preserves_other_rounds(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import soul

    base_messages = [
        {"role": "user", "content": "round one question", "round_id": "round_1"},
        {"role": "assistant", "content": "round one reply", "round_id": "round_1"},
        {"role": "user", "content": "other round question", "round_id": "round_2"},
        {"role": "assistant", "content": "other round reply", "round_id": "round_2"},
    ]

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(_agent_session, "_refresh_session_labels", AsyncMock())
    monkeypatch.setattr(agent, "get_context", lambda max_chars=5000: "")
    agent.STATE_FILE.write_text(json.dumps({"messages": base_messages}, ensure_ascii=False), encoding="utf-8")

    async def fake_run_main_agent(user_message, history, bot, chat_id, db_path, system_prompt="", client_request_id="", persist_user_message=True, lang="", **kwargs):
        round_id = agent._current_round_id.get()
        await agent._save_session_messages([
            *history,
            {"role": "user", "content": user_message, "round_id": round_id},
            {"role": "assistant", "content": "raw reply", "round_id": round_id},
        ])
        return "raw reply"

    monkeypatch.setattr(_agent_core, "_run_main_agent", fake_run_main_agent)

    result = await agent._run_chat_agent(
        "guided follow-up",
        None,
        0,
        "db.sqlite3",
        forced_round_id="round_1",
        history_override=base_messages[:2],
    )
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert result == "raw reply"
    assert [msg["content"] for msg in saved] == [
        "round one question",
        "round one reply",
        "other round question",
        "other round reply",
        "guided follow-up",
        "raw reply",
    ]


async def test_run_chat_agent_persist_insert_at_keeps_later_queued_messages_in_place(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    base_messages = [
        {"role": "user", "content": "round one question", "round_id": "round_1"},
        {"role": "assistant", "content": "round one reply", "round_id": "round_1"},
        {"role": "user", "content": "later queued guidance", "round_id": "round_1", "queued_guidance_id": "guide_2"},
    ]

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(_agent_session, "_refresh_session_labels", AsyncMock())
    monkeypatch.setattr(agent, "get_context", lambda max_chars=5000: "")
    agent.STATE_FILE.write_text(json.dumps({"messages": base_messages}, ensure_ascii=False), encoding="utf-8")

    async def fake_run_main_agent(user_message, history, bot, chat_id, db_path, system_prompt="", client_request_id="", persist_user_message=True, public_user_message=None, public_attachments=None, lang="", **kwargs):
        round_id = agent._current_round_id.get()
        await agent._save_session_messages([
            *history,
            {"role": "user", "content": user_message, "round_id": round_id},
            {"role": "assistant", "content": "reply to current guidance", "round_id": round_id},
        ])
        return "reply to current guidance"

    monkeypatch.setattr(_agent_core, "_run_main_agent", fake_run_main_agent)

    result = await agent._run_chat_agent(
        "current queued guidance",
        None,
        0,
        "db.sqlite3",
        forced_round_id="round_1",
        history_override=base_messages[:2],
        persist_base_messages=base_messages,
        persist_insert_at=2,
    )
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert result == "reply to current guidance"
    assert [msg["content"] for msg in saved] == [
        "round one question",
        "round one reply",
        "current queued guidance",
        "reply to current guidance",
        "later queued guidance",
    ]


async def test_run_chat_agent_live_merge_preserves_concurrent_guidance(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import soul

    base_messages = [
        {"role": "user", "content": "previous question", "round_id": "round_0"},
        {"role": "assistant", "content": "previous reply", "round_id": "round_0"},
    ]

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(_agent_session, "_refresh_session_labels", AsyncMock())
    monkeypatch.setattr(agent, "get_context", lambda max_chars=5000: "")
    agent.STATE_FILE.write_text(json.dumps({"messages": base_messages}, ensure_ascii=False), encoding="utf-8")

    async def fake_run_main_agent(user_message, history, bot, chat_id, db_path, system_prompt="", client_request_id="", persist_user_message=True, public_user_message=None, public_attachments=None, lang="", **kwargs):
        await agent._append_session_message({
            "role": "user",
            "content": "queued guidance",
            "round_id": "round_2",
            "queued_guidance_id": "guide_1",
        })
        round_id = agent._current_round_id.get()
        await agent._save_session_messages([
            *history,
            {"role": "user", "content": user_message, "round_id": round_id},
            {"role": "assistant", "content": "raw reply", "round_id": round_id},
        ])
        return "raw reply"

    monkeypatch.setattr(_agent_core, "_run_main_agent", fake_run_main_agent)

    result = await agent._run_chat_agent("current request", None, 0, "db.sqlite3", forced_round_id="round_1")
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert result == "raw reply"
    assert [msg["content"] for msg in saved] == [
        "previous question",
        "previous reply",
        "current request",
        "raw reply",
        "queued guidance",
    ]
    assert saved[-1]["queued_guidance_id"] == "guide_1"


async def test_save_session_messages_replaces_live_round_block_without_duplication(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    base_messages = [
        {"role": "user", "content": "previous question", "round_id": "round_0"},
        {"role": "assistant", "content": "previous reply", "round_id": "round_0"},
    ]

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    agent.STATE_FILE.write_text(json.dumps({"messages": base_messages}, ensure_ascii=False), encoding="utf-8")

    round_token = agent._current_round_id.set("round_1")
    base_token = agent._persist_base_messages.set(None)
    merge_token = agent._persist_merge_live_state.set(True)
    prefix_token = agent._persist_history_prefix_len.set(len(base_messages))
    insert_token = agent._persist_insert_at.set(len(base_messages))
    try:
        await agent._append_session_message({
            "role": "user",
            "content": "current request",
            "round_id": "round_1",
        })
        await agent._append_session_message({
            "role": "user",
            "content": "queued guidance",
            "round_id": "round_2",
            "queued_guidance_id": "guide_1",
        })
        await agent._save_session_messages([
            *base_messages,
            {"role": "user", "content": "current request", "round_id": "round_1"},
            {"role": "assistant", "content": "raw reply", "round_id": "round_1"},
        ])
    finally:
        agent._persist_insert_at.reset(insert_token)
        agent._persist_history_prefix_len.reset(prefix_token)
        agent._persist_merge_live_state.reset(merge_token)
        agent._persist_base_messages.reset(base_token)
        agent._current_round_id.reset(round_token)

    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert [msg["content"] for msg in saved] == [
        "previous question",
        "previous reply",
        "current request",
        "raw reply",
        "queued guidance",
    ]
    assert saved[-1]["queued_guidance_id"] == "guide_1"


async def test_run_chat_agent_history_override_visible_reply_update_does_not_duplicate_messages(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    base_messages = [
        {"role": "user", "content": "round one question", "round_id": "round_1"},
        {"role": "assistant", "content": "round one reply", "round_id": "round_1"},
        {"role": "user", "content": "other round question", "round_id": "round_2"},
        {"role": "assistant", "content": "other round reply", "round_id": "round_2"},
    ]

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(_agent_session, "_refresh_session_labels", AsyncMock())
    monkeypatch.setattr(agent, "get_context", lambda max_chars=5000: "")
    agent.STATE_FILE.write_text(json.dumps({"messages": base_messages}, ensure_ascii=False), encoding="utf-8")

    async def fake_run_main_agent(user_message, history, bot, chat_id, db_path, system_prompt="", client_request_id="", persist_user_message=True, lang="", **kwargs):
        round_id = agent._current_round_id.get()
        await agent._save_session_messages([
            *history,
            {"role": "user", "content": user_message, "round_id": round_id},
            {"role": "assistant", "content": "raw reply", "round_id": round_id},
        ])
        return "raw reply"

    monkeypatch.setattr(_agent_core, "_run_main_agent", fake_run_main_agent)

    result = await agent._run_chat_agent(
        "guided follow-up",
        None,
        0,
        "db.sqlite3",
        forced_round_id="round_1",
        history_override=base_messages[:2],
    )
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert result == "raw reply"
    assert [msg["content"] for msg in saved] == [
        "round one question",
        "round one reply",
        "other round question",
        "other round reply",
        "guided follow-up",
        "raw reply",
    ]


def test_inbox_send_message_is_serialized():
    from cyrene import inbox
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        inbox.INBOX_DIR = Path(tmp) / "inbox"

        async def send_and_read():
            await asyncio.gather(*[
                inbox.send_message(f"sender_{i}", "receiver", "chat", f"payload_{i}")
                for i in range(20)
            ])
            return await inbox.read_messages("receiver", mark_read=False)

        messages = asyncio.run(send_and_read())
        ids = [m["message_id"] for m in messages]
        assert len(messages) == 20
        assert len(set(ids)) == 20
        assert inbox.get_unread_count("receiver") == 20


async def test_subagent_registry_emits_update_events(monkeypatch):
    from cyrene import debug
    from cyrene import subagent

    seen = []

    async def fake_publish_event(event):
        seen.append(event)

    monkeypatch.setattr(debug, "publish_event", fake_publish_event)
    await subagent.clear()
    await subagent.register("alice", "review ssh", round_id="round_live")
    await subagent.save_messages("alice", [{"role": "assistant", "content": "checking"}])
    await subagent.set_waiting("alice", result="draft ready")
    await subagent.set_resumed("alice")
    await subagent.mark_done("alice", result="done")

    event_types = [event["type"] for event in seen]
    statuses = [event.get("status") for event in seen if event.get("type") == "subagent_update"]

    assert "subagent_update" in event_types
    assert statuses[0] == "running"
    assert "waiting" in statuses
    assert "resumed" in statuses
    assert statuses[-1] == "done"
    assert seen[-1]["round_id"] == "round_live"
    assert seen[-1]["message_count"] == 1


async def test_run_subagent_persists_quit_tool_messages_before_resume(monkeypatch):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import subagent

    llm_inputs = []
    responses = iter([
        {
            "content": "initial finding",
            "tool_calls": [{"id": "q1", "function": {"name": "quit", "arguments": "{}"}}],
        },
        {
            "content": "final finding",
            "tool_calls": [{"id": "q2", "function": {"name": "quit", "arguments": "{}"}}],
        },
    ])
    wait_results = iter([
        "[from host_moderator] (chat) 请补充一条后勤建议",
        "",
    ])

    async def fake_call_llm(messages, tools=None, max_tokens=32000, **kwargs):
        snapshot = json.loads(json.dumps(messages, ensure_ascii=False))
        llm_inputs.append(snapshot)
        assert max_tokens is None
        if len(llm_inputs) == 2:
            assert any(
                msg.get("role") == "tool" and msg.get("tool_call_id") == "q1"
                for msg in snapshot
            ), "Resumed subagent history must include the prior quit tool response"
        return next(responses)

    async def fake_wait_for_others(agent_id, inbox_check_func, mark_read_func=None, max_wait=600, result=""):
        return next(wait_results)

    _patch_call_llm(monkeypatch, fake_call_llm)
    monkeypatch.setattr(subagent, "wait_for_others", fake_wait_for_others)

    await subagent.clear()
    await subagent.register("alice", "research topic")

    result = await subagent._run_subagent("alice", "research topic", None, 0, "db.sqlite3")
    raw = await subagent.get_raw_messages("alice")

    assert result == "final finding"
    assert any(msg.get("role") == "tool" and msg.get("tool_call_id") == "q1" for msg in raw)
    assert any(msg.get("role") == "tool" and msg.get("tool_call_id") == "q2" for msg in raw)


def test_live_flow_contains_tool_nodes_and_comm_edges(tmp_path, monkeypatch):
    from cyrene import debug
    from cyrene import inbox
    from webui import routes

    inbox.INBOX_DIR = tmp_path / "inbox"
    asyncio.run(inbox.send_message("alice", "bob", "chat", "Discuss firewall baselines"))

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [
        {
            "type": "tool_call",
            "caller": "main_agent",
            "tool": "spawn_subagent",
            "args": {"agent_id": "alice"},
            "result_preview": "spawned",
        }
    ])

    raw_msgs = [
        {"role": "user", "content": "do work"},
        {"role": "assistant", "content": "", "reasoning_content": "thinking", "usage": {"prompt_tokens": 120, "completion_tokens": 40}, "tool_calls": [
            {"id": "t1", "function": {"name": "spawn_subagent", "arguments": '{"agent_id":"alice"}'}}
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": "spawned"},
        {"role": "assistant", "content": "final answer", "usage": {"prompt_tokens": 30, "completion_tokens": 12}},
    ]
    ui_msgs = routes._convert_messages(raw_msgs)
    subagents = [{
        "id": "alice",
        "name": "alice",
        "status": "running",
        "task": "task A",
        "tokens": 2,
        "elapsed": "00:01",
        "progress": 0.45,
        "result": "",
        "messageCount": 2,
        "createdAt": "12:00:00",
        "updatedAt": "12:00:01",
    }, {
        "id": "bob",
        "name": "bob",
        "status": "queued",
        "task": "task B",
        "tokens": 1,
        "elapsed": "00:01",
        "progress": 0.82,
        "result": "",
        "messageCount": 1,
        "createdAt": "12:00:00",
        "updatedAt": "12:00:01",
    }]
    registry = {
        "alice": {"messages": [{"role": "assistant", "content": "a", "usage": {"prompt_tokens": 18, "completion_tokens": 7}}], "result": "", "status": "running"},
        "bob": {"messages": [], "result": "", "status": "waiting"},
    }

    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    flow = routes._build_live_flow(raw_msgs, ui_msgs, subagents, registry)

    tool_nodes = [node for node in flow["nodes"] if node["kind"] == "tool"]
    comm_edges = [edge for edge in flow["edges"] if edge.get("kind") == "comm"]
    output_nodes = [node for node in flow["nodes"] if node["kind"] == "output"]

    assert any(node["title"] == "spawn_subagent" for node in tool_nodes)
    assert any(edge["message"]["body"] == "Discuss firewall baselines" for edge in comm_edges)
    assert output_nodes and output_nodes[0]["detail"]["content"] == "final answer"
    main_node = next(node for node in flow["nodes"] if node["id"] == "n_main")
    alice_node = next(node for node in flow["nodes"] if node["title"] == "subagent · alice")
    assert main_node["detail"]["tokensIn"] == 150
    assert main_node["detail"]["tokensOut"] == 52
    assert alice_node["detail"]["tokensIn"] == 18
    assert alice_node["detail"]["tokensOut"] == 7


def test_live_flow_marks_empty_tool_outputs_done(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    raw_msgs = [
        {"role": "user", "content": "run command"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "bash", "arguments": json.dumps({"cmd": "true"})}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": ""},
        {"role": "assistant", "content": "done"},
    ]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), [], {})
    tool = next(node for node in flow["nodes"] if node["kind"] == "tool")

    assert tool["status"] == "done"
    assert tool["detail"]["output"] == "Completed with no captured output."


def test_live_flow_marks_tool_without_captured_output_done_after_followup(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    raw_msgs = [
        {"role": "user", "content": "research"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "search", "arguments": json.dumps({"query": "alpha"})}},
        ]},
        {"role": "assistant", "content": "summary"},
    ]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), [], {})
    tool = next(node for node in flow["nodes"] if node["kind"] == "tool")

    assert tool["status"] == "done"
    assert "no tool output was captured" in tool["detail"]["output"]


def test_live_flow_marks_recent_overlay_tools_done(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [
        {
            "type": "tool_call",
            "caller": "main_agent",
            "tool": "web_search",
            "args": {"query": "latest"},
            "result_preview": "search complete",
            "round_id": "round_live",
        }
    ])
    raw_msgs = [
        {"role": "user", "content": "check latest", "round_id": "round_live"},
        {"role": "assistant", "content": "working", "round_id": "round_live"},
    ]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), [], {})
    tool = next(node for node in flow["nodes"] if node["kind"] == "tool")
    tool_edge = next(edge for edge in flow["edges"] if edge["to"] == tool["id"])

    assert tool["title"] == "web_search"
    assert tool["status"] == "done"
    assert tool_edge.get("kind") is None


def test_build_current_session_uses_live_shell_snapshots(monkeypatch, tmp_path):
    from webui import routes

    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    routes.STATE_FILE.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "run server", "round_id": "round_1"},
                {"role": "assistant", "content": "", "round_id": "round_1", "tool_calls": [
                    {"id": "bash_1", "function": {"name": "Bash", "arguments": json.dumps({"command": "python -m http.server"})}},
                ]},
                {"role": "tool", "tool_call_id": "bash_1", "content": "started", "round_id": "round_1"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(routes, "list_live_shells", lambda include_exited=False: [{
        "id": "shell_live",
        "title": "dev server",
        "cwd": ".",
        "pid": 1234,
        "status": "running",
        "elapsed": "00:12",
        "updatedAt": "12:00:00",
        "lines": [{"kind": "meta", "text": "[shell started]"}],
    }])

    session = routes._build_current_session()

    assert session["shells"][0]["id"] == "shell_live"
    assert len(session["shells"]) == 1


def test_build_current_session_done_event_clears_recent_activity(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    routes.STATE_FILE.write_text(
        '{"messages":[{"role":"user","content":"hello"},{"role":"assistant","content":"done"}]}',
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [
        {"type": "tool_call", "caller": "main_agent", "timestamp": (now - timedelta(seconds=2)).isoformat()},
        {"type": "session_update", "status": "done", "timestamp": now.isoformat()},
        {"type": "llm_call", "caller": "behavior_learning", "timestamp": now.isoformat()},
        {"type": "llm_call", "caller": "compactor", "timestamp": now.isoformat()},
    ])

    session = routes._build_current_session()

    assert session["status"] == "done"


async def test_compress_old_messages_labels_llm_as_compactor(monkeypatch):
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session

    callers = []

    async def fake_call_llm(messages, tools=None, max_tokens=32000):
        callers.append(_agent_state._caller_type.get())
        return {"content": ""}

    monkeypatch.setattr(_agent_session, "_call_llm", fake_call_llm)

    await _agent_session._compress_old_messages([
        {"role": "user", "content": "remember this"},
        {"role": "assistant", "content": "noted"},
    ])

    assert callers == ["compactor"]


def test_build_current_session_detects_activity_after_done_event(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    routes.STATE_FILE.write_text(
        '{"messages":[{"role":"user","content":"hello"},{"role":"assistant","content":"done"}]}',
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [
        {"type": "session_update", "status": "done", "timestamp": (now - timedelta(seconds=2)).isoformat()},
        {"type": "llm_call", "caller": "main_agent", "timestamp": now.isoformat()},
    ])

    session = routes._build_current_session()

    assert session["status"] == "running"


def test_build_sessions_includes_today_archive_when_live_session_exists(tmp_path, monkeypatch):
    from webui import routes

    monkeypatch.setattr(routes, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    routes.CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)

    today = routes.datetime.now().astimezone().strftime("%Y-%m-%d")
    (routes.CONVERSATIONS_DIR / f"{today}.md").write_text(
        "# Conversations\n\n## 08:00:00 UTC\n\n**User**: hi\n\n**Ape**: archived\n\n---\n",
        encoding="utf-8",
    )
    routes.STATE_FILE.write_text(
        '{"messages":[{"role":"user","content":"live hi"},{"role":"assistant","content":"live reply"}]}',
        encoding="utf-8",
    )

    sessions = routes._build_sessions()
    ids = [session["id"] for session in sessions]

    assert ids[0] == "run_live"
    assert f"archive_{today}_legacy_{today}" in ids


def test_build_sessions_skips_archive_copy_of_current_live_session(tmp_path, monkeypatch):
    from webui import routes

    monkeypatch.setattr(routes, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    routes.CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)

    today = routes.datetime.now().astimezone().strftime("%Y-%m-%d")
    (routes.CONVERSATIONS_DIR / f"{today}.md").write_text(
        "# Conversations\n\n"
        "## 08:00:00 UTC\n\n"
        "<!-- archive_session_id: session_live -->\n"
        "<!-- session_title: 当前会话 -->\n\n"
        "**User**: live hi\n\n"
        "**Ape**: archived live reply\n\n"
        "---\n\n"
        "## 09:00:00 UTC\n\n"
        "<!-- archive_session_id: session_other -->\n"
        "<!-- session_title: 另一场会话 -->\n\n"
        "**User**: other hi\n\n"
        "**Ape**: other reply\n\n"
        "---\n",
        encoding="utf-8",
    )
    routes.STATE_FILE.write_text(
        '{"archive_session_id":"session_live","messages":[{"role":"user","content":"live hi"},{"role":"assistant","content":"live reply"}]}',
        encoding="utf-8",
    )

    sessions = routes._build_sessions()
    ids = [session["id"] for session in sessions]

    assert ids[0] == "run_live"
    assert f"archive_{today}_session_live" not in ids
    assert f"archive_{today}_session_other" in ids


def test_build_current_session_recovers_subagents_from_state_and_inbox(tmp_path, monkeypatch):
    from cyrene import inbox
    from webui import routes

    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")

    routes.STATE_FILE.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "start", "round_id": "round_live"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "call_1", "function": {
                        "name": "spawn_subagent",
                        "arguments": json.dumps({"agent_id": "alice", "task": "review firewall"})
                    }}
                ], "round_id": "round_live"},
                {"role": "tool", "tool_call_id": "call_1", "content": "spawned", "round_id": "round_live"},
                {"role": "assistant", "content": "done", "round_id": "round_live"},
            ]
        }),
        encoding="utf-8",
    )
    asyncio.run(inbox.send_message("alice", "bob", "chat", "Use ufw and fail2ban", round_id="round_live"))

    session = routes._build_current_session()
    subagents = {item["name"]: item for item in session["subagents"]}
    subagent_names = set(subagents)
    flow_titles = {node["title"] for node in session["flow"]["nodes"] if node["kind"] == "subagent"}
    comm_edges = [edge for edge in session["flow"]["edges"] if edge.get("kind") == "comm"]

    assert {"alice", "bob"}.issubset(subagent_names)
    assert session["currentRoundId"] == "round_live"
    assert subagents["alice"]["roundId"] == "round_live"
    assert subagents["bob"]["roundId"] == "round_live"
    assert "subagent · alice" in flow_titles
    assert "subagent · bob" in flow_titles
    assert any(edge["message"]["body"] == "Use ufw and fail2ban" for edge in comm_edges)


async def test_clear_session_id_removes_live_flow_residue(tmp_path, monkeypatch):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import inbox
    from cyrene import subagent
    from webui import routes

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(_agent_session, "_compress_old_messages", AsyncMock())

    agent.STATE_FILE.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "start", "round_id": "round_live"},
                {"role": "assistant", "content": "", "round_id": "round_live", "tool_calls": [
                    {"id": "call_1", "function": {
                        "name": "spawn_subagent",
                        "arguments": json.dumps({"agent_id": "alice", "task": "review firewall"})
                    }}
                ]},
                {"role": "tool", "tool_call_id": "call_1", "content": "spawned", "round_id": "round_live"},
                {"role": "assistant", "content": "waiting", "round_id": "round_live"},
            ]
        }),
        encoding="utf-8",
    )
    await subagent.register("alice", "review firewall", round_id="round_live")
    await inbox.send_message("alice", "bob", "chat", "Use ufw and fail2ban", round_id="round_live")

    before = routes._build_current_session()
    assert before["flow"]["nodes"]

    await agent.clear_session_id()

    after = routes._build_current_session()
    assert after["title"] == "new session"
    assert after["subagents"] == []
    assert after["flow"]["nodes"] == []
    assert after["flow"]["edges"] == []


def test_build_user_reads_local_username(monkeypatch):
    from webui import routes

    monkeypatch.setenv("USER", "localtester")
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    monkeypatch.setattr(routes.getpass, "getuser", lambda: "ignored-user")

    user = routes._build_user()

    assert user["name"] == "localtester"
    assert user["handle"] == "localtester"
    assert user["initials"] == "L"


def test_live_flow_staggers_subagents_when_tool_stacks_are_tall(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    raw_msgs = [
        {"role": "user", "content": "plan"},
        {"role": "assistant", "content": "final"},
    ]
    ui_msgs = routes._convert_messages(raw_msgs)
    subagents = [
        {
            "id": "alpha",
            "name": "alpha",
            "status": "done",
            "task": "task alpha",
            "tokens": 0,
            "elapsed": "00:01",
            "progress": 1.0,
            "result": "",
            "messageCount": 0,
            "createdAt": "12:00:00",
            "updatedAt": "12:00:01",
        },
        {
            "id": "beta",
            "name": "beta",
            "status": "done",
            "task": "task beta",
            "tokens": 0,
            "elapsed": "00:01",
            "progress": 1.0,
            "result": "",
            "messageCount": 0,
            "createdAt": "12:00:02",
            "updatedAt": "12:00:03",
        },
    ]
    registry = {
        "alpha": {
            "messages": [
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "a1", "function": {"name": "search", "arguments": "{}"}},
                    {"id": "a2", "function": {"name": "bash", "arguments": "{}"}},
                    {"id": "a3", "function": {"name": "read", "arguments": "{}"}},
                ]},
                {"role": "tool", "tool_call_id": "a1", "content": "ok"},
                {"role": "tool", "tool_call_id": "a2", "content": "ok"},
                {"role": "tool", "tool_call_id": "a3", "content": "ok"},
            ],
            "result": "",
            "status": "done",
        },
        "beta": {
            "messages": [],
            "result": "",
            "status": "done",
        },
    }

    flow = routes._build_live_flow(raw_msgs, ui_msgs, subagents, registry)
    alpha = next(node for node in flow["nodes"] if node["title"] == "subagent · alpha")
    beta = next(node for node in flow["nodes"] if node["title"] == "subagent · beta")

    assert beta["y"] - alpha["y"] >= 300


def test_live_flow_stacks_multiple_rounds_and_keeps_comm_edges(tmp_path, monkeypatch):
    from cyrene import inbox
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")
    asyncio.run(inbox.send_message("alice", "bob", "chat", "Round one message"))

    raw_msgs = [
        {"role": "user", "content": "round one"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "task a"})}},
            {"id": "call_2", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "bob", "task": "task b"})}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "spawned"},
        {"role": "tool", "tool_call_id": "call_2", "content": "spawned"},
        {"role": "assistant", "content": "round one done"},
        {"role": "user", "content": "round two"},
        {"role": "assistant", "content": "round two done"},
    ]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), [], {})

    round0_main = next(node for node in flow["nodes"] if node["id"] == "r0_n_main")
    round1_main = next(node for node in flow["nodes"] if node["id"] == "r1_n_main")
    comm_edges = [edge for edge in flow["edges"] if edge.get("kind") == "comm"]

    assert round1_main["y"] > round0_main["y"]
    assert any(edge["message"]["body"] == "Round one message" for edge in comm_edges)


def test_live_flow_prunes_extra_user_only_tail_rounds(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    raw_msgs = [
        {"role": "user", "content": "round one", "round_id": "round_1"},
        {"role": "assistant", "content": "done one", "round_id": "round_1"},
        {"role": "user", "content": "round two pending", "round_id": "round_2"},
        {"role": "user", "content": "round three pending", "round_id": "round_3"},
    ]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), [], {})
    input_ids = [node["id"] for node in flow["nodes"] if node["kind"] == "input"]

    assert input_ids == ["r0_n_user", "r1_n_user"]
    assert all(not node["id"].startswith("r2_") for node in flow["nodes"])


def test_live_flow_keeps_guided_continuation_inside_same_round(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    raw_msgs = [
        {"role": "user", "content": "original task", "round_id": "round_1", "round_title": "round one"},
        {"role": "assistant", "content": "first reply", "round_id": "round_1", "round_title": "round one"},
        {"role": "user", "content": "please adjust the answer", "round_id": "round_1", "round_title": "round one", "queued_guidance_id": "guide_1"},
        {"role": "assistant", "content": "已接受引导。我会按这条新要求调整当前这一轮的工作，并在完成后给你更新。", "round_id": "round_1", "round_title": "round one", "guidance_ack_for_guidance_id": "guide_1"},
        {"role": "assistant", "content": "adjusted final reply", "round_id": "round_1", "round_title": "round one", "in_reply_to_guidance_id": "guide_1"},
    ]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), [], {})
    input_nodes = [node for node in flow["nodes"] if node["kind"] == "input"]
    main_nodes = [node for node in flow["nodes"] if node["kind"] == "main"]
    output_nodes = [node for node in flow["nodes"] if node["kind"] == "output"]

    assert len(input_nodes) == 1
    assert len(main_nodes) == 1
    assert len(output_nodes) == 1
    assert input_nodes[0]["title"] == "round one"
    assert output_nodes[0]["detail"]["content"] == "adjusted final reply"


def test_live_flow_attaches_live_registry_to_latest_substantive_round(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    raw_msgs = [
        {"role": "user", "content": "start debate", "round_id": "round_a"},
        {"role": "assistant", "content": "", "round_id": "round_a", "tool_calls": [
            {"id": "call_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "debate"})}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "spawned", "round_id": "round_a"},
        {"role": "assistant", "content": "working", "round_id": "round_a"},
        {"role": "user", "content": "hello", "round_id": "round_b"},
    ]
    registry = {
        "alice": {
            "task": "debate",
            "status": "running",
            "result": "",
            "messages": [{"role": "assistant", "content": "still working"}],
            "created_at": "2026-05-16T04:00:00+00:00",
            "updated_at": "2026-05-16T04:00:10+00:00",
            "round_id": "round_a",
        }
    }
    subagents = [{
        "id": "alice",
        "name": "alice",
        "status": "running",
        "task": "debate",
        "tokens": 1,
        "elapsed": "00:01",
        "progress": 0.45,
        "result": "",
        "messageCount": 1,
        "createdAt": "12:00:00",
        "updatedAt": "12:00:10",
    }]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), subagents, registry)
    alice = next(node for node in flow["nodes"] if node["title"] == "subagent · alice")

    assert alice["id"].startswith("r0_")
    assert all(not node["title"] == "subagent · alice" or node["id"].startswith("r0_") for node in flow["nodes"])


def test_live_flow_filters_comm_edges_by_round_id(tmp_path, monkeypatch):
    from cyrene import inbox
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")
    asyncio.run(inbox.send_message("alice", "bob", "chat", "Old round", round_id="round_old"))
    asyncio.run(inbox.send_message("alice", "bob", "chat", "New round", round_id="round_new"))

    raw_msgs = [
        {"role": "user", "content": "old", "round_id": "round_old"},
        {"role": "assistant", "content": "", "round_id": "round_old", "tool_calls": [
            {"id": "old_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "a"})}},
            {"id": "old_2", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "bob", "task": "b"})}},
        ]},
        {"role": "assistant", "content": "done old", "round_id": "round_old"},
        {"role": "user", "content": "new", "round_id": "round_new"},
        {"role": "assistant", "content": "", "round_id": "round_new", "tool_calls": [
            {"id": "new_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "a"})}},
            {"id": "new_2", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "bob", "task": "b"})}},
        ]},
        {"role": "assistant", "content": "done new", "round_id": "round_new"},
    ]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), [], {})
    old_edges = [edge for edge in flow["edges"] if edge.get("kind") == "comm" and edge["from"].startswith("r0_")]
    new_edges = [edge for edge in flow["edges"] if edge.get("kind") == "comm" and edge["from"].startswith("r1_")]

    assert any(edge["message"]["body"] == "Old round" for edge in old_edges)
    assert all(edge["message"]["body"] != "New round" for edge in old_edges)
    assert any(edge["message"]["body"] == "New round" for edge in new_edges)


def test_live_flow_filters_recent_events_to_current_round(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [
        {
            "type": "tool_call",
            "caller": "main_agent",
            "tool": "old_search",
            "args": {"query": "old"},
            "result_preview": "old result",
            "round_id": "round_old",
        },
        {
            "type": "tool_call",
            "caller": "main_agent",
            "tool": "new_search",
            "args": {"query": "new"},
            "result_preview": "new result",
            "round_id": "round_new",
        },
    ])
    raw_msgs = [
        {"role": "user", "content": "old", "round_id": "round_old"},
        {"role": "assistant", "content": "done old", "round_id": "round_old"},
        {"role": "user", "content": "new", "round_id": "round_new"},
        {"role": "assistant", "content": "working new", "round_id": "round_new"},
    ]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), [], {})
    round1_tools = [
        node["title"]
        for node in flow["nodes"]
        if node["kind"] == "tool" and node["id"].startswith("r1_")
    ]

    assert "new_search" in round1_tools
    assert "old_search" not in round1_tools


def test_live_flow_does_not_merge_stale_subagent_card_into_new_round(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    raw_msgs = [
        {"role": "user", "content": "old", "round_id": "round_old"},
        {"role": "assistant", "content": "", "round_id": "round_old", "tool_calls": [
            {"id": "old_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "old task"})}},
        ]},
        {"role": "tool", "tool_call_id": "old_1", "content": "spawned", "round_id": "round_old"},
        {"role": "assistant", "content": "done old", "round_id": "round_old"},
        {"role": "user", "content": "new", "round_id": "round_new"},
        {"role": "assistant", "content": "", "round_id": "round_new", "tool_calls": [
            {"id": "new_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "new task"})}},
        ]},
        {"role": "tool", "tool_call_id": "new_1", "content": "spawned", "round_id": "round_new"},
        {"role": "assistant", "content": "working new", "round_id": "round_new"},
    ]
    subagents = [{
        "id": "alice",
        "name": "alice",
        "status": "done",
        "task": "old task",
        "roundId": "round_old",
        "tokens": 1,
        "elapsed": "00:02",
        "progress": 1.0,
        "result": "old result",
        "messageCount": 1,
        "createdAt": "12:00:00",
        "updatedAt": "12:00:01",
    }]
    registry = {
        "alice": {
            "task": "old task",
            "status": "done",
            "result": "old result",
            "messages": [{"role": "assistant", "content": "old result"}],
            "round_id": "round_old",
        }
    }

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), subagents, registry)
    new_alice = next(
        node for node in flow["nodes"]
        if node["title"] == "subagent · alice" and node["id"].startswith("r1_")
    )

    assert new_alice["detail"]["task"] == "new task"
    assert new_alice["detail"]["result"] == ""


def test_infer_subagent_entries_prefers_latest_spawn_round_over_stale_registry():
    from webui import routes

    raw_msgs = [
        {"role": "user", "content": "old", "round_id": "round_old"},
        {"role": "assistant", "content": "", "round_id": "round_old", "tool_calls": [
            {"id": "old_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "old task"})}},
        ]},
        {"role": "user", "content": "new", "round_id": "round_new"},
        {"role": "assistant", "content": "", "round_id": "round_new", "tool_calls": [
            {"id": "new_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "new task"})}},
        ]},
    ]
    registry = {
        "alice": {
            "task": "old task",
            "status": "done",
            "result": "old result",
            "messages": [{"role": "assistant", "content": "old result"}],
            "round_id": "round_old",
            "created_at": "2026-05-16T04:00:00+00:00",
            "updated_at": "2026-05-16T04:00:10+00:00",
        }
    }

    entries = routes._infer_subagent_entries(raw_msgs, registry)

    assert entries["alice"]["task"] == "new task"
    assert entries["alice"]["round_id"] == "round_new"
    assert entries["alice"]["status"] == "running"
    assert entries["alice"]["result"] == ""
    assert entries["alice"]["messages"] == []


def test_live_flow_uses_registry_when_state_is_empty(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [
        {"type": "phase_transition", "detail": "Live subagents spawned"},
    ])
    registry = {
        "alice": {
            "task": "discuss architecture",
            "status": "running",
            "result": "",
            "messages": [{"role": "assistant", "content": "working"}],
            "created_at": "2026-05-15T09:00:00+00:00",
            "updated_at": "2026-05-15T09:00:10+00:00",
            "round_id": "round_live",
        }
    }
    subagents = [{
        "id": "alice",
        "name": "alice",
        "status": "running",
        "task": "discuss architecture",
        "tokens": 1,
        "elapsed": "00:01",
        "progress": 0.45,
        "result": "",
        "messageCount": 1,
        "createdAt": "17:00:00",
        "updatedAt": "17:00:10",
    }]

    flow = routes._build_live_flow([], [], subagents, registry)

    assert any(node["kind"] == "main" for node in flow["nodes"])
    assert any(node["title"] == "subagent · alice" for node in flow["nodes"])


def test_convert_messages_keeps_assistant_entries_with_thinking_or_tools():
    from webui import routes

    raw_msgs = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "", "reasoning_content": "thinking"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "spawn_subagent", "arguments": "{}"}}
        ]},
    ]

    ui_msgs = routes._convert_messages(raw_msgs)

    assert len(ui_msgs) == 2
    assert ui_msgs[1]["role"] == "agent"
    assert ui_msgs[1]["thinking"] == "thinking"
    assert ui_msgs[1]["tools"][0]["name"] == "spawn_subagent"


def test_convert_messages_merges_adjacent_trace_only_assistant_entries():
    from webui import routes

    raw_msgs = [
        {"role": "assistant", "content": "", "reasoning_content": "first pass", "round_id": "round_1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "search", "arguments": "{}"}}
        ], "round_id": "round_1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t2", "function": {"name": "fetch", "arguments": "{}"}}
        ], "round_id": "round_1"},
    ]

    ui_msgs = routes._convert_messages(raw_msgs)

    assert len(ui_msgs) == 1
    assert ui_msgs[0]["thinking"] == "first pass"
    assert [tool["name"] for tool in ui_msgs[0]["tools"]] == ["search", "fetch"]


def test_convert_messages_merges_adjacent_trace_only_entries_with_same_client_request_id():
    from webui import routes

    raw_msgs = [
        {"role": "assistant", "content": "", "reasoning_content": "first", "round_id": "round_1", "client_request_id": "req_1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "search", "arguments": "{}"}}
        ], "round_id": "round_1", "client_request_id": "req_1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t2", "function": {"name": "fetch", "arguments": "{}"}}
        ], "round_id": "round_1", "client_request_id": "req_1"},
    ]

    ui_msgs = routes._convert_messages(raw_msgs)

    assert len(ui_msgs) == 1
    assert ui_msgs[0]["clientRequestId"] == "req_1"
    assert ui_msgs[0]["thinking"] == "first"
    assert [tool["name"] for tool in ui_msgs[0]["tools"]] == ["search", "fetch"]


def test_convert_messages_merges_trace_only_assistant_into_following_body_reply():
    from webui import routes

    raw_msgs = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "thinking before tool",
            "tool_calls": [{"id": "t1", "function": {"name": "search", "arguments": "{}"}}],
            "round_id": "round_1",
        },
        {
            "role": "assistant",
            "content": "final answer",
            "reasoning_content": "thinking after tool",
            "round_id": "round_1",
            "client_request_id": "req_1",
        },
    ]

    ui_msgs = routes._convert_messages(raw_msgs)

    assert len(ui_msgs) == 1
    assert ui_msgs[0]["body"] == "final answer"
    assert ui_msgs[0]["clientRequestId"] == "req_1"
    assert ui_msgs[0]["thinking"] == "thinking before tool\n\nthinking after tool"
    assert [tool["name"] for tool in ui_msgs[0]["tools"]] == ["search"]


def test_convert_messages_collapses_consecutive_duplicate_user_messages():
    from webui import routes

    raw_msgs = [
        {"role": "user", "content": "介绍你自己和你能做的事", "round_id": "round_1", "client_request_id": "req_1"},
        {"role": "user", "content": "介绍你自己和你能做的事", "round_id": "round_2", "client_request_id": "req_2"},
        {"role": "assistant", "content": "ok", "round_id": "round_2", "client_request_id": "req_2"},
    ]

    ui_msgs = routes._convert_messages(raw_msgs)

    assert len(ui_msgs) == 2
    assert ui_msgs[0]["role"] == "user"
    assert ui_msgs[0]["clientRequestId"] == "req_2"
    assert ui_msgs[1]["role"] == "agent"


def test_convert_messages_dedupes_repeated_message_ids_even_when_not_adjacent():
    from webui import routes

    raw_msgs = [
        {"role": "user", "content": "same prompt", "message_id": "u1", "round_id": "round_1", "client_request_id": "req_1"},
        {"role": "assistant", "content": "reply", "message_id": "a1", "round_id": "round_1", "client_request_id": "req_1"},
        {"role": "user", "content": "same prompt", "message_id": "u1", "round_id": "round_1", "client_request_id": "req_1"},
    ]

    ui_msgs = routes._convert_messages(raw_msgs)

    assert len(ui_msgs) == 2
    assert [msg["messageId"] for msg in ui_msgs] == ["u1", "a1"]


def test_convert_messages_collapses_repeated_user_bodies_within_one_user_block():
    from webui import routes

    raw_msgs = [
        {"role": "user", "content": "check", "message_id": "u1"},
        {"role": "user", "content": "现在先看看多伦多的天气", "message_id": "u2"},
        {"role": "user", "content": "check", "message_id": "u3"},
        {"role": "user", "content": "现在先看看多伦多的天气", "message_id": "u4"},
        {"role": "assistant", "content": "ok", "message_id": "a1"},
    ]

    ui_msgs = routes._convert_messages(raw_msgs)

    assert len(ui_msgs) == 3
    assert [msg["messageId"] for msg in ui_msgs] == ["u3", "u4", "a1"]
    assert [msg["body"] for msg in ui_msgs[:2]] == ["check", "现在先看看多伦多的天气"]


def test_convert_messages_marks_intermediate_replies():
    from webui import routes

    raw_msgs = [
        {
            "role": "assistant",
            "content": "先汇报一个阶段性结论",
            "round_id": "round_1",
            "client_request_id": "req_1",
            "message_id": "a_mid",
            "intermediate_reply": True,
        },
        {
            "role": "assistant",
            "content": "最终答复",
            "round_id": "round_1",
            "client_request_id": "req_1",
            "message_id": "a_final",
        },
    ]

    ui_msgs = routes._convert_messages(raw_msgs)

    assert ui_msgs[0]["messageId"] == "a_mid"
    assert ui_msgs[0]["intermediateReply"] is True
    assert ui_msgs[1]["messageId"] == "a_final"
    assert "intermediateReply" not in ui_msgs[1]


async def test_run_chat_agent_returns_main_agent_text_directly(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(_agent_session, "_refresh_session_labels", AsyncMock())
    monkeypatch.setattr(agent, "get_context", lambda max_chars=5000: "")

    async def fake_run_main_agent(user_message, history, bot, chat_id, db_path, system_prompt="", client_request_id="", persist_user_message=True, lang="", **kwargs):
        round_id = agent._current_round_id.get()
        await agent._save_session_messages([
            {"role": "user", "content": user_message, "round_id": round_id},
            {"role": "assistant", "content": "raw final", "round_id": round_id},
        ])
        return "raw final"

    monkeypatch.setattr(_agent_core, "_run_main_agent", fake_run_main_agent)

    result = await agent._run_chat_agent("hi", None, 0, "db.sqlite3")
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert result == "raw final"
    assert saved[-1]["role"] == "assistant"
    assert saved[-1]["content"] == "raw final"


async def test_run_chat_agent_returns_main_text_when_internal_trace_has_no_final_message(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(_agent_session, "_refresh_session_labels", AsyncMock())
    monkeypatch.setattr(agent, "get_context", lambda max_chars=5000: "")

    async def fake_run_main_agent(user_message, history, bot, chat_id, db_path, system_prompt="", client_request_id="", persist_user_message=True, lang="", **kwargs):
        round_id = agent._current_round_id.get()
        await agent._save_session_messages([
            {"role": "user", "content": user_message, "round_id": round_id},
            {
                "role": "assistant",
                "content": "",
                "round_id": round_id,
                "tool_calls": [{"id": "s1", "function": {"name": "spawn_subagent", "arguments": "{}"}}],
            },
        ])
        return "[Sub-agents are still working in the background. You can continue the conversation.]"

    monkeypatch.setattr(_agent_core, "_run_main_agent", fake_run_main_agent)

    result = await agent._run_chat_agent("keep going", None, 0, "db.sqlite3")
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert result.startswith("[Sub-agents are still working in the background.")
    assert saved[-1]["role"] == "assistant"
    assert saved[-1]["content"] == ""
    assert "tool_calls" in saved[-1]


async def test_tool_bash_returns_early_when_interrupted(monkeypatch):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import tools

    interrupt_event = asyncio.Event()
    monkeypatch.setattr(_agent_state, "_interrupt_event", interrupt_event)

    task = asyncio.create_task(tools._tool_bash(
        {"command": "sleep 30", "timeout_ms": 60000},
        None,
        0,
        "db.sqlite3",
        None,
    ))
    await asyncio.sleep(0.2)
    interrupt_event.set()

    payload = json.loads(await task)

    assert payload["exit_code"] == -1
    assert "interrupted" in payload["stderr"].lower()


async def test_interrupt_active_run_clears_after_locked_run_finishes():
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    agent._interrupt_event.clear()
    locked = asyncio.Event()
    release = asyncio.Event()

    async def hold_lock():
        async with agent._agent_lock:
            locked.set()
            await release.wait()

    task = asyncio.create_task(hold_lock())
    await locked.wait()

    assert agent.interrupt_active_run() is True
    assert agent._interrupt_event.is_set() is True

    release.set()
    await task
    await asyncio.sleep(0.1)

    assert agent._interrupt_event.is_set() is False


async def test_run_agent_clears_stale_interrupt_before_starting(monkeypatch):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    seen = {}

    async def fake_run_chat_agent(user_message, bot, chat_id, db_path, **kwargs):
        seen["interrupt_before_start"] = agent._interrupt_event.is_set()
        return "ok"

    monkeypatch.setattr(_agent_coordinator, "_run_chat_agent", fake_run_chat_agent)
    agent._interrupt_event.set()

    result = await agent.run_agent("hi", None, 0, "db.sqlite3")

    assert result == "ok"
    assert seen["interrupt_before_start"] is False


async def test_run_main_agent_returns_background_notice_when_monitoring_is_interrupted(monkeypatch):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round
    from cyrene import inbox
    from cyrene import subagent

    interrupt_event = asyncio.Event()
    monkeypatch.setattr(_agent_state, "_interrupt_event", interrupt_event)

    responses = iter([
        {
            "content": "",
            "tool_calls": [{"id": "u1", "function": {"name": "use_tools", "arguments": '{"task":"check"}'}}],
        },
        {
            "content": "",
            "tool_calls": [{"id": "s1", "function": {"name": "spawn_subagent", "arguments": '{"agent_id":"alice","task":"research"}'}}],
        },
    ])
    saved = []

    async def fake_call_llm(messages, tools=None, max_tokens=32000):
        return next(responses)

    async def fake_execute_tool(name, args, bot, chat_id, db_path, notify_state):
        return "spawned"

    async def fake_save(messages):
        saved.append(messages)

    async def fake_snapshot():
        return {"alice": {"status": "running", "task": "research"}}

    _patch_call_llm(monkeypatch, fake_call_llm)
    _patch_execute_tool(monkeypatch, fake_execute_tool)
    _patch_save_session(monkeypatch, fake_save)
    monkeypatch.setattr(subagent, "collect_results", lambda: asyncio.sleep(0, result="summary"))
    monkeypatch.setattr(subagent, "clear", lambda: asyncio.sleep(0))
    monkeypatch.setattr(subagent, "get_snapshot", fake_snapshot)
    monkeypatch.setattr(subagent, "get_raw_messages", lambda aid: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(subagent, "reactivate", lambda aid: asyncio.sleep(0, result=False))
    monkeypatch.setattr(inbox, "get_unread_count", lambda aid: 0)

    task = asyncio.create_task(agent._run_main_agent("check", [], None, 0, "db.sqlite3"))
    await asyncio.sleep(0.1)
    interrupt_event.set()
    result = await task

    assert result == "[Sub-agents are still working in the background. You can continue the conversation.]"
    assert saved


async def test_run_main_agent_retries_invalid_phase1_tool_and_returns_model_explanation(monkeypatch):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    calls = []
    responses = iter([
        {
            "content": "好的，先看天气。",
            "tool_calls": [
                {"id": "w1", "function": {"name": "WebSearch", "arguments": '{"query":"Toronto weather today"}'}},
            ],
        },
        {
            "content": "当前阶段没有合适工具，请改用 use_tools 进入完整工具阶段。",
            "tool_calls": [],
        },
    ])
    saved = []

    async def fake_call_llm(messages, tools=None, max_tokens=32000):
        calls.append(tools)
        return next(responses)

    async def fake_save(messages):
        saved.append(messages)

    _patch_call_llm(monkeypatch, fake_call_llm)
    _patch_save_session(monkeypatch, fake_save)

    result = await agent._run_main_agent("现在先看看多伦多的天气", [], None, 0, "db.sqlite3")

    assert result == "当前阶段没有合适工具，请改用 use_tools 进入完整工具阶段。"
    assert calls[0] is _agent_state._LIGHT_TOOL_DEFS
    assert calls[1] is _agent_state._LIGHT_TOOL_DEFS
    assert saved


async def test_refresh_session_labels_persists_titles(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene.agent import state as _agent_state
    from cyrene.agent import session as _agent_session
    from cyrene.agent import agent as _agent_core
    from cyrene.agent import coordinator as _agent_coordinator
    from cyrene.agent import guidance as _agent_guidance
    from cyrene.agent import round as _agent_round

    _patch_state_file(monkeypatch, tmp_path / "state.json")
    _patch_data_dir(monkeypatch, tmp_path)

    await agent._save_session_messages([
        {"role": "user", "content": "讨论加密货币辩论结构", "round_id": "round_1"},
        {"role": "assistant", "content": "ok", "round_id": "round_1"},
    ])

    async def fake_call_llm(messages, tools=None, max_tokens=32000, **kwargs):
        return {"content": '{"round_title":"加密货币辩论","session_title":"加密货币多代理讨论"}'}

    _patch_call_llm(monkeypatch, fake_call_llm)

    await agent._refresh_session_labels("讨论加密货币辩论结构", "round_1")
    state = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))
    labels = agent.get_session_labels()

    assert state["session_title"] == "加密货币多代理讨论"
    assert all(msg.get("round_title") == "加密货币辩论" for msg in state["messages"] if msg.get("round_id") == "round_1")
    assert labels["round_title"] == "加密货币辩论"
    assert labels["session_title"] == "加密货币多代理讨论"

    await agent._save_session_messages(state["messages"])
    preserved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))
    assert preserved["session_title"] == "加密货币多代理讨论"


def test_build_current_session_uses_saved_session_and_round_titles(tmp_path, monkeypatch):
    from webui import routes

    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)

    routes.STATE_FILE.write_text(
        json.dumps({
            "session_title": "加密货币多代理讨论",
            "messages": [
                {"role": "user", "content": "讨论加密货币辩论结构", "round_id": "round_1", "round_title": "加密货币辩论"},
                {"role": "assistant", "content": "ok", "round_id": "round_1", "round_title": "加密货币辩论"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    session = routes._build_current_session()
    user_node = next(node for node in session["flow"]["nodes"] if node["kind"] == "input")

    assert session["title"] == "加密货币多代理讨论"
    assert session["currentRoundId"] == "round_1"
    assert session["currentRoundTitle"] == "加密货币辩论"
    assert user_node["title"] == "加密货币辩论"


def test_build_current_session_uses_latest_round_id_for_chat_sidebar(tmp_path, monkeypatch):
    from webui import routes

    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)

    routes.STATE_FILE.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "first round", "round_id": "round_1", "round_title": "第一轮"},
                {"role": "assistant", "content": "done", "round_id": "round_1", "round_title": "第一轮"},
                {"role": "user", "content": "second round", "round_id": "round_2", "round_title": "第二轮"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    session = routes._build_current_session()

    assert session["currentRoundId"] == "round_2"
    assert session["currentRoundTitle"] == "第二轮"


def test_build_archive_sessions_reads_titles_and_splits_rounds(tmp_path, monkeypatch):
    from webui import routes

    monkeypatch.setattr(routes, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    routes.CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    routes.STATE_FILE.write_text('{"messages":[]}', encoding="utf-8")

    date_str = "2026-05-15"
    (routes.CONVERSATIONS_DIR / f"{date_str}.md").write_text(
        "# Conversations - 2026-05-15\n\n"
        "<!-- session_title: 加密货币多代理讨论 -->\n\n"
        "## 08:00:00 UTC\n\n"
        "<!-- round_id: round_a -->\n"
        "<!-- round_title: 设计辩论角色 -->\n\n"
        "**User**: 先设计角色\n\n"
        "**Ape**: 好\n\n"
        "---\n\n"
        "## 08:05:00 UTC\n\n"
        "<!-- round_id: round_b -->\n"
        "<!-- round_title: 让双方开始辩论 -->\n\n"
        "**User**: 现在开始辩论\n\n"
        "**Ape**: 开始\n\n"
        "---\n",
        encoding="utf-8",
    )

    sessions = routes._build_archive_sessions()
    flow = sessions[0]["flow"]
    input_titles = [node["title"] for node in flow["nodes"] if node["kind"] == "input"]

    assert sessions[0]["title"] == "加密货币多代理讨论"
    assert input_titles == ["设计辩论角色", "让双方开始辩论"]


def test_build_archive_sessions_splits_multiple_same_day_sessions_by_archive_session_id(tmp_path, monkeypatch):
    from webui import routes

    monkeypatch.setattr(routes, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    routes.CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    routes.STATE_FILE.write_text('{"messages":[]}', encoding="utf-8")

    date_str = "2026-05-15"
    (routes.CONVERSATIONS_DIR / f"{date_str}.md").write_text(
        "# Conversations - 2026-05-15\n\n"
        "## 08:00:00 UTC\n\n"
        "<!-- archive_session_id: session_alpha -->\n"
        "<!-- session_title: 第一场 -->\n"
        "<!-- round_id: round_a -->\n"
        "<!-- round_title: 设计角色 -->\n\n"
        "**User**: 第一场开始\n\n"
        "**Ape**: 好\n\n"
        "---\n\n"
        "## 08:05:00 UTC\n\n"
        "<!-- archive_session_id: session_alpha -->\n"
        "<!-- session_title: 第一场 -->\n"
        "<!-- round_id: round_b -->\n"
        "<!-- round_title: 继续讨论 -->\n\n"
        "**User**: 第一场继续\n\n"
        "**Ape**: 继续\n\n"
        "---\n\n"
        "## 09:00:00 UTC\n\n"
        "<!-- archive_session_id: session_beta -->\n"
        "<!-- session_title: 第二场 -->\n"
        "<!-- round_id: round_c -->\n"
        "<!-- round_title: 新话题 -->\n\n"
        "**User**: 第二场开始\n\n"
        "**Ape**: 开始\n\n"
        "---\n",
        encoding="utf-8",
    )

    sessions = routes._build_archive_sessions()

    assert [session["title"] for session in sessions] == ["第二场", "第一场"]
    assert sessions[0]["id"] == "archive_2026-05-15_session_beta"
    assert sessions[0]["chat"]["messages"][0]["body"] == "第二场开始"
    assert [node["title"] for node in sessions[1]["flow"]["nodes"] if node["kind"] == "input"] == ["设计角色", "继续讨论"]
