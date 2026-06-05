"""Tests for the desktop-local auth boundary (webui.auth.LocalAuthMiddleware).

Covers the token enforcement and Host/Origin (DNS-rebinding) checks wired into
``create_app`` via ``app.add_middleware``.
"""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _make_app() -> FastAPI:
    from webui.auth import LocalAuthMiddleware

    app = FastAPI()
    app.add_middleware(LocalAuthMiddleware)

    @app.get("/api/instance-id")
    async def instance_id():
        return {"instance_id": "test"}

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return app


def test_no_token_configured_allows(monkeypatch):
    monkeypatch.delenv("CYRENE_AUTH_TOKEN", raising=False)
    client = TestClient(_make_app(), base_url="http://127.0.0.1")
    resp = client.get("/ping")
    assert resp.status_code == 200


def test_token_configured_correct_header_allows(monkeypatch):
    monkeypatch.setenv("CYRENE_AUTH_TOKEN", "secret-token")
    client = TestClient(_make_app(), base_url="http://127.0.0.1")
    resp = client.get("/ping", headers={"X-Cyrene-Token": "secret-token"})
    assert resp.status_code == 200


def test_token_configured_missing_header_rejected(monkeypatch):
    monkeypatch.setenv("CYRENE_AUTH_TOKEN", "secret-token")
    client = TestClient(_make_app(), base_url="http://127.0.0.1")
    resp = client.get("/ping")
    assert resp.status_code == 401


def test_token_configured_wrong_header_rejected(monkeypatch):
    monkeypatch.setenv("CYRENE_AUTH_TOKEN", "secret-token")
    client = TestClient(_make_app(), base_url="http://127.0.0.1")
    resp = client.get("/ping", headers={"X-Cyrene-Token": "wrong"})
    assert resp.status_code == 401


def test_health_path_exempt_from_token(monkeypatch):
    monkeypatch.setenv("CYRENE_AUTH_TOKEN", "secret-token")
    client = TestClient(_make_app(), base_url="http://127.0.0.1")
    # No token header, but /api/instance-id is exempt so the probe still works.
    resp = client.get("/api/instance-id")
    assert resp.status_code == 200


def test_bad_host_rejected(monkeypatch):
    monkeypatch.delenv("CYRENE_AUTH_TOKEN", raising=False)
    client = TestClient(_make_app(), base_url="http://127.0.0.1")
    resp = client.get("/ping", headers={"Host": "evil.example.com"})
    assert resp.status_code == 403


def test_local_host_allowed(monkeypatch):
    monkeypatch.delenv("CYRENE_AUTH_TOKEN", raising=False)
    client = TestClient(_make_app(), base_url="http://127.0.0.1")
    resp = client.get("/ping", headers={"Host": "127.0.0.1:8080"})
    assert resp.status_code == 200


def test_cross_origin_rejected(monkeypatch):
    monkeypatch.delenv("CYRENE_AUTH_TOKEN", raising=False)
    client = TestClient(_make_app(), base_url="http://127.0.0.1")
    resp = client.get(
        "/ping",
        headers={"Host": "127.0.0.1:8080", "Origin": "http://evil.example.com"},
    )
    assert resp.status_code == 403


def test_local_origin_allowed(monkeypatch):
    monkeypatch.delenv("CYRENE_AUTH_TOKEN", raising=False)
    client = TestClient(_make_app(), base_url="http://127.0.0.1")
    resp = client.get(
        "/ping",
        headers={"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080"},
    )
    assert resp.status_code == 200
