# Architecture constraints

- `agent.py` — core agent loop (Phase 1/2, chat filter, session), DO NOT add tool handlers here.
- `tools.py` — all tool handlers + TOOL_DEFS + TOOL_HANDLERS. Add new tools here.
- `subagent.py` — sub-agent lifecycle + registry. Keep `_run_subagent` here.
- `llm.py` — `_call_llm`, `_assistant_text`, `_truncate`. Pure functions.
- `search.py` — multi-engine search. DO NOT add search logic to tools.py.
- Phase 2 must use original `user_message`, NOT the LLM-rewritten `task`.
- SOUL.md goes to Chat Filter, not Main Agent.
- `spawn_subagent` is a tool like any other. No hardcoded debate/discussion behavior.
