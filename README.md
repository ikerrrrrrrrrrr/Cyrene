# Cyrene — AI Agent Framework

Cyrene is an open-source AI agent framework with a pluggable personality system, multi-agent orchestration, and layered memory architecture.

## Quick Start

```bash
# 1. Set up environment
conda create -n cyrene python=3.12 -y
conda activate cyrene
pip install -e .

# 2. Configure LLM API
cp .env.example .env
# Edit .env with your API key and model

# 3. Launch CLI
python -m cyrene.local_cli
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
    Phase 2 (all 17 tools, subagent support)
    │   ├── Write/Read/Edit/Bash/Grep/Glob
    │   ├── WebSearch/WebFetch (SearxNG + DDG + Bing + Baidu)
    │   ├── spawn_subagent → parallel sub-agents with inbox communication
    │   └── quit → return
    │
    ▼
Chat Filter (reads SOUL.md → translates assistant tone to character voice)
    │
    ▼
User hears
```

## Key Features

### Personality System
- Inject any personality (real person, fictional character) via SOUL.md
- Chat Filter translates the assistant's dry output into the character's voice
- No biography in prompts — pure behavior profile

### Multi-Agent Orchestration
- Spawn sub-agents for parallel research, debate, or complex tasks
- Sub-agents have full tool access and communicate via inbox
- Automatic lifecycle: alive → willing_to_quit → done

### Memory System
- Three-layer: context window (≤40 turns) → short-term (compressed) → long-term (archived)
- Compressor triggers at 45+ messages, extracts facts/preferences into short-term memory
- No RAG — structured event extraction instead

### Search
- Prioritizes local SearxNG instance (no rate limits, no captchas)
- Falls back to DDG + Bing + Baidu in parallel
- Built-in rate limiting and cooldown for each engine

### Debugging
```bash
python -m cyrene.local_cli --verbose
```
Logs every LLM call (prompt, tools, response, duration) to `data/debug_*.jsonl`.

### Controls
- `/h` — Help menu (relogin, clear context, reset personality, system status)
- `/clear` — Reset session context
- `quit` — Exit CLI

## Project Structure

```
src/cyrene/
├── agent.py          # Agent loop: Phase 1/2, chat filter, session management
├── tools.py          # 17 tool handlers + definitions
├── subagent.py       # Sub-agent lifecycle + inbox communication
├── llm.py            # LLM call helpers
├── search.py         # Multi-engine search (SearxNG, DDG, Bing, Baidu)
├── soul.py           # SOUL.md personality system
├── short_term.py     # Short-term memory management
├── chat_filter.py    # Character voice translation (in agent.py)
├── scheduler.py      # Heartbeat + lottery + steward
├── config.py         # Environment config
├── inbox.py          # Agent-to-agent inbox communication
├── debug.py          # Verbose debug logging
├── setup.py          # Personality setup wizard
├── bot.py            # Telegram bot interface
└── local_cli.py      # CLI interface

workspace/             # Runtime files (SOUL.md, conversations/)
data/                  # Runtime data (state, memories, debug logs)
store/                 # SQLite database
```

## Configuration

See `.env.example` for all options:

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_API_KEY` | LLM API key (OpenAI/DeepSeek) | — |
| `OPENAI_BASE_URL` | API endpoint | `https://api.deepseek.com/v1` |
| `OPENAI_MODEL` | Model name | `deepseek-v4-pro` |
| `SEARCH_PROXY` | HTTP proxy for search | — |
| `SEARXNG_URL` | Self-hosted SearxNG | — |

## SearxNG (Recommended for Search)

```yaml
# docker-compose.yml
services:
  searxng:
    image: searxng/searxng:latest
    ports:
      - "8888:8080"
    restart: unless-stopped
```

Then set `SEARXNG_URL=http://localhost:8888` in `.env`.

## License

MIT
