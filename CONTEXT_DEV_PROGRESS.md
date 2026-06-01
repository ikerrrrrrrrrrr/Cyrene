# Context Dev Progress

Date: 2026-06-01
Branch: `codex/context-dev`

This is the current handoff state after the context debugger work and the SimpleXNG/search cleanup.

## Goal

Build native context debugging for Cyrene agents:

- Tag every possible LLM context source with stable provenance metadata.
- Record which tagged blocks/messages/tools were included in each LLM call.
- Provide a debugger for verbose JSONL logs so we can answer: "what context was sent to this call?"

Secondary issue found during CLI testing:

- Search should use the built-in `simplexng`, not the old Docker SearXNG setup.
- DDG/Bing/Baidu scraping fallback code is deprecated and should be removed.

## Done

### Web UI Context Debugger

New file:

- `src/webui/static/app/context-debugger.jsx`

Implemented:

- Added standalone left-sidebar page `上下文` / `Context`.
- Added `GET /api/context-debug/events`.
- Added `GET /api/context-debug/events/{event_id}`.
- Context debugger list reads both in-memory recent runtime events and persisted `data/debug_*.jsonl` logs.
- Detail pane shows token estimate, included context blocks, token breakdown by type, and message-to-block map.
- Bumped Web UI cache keys in `index.html` and rebuilt compiled assets.
- Fixed `--web` startup to support automatic free-port selection and explicit `--port`.

Verified:

```powershell
npm run build
C:\Users\linuo\miniforge3\envs\cyrene\python.exe -m py_compile src\webui\routes.py src\cyrene\local_cli.py
```

- Dev server started at `http://127.0.0.1:4242`.
- `GET /api/context-debug/events?limit=1` returns 200.
- In-app browser shows the `上下文` menu and renders Context Debugger rows/details.

### Git / Setup

- Created branch `codex/context-dev`.
- Added `AGENTS.md` to `.gitignore`.
- Pulled remote changes while preserving local `AGENTS.md`.
- Cloned Conda env `cyrene` to `cyrene-bak` before environment repair.
- Repaired `cyrene` env:
  - Installed/fixed missing deps including `h2`.
  - `pip check` passes.
  - SimpleXNG starts and returns search results.

### Context Debugger First Pass

New files:

- `src/cyrene/context_trace.py`
- `src/cyrene/context_debug.py`
- `tests/test_context_trace.py`

Implemented:

- `_ctx` metadata helpers for context blocks.
- `call_llm.py` strips `_ctx` before sending API payload and emits `context_trace` in SSE events.
- `debug.py` writes `context_trace` into verbose debug JSONL and strips `_ctx` from logged messages.
- `agent/session.py` strips `_ctx` before persisting session state.
- `agent/coordinator.py` tags main system/context blocks.
- `agent/agent.py` tags history, user message, phase rules, and tool results.
- `context_debug.py` can inspect `data/debug_*.jsonl` calls and show context trace/message/tool summaries.

Verified:

```powershell
conda run -n cyrene python -m compileall src/cyrene tests/test_context_trace.py
conda run --no-capture-output -n cyrene python -m pytest tests/test_context_trace.py -q
```

Latest test result:

- `tests/test_context_trace.py`: `3 passed`
- Known warning: `Unknown config option: asyncio_mode`

### CLI / Agent Prompt Fix

Always run Cyrene CLI with `--verbose`.

Minimal CLI smoke worked:

```powershell
cmd /c "(echo /clear&&echo 请只回复 OK，用于 CLI smoke test。&&echo quit) | conda run --no-capture-output -n cyrene python -m cyrene.local_cli --verbose"
```

Big test prompt:

```text
生成2个subagent帮我看看墨尔本和温哥华最近的天气，并且讨论后向我推荐去哪个城市旅游合适
```

Findings:

- First run spawned only one subagent.
- Fixed main-agent/subagent guidance in:
  - `src/cyrene/agent/prompts.py`
  - `src/cyrene/tools.py`
- Second run did spawn/use both city weather paths and produced a recommendation.

Useful debug logs:

- `data/debug_20260531_181644.jsonl` minimal smoke.
- `data/debug_20260531_181945.jsonl` first failed big test.
- `data/debug_20260531_182316.jsonl` second big test after prompt fix.

