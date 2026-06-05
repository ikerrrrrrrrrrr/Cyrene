"""Desktop-local authentication boundary for the FastAPI backend.

The backend binds to ``127.0.0.1`` on a dynamic port and is loaded by the
Electron desktop shell.  Without a guard, any local process or malicious
webpage could hit the unauthenticated API / SSE / WebSocket endpoints.

This module provides a pure-ASGI middleware (so it covers BOTH the ``http``
and ``websocket`` scopes — a FastAPI ``@app.middleware("http")`` would miss
WebSocket upgrades).  It enforces two things:

1. **Shared token** (when ``CYRENE_AUTH_TOKEN`` is set in the environment):
   every request must carry a matching ``X-Cyrene-Token`` header.  Electron
   provisions the token via env and injects the header on every request.
   When no token is configured (manual / dev web mode without Electron) the
   token check is skipped — preserving the previous behaviour — but a single
   startup warning is logged.

2. **Host / Origin validation** (always on, even without a token): defends
   against DNS-rebinding by rejecting requests whose ``Host`` header is not a
   recognised local host, and rejecting browser requests whose ``Origin`` is
   not a local origin.
"""

import hmac
import logging
import os

logger = logging.getLogger(__name__)

# Header carrying the shared desktop-local token.
TOKEN_HEADER = b"x-cyrene-token"

# Paths exempt from token auth so health probes keep working without the token.
# ``/api/instance-id`` is used by the CLI/browser-fallback health check
# (see ``cyrene.local_cli._fallback_to_browser``).
_EXEMPT_PATHS = frozenset({"/api/instance-id"})

# Hostnames considered local (port suffix is validated separately).
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def _host_is_local(host_header: str) -> bool:
    """Return True if the ``Host`` header names a recognised local host."""
    if not host_header:
        # No Host header at all (e.g. some non-browser clients) — allow.
        return True
    host = host_header.strip()
    # Strip the port, taking care with the IPv6 ``[::1]:port`` form.
    if host.startswith("["):
        # ``[::1]`` or ``[::1]:port``
        bracket = host.find("]")
        hostname = host[: bracket + 1] if bracket != -1 else host
    else:
        hostname = host.rsplit(":", 1)[0] if ":" in host else host
    return hostname in _LOCAL_HOSTS


def _origin_is_local(origin: str) -> bool:
    """Return True if the ``Origin`` header is a local origin.

    A missing Origin is handled by the caller (allowed). When present it must
    point at ``http://127.0.0.1[:port]`` or ``http://localhost[:port]``.
    """
    if origin in ("null", ""):
        # Opaque origin (e.g. ``file://`` pages, sandboxed iframes) — reject.
        return False
    for scheme in ("http://", "https://"):
        if origin.startswith(scheme):
            rest = origin[len(scheme):]
            hostname = rest.split("/", 1)[0]
            hostname = hostname.rsplit(":", 1)[0] if ":" in hostname and not hostname.startswith("[") else hostname
            if hostname.startswith("["):
                bracket = hostname.find("]")
                hostname = hostname[: bracket + 1] if bracket != -1 else hostname
            return hostname in _LOCAL_HOSTS
    return False


def _header(scope_headers, name: bytes) -> str:
    """Return a request header value (latin-1 decoded) or empty string."""
    for key, value in scope_headers:
        if key.lower() == name:
            try:
                return value.decode("latin-1")
            except Exception:
                return ""
    return ""


class LocalAuthMiddleware:
    """Pure-ASGI middleware enforcing token + Host/Origin checks."""

    def __init__(self, app) -> None:
        self.app = app
        self._expected_token = os.environ.get("CYRENE_AUTH_TOKEN") or ""
        if not self._expected_token:
            logger.warning(
                "CYRENE_AUTH_TOKEN is not set — desktop-local token auth is "
                "DISABLED (Host/Origin checks still apply). This is expected "
                "for manual/dev web mode but should not happen under Electron."
            )

    async def __call__(self, scope, receive, send) -> None:
        scope_type = scope.get("type")

        # Lifespan and any other non-request scopes pass straight through.
        if scope_type not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        path = scope.get("path", "")

        # --- Host / Origin validation (always on) ---------------------------
        host = _header(headers, b"host")
        if not _host_is_local(host):
            await self._reject(scope, send, http_status=403, reason="bad host")
            return

        origin = _header(headers, b"origin")
        if origin and not _origin_is_local(origin):
            await self._reject(scope, send, http_status=403, reason="bad origin")
            return

        # --- Token enforcement ---------------------------------------------
        if self._expected_token and path not in _EXEMPT_PATHS:
            provided = _header(headers, TOKEN_HEADER)
            if not provided or not hmac.compare_digest(provided, self._expected_token):
                await self._reject(scope, send, http_status=401, reason="bad token")
                return

        await self.app(scope, receive, send)

    async def _reject(self, scope, send, http_status: int, reason: str) -> None:
        """Reject the request appropriately for its scope type."""
        if scope.get("type") == "websocket":
            # Per ASGI: respond to the connect with a close (1008 = policy
            # violation). Sending ``websocket.close`` before ``accept`` rejects
            # the handshake.
            await send({"type": "websocket.close", "code": 1008})
            return
        # http
        body = reason.encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": http_status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
