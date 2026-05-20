"""
MCP (Model Context Protocol) manager for Cyrene.

Manages MCP server connections and tool lifecycle. Follows the
searxng_manager.py pattern for subprocess management (stdio transport)
and settings_store.py pattern for configuration persistence.

Supports two transport modes:
  - "stdio": spawn a subprocess and communicate over stdin/stdout
  - "sse": connect to a remote HTTP endpoint using Server-Sent Events
"""

import asyncio
import json
import logging
import os
from typing import Any

from cyrene.config import DATA_DIR

logger = logging.getLogger(__name__)

_MCP_SERVERS_FILE = DATA_DIR / "mcp_servers.json"

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_manager: "MCPManager | None" = None


def get_manager() -> "MCPManager":
    """Return the global MCPManager singleton (lazy init)."""
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager


async def start_mcp() -> None:
    """Start all enabled MCP servers via the global manager."""
    manager = get_manager()
    await manager.start()


def stop_mcp() -> None:
    """Synchronous wrapper — stops all MCP servers.

    Used in ``finally`` blocks outside the event loop (e.g. after
    ``asyncio.run()``), so we create a fresh loop to drive the async
    disconnect.
    """
    global _manager
    if _manager is not None:
        try:
            asyncio.run(_manager.stop())
        except Exception:
            logger.exception("MCP manager stop failed")
        finally:
            _manager = None


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

_DEFAULT_MCP_SERVERS: list[dict[str, Any]] = []


def get_mcp_servers() -> list[dict[str, Any]]:
    """Load MCP server configs from ``data/mcp_servers.json``."""
    if not _MCP_SERVERS_FILE.exists():
        return list(_DEFAULT_MCP_SERVERS)
    try:
        data = json.loads(_MCP_SERVERS_FILE.read_text(encoding="utf-8"))
        servers = data.get("servers", [])
        return servers if isinstance(servers, list) else list(_DEFAULT_MCP_SERVERS)
    except Exception:
        logger.exception("Failed to load MCP server config")
        return list(_DEFAULT_MCP_SERVERS)


