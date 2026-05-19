# Architecture

## Two-Phase Agent Loop

Cyrene uses a two-phase decision loop to minimize LLM calls for simple chat while enabling full tool use when needed:

```
User Message
    │
    ▼
Phase 1 (lightweight: only use_tools + quit)
    ├── Pure chat → return directly (1 LLM call)
    └── Needs tools → Phase 2
            │
            ▼
    Phase 2 (full tool set, up to 12 rounds)
    │   ├── File ops: Read/Write/Edit/Glob/Grep
    │   ├── Shell: Bash + persistent shells
    │   ├── Search: WebSearch/WebFetch (SearXNG built-in)
    │   ├── Subagents: spawn_subagent → parallel agents
    │   ├── MCP tools: from connected MCP servers
    │   ├── Tasks: schedule/list/pause/resume/cancel
    │   └── quit → end interaction
    │
    ▼
Response returned to user
```

## Key Features

### Personality System (SOUL.md)

Inject any personality via `workspace/SOUL.md` — a structured document with identity, beliefs, relationship dynamics, memory, and patterns. A **Steward Agent** runs every ~30 minutes to review conversations and update SOUL.md via `APPEND`/`ERASE`/`MERGE` commands. Temporary entries auto-expire after 24 hours. The chat filter translates all assistant output into the character's voice.

### Multi-Agent Orchestration

Spawn sub-agents for parallel work. Each sub-agent has full tool access and communicates via a **file-based inbox** system. Lifecycle states: `running → waiting → resumed → done / timeout`. Sub-agents wait for siblings, process inbox messages, and coordinate results. The main agent collects and synthesizes outputs.

### Three-Layer Memory

| Layer | Storage | Capacity | Maintained by |
|---|---|---|---|
| **Context Window** | `data/state.json` | ~40 messages | Auto-trimmed |
| **Short-Term** | `data/short_term.json` | Compressed summaries | Background compressor |
| **Long-Term** | `workspace/SOUL.md` | Structured document | Steward Agent (~30min) |

The short-term memory tracks emotional valence, mention count, and entry type (fact / pattern / preference / emotion). High-frequency entries (≥3 mentions) and extreme valence entries are preserved automatically.

### MCP Protocol Support

Cyrene connects to any MCP (Model Context Protocol) server — both stdio (subprocess) and SSE (HTTP) transports. Connected MCP servers expose their tools alongside built-in tools. Manage servers via the Web UI (Settings → MCP Servers) or CLI (`cyrene mcp add/list/remove/toggle`).

### Task Scheduler

Create cron, interval, or one-shot tasks via the `schedule_task` tool. A heartbeat runs every 60s to execute due tasks. Tasks persist in SQLite with execution history. A **lottery system** allows the agent to send proactive messages to the user based on probability accumulation.

### Web UI

- Real-time chat with Markdown rendering and SSE event stream
- **Agent Timeline** — SVG flowchart of every LLM call, tool execution, subagent spawn
- **Live Rounds** — monitor and send guidance to in-progress rounds
- **Memory Pipeline** — view SOUL.md, short-term memory, context window
- **Settings Editor** — edit API keys, models, tools, MCP servers at runtime
- **Sessions** — browse, switch, delete conversation sessions

### Search

Built-in SearXNG via [SimpleXNG](https://github.com/jlevy/simplexng) — no Docker required. Auto-starts on port 8888. Deep search pipeline: query generation → parallel search → filtering → synthesis (all using LLM).

### CLI

A thin HTTP client that talks to the daemon at `localhost:4242`. All Web UI features are available via CLI commands. See [Usage](usage.md#cli).

## Project Structure

```
src/
├── cyrene/                        # Core engine
│   ├── agent.py                   # Two-phase loop, session management
│   ├── tools.py                   # Tool definitions, execution, MCP integration
│   ├── subagent.py                # Sub-agent lifecycle, inbox coordination
│   ├── inbox.py                   # File-based inter-agent messaging
│   ├── search.py                  # Deep search pipeline
│   ├── searxng_manager.py         # SearXNG subprocess lifecycle
│   ├── scheduler.py               # Heartbeat, cron, lottery, steward
│   ├── soul.py                    # SOUL.md read/write
│   ├── short_term.py              # Short-term memory compression
│   ├── memory.py                  # Memory context assembly
│   ├── shells.py                  # Persistent shell sessions
│   ├── conversations.py           # Conversation archiving
│   ├── db.py                      # SQLite database
│   ├── debug.py                   # JSONL logging + SSE event bus
│   ├── llm.py                     # LLM call helpers
│   ├── config.py                  # Environment config
│   ├── settings_store.py          # Runtime settings persistence
│   ├── setup.py                   # Personality setup wizard
│   ├── bot.py                     # Telegram bot interface
│   ├── cli.py                     # CLI client
│   ├── mcp_manager.py             # MCP server lifecycle
│   └── local_cli.py               # Entry point
├── webui/                         # FastAPI + React SPA
│   ├── server.py                  # FastAPI app factory
│   ├── routes.py                  # REST API + SSE streams
│   └── static/app/
│       ├── chat.jsx               # Chat UI
│       ├── agents.jsx             # SVG timeline
│       ├── sessions.jsx           # Session browser
│       ├── memory.jsx             # Memory pipeline
│       ├── settings.jsx           # Settings panels
│       ├── status.jsx             # System metrics
│       └── ...                    # Support components
data/                               # Runtime state, debug logs
workspace/                          # SOUL.md, conversations/
store/                              # SQLite database
tests/                              # Test suite
```
