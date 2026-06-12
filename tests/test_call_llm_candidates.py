"""Candidate resilience in cyrene.call_llm — cooldown, connect timeout, resolution.

Regression tests for the 2026-06-11 latency incident: a dead LAN endpoint in the
model list added ~120s to every LLM call. Also pins the candidate model: the
model list is the sole ordered source of truth, with no phantom env candidate
prepended (that duplicate 401'd on every call when its key was empty).
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import cyrene.call_llm as cl


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    cl._candidate_cooldowns.clear()
    yield
    cl._candidate_cooldowns.clear()


class _CountingHandler(BaseHTTPRequestHandler):
    """Tiny OpenAI-compatible stub; per-server hit counter + fixed status."""

    def do_POST(self):  # noqa: N802
        self.server.hits += 1
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.server.status != 200:
            self.send_response(self.server.status)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "pong"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def stub_server_factory():
    servers = []

    def make(status: int):
        server = HTTPServer(("127.0.0.1", 0), _CountingHandler)
        server.status = status
        server.hits = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        base = f"http://127.0.0.1:{server.server_port}/v1"
        return server, {
            "id": f"stub-{status}-{server.server_port}",
            "model": "stub-model",
            "base_url": base,
            "api_key": "k",
            "endpoints": [f"{base}/chat/completions"],
        }

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


async def test_failed_candidate_gets_cooldown_and_is_skipped(stub_server_factory):
    bad_server, bad = stub_server_factory(500)
    good_server, good = stub_server_factory(200)

    msg = await cl.call_llm(
        [{"role": "user", "content": "hi"}],
        candidates=[bad, good],
        publish_events=False, record_usage=False,
    )
    assert msg.get("content") == "pong"
    assert bad_server.hits == 1
    assert cl._candidate_cooling(cl._candidate_key(bad))
    assert not cl._candidate_cooling(cl._candidate_key(good))

    # Second call: the failed candidate is cooling and must be skipped entirely.
    msg = await cl.call_llm(
        [{"role": "user", "content": "hi"}],
        candidates=[bad, good],
        publish_events=False, record_usage=False,
    )
    assert msg.get("content") == "pong"
    assert bad_server.hits == 1  # unchanged — skipped
    assert good_server.hits == 2


async def test_all_candidates_cooling_still_tries(stub_server_factory):
    good_server, good = stub_server_factory(200)
    cl._set_candidate_cooldown(cl._candidate_key(good))

    msg = await cl.call_llm(
        [{"role": "user", "content": "hi"}],
        candidates=[good],
        publish_events=False, record_usage=False,
    )
    assert msg.get("content") == "pong"
    # Success clears the cooldown again.
    assert not cl._candidate_cooling(cl._candidate_key(good))


async def test_connection_refused_fails_fast_and_cools_down(stub_server_factory):
    # A closed local port refuses instantly; the candidate must be cooled down
    # so the next call does not retry it.
    refused = {
        "id": "dead",
        "model": "dead-model",
        "base_url": "http://127.0.0.1:9",
        "endpoints": ["http://127.0.0.1:9/chat/completions"],
        "api_key": "",
    }
    good_server, good = stub_server_factory(200)
    msg = await cl.call_llm(
        [{"role": "user", "content": "hi"}],
        candidates=[refused, good],
        publish_events=False, record_usage=False,
    )
    assert msg.get("content") == "pong"
    assert cl._candidate_cooling(cl._candidate_key(refused))


def test_resolve_llm_candidates_is_the_model_list_in_order(monkeypatch):
    """The model list is the sole source of truth — no phantom env candidate
    prepended, entries kept in their configured order."""
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setattr(cl, "get_models", lambda: [
        {"id": "primary", "model": "deepseek-v4-flash", "api_key": "key-flash", "base_url": "https://api.deepseek.com"},
        {"id": "lan", "model": "qwen", "api_key": "", "base_url": "http://10.0.0.1:1234/v1"},
    ])
    candidates = cl._resolve_llm_candidates()
    assert [c["id"] for c in candidates] == ["primary", "lan"]
    assert candidates[0]["api_key"] == "key-flash"


def test_resolve_llm_candidates_allows_keyless_local_endpoint(monkeypatch):
    """A provider that needs no key (local model server) stays keyless and is
    not force-fed an unrelated provider's key."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setattr(cl, "get_models", lambda: [
        {"id": "lan", "model": "qwen", "api_key": "", "base_url": "http://10.0.0.1:1234/v1"},
        {"id": "cloud", "model": "deepseek", "api_key": "cloud-key", "base_url": "https://api.deepseek.com"},
    ])
    candidates = cl._resolve_llm_candidates()
    lan = next(c for c in candidates if c["id"] == "lan")
    assert lan["api_key"] == ""  # different endpoint → no inheritance


def test_resolve_llm_candidates_shares_key_within_same_endpoint(monkeypatch):
    """Same-endpoint candidates may inherit the first filled-in key, so the
    user need not paste it onto every row."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cl, "get_models", lambda: [
        {"id": "a", "model": "deepseek-v4-flash", "api_key": "shared", "base_url": "https://api.deepseek.com"},
        {"id": "b", "model": "deepseek-reasoner", "api_key": "", "base_url": "https://api.deepseek.com/v1"},
    ])
    candidates = cl._resolve_llm_candidates()
    assert next(c for c in candidates if c["id"] == "b")["api_key"] == "shared"


def test_resolve_llm_candidates_empty_when_list_empty(monkeypatch):
    """No phantom env candidate: an unconfigured install yields no candidates,
    so the caller can raise a clear 'configure a model' error."""
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setattr(cl, "get_models", lambda: [])
    assert cl._resolve_llm_candidates() == []


async def test_call_llm_returns_empty_when_no_model_configured(monkeypatch):
    """No candidates → historical empty-string contract (callers degrade), but
    the phantom env candidate that used to 401 is gone."""
    monkeypatch.setattr(cl, "get_models", lambda: [])
    monkeypatch.setattr(cl, "get_vision_models", lambda: [])
    monkeypatch.setattr(cl, "get_secondary_model", lambda: {"model": ""})
    result = await cl.call_llm(
        [{"role": "user", "content": "hi"}],
        publish_events=False, record_usage=False,
    )
    assert result == ""
