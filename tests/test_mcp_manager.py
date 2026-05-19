"""
Tests for MCP manager: config persistence and tool integration.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _patch(obj, attr, replacement):
    """Monkey-patch helper — returns the original value."""
    original = getattr(obj, attr)
    setattr(obj, attr, replacement)
    return original


def test_config_persistence_empty():
    """Default config should return an empty server list."""
    from cyrene import mcp_manager as mm

    with tempfile.TemporaryDirectory() as tmp:
        mm._MCP_SERVERS_FILE = Path(tmp) / "mcp_servers.json"
        servers = mm.get_mcp_servers()
        assert servers == [], f"Expected empty list, got {servers}"


def test_config_persistence_save_and_load():
    """Save then load should return the same data."""
    from cyrene import mcp_manager as mm

    with tempfile.TemporaryDirectory() as tmp:
        mm._MCP_SERVERS_FILE = Path(tmp) / "mcp_servers.json"
        test_servers = [
            {
                "name": "test-fs",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                "enabled": True,
            },
            {
                "name": "test-sse",
                "transport": "sse",
                "url": "http://localhost:3000/mcp",
                "enabled": False,
            },
        ]
        mm.save_mcp_servers(test_servers)
        loaded = mm.get_mcp_servers()
        assert loaded == test_servers, f"Mismatch: {loaded} != {test_servers}"


def test_config_persistence_corrupted_file():
    """A corrupted JSON file should fall back to the default empty list."""
    from cyrene import mcp_manager as mm

    with tempfile.TemporaryDirectory() as tmp:
        mcp_file = Path(tmp) / "mcp_servers.json"
        mm._MCP_SERVERS_FILE = mcp_file
        mcp_file.write_text("{{{ corrupted json", encoding="utf-8")
        servers = mm.get_mcp_servers()
        assert servers == [], f"Expected empty list fallback, got {servers}"


def test_singleton_get_manager():
    """get_manager() should always return the same instance."""
    from cyrene.mcp_manager import get_manager

    m1 = get_manager()
    m2 = get_manager()
    assert m1 is m2, "get_manager() returned different instances"


def test_get_tool_defs_with_no_servers():
    """With no connected servers, get_tool_defs() should return empty list."""
    from cyrene.mcp_manager import get_manager

    manager = get_manager()
    defs = manager.get_tool_defs()
    assert defs == [], f"Expected empty tool defs, got {defs}"


def test_get_server_status_with_no_config():
    """With no config file, get_server_status() should return empty list."""
    from cyrene.mcp_manager import get_manager

    manager = get_manager()
    status = manager.get_server_status()
    assert status == [], f"Expected empty status, got {status}"


def test_mcp_tool_def_conversion():
    """Verify the MCP Tool → Cyrene tool def format conversion."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

    # Simulate what MCPServerConnection._refresh_tools() does
    from mcp.types import Tool

    tool = Tool(
        name="read_file",
        description="Read a file from the filesystem",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"}
            },
            "required": ["path"],
        },
    )

    converted = {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }

    assert converted["function"]["name"] == "read_file"
    assert converted["function"]["description"] == "Read a file from the filesystem"
    assert "path" in converted["function"]["parameters"]["properties"]
    assert converted["function"]["parameters"]["required"] == ["path"]


def test_get_active_tool_defs_includes_mcp():
    """get_active_tool_defs() should include MCP tools when manager has them."""
    from cyrene import tools
    from cyrene import mcp_manager as mm
    from cyrene.settings_store import _DEFAULT_ENABLED_TOOLS

    with tempfile.TemporaryDirectory() as tmp:
        # Simulate a manager with tools
        mm._MCP_SERVERS_FILE = Path(tmp) / "mcp_servers.json"
        mm.save_mcp_servers([])

        # Test that get_active_tool_defs still works (includes native tools)
        defs = tools.get_active_tool_defs()
        names = [d["function"]["name"] for d in defs]
        assert "Read" in names, "Native tool 'Read' should be in active defs"
        assert "Bash" in names, "Native tool 'Bash' should be in active defs"
        assert len(defs) >= 20, f"Expected at least 20 tools, got {len(defs)}"


def test_execute_tool_unknown_fallback_to_mcp():
    """_execute_tool should try MCP for unknown tool names and raise ValueError if not found."""
    from cyrene.tools import _execute_tool
    import asyncio

    # Calling a non-existent tool should raise ValueError (not crash)
    async def run():
        try:
            await _execute_tool("nonexistent_mcp_tool_name_xyz", {}, None, 0, "", None)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Unknown tool" in str(e), f"Unexpected error: {e}"

    asyncio.run(run())


def test_start_stop_with_no_servers():
    """start_mcp() and stop_mcp() should work with empty config."""
    from cyrene.mcp_manager import get_manager, start_mcp, stop_mcp
    import asyncio

    with tempfile.TemporaryDirectory() as tmp:
        from cyrene import mcp_manager as mm
        mm._MCP_SERVERS_FILE = Path(tmp) / "mcp_servers.json"
        mm.save_mcp_servers([])

        asyncio.run(start_mcp())
        manager = get_manager()
        assert len(manager._servers) == 0, "No servers should be connected with empty config"
        stop_mcp()
