# Cyrene — AI Agent That Evolves

Cyrene is an open-source AI agent framework designed to feel alive. It runs on your own hardware, needs zero external infrastructure, and gets smarter over time through a self-evolving personality system.

## Why not just use OpenClaw / LangChain / AutoGPT?

| | Cyrene | Others |
|---|---|---|
| **Personality** | SOUL.md — a living document the agent rewrites itself via a Steward Agent. Your AI isn't stateless, it grows. | Static system prompts. No memory of who it is. |
| **Memory** | Three-tier: context window → compressed short-term (with emotional valence) → long-term SOUL.md. Conversations become the agent's identity. | Single context window. Everything is ephemeral. |
| **Cost** | Two-phase loop: lightweight decision first, full tool execution only when needed. Pure chat costs 1 LLM call. | Every turn burns tokens on tool schemas and reasoning. |
| **Search** | Built-in SearXNG via SimpleXNG. No Docker, no API key, no external service. Zero setup. | Bring your own. Or pay per-search API fees. |
| **Proactivity** | Lottery system: the agent initiates conversation when it has something to say, not just when spoken to. | Purely reactive. Never speaks unless prompted. |
| **Infrastructure** | SQLite + filesystem. That's it. No Docker, no Redis, no vector DB, no Kubernetes. | Redis, Postgres, Qdrant, S3, Docker Compose — a full cloud stack just to run locally. |
| **MCP** | Connects to any MCP server. Use community tools as if they were native. | Vendor-locked or no standard protocol. |
| **Observability** | Real-time SVG agent timeline in the browser. Every LLM call, tool execution, subagent spawn — visualized live. | Log files. Maybe. |
| **Subagents** | Inbox-based async communication. Spawn parallel workers, they coordinate via file-based message passing. | Thread-based or no parallelism. |
| **Configuration** | Edit API keys, models, tools, search mode from the Web UI at runtime. No restart needed. | Edit .env, restart, pray. |

## Quick Start

```bash
# 1. Set up environment
conda create -n cyrene python=3.12 -y
conda activate cyrene

# 2. Install dependencies
#    Linux/macOS:
pip install -e .
#    Windows (uvloop → winloop replacement):
pip install aiosqlite apscheduler croniter fastapi httpx jinja2 python-dotenv python-telegram-bot requests sniffio uvicorn "mcp>=1.27.0"
pip install winloop  # uvloop replacement for Windows
pip install simplexng --no-deps
pip install babel brotli clideps flask flask-babel httpx-socks isodate lxml markdown-it-py msgspec platformdirs pyyaml rich setproctitle typer-slim valkey whitenoise
pip install -e . --no-build-isolation
#    See "Windows note" below for post-install patches.
#    Or install all at once:
pip install -e . && pip install simplexng --no-deps  # Linux/macOS

# 3. Configure LLM API
cp .env.example .env
# Edit .env with your API key and model

# 4. Launch with Web UI (default)
PYTHONPATH=src python -m cyrene.local_cli --web

#    or headless CLI only:
PYTHONPATH=src python -m cyrene.local_cli --headless
```

On first launch, a personality setup wizard will guide you through injecting a personality (real or fictional) into the agent. Open `http://localhost:4242` for the web UI.

