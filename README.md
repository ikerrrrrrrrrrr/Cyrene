# Cyrene — AI Agent Framework

Cyrene is an open-source AI agent framework with a pluggable personality system, multi-agent orchestration, layered memory architecture, and a real-time web UI with agent activity visualization.

## Quick Start

```bash
# 1. Set up environment
conda create -n cyrene python=3.12 -y
conda activate cyrene
pip install -e .

# 2. Configure LLM API
cp .env.example .env
# Edit .env with your API key and model

# 3. Launch (Web UI + CLI)
python -m cyrene.local_cli

#   or headless CLI only:
python -m cyrene.local_cli --headless
```

On first launch, a personality setup wizard will guide you through injecting a personality (real or fictional) into the agent.

## Architecture

```
User Message
    │
    ▼
Phase 1 (lightweight: use_tools + quit)
    ├── Pure chat → return directly (1 LLM call)
    └── Needs tools → Phase 2
            │
            ▼
    Phase 2 (all tools, subagent support)
    │   ├── Write/Read/Edit/Bash/Grep/Glob
    │   ├── WebSearch/WebFetch (SearXNG, built-in)
    │   ├── spawn_subagent → parallel sub-agents with inbox
    │   └── quit → return
    │
    ▼
Chat Filter (reads SOUL.md → translates to character voice)
    │
    ▼
User hears
```

## Key Features

### Web UI + Real-Time Timeline
- Web UI at `http://localhost:4242` (default), with real-time agent activity visualization
- Every LLM call, tool execution, and subagent spawn rendered as SVG timeline
- Inbox messages shown as directed arrows between agents
- CLI and Web UI share the same session via SSE events

### Personality System
- Inject any personality (real person, fictional character) via SOUL.md
- Chat Filter translates assistant output into the character's voice
- Non-biography behavior profiles: speech patterns, verbal tics, catchphrases, contradictions

### Multi-Agent Orchestration
- Spawn sub-agents for parallel research, debate, or complex tasks
- Sub-agents have full tool access and communicate via inbox
- Lifecycle states: running → waiting → resumed → done
- Quit validator: rejects empty quits, sends feedback to agent inbox

### Memory System
- Three-layer: context window (≤40 turns) → short-term (compressed summary) → long-term
- Compressor triggers at 45+ messages, extracts facts/preferences
- No RAG — structured event extraction

### Search
- Built-in SearXNG via SimpleXNG — no Docker required, auto-starts on launch
- Falls back to DDG/Bing/Baidu if SearXNG is unavailable

### Controls
- `/h` — Help menu (relogin, clear context, reset personality, system status)
- `/clear` — Reset session context
- `quit` — Exit

### Debugging
```bash
python -m cyrene.local_cli --headless --verbose
```
Logs every LLM call (full prompt, tools, response, duration) to `data/debug_*.jsonl`.

## Project Structure

```
src/
├── cyrene/
│   ├── agent.py          # Agent loop: Phase 1/2, chat filter, session
│   ├── tools.py          # 17 tool handlers + definitions
│   ├── subagent.py       # Sub-agent lifecycle + inbox + quit validator
│   ├── search.py         # Multi-engine search (SearXNG first)
│   ├── searxng_manager.py # SearXNG subprocess lifecycle
│   ├── soul.py           # SOUL.md personality system
│   ├── short_term.py     # Memory management
│   ├── scheduler.py      # Heartbeat + lottery + steward
│   ├── setup.py          # Personality setup wizard
│   ├── inbox.py          # Agent inbox communication
│   ├── debug.py          # Verbose logging + SSE event bus
│   ├── llm.py            # LLM call helpers
│   ├── local_cli.py      # CLI + web launcher
│   └── bot.py            # Telegram interface
├── webui/
│   ├── server.py         # FastAPI app + WebBot adapter
│   ├── routes.py         # Routes + SSE events endpoint
│   └── templates/        # Jinja2 + timeline SVG
└── pyproject.toml

workspace/             # Runtime files (SOUL.md, conversations/)
data/                  # Runtime data (state, memories, debug logs)
store/                 # SQLite database
```

## Configuration

See `.env.example`. Key variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_API_KEY` | LLM API key (OpenAI/DeepSeek) | — |
| `OPENAI_BASE_URL` | API endpoint | `https://api.deepseek.com/v1` |
| `OPENAI_MODEL` | Model name | `deepseek-v4-flash` |
| `SEARXNG_URL` | SearXNG URL (override auto-start) | — |
| `SEARXNG_AUTO_START` | Auto-launch SearXNG on startup | `1` (enabled) |
| `SEARXNG_PORT` | SearXNG listen port | `8888` |
| `WEB_PORT` | Web UI port | `4242` |

## SearXNG (Built-in, no Docker)

SearXNG is now built-in via [SimpleXNG](https://github.com/jlevy/simplexng) — a standalone, Docker-free package of SearXNG.

```bash
pip install simplexng
```

Cyrene auto-starts SearXNG on launch (default port 8888). No manual config needed.

To disable auto-start, set `SEARXNG_AUTO_START=0` in `.env`. You can also point to an external SearXNG instance with `SEARXNG_URL=http://...`.

## License

MIT