### Search / SimpleXNG

Implemented/fixed:

- `src/cyrene/search.py`
  - Removed DDG/Bing/Baidu search helpers from the main search path.
  - `deep_search()` now calls only built-in SimpleXNG via `_search_simplexng()`.
  - Local SimpleXNG HTTP calls use `requests.Session(trust_env=False)` so `127.0.0.1` never goes through proxy env vars.
- `src/cyrene/tools.py`
  - `_tool_websearch()` always calls `deep_search(query)`.
  - Removed short-query DDG/Bing scraping.
- `src/cyrene/searxng_manager.py`
  - Generates Cyrene-managed `data/simplexng_settings.yml`.
  - Writes valid secret key and enables JSON output.
  - Writes SimpleXNG `outgoing.proxies` using an effective proxy.
  - Effective proxy behavior:
    - `SEARCH_PROXY` is only a manual override.
    - If empty, discover system/environment proxy.
    - If discovered proxy is unreachable, ignore it and direct-connect.
    - Always sets `NO_PROXY/no_proxy` for `127.0.0.1, localhost, ::1`.
  - Passes `SEARXNG_SETTINGS_PATH` to SimpleXNG child process.
  - Uses SimpleXNG `--flask` local mode because Waitress returned empty 502 in this Windows env while Flask/app test client worked.
- Web settings cleanup:
  - `src/webui/static/app/settings.jsx` no longer exposes external/fallback search modes.
  - `src/webui/static/app/i18n.jsx` no longer describes DDG/Bing/Baidu fallback search.
  - `src/webui/routes.py` normalizes search settings back to built-in SimpleXNG.

Verified:

- Local encrypted `SEARCH_PROXY` was cleared.
- Auto-detected current system proxy: `http://192.168.31.33:6578`.
- SimpleXNG search for `Vancouver weather forecast` returned 5 real results.
- `pip check`: no broken requirements.

Important note:

- `data/simplexng_settings.yml` is runtime generated/ignored. It may contain the current machine's proxy address, but it should not be committed.

## Latest Full CLI Retest

Reran the big prompt after search fixes and date anchoring:

```powershell
cmd /c "(echo /clear&&echo 生成2个subagent帮我看看墨尔本和温哥华最近的天气，并且讨论后向我推荐去哪个城市旅游合适&&echo quit) | conda run --no-capture-output -n cyrene python -m cyrene.local_cli --verbose"
```

Debug log:

- `data/debug_20260601_040800.jsonl`

Result:

- Main agent spawned both requested subagents.
- Search returned SimpleXNG results.
- Final answer recommended Vancouver.
- The previous `CancelledError` callback traceback did not recur after patching `_log_task_exception`.
- Caveat: answer quality is acceptable but still leans on June climate/monthly forecast rather than exact current day-by-day weather. Future improvement: weather-specific extraction or a weather API would make this stronger.

Context debugger check:

```powershell
conda run --no-capture-output -n cyrene python -m cyrene.context_debug data/debug_20260601_040800.jsonl --call 1
```

Confirmed the trace includes `runtime.temporal_context`, `memory.context`, `spawn_policy.conservative`, restored short-term context, and the current user request.

## Still Not Done

- CLI shutdown can log `RuntimeError: Event loop is closed` from `aiosqlite`.
- Weather-specific answer quality can be improved. Current generic web search works, but for weather it may prefer climate/monthly pages. Consider adding a weather-specific tool/API or query constraints.

## Current Git Status Snapshot

Modified:

- `.gitignore`
- `src/cyrene/agent/agent.py`
- `src/cyrene/agent/coordinator.py`
- `src/cyrene/agent/prompts.py`
- `src/cyrene/agent/session.py`
- `src/cyrene/call_llm.py`
- `src/cyrene/debug.py`
- `src/cyrene/search.py`
- `src/cyrene/searxng_manager.py`
- `src/cyrene/subagent.py`
- `src/cyrene/tools.py`
- `src/webui/routes.py`
- `src/webui/static/app/i18n.jsx`
- `src/webui/static/app/settings.jsx`

Untracked:

- `CONTEXT_DEV_PROGRESS.md`
- `src/cyrene/context_debug.py`
- `src/cyrene/context_trace.py`
- `tests/test_context_trace.py`
