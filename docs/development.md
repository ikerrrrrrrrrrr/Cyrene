# Development

## Debugging

### Verbose Mode

Logs every LLM call (full prompt, tools, response, duration) to `data/debug_*.jsonl`:

```bash
PYTHONPATH=src python -m cyrene.local_cli --headless --verbose
# or
PYTHONPATH=src python -m cyrene.local_cli --web --verbose
```

### Debug Logs

With `--verbose`, events are written to timestamped JSONL files:

```text
data/debug_20260519_133426.jsonl
data/debug_20260519_134417.jsonl
```

Each log line is a JSON object:

```json
{"type": "llm_call", "caller": "main_agent", "phase": "phase1",
 "messages": [...], "response": {...}, "duration_ms": 423.0}

{"type": "tool_call", "caller": "subagent_poet", "tool": "send_agent_message",
 "args": {"to": "painter", "content": "..."}, "result": "Message sent.",
 "duration_ms": 150.2}
```

### Event Inspection

When `--verbose` is enabled, every LLM call and tool call gets a unique `event_id` (e.g., `evt_3b22f9a5c0cb`) that persists to disk. Even after a daemon restart, you can inspect full event details:

```bash
# List recent event IDs
curl http://localhost:4242/api/events/list

# Get full event detail (LLM input/output or tool args/result)
curl http://localhost:4242/api/events/evt_3b22f9a5c0cb
```

Via the CLI:

```bash
cyrene flow --session run_live --round round_xxx --id evt_3b22f9a5c0cb
```

### Web UI Debug

The **Status** page shows live debug logs, system metrics, worker status, and service health. The **Agent Flow** page visualizes every step of the agent's execution as an interactive SVG flowchart.

## Testing

```bash
# Fresh dev test setup (installs package + test dependencies)
uv pip install -e ".[dev]"

# Run MCP manager tests
python -m pytest tests/test_mcp_manager.py -v

# Run all tests (editable install makes PYTHONPATH=src unnecessary)
pytest -q
```

Some tests require an LLM endpoint to be configured.

## Project Conventions

### Code Style

- Python 3.12+
- Ruff for linting (line length: 180)
- Type hints for all function signatures
- Async/await throughout (asyncio)

### Module Pattern

Each module has a single responsibility. Cross-module communication uses:
- Function calls for direct imports
- Event bus (`debug.publish_event` / `debug.subscribe`) for real-time UI updates
- File-based inbox (`inbox.py`) for inter-agent messaging

### Adding New Tools

1. Add the handler function in `tools.py`
2. Add the tool definition to `TOOL_DEFS`
3. Add the handler to `TOOL_HANDLERS`
4. Optionally add MCP server support via `mcp_manager.py`

## CI

GitHub Actions workflow at `.github/workflows/ci.yml` runs tests on each push.
It currently runs `uv run pytest -q` and `python -m compileall src`.
