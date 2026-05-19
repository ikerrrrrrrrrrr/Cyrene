# Configuration

## Environment Variables

Copy `.env.example` to `.env` and configure:

### LLM

| Variable | Description | Default |
|---|---|---|
| `OPENAI_API_KEY` | API key (OpenAI / DeepSeek / compatible) | — |
| `OPENAI_BASE_URL` | API endpoint URL | `https://api.deepseek.com/v1` |
| `OPENAI_MODEL` | Model name | `deepseek-v4-flash` |

### Agent

| Variable | Description | Default |
|---|---|---|
| `ASSISTANT_NAME` | Agent display name | `Cyrene` |

### Telegram (optional)

| Variable | Description | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token for Telegram interface | — |
| `OWNER_ID` | Your Telegram user ID | — |

### Scheduling

| Variable | Description | Default |
|---|---|---|
| `SCHEDULER_INTERVAL` | Heartbeat interval in seconds | `60` |

### SearXNG

| Variable | Description | Default |
|---|---|---|
| `SEARXNG_AUTO_START` | Auto-launch SearXNG | `1` (enabled) |
| `SEARXNG_PORT` | SearXNG listen port | `8888` |
| `SEARXNG_HOST` | SearXNG bind address | `127.0.0.1` |
| `SEARXNG_URL` | External SearXNG URL (overrides auto-start) | — |
| `SEARCH_PROXY` | Proxy for search HTTP requests | — |

### Web UI

| Variable | Description | Default |
|---|---|---|
| `WEB_PORT` | Web UI port | `4242` |

## Runtime Settings

Most settings can be edited at runtime through the Web UI **Settings** page without restarting:

- **API Keys** — Update `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `TELEGRAM_BOT_TOKEN`
- **Models** — Add or remove model configurations
- **Tools** — Enable or disable specific tools
- **Search** — Switch between built-in SearXNG, external URL, or fallback mode
- **MCP Servers** — Add, remove, and restart MCP server connections

## Model Pricing

Cyrene tracks token usage and estimates costs for known models:

| Model | Input (per 1M tokens) | Output (per 1M tokens) |
|---|---|---|
| DeepSeek Chat | $0.14 | $0.28 |
| Claude Haiku 4.5 | $0.25 | $1.25 |
| Claude Sonnet 4.6 | $3.00 | $15.00 |
| Claude Opus 4.7 | $15.00 | $75.00 |