> **Windows note:** `simplexng` (built-in SearXNG) depends on `uvloop` which does not support Windows ([tracking issue](https://github.com/MagicStack/uvloop/issues/14)). Cyrene patches it automatically with `winloop` (a drop-in replacement), but some manual setup is required:
>
> ```bash
> # 1. Install pip packages with Tsinghua mirror (recommended for China users)
> pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
>
> # 2. Install winloop (uvloop replacement for Windows)
> pip install winloop
>
> # 3. Install all simplexng dependencies (omitted by --no-deps)
> pip install babel brotli clideps fasttext-predict flask flask-babel \
>   httpx-socks isodate lxml markdown-it-py msgspec platformdirs pyyaml \
>   rich setproctitle typer-slim valkey whitenoise
>
> # 4. Apply Windows compatibility patches:
> #    Replace uvloop with winloop in simplexng's vendored SearXNG
> #    Edit: Lib/site-packages/simplexng/_vendor/searx/network/client.py
> #      import winloop instead of uvloop on Windows
> #    Edit: Lib/site-packages/simplexng/_vendor/searx/plugins/calculator.py
> #      mp_fork = "spawn" instead of "fork" on Windows
> #    Create: Lib/site-packages/pwd.py (stub for Unix-only module)
> #    Edit: Lib/site-packages/simplexng/settings/settings_template.yml
> #      Add "json" to search.formats
> ```
>
> Alternatively, set `SEARXNG_URL` in `.env` to point to an external SearXNG instance and skip the built-in one entirely.

## Architecture

Cyrene uses a **two-phase agent loop** to minimize LLM calls for simple chat while enabling full tool use when needed:

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
    │   ├── Shell: Bash + persistent interactive shells
    │   ├── Search: WebSearch/WebFetch (SearXNG built-in)
    │   ├── Subagents: spawn_subagent → parallel agents with inbox
    │   ├── Tasks: schedule/list/pause/resume/cancel
    │   └── quit → end interaction
    │
    ▼
Response returned to user
```

## Key Features

### Web UI
- Real-time chat at `http://localhost:4242` with Markdown rendering
- **Agent Timeline** — SVG flowchart visualizing every LLM call, tool execution, subagent spawn, and inbox message in real time
- **Live Rounds** — monitor running agent rounds, send guidance to in-progress rounds
- **Memory Visualization** — view the full memory pipeline: SOUL.md, short-term memory, context window, and conversation archive
- **Settings Editor** — edit API keys, models, tools, search mode, and appearance from the UI at runtime
- **Sessions** — browse, switch, and delete conversation sessions (live + archived)
- SSE event stream for real-time updates across all components
- Dark/light themes, adjustable density and font size

### Personality System
- Inject any personality (real person, fictional character) via `workspace/SOUL.md`
- Structured sections: identity, beliefs, relationship dynamics, memory, patterns
- **Steward Agent** runs periodically (every ~30 min) to review conversations and update SOUL.md via APPEND/ERASE/MERGE commands
- Temporary memory entries auto-expire after 24 hours
- Chat filter translates assistant output into the character's voice

### Multi-Agent Orchestration
- Spawn sub-agents for parallel research, debate, or complex tasks
- Sub-agents have full tool access and communicate via a **file-based inbox** system
- Lifecycle state machine: `running → waiting → resumed → done / timeout`
- Sub-agents wait for siblings, process inbox messages, and coordinate results
- The main agent collects and synthesizes sub-agent outputs

### Memory System (Three Layers)
1. **Context Window** — last ~40 messages in the active conversation
2. **Short-Term Memory** — compressed summaries with emotional valence tracking, auto-extracted when context exceeds 45 messages
3. **Long-Term Memory** — `workspace/SOUL.md`, a living personality document maintained by the steward agent

### Task Scheduler
- Create cron, interval, or one-shot tasks via the `schedule_task` tool
- Heartbeat runs every 60s to check and execute due tasks
- Tasks persist in SQLite, with execution history and run logs
- Lottery system for probabilistic proactive messages from the agent

### Search
- Built-in SearXNG via [SimpleXNG](https://github.com/jlevy/simplexng) — no Docker required, auto-starts on launch (port 8888)
- Falls back to DDG → Bing → Baidu if SearXNG is unavailable
- Deep search pipeline: query generation (LLM) → parallel search → filtering (LLM) → synthesis (LLM)

### Telegram Bot (Optional)
- Set `TELEGRAM_BOT_TOKEN` and `OWNER_ID` in `.env` to enable
- The scheduler heartbeat and proactive messages work through the bot
- In web-only mode, a `WebBot` adapter simulates the bot interface

### Conversation Archive
- All exchanges archived to `workspace/conversations/YYYY-MM-DD.md`
- Markdown format with metadata (session title, round ID, timestamps)
- Searchable by date and text content

### Persistent Shells
- Long-running interactive shell sessions via `StartShell`/`SendShell` tools
- Maintain separate working directories and environment per shell
- Ideal for multi-step development workflows within a single agent run

### Debugging
```bash
PYTHONPATH=src python -m cyrene.local_cli --headless --verbose
```
Logs every LLM call (full prompt, tools, response, duration) to `data/debug_*.jsonl`. The web UI also shows live debug logs on the Status page.

## Project Structure

```
src/
├── cyrene/
│   ├── agent.py            # Two-phase agent loop, session management, live rounds
│   ├── tools.py            # 22 tool definitions, execution, and filtering
│   ├── subagent.py         # Sub-agent lifecycle, registry, inbox coordination
│   ├── inbox.py            # File-based inter-agent message passing
│   ├── search.py           # Multi-engine deep search pipeline
│   ├── searxng_manager.py  # SearXNG subprocess lifecycle (auto-start/stop)
│   ├── scheduler.py        # Heartbeat, cron tasks, lottery, steward trigger
│   ├── soul.py             # SOUL.md read/write, steward command processing
│   ├── short_term.py       # Short-term memory compression and cleanup
│   ├── memory.py           # Memory context assembly for LLM prompts
│   ├── shells.py           # Persistent interactive shell sessions
│   ├── conversations.py    # Conversation archiving and search
│   ├── db.py               # SQLite database (tasks + run logs)
│   ├── debug.py            # Verbose JSONL logging + SSE event bus
│   ├── llm.py              # LLM call helpers, output truncation
│   ├── config.py           # Environment config, path constants
│   ├── settings_store.py   # Runtime settings persistence
│   ├── setup.py            # First-run personality setup wizard
│   ├── bot.py              # Telegram bot interface
│   ├── local_cli.py        # Entry point: CLI, web, and headless modes
│   └── __main__.py         # python -m cyrene support
├── webui/
│   ├── server.py           # FastAPI app factory + WebBot adapter
│   ├── routes.py           # REST API + SSE event stream
│   └── static/app/
│       ├── index.html      # SPA entry point
│       ├── app.jsx         # App shell, sidebar, theme, navigation
│       ├── chat.jsx        # Chat UI with Markdown, live rounds, guidance
│       ├── agents.jsx      # SVG agent timeline visualization
│       ├── data.jsx        # Data layer (bootstrap + polling)
│       ├── sessions.jsx    # Session browser (live + archived)
│       ├── memory.jsx      # Memory pipeline viewer + SOUL.md editor
│       ├── settings.jsx    # Settings: models, tools, search, API keys, appearance
│       ├── skills.jsx      # Skill/tool library browser
│       ├── status.jsx      # System metrics, workers, health, logs
│       ├── tweaks-panel.jsx # Theme/density/font/direction overrides
│       └── styles.css      # Component styles, dark/light themes
└── pyproject.toml

workspace/                  # Runtime: SOUL.md, conversations/
data/                       # Runtime: state.json, short_term.json, inbox/, debug logs
store/                      # SQLite database (cyrene.db)
tests/                      # Test suite (runtime fixes, subagent fixes, cache fixes)
```

## Configuration

Copy `.env.example` to `.env` and configure:

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_API_KEY` | LLM API key (OpenAI/DeepSeek compatible) | — |
| `OPENAI_BASE_URL` | API endpoint | `https://api.deepseek.com/v1` |
| `OPENAI_MODEL` | Model name | `deepseek-v4-flash` |
| `ASSISTANT_NAME` | Agent display name | `Cyrene` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional) | — |
| `OWNER_ID` | Telegram user ID for heartbeat messages | — |
| `SCHEDULER_INTERVAL` | Heartbeat interval in seconds | `60` |
| `SEARXNG_AUTO_START` | Auto-launch SearXNG on startup | `1` (enabled) |
| `SEARXNG_PORT` | SearXNG listen port | `8888` |
| `SEARXNG_URL` | External SearXNG URL (overrides auto-start) | — |
| `WEB_PORT` | Web UI port | `4242` |

Most settings can also be edited at runtime through the Web UI Settings page.

## In-Conversation Commands

| Command | Action |
|---------|--------|
| `/h` | Help menu (clear context, reset personality, system status) |
| `/clear` | Reset session context |
| `quit` | End the interaction |

## SearXNG (Built-in, no Docker)

SearXNG is built-in via [SimpleXNG](https://github.com/jlevy/simplexng) — a standalone, Docker-free package of SearXNG.

```bash
pip install simplexng
```

Cyrene auto-starts SearXNG on launch (default port 8888). To disable, set `SEARXNG_AUTO_START=0` in `.env`. You can also point to an external SearXNG instance with `SEARXNG_URL=http://...`.

## License

MIT
