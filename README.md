<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue" alt="Python">
  <img src="https://img.shields.io/badge/version-0.5.1-blue" alt="Version">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="License">
  <img src="https://img.shields.io/badge/status-alpha-yellow" alt="Status">
</p>

<p align="center">
  <img src="docs/assets/cyrene-hero.png" alt="Cyrene hero image" width="100%">
</p>

<h1 align="center">Cyrene — AI Agent That Evolves</h1>

<p align="center">
  An open-source AI agent framework with a living personality, parallel subagents,<br>
  and zero infrastructure. No Docker, no Redis, just Python.
</p>

---

## What is Cyrene?

Cyrene is an AI agent that **runs continuously** — it has a personality (SOUL.md) it rewrites itself, remembers conversations across sessions, spawns sub-agents for parallel work, and can act proactively via scheduled tasks.

It runs as a local daemon with a Web UI (and optional Telegram/WeChat bot), connecting to any OpenAI-compatible LLM API.

---

## Feature Overview

| Feature | Status |
|---|---|
| **Two-phase agent loop** — chat-only (1 LLM call) or full tool use | Stable |
| **SOUL.md personality** — agent rewrites its own personality document | Stable |
| **Three-tier memory** — context window → short-term → long-term | Stable |
| **Parallel sub-agents** — spawn agents with full tool access, inbox coordination | Stable |
| **Deep research** — multi-round research pipeline with PDF report export | Stable |
| **Deep Reflection** — multi-round context reframing for complex or ambiguous queries | Beta |
| **Built-in web search** — SearXNG via SimpleXNG, no Docker needed | Stable |
| **MCP protocol** — connect any stdio/SSE MCP server | Stable |
| **Task scheduler** — cron, interval, one-shot tasks + proactive lottery system | Stable |
| **Behavior learning** — learns reusable skills from conversation patterns | Beta |
| **Browser live view** — WebSocket screencasting of agent's browser; headed takeover for login | Beta |
| **Web UI** — real-time chat, agent flow timeline, sessions, settings | Stable |
| **Electron desktop app** — CI builds for macOS/Windows/Linux; OS keyring auth | Beta |
| **Telegram bot** — full agent access via Telegram | Stable |
| **WeChat bot** — basic WeChat integration | Alpha |
| **Map engine** — AMap/Leaflet interactive map with pins | Beta |

---

## Limitations (current as of v0.5.1)

- **Single-user** — one workspace, one SOUL.md, no user isolation
- **Local-only Web UI** — binds to `127.0.0.1`; desktop app uses OS keyring auth, raw web server has no auth layer
- **No data retention policy** — session history grows indefinitely
- **Limited error recovery** — agent crashes are silently caught, user isn't notified
- **No API versioning** — all endpoints under bare `/api/`
- **No rate/cost limiting** — no LLM call quota protection
- **Windows from source** — requires manual patching of vendored dependencies
- **Testing** — unit tests exist but no CI test run, no integration/E2E tests

---

## Quick Start

### Option A: Pre-built (macOS / Windows / Linux)

Download the latest release for your platform from the [Releases page](https://github.com/Yongchu-Yitao/Cyrene/releases).

### Option B: From source

```bash
conda create -n cyrene python=3.12 -y
conda activate cyrene
pip install -e .

# Classic agent UI (stable)
PYTHONPATH=src python -m cyrene --agent

# New workbench UI (beta)
PYTHONPATH=src python -m cyrene --workbench
```

Open `http://localhost:4242`. First launch runs an onboarding wizard that guides you through API key configuration and personality setup.

> No `.env` file needed. All configuration is stored in an encrypted store (`data/config.enc`) and managed through the Web UI settings or onboarding wizard.

> **Windows?** Pre-built binary recommended. For source, see [docs/installation.md](docs/installation.md#windows).

---

## Documentation

- [Installation](docs/installation.md) — Linux, macOS, Windows
- [Architecture](docs/architecture.md) — Two-phase loop, features, project structure
- [Usage](docs/usage.md) — Web UI, CLI commands, in-conversation commands
- [Configuration](docs/configuration.md) — Environment variables reference
- [Development](docs/development.md) — Debugging, verbose logging, testing

---

## Tech Stack

- **Runtime** — Python 3.12+, FastAPI, Uvicorn, SQLite
- **LLM** — OpenAI-compatible API (default: DeepSeek, works with Claude/GPT/Qwen)
- **Search** — SearXNG via SimpleXNG (bundled, no Docker)
- **Browser** — Playwright (headless/headed), WebSocket screencasting
- **Desktop** — Electron + electron-builder, OS keyring (keyring)
- **Channels** — python-telegram-bot, WeChat (itchat)
- **Encryption** — Fernet (cryptography) for config store

---

## License

Apache 2.0
