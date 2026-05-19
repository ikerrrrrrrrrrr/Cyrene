# Usage

## Web UI

```bash
# Start the daemon with Web UI (default)
PYTHONPATH=src python -m cyrene.local_cli --web
```

Open `http://localhost:4242`. The Web UI includes:

| Page | Section | What you can do |
|---|---|---|
| **Chat** | Main | Send messages, view Markdown-rendered replies, see live progress |
| | Guidance | Send guidance to running agent rounds |
| | Subagents | Monitor active sub-agents and shells |
| **Agent Flow** | Canvas | SVG timeline of LLM calls, tool executions, subagent communication |
| **Sessions** | List | Browse, search, and delete sessions |
| | Detail | View messages, tokens, subagents per session |
| **Memory** | SOUL.md | Browse and edit the personality document |
| | Short-Term | View compressed memory with emotional valence |
| | Context | Monitor context window usage |
| **Status** | Metrics | Subagents, sessions, memory, tasks |
| | Workers | Main agent and sub-agent status |
| | Services | LLM endpoint, SOUL.md, MCP servers health |
| **Settings** | General | Edit SOUL.md directly, toggle stream reasoning |
| | Models | Add/remove/select LLM models |
| | Tools | Enable/disable individual tools |
| | MCP Servers | Add/remove/restart MCP server connections |
| | Search | Switch between built-in / external / fallback modes |
| | API Keys | Edit API keys and endpoints at runtime |
| | Appearance | Theme, text size, density |

## CLI

The CLI is a thin HTTP client that communicates with the daemon at `localhost:4242`.

```bash
# Start daemon (background terminal)
PYTHONPATH=src python -m cyrene.local_cli --web

# In a new terminal:
PYTHONPATH=src python -m cyrene.cli status
PYTHONPATH=src python -m cyrene.cli do "your task" --session run_live
```

### Commands

| Command | Description |
|---|---|
| `cyrene do <text> --session <id>` | Send a message to an agent session |
| `cyrene session list` | List all sessions (live + archived) |
| `cyrene session status --session <id>` | Show session details |
| `cyrene session delete --session <id>` | Delete a session |
| `cyrene flow --session <id>` | List agent rounds |
| `cyrene flow --session <id> --round <r>` | Show round execution trace |
| `cyrene flow --session <id> --round <r> --id <e>` | Inspect a specific event (LLM call or tool call) |
| `cyrene memory soul` | Print SOUL.md |
| `cyrene memory short-term` | Print short-term memory entries |
| `cyrene memory context` | Print context window status |
| `cyrene status` | System status and metrics |
| `cyrene mcp list` | List MCP servers and their tools |
| `cyrene mcp add <name> stdio <cmd> [args...]` | Add a stdio MCP server |
| `cyrene mcp add <name> sse <url>` | Add an SSE MCP server |
| `cyrene mcp remove <name>` | Remove an MCP server |
| `cyrene mcp toggle <name>` | Enable/disable an MCP server |

Use `--json` for machine-readable output.

## Headless Mode

```bash
PYTHONPATH=src python -m cyrene.local_cli --headless
```

Interactive CLI with in-conversation commands:

| Command | Action |
|---|---|
| `/h` | Help menu — clear context, reset personality, system status |
| `/mcp` | MCP server management (list/add/remove/toggle/test) |
| `/clear` | Reset session context |
| `quit` | Exit |

## MCP Server Management

Cyrene supports the [Model Context Protocol](https://modelcontextprotocol.io) for connecting external tools.

### Add a stdio server

```bash
# Filesystem tools via npm package
cyrene mcp add filesystem stdio npx -y @modelcontextprotocol/server-filesystem /path/to/workspace

# Python-based MCP server
cyrene mcp add marp-deck stdio python /path/to/mcp_server.py
```

### Add an SSE server

```bash
cyrene mcp add my-api sse http://localhost:3000/mcp
```

### List connected servers

```bash
cyrene mcp list
```

```text
Name              Transport    Status         Tools    Endpoint
filesystem        stdio        connected      3        npx -y @modelcontextprotocol/server-filesystem .
marp-deck         stdio        connected      4        python mcp_server.py
```

MCP tools automatically appear alongside built-in tools — no restart needed.

## Telegram Bot

Set these in `.env`:

```ini
TELEGRAM_BOT_TOKEN=your_bot_token
OWNER_ID=your_telegram_user_id
```

Then run:

```bash
python -m cyrene
```

The Telegram bot supports the same two-phase loop, subagents, and tools as the Web UI.