def save_mcp_servers(servers: list[dict[str, Any]]) -> None:
    """Save MCP server configs to ``data/mcp_servers.json``."""
    try:
        _MCP_SERVERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MCP_SERVERS_FILE.write_text(
            json.dumps({"servers": servers}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("Failed to save MCP server config")


# ---------------------------------------------------------------------------
# Single server connection
# ---------------------------------------------------------------------------


class MCPServerConnection:
    """Manages one MCP server connection."""

    def __init__(self, name: str, transport: str, config: dict[str, Any]) -> None:
        self.name = name
        self.transport = transport  # "stdio" | "sse"
        self.config = config
        self._session: Any = None
        self._read_stream: Any = None
        self._write_stream: Any = None
        self._process: asyncio.subprocess.Process | None = None
        self._ctx_stack: Any = None
        self._tools: list[dict[str, Any]] = []
        self.status = "disconnected"

    async def connect(self) -> None:
        """Connect to the MCP server and discover tools."""
        if self.transport == "stdio":
            await self._connect_stdio()
        elif self.transport == "sse":
            # SSE transport still uses the MCP SDK (SSE has no anyio conflict)
            from mcp.client.sse import sse_client

            url = str(self.config.get("url", ""))
            if not url:
                raise ValueError(f"MCP server '{self.name}' has no URL configured")

            ctx = sse_client(url)
            self._ctx_stack = ctx
            self._read_stream, self._write_stream = await ctx.__aenter__()
            from mcp import ClientSession
            self._session = ClientSession(self._read_stream, self._write_stream)
            await self._session.initialize()
        else:
            raise ValueError(f"Unsupported MCP transport: {self.transport}")

        if self.transport == "stdio":
            # Initialize via raw JSON-RPC
            init_result = await self._json_rpc_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cyrene", "version": "0.1.8"},
            })
            # Send initialized notification
            notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            if self._process and self._process.stdin:
                self._process.stdin.write(notif.encode("utf-8"))
                await self._process.stdin.drain()

        self.status = "connected"

        # Discover tools
        try:
            await self._refresh_tools()
        except Exception:
            logger.warning("MCP server '%s' tool discovery failed", self.name, exc_info=True)

        logger.info("MCP server '%s' connected (%d tools)", self.name, len(self._tools))

    async def _connect_stdio(self) -> None:
        """Connect via stdio transport using raw asyncio subprocess + JSON-RPC.

        Uses pure asyncio instead of the MCP SDK's anyio-based stdio_client to
        avoid compatibility issues with uvicorn's event loop on Windows.
        """
        command = str(self.config.get("command", ""))
        args = list(self.config.get("args", []))
        if not command:
            raise ValueError(f"MCP server '{self.name}' has no command configured")

        full_args = [command] + args
        _cwd = self.config.get("cwd") or None
        _env = dict(os.environ)
        _env["PYTHONUNBUFFERED"] = "1"

        self._process = await asyncio.create_subprocess_exec(
            *full_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_cwd,
            env=_env,
        )

        logger.info("MCP server '%s' subprocess started (pid=%s)", self.name, self._process.pid)

    async def _json_rpc_request(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC request to the subprocess and return the result."""
        import uuid as _uuid
        req_id = _uuid.uuid4().hex[:8]
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params

        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError(f"MCP server '{self.name}' not running")

        payload = (json.dumps(request) + "\n").encode("utf-8")
        self._process.stdin.write(payload)
        await self._process.stdin.drain()

        # Read response line
        line = await asyncio.wait_for(self._process.stdout.readline(), timeout=15.0)
        response = json.loads(line.decode("utf-8").strip())

        if "error" in response:
            raise RuntimeError(f"MCP server '{self.name}' error: {response['error']}")
        return response.get("result", {})

    async def _connect_sse(self) -> None:
        """Connect via SSE transport."""
        from mcp.client.sse import sse_client

        url = str(self.config.get("url", ""))
        if not url:
            raise ValueError(f"MCP server '{self.name}' has no URL configured")

        ctx = sse_client(url)
        self._ctx_stack = ctx
        self._read_stream, self._write_stream = await ctx.__aenter__()

    async def _refresh_tools(self) -> None:
        """Fetch and cache tool definitions from the server via JSON-RPC."""
        try:
            result = await self._json_rpc_request("tools/list")
            raw_tools = result.get("tools", [])
            self._tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", "") or "",
                        "parameters": t.get("inputSchema", {}),
                    },
                }
                for t in raw_tools
                if t.get("name")
            ]
        except Exception:
            logger.exception("Failed to list tools from MCP server '%s'", self.name)
            self._tools = []

    def get_tool_defs(self) -> list[dict[str, Any]]:
        """Return cached tool definitions in OpenAI-compatible format."""
        return list(self._tools)

    def has_tool(self, name: str) -> bool:
        """Check if this server has a tool with the given name."""
        return any(td["function"]["name"] == name for td in self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call a tool on this server and return the text result."""
        if self.transport == "stdio":
            # Raw JSON-RPC for stdio
            result = await self._json_rpc_request("tools/call", {
                "name": name,
                "arguments": arguments or {},
            })
            content_items = result.get("content", [])
            parts: list[str] = []
            is_error = result.get("isError", False)
            for item in content_items:
                if item.get("type") == "text" and item.get("text"):
                    parts.append(item["text"])
            text = "\n".join(parts) if parts else f"(Tool '{name}' returned no text content)"
            if is_error:
                raise RuntimeError(text)
            return text
        else:
            # SSE transport uses the MCP SDK session
            if self._session is None:
                raise RuntimeError(f"MCP server '{self.name}' is not connected")
            from mcp.types import TextContent
            result = await self._session.call_tool(name, arguments or {})
            if result.isError:
                error_text = " | ".join(
                    item.text for item in result.content if isinstance(item, TextContent)
                ) or f"Tool '{name}' returned an error"
                raise RuntimeError(error_text)
            parts = [item.text for item in result.content if isinstance(item, TextContent) and item.text]
            return "\n".join(parts) if parts else f"(Tool '{name}' returned no text content)"

    async def disconnect(self) -> None:
        """Disconnect from the server and clean up resources."""
        self.status = "disconnected"
        self._tools = []

        # Close SSE session/context if present
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
        if self._ctx_stack is not None:
            try:
                await self._ctx_stack.__aexit__(None, None, None)
            except Exception:
                pass
            self._ctx_stack = None

        # Terminate subprocess (stdio transport)
        if self._process is not None:
            try:
                self._process.stdin.close()
            except Exception:
                pass
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    self._process.kill()
                    await asyncio.wait_for(self._process.wait(), timeout=3)
                except Exception:
                    pass
            except Exception:
                pass
            self._process = None

        logger.info("MCP server '%s' disconnected", self.name)


# ---------------------------------------------------------------------------
# Manager (singleton)
# ---------------------------------------------------------------------------


class MCPManager:
    """Singleton managing all MCP server connections."""

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerConnection] = {}

    async def start(self) -> None:
        """Load config and connect all enabled servers."""
        servers = get_mcp_servers()
        for cfg in servers:
            name = str(cfg.get("name", "")).strip()
            if not name:
                continue
            if not cfg.get("enabled", True):
                continue

            transport = str(cfg.get("transport", "stdio")).strip()
            conn = MCPServerConnection(name, transport, cfg)
            try:
                await conn.connect()
                self._servers[name] = conn
            except Exception:
                logger.warning("Failed to connect MCP server '%s'", name, exc_info=True)

    async def stop(self) -> None:
        """Disconnect all servers."""
        for name, conn in list(self._servers.items()):
            try:
                await conn.disconnect()
            except Exception:
                logger.exception("Failed to disconnect MCP server '%s'", name)
        self._servers.clear()

    def get_tool_defs(self) -> list[dict[str, Any]]:
        """Aggregate tool definitions from all connected servers."""
        defs: list[dict[str, Any]] = []
        for conn in self._servers.values():
            defs.extend(conn.get_tool_defs())
        return defs

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Find the server that owns *name* and call it.

        Uses "first match wins" — iterates servers in insertion order
        and calls the first one that has a tool with *name*.
        """
        for conn in self._servers.values():
            if conn.has_tool(name):
                return await conn.call_tool(name, arguments)
        raise ValueError(f"MCP tool '{name}' not found on any connected server")

    def get_server_status(self) -> list[dict[str, Any]]:
        """Return status for all configured servers."""
        servers = get_mcp_servers()
        result: list[dict[str, Any]] = []
        for cfg in servers:
            name = str(cfg.get("name", "")).strip()
            if not name:
                continue
            conn = self._servers.get(name)
            tool_count = len(conn.get_tool_defs()) if conn else 0
            result.append({
                "name": name,
                "transport": cfg.get("transport", "stdio"),
                "command": cfg.get("command", ""),
                "url": cfg.get("url", ""),
                "enabled": cfg.get("enabled", True),
                "status": conn.status if conn else "disconnected",
                "tool_count": tool_count,
            })
        return result
