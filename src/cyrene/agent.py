import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

import httpx

from contextvars import ContextVar

from cyrene.config import ASSISTANT_NAME, DATA_DIR, STATE_FILE
from cyrene.memory import get_memory_context
from cyrene.short_term import get_context, touch_entry
from cyrene.skills_registry import build_skill_prompt_block
from cyrene.settings_store import get_spawn_policy
from cyrene import debug
from cyrene.attachments import build_public_attachment_payload, register_generated_attachment
from cyrene.conversations import get_archived_round
from cyrene.llm import _assistant_text, _truncate
from cyrene.tools import get_active_tool_defs, TOOL_HANDLERS, _execute_tool
from cyrene.subagent import (
    clear as _clear_subagents,
)

logger = logging.getLogger(__name__)

# 当前 agent ID，用于 send_agent_message 识别发送者
_current_agent_id: ContextVar[str] = ContextVar("_current_agent_id", default="main")
# 当前对话轮次 ID，用于隔离多轮 flow / inbox 通信
_current_round_id: ContextVar[str] = ContextVar("_current_round_id", default="")
_current_client_request_id: ContextVar[str] = ContextVar("_current_client_request_id", default="")
# 当前调用者类型，用于 debug 日志
_caller_type: ContextVar[str] = ContextVar("_caller_type", default="main_agent")
_persist_base_messages: ContextVar[list[dict[str, Any]] | None] = ContextVar("_persist_base_messages", default=None)
_persist_merge_live_state: ContextVar[bool] = ContextVar("_persist_merge_live_state", default=False)
_persist_history_prefix_len: ContextVar[int] = ContextVar("_persist_history_prefix_len", default=0)
_persist_insert_at: ContextVar[int | None] = ContextVar("_persist_insert_at", default=None)
_pending_intermediate_user_replies: ContextVar[list[dict[str, Any]] | None] = ContextVar("_pending_intermediate_user_replies", default=None)
_reply_stream_writer: ContextVar[Callable[[dict[str, Any]], Awaitable[None]] | None] = ContextVar("_reply_stream_writer", default=None)
_agent_lock = asyncio.Lock()
_session_state_lock = asyncio.Lock()
_interrupt_event = asyncio.Event()
_MAX_HISTORY_MESSAGES = 40
_MAX_TOOL_ROUNDS = 16
# 后台 compressor 任务，防止被事件循环 GC
_pending_compressors: set[asyncio.Task] = set()
_pending_label_refreshes: set[asyncio.Task] = set()
_pending_interrupt_clearers: set[asyncio.Task] = set()
_main_inbox_worker: asyncio.Task | None = None
_active_main_round_id = ""
_active_main_round_prompt = ""
_active_main_round_public_prompt = ""
_active_main_round_started_at = 0.0
_MAIN_INBOX_AGENT_ID = "main"
_AWAITING_USER_SENTINEL = "[[cyrene.awaiting_user]]"
_ui_round_hide_initial_detail: ContextVar[bool] = ContextVar("_ui_round_hide_initial_detail", default=False)
_ui_round_assistant_meta: ContextVar[dict[str, Any] | None] = ContextVar("_ui_round_assistant_meta", default=None)
_deep_research_mode: ContextVar[bool] = ContextVar("_deep_research_mode", default=False)
_current_command: ContextVar[str] = ContextVar("_current_command", default="")
_REPORT_REF_PREFIX = "[Deep research report]"
_REPORT_REF_MAX_PREVIEW = 280

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_MAIN_AGENT_PROMPT = """You are a capable AI assistant. Get things done efficiently.

## Values
- **Ownership**: Take responsibility end-to-end. Do not stop at analysis — implement, verify, and confirm.
- **Honesty over deference**: If something is wrong or risky, say so directly. Do not fabricate results.
- **Clarity > Speed**: When a decision has non-obvious consequences, pause and explain. For routine tasks, just do it.

## Communication
- Respond clearly and directly. No conversational interjections ("Got it", "Sure", "Great question").
- No emoji. Never.
- Match the user's language. Always reply in the same language the user writes in.
- While working, give brief progress updates (1-2 sentences). After completion, give a concise final answer.
- Final answer: prefer 1-2 short paragraphs. Use lists only when the content is inherently list-shaped. Keep it flat.

## Tools
- **You have full tool access** — use it proactively. Any request that involves files, search, web, code, shell commands, scheduling, data, or sub-agents REQUIRES tools. Do NOT try to answer with text alone when a tool would help.
- The ONLY exception is pure conversation (opinions, greetings, explanations, or questions about concepts that don't need real-world data).
- When in doubt, use tools. A tool-backed answer is always better than a guess.
- If you have actually created a file (via Write, Bash, or another tool) that the user should download, call `send_file` with the real file path. The path MUST point to a file that exists — never guess or fabricate paths. Never reply with only a bare filename or path such as `report.pdf` or `/tmp/out.csv`.
- Never output a raw shell command, filename, or path as a standalone final answer unless the user explicitly asked for that exact literal text. A filename is not a command.
- For **Claude Code** operations: use `CheckClaudeCode` to see if it's running, and `StartClaudeCode` to launch it. Never use Bash to start or manage Claude Code — these dedicated tools handle tmux session creation, naming, and WebUI integration automatically.
- If the user wants Claude Code to perform a task, prefer `PromptClaudeCode` to optimize the prompt and ask for confirmation before sending it into Claude Code.
- If it helps the user stay oriented during a long task, you may call `send_message` to post a brief in-progress update before the final answer. Use it sparingly and only when there is real new information.
- Call `ask_user` proactively. Ask when: the request is ambiguous, a key detail is missing, multiple valid approaches exist and the choice matters, or you need confirmation before a high-stakes action. Guessing wrong costs more than asking. Use freeform text or add a short options list when structured choices help.
- When a task is complete, call the `quit` tool.
"""

_PHASE1_DECISION_PROMPT = """Decision phase rules:
- The only available tools right now are `use_tools`, `ask_user`, and `quit`. You cannot call concrete tools (WebSearch, Bash, Read, etc.) directly — you must use `use_tools` to unlock them.
- ALWAYS call `use_tools` when the user asks you to DO anything — file ops, search, web, code, shell, scheduling, data queries, sub-agents, etc.
- Call `quit` ONLY when the request is pure conversation (opinions, greetings, conceptual explanations) AND you are completely sure no tool could improve the answer.
- Call `ask_user` when the request is unclear, incomplete, or has multiple valid interpretations. Prefer asking over guessing — a quick question avoids wrong work. Common triggers: missing file paths, ambiguous scope, conflicting instructions, unclear preferences among reasonable alternatives.
- When in doubt between answering directly or calling `use_tools`, call `use_tools`. It is always better to have tools available than to answer blindly.
"""

_EXECUTION_SYSTEM_PROMPT = """You are a capable execution agent. Your job is to complete tasks using tools.

Rules:
- Use tools to complete the task efficiently.
- Read/Write/Edit files, run Bash commands, search the web as needed.
- You may call `send_message` to post a brief user-visible progress reply mid-run when helpful, but do not overuse it and do not treat it as the final answer.
- If you wrote a deliverable file (via Write/Bash) that the user should receive, call `send_file` with the actual path of that file. The file must already exist — never fabricate a path. Do not merely mention the filename/path in chat.
- Never emit a bare filename, bare path, or raw command line as your final answer unless the user explicitly requested literal output.
- Call `ask_user` whenever you encounter ambiguity, missing information, or a decision point that affects the outcome. Ask early — don't wait until you're stuck. Stop and wait for the user's answer before continuing.
- Return the RESULT of what you did, not a conversation.
- Be concise in tool usage.
- When done, call the `quit` tool.
- Do not fabricate results. If a tool fails or returns nothing useful, state that clearly.
"""

_DEEP_RESEARCH_PROMPT = """## Deep Research Mode

You are in **Deep Research** mode. The user has asked a question that requires thorough, multi-angle investigation. Follow this process rigorously:

### Pre-Research: Determine Report Length
**Before spawning any subagents or starting research**, you MUST ask the user about the desired report length. Call the `ask_user` tool with the question text and structured options like this:

- text: "请选择报告篇幅"
- options: ["长（30+页）：全面深度研究，覆盖所有维度", "中（20+页）：中等深度，覆盖主要维度", "短（10+页）：聚焦核心问题，精简报告"]

Do NOT list the options inside the text argument — use the dedicated options parameter. The user can also type a custom answer directly. Wait for their response, then save the chosen length for use when writing the report.

### Phase 1: Decomposition
1. Analyze the user's question and identify all sub-questions, angles, and dimensions that need investigation.
2. Break the question down into 3–8 independent research tracks. Each track should be a self-contained research question.
3. For each track, write a clear research brief: what to investigate, what kind of sources to look for, and what a good answer should cover.

### Phase 2: Parallel Research
1. **Spawn subagents for EVERY track.** You are a research coordinator, not a researcher. Your sole job is to delegate. Do ZERO research yourself — every single question, sub-question, and follow-up must go to a dedicated subagent. Launch ALL subagents simultaneously in one batch.
2. Each subagent produces a detailed research dossier packed with raw findings.
3. **If a track feels too broad, split it** into 2–3 narrower sub-tracks and spawn a subagent for each.
4. **If results come back thin or contradictory**, spawn another wave of subagents to dig deeper.
5. Never answer the user directly during this phase. Everything goes through subagents.

### Phase 3: Write the Research Report
1. You have been given research materials gathered from multiple angles. Your job is to write the final research report AS IF you personally conducted all the research. You are the author — not a coordinator, not an editor summarizing others' work.
2. Read ALL the research materials thoroughly. Identify the narrative arc: what is the central question, what are the key themes, how do different findings connect to and build on each other, where do they conflict.
3. Write a unified research report as a single expert author:
   - Start with a compelling title that captures the research question.
   - **Executive Summary** — the key takeaways a busy reader needs. Frame the question, preview the answer, highlight the most important finding.
   - **Background & Context** — set the stage. Why does this question matter? What does the reader need to know before diving in?
   - **Findings** — the body of the report. Organize by theme. When different research materials cover complementary angles on the same topic, merge them into one seamless narrative. When they contain conflicting information, present both sides and analyze the tension. Use sub-headings to guide the reader.
   - **Analysis & Implications** — what do these findings mean? Connect the dots. Identify patterns, contradictions, and gaps. Add your own analytical perspective.
   - **Limitations** — what couldn't be determined, what information was unavailable, what would require further investigation.
   - **Conclusion** — tie everything together. Answer the original question directly. Be decisive where the evidence supports it, measured where it doesn't.
   - **References** — the FINAL section. List EVERY source cited in the report with: author/organization, title, publication date (if available), and full URL. Number them [1], [2], [3]... so they can be cross-referenced.
4. **Citation format**: Every factual claim, data point, statistic, and quote MUST be marked with its source number in brackets — e.g. "according to a 2024 industry report [3], the market grew 27%". The numbered references must exactly match the References section.
5. **Forbidden**: Do NOT mention "subagents", "research tracks", "delegation", or the research process. Do NOT say things like "Subagent A found..." or "Research track 3 revealed...". The reader must believe YOU did all the research. Your report is the only thing they see — make it complete and self-contained.
6. Preserve ALL data points, specific numbers, source URLs, and important quotes from the research materials. Do not cut content — integrate it into a flowing narrative.

### Critical Output Rules
- Output ONLY the research report. No preamble, no sign-offs, no meta-commentary. The title is the first thing the user sees.
- **Language**: The report MUST be written in the user's language. This is strict. Check the user's messages and the system language setting — the entire report in Chinese or the entire report in English. Do not mix languages.
- Call `quit` immediately after the report ends.
"""

_DEEP_RESEARCH_SUBAGENT_PROMPT = """## Deep Research Subagent Mode

You are a research specialist. Your job is to gather and deliver raw, detailed findings. You are NOT a summarizer — you are a fact collector and reporter.

### Core Principle: Preserve, Don't Summarize
- Your output is the PRIMARY source material for the final report. If you condense too much, information is lost forever.
- Reproduce source content directly wherever valuable: copy key data tables, quote important passages verbatim, include full statistics rather than rounding.
- A long, detailed, information-dense report is BETTER than a concise summary. Err on the side of including too much rather than too little.

### Research Standards
- **Exhaust the web.** Run MANY searches with different queries, angles, and keywords. Follow citation chains. Read primary sources — don't settle for summaries or abstracts.
- **Triangulate.** At least 3 independent sources per key claim. Present conflicting information explicitly with sources for each side.
- **Be quantitative.** Include full numbers, statistics, dates, prices, benchmarks, survey results. Not "prices vary" but "Amazon lists $299, direct from manufacturer is $249, used on eBay averages $180-220".
- **Surface the unexpected.** Hunt for contrarian views, recent developments, hidden assumptions, edge cases.
- **Acknowledge uncertainty.** Mark confidence: [High]/[Medium]/[Low]. Distinguish facts from consensus from speculation.

### Information Gathering Process
1. Start broad to map the landscape, then deep-dive on each sub-topic.
2. For each sub-topic, run at least 3–5 different search queries.
3. Search across diverse source types: academic papers, industry reports, official docs, expert blogs, forums (Reddit, HN, Stack Exchange), GitHub, news, comparison sites.
4. If information is scarce, try alternative phrasings, adjacent topics, or different languages.
5. Don't stop at the first answer. Keep digging until you've exhausted available information.

### Output Format
- Structured report with clear sections and sub-headings.
- For each sub-topic, include: all data points found, verbatim quotes from key sources, source URLs inline, competing perspectives with their evidence.
- **Source tracking**: For every source you use, record: author/organization name, title of the page/article, publication date (if findable), and full URL. Number your sources [S1], [S2], [S3]... and place the number after each claim that draws from that source — e.g. "the market grew 27% in 2024 [S3]". This numbering will be merged into the final report's References section.
- End your report with a "## Sources" section listing every numbered source with its full details.
- Note gaps: what you couldn't find, what remains uncertain.
"""

# ---------------------------------------------------------------------------
# Deep Research Phase 3 — multi-turn report generation prompts
# ---------------------------------------------------------------------------

_OUTLINE_GENERATION_PROMPT = """You are planning a deep research report. Based on the template and research materials below, create a detailed outline in STRICT JSON format.

## Report Template
{template}

## Research Materials
{source_material}

## Rules
- You MUST include ALL top-level sections from the template. Do not skip any.
- For section "核心发现" (Core Findings), break it down into granular subsections.
  Each subsection should cover ONE focused sub-topic.
- **Length preference**: {length_pref}
  Units range: {unit_range}
  Adjust the number of subsections accordingly — more subsections = more thorough report.
- Write a detailed "prompt" for each unit describing what to cover and which aspects of the research materials to draw from.
- The "title" should be derived from the research question. Replace {{title}} and {{question}} in the template.
- **CRITICAL: Do NOT include "参考文献" / References as a writing unit.** The references section is assembled automatically by the system. Every writing unit will output its own citations, and they will be merged globally.
- Output ONLY valid JSON. No explanation, no markdown fences.

## Output JSON format
{{"title": "Report Title", "units": [
  {{"id": 1, "heading": "## 1. 执行摘要", "brief": "...", "prompt": "..."}},
  {{"id": 2, "heading": "## 2. 背景与上下文", "brief": "...", "prompt": "..."}},
  {{"id": "3.1", "heading": "### 3.1 ...", "brief": "...", "prompt": "..."}},
  ...
]}}"""

_SECTION_WRITE_PROMPT = """You are writing unit {unit_no}/{total_units} of a deep research report.

## Report Outline
{outline_json}

## Research Materials
{source_material}

## Report Written So Far
{report_so_far}

## References Already Used
{references_so_far}

## Current Unit
{unit_heading}
{brief}

## Writing Instructions
1. Write this unit in {lang}. Write in the style of a professional research report — formal, precise, and data-driven.
2. BE THOROUGH. This unit must be a substantive deep-dive, not a summary. Cover every relevant data point, quote, and finding from the research materials for this topic. If the materials contain rich information, cover ALL of it.
3. Minimum {min_words} words for this unit. If the material justifies more, write more. There is no upper limit.
4. Use [N] for citations (e.g. "market grew 27% in 2024 [1]"). BEFORE assigning a new number, check "References Already Used" above — if the source already exists there, REUSE its number. If citing a NEW source not yet in that list, assign the next available number.
5. **REFERENCE OUTPUT — STRICT FORMAT. Follow this exactly.**

After the unit body, IF you introduced any new sources, add this exact line:

## New References

Then list each new source on its own line in this format:
[N] Author/Org, "Title", publication date, URL

Example:
## New References
[3] Market Research Inc, "Global AI Report 2024", 2024, https://example.com
[4] Tech Analysis Corp, "AI Trends", 2025, https://example.com

### STRICT RULES (violations will produce a broken report):
- The marker MUST be exactly "## New References". NOT "###", NOT "References", NOT "## 参考文献", NOT "## Sources". ONLY "## New References".
- The marker MUST be at the very end of your output. Nothing after it.
- Every [N] you use in the body MUST have a matching entry in either "References Already Used" or "## New References". No orphan citations.
- One source per line. No blank lines between sources.
- If you cited ZERO new sources, do NOT include "## New References" at all. Just end after the section body."""

_EXPANSION_PROMPT = """You are reviewing a draft research report to identify sections that need expansion.

## Completed Report
{final_report}

## Research Materials
{source_material}

## Instructions
1. Read the draft carefully. Identify any section that feels too thin, underdeveloped, or lacking in detail.
2. For each such section, write an expanded version that is at least 500 words and incorporates more data points, quotes, and analysis from the research materials.
3. Output the expanded sections with headers matching the originals that should be REPLACED.
4. If all sections are already substantive, output nothing."""


_QUICK_ANSWER_PROMPT = """## Quick Answer Mode

You are in **Quick Answer** mode. The user wants a fast, direct, text-only answer.

### Rules
- Answer in pure text. Do NOT call any tools — not even Read, WebSearch, or Bash.
- Your ONLY available tool is `quit` — use it after delivering your answer.
- This is for pure conversation, explanations, opinions, and conceptual questions.
- If the question genuinely requires tools to answer (e.g. "what files are in my directory"), briefly explain that Quick Answer mode cannot use tools, and suggest deselecting the command.
- Be concise. No research, no file access, no web search.
- Match the user's language.
"""

_HELP_ME_DECIDE_PROMPT = """## Help Me Decide Mode

You are in **Help Me Decide** mode. The user is facing a decision and needs a structured analysis to choose.

### Phase 1: Clarify the Decision
1. Identify what the user is deciding between (the options).
2. Decompose the decision into 3-6 evaluation dimensions (e.g. cost, time, risk, long-term value, personal fit, flexibility).
3. For each option, write a clear research brief covering all dimensions.

### Phase 2: Parallel Research
1. **Spawn one subagent per option.** Launch ALL simultaneously.
2. Each subagent researches its assigned option across ALL dimensions, gathering data, reviews, comparisons, and expert opinions.
3. Do ZERO research yourself — your job is to coordinate.

### Phase 3: Synthesis
1. Once all subagents return, synthesize into a decision report:
   - **Decision at Hand** — restate the choice
   - **Option-by-Option Analysis** — one section per option, covering performance on each dimension
   - **Cross-Comparison** — side-by-side comparison on the most important dimensions
   - **Recommendation** — which option to choose and WHY, with confidence level
   - **Key Trade-offs** — what the user gives up with the recommended choice
2. Be honest about which option is best. Do not force false balance.
3. Cite sources. Be clear about what is data-backed vs. inferred.
"""

_DECISION_SUBAGENT_PROMPT = """## Decision Research Subagent

You are researching ONE specific option in a decision analysis. Your job is to gather and present comprehensive information about this option.

### Rules
- Use every available tool (web search, file reading, etc.) to research your assigned option.
- Cover ALL evaluation dimensions provided in your task brief.
- For each dimension: find data, reviews, expert opinions, pricing, and real user experiences.
- Cross-check facts across at least 3 independent sources.
- Structure your report:
  1. **Option Overview** — what it is, key facts
  2. **Dimension-by-Dimension Analysis** — detailed findings per dimension
  3. **Pros & Cons** — weighted by importance
  4. **Confidence Levels** — [High]/[Medium]/[Low] for each key claim
- Be fair. Acknowledge both strengths and weaknesses of your option.
- Return your report to the main agent for synthesis.
"""

_LEARNING_PLAN_PROMPT = """## Learning Plan Mode

You are in **Learning Plan** mode. The user wants to learn a skill or subject. You will design a structured learning plan AND schedule ongoing support.

### Phase 1: Understand the Learner
1. If the user hasn't already specified, use `ask_user` to clarify: their current level, how much time they can commit per week, their learning style (video/text/hands-on), and their ultimate goal.
2. Decompose the subject into 3-6 knowledge modules. Each module should be a coherent learning unit.

### Phase 2: Parallel Research
1. **Spawn one subagent per knowledge module.** Launch ALL simultaneously.
2. Each subagent researches the BEST learning resources for its module: books, courses, tutorials, projects, communities.
3. Each subagent must also design practice exercises and quiz questions for its module.
4. Do ZERO research yourself — delegate everything.

### Phase 3: Build the Timed Learning Plan
1. Synthesize all subagent findings into a structured learning plan with a concrete TIMELINE:
   - **Goal & Prerequisites** — what the user wants to achieve and what they need first
   - **Timeline Overview** — week-by-week or day-by-day schedule. Map each module to specific calendar slots based on the user's weekly time commitment. Example: "Week 1 (Mon-Wed): Module 1 foundation, Thu-Fri: Module 1 practice exercises, Sat: Module 1 quiz"
   - **Per Module**: topic overview, recommended resources (with links/names), estimated hours, practice exercises with due dates, completion criteria, quiz questions with scheduled quiz dates
   - **Practice Sessions** — specific dates and times when the user should do hands-on exercises. What to build, what problems to solve.
   - **Quiz Schedule** — specific dates when the agent will quiz the user. For each quiz, specify: what topics are covered, what format (Q&A / problem-solving / project review), and how many questions.
   - **Milestones** — dated checkpoints to verify progress (e.g. "By Week 2 Friday, you should be able to build X independently")
   - **Total Time Estimate** — realistic time budget broken down by module and activity type
   - **Tips & Pitfalls** — common mistakes and how to avoid them

### Phase 4: Schedule Everything
1. Use the `schedule_task` tool to create real scheduled reminders. Create ONE task per milestone/quiz:
   - **Module start reminders**: "📚 今天开始学习 [模块名]。目标：[具体目标]。资源：[资源名]"
   - **Practice session reminders**: "🛠️ 今天是练习日！完成 [练习任务]。完成后告诉我你的进度。"
   - **Quiz sessions**: "🧠 今天是测验日！我会考你 [模块名] 的内容。准备好了就回复我开始。"
2. Schedule quiz sessions at module boundaries (after each module's practice is complete) and a final comprehensive quiz at the end.
3. Use `schedule_type: "cron"` or `"interval"` depending on the user's preferred rhythm. For regular study sessions (e.g. every Mon/Wed/Fri), use cron. For one-time milestones, use `"once"`.
4. Tell the user clearly: which dates/times the agent will check in and quiz them, and what they should prepare for each session.

### Important
- Make the plan immediately actionable. The user should know what to do TODAY.
- When a scheduled quiz fires, the agent will use `ask_user` to present quiz questions and evaluate answers.
- The agent should give feedback on quiz answers — celebrating progress and gently correcting mistakes.
- Match the user's language throughout.
"""

_LEARNING_SUBAGENT_PROMPT = """## Learning Resource Subagent

You are researching ONE knowledge module for a learning plan. Your job is to find the best learning resources, design practice exercises, and write quiz questions.

### Rules
- Use web search extensively to find learning resources: books, online courses, tutorials, documentation, projects, communities.
- For each resource, evaluate: quality, difficulty level, cost, time commitment, and prerequisite knowledge.
- Find resources for different budgets and learning styles (video vs. text vs. hands-on).

### Practice Design
- Design 2-4 specific hands-on exercises for this module. Each exercise should:
  - Have a clear goal ("Build X that does Y")
  - Be achievable within the estimated time for this module
  - Build on concepts taught in the recommended resources
  - Include success criteria (what "done" looks like)

### Quiz Design
- Design 3-6 quiz questions that test understanding of this module. Mix question types:
  - **Knowledge check**: "What is X? Explain in your own words."
  - **Application**: "How would you solve Y using what you learned?"
  - **Comparison**: "Compare approach A and B. When would you use each?"
  - **Debugging**: "Here's a piece of code with a bug. Find and fix it."
- Include expected answers or grading criteria for each question.

### Report Structure
1. **Module Overview** — what this module covers
2. **Recommended Resources** — ranked list with evaluation, links, why each is good
3. **Suggested Learning Order** — how to consume the resources (what first, what next)
4. **Practice Exercises** — detailed exercises with goals, steps, and success criteria
5. **Quiz Questions** — questions with expected answers/grading criteria
6. **Estimated Time** — realistic hours needed, broken into learning vs. practice
- Flag free vs. paid resources clearly.
- Return your report to the main agent for synthesis.
"""

_DAILY_REVIEW_PROMPT = """## Daily Review Mode

You are in **Daily Review** mode. Review today's activity and produce a personal daily report.

### What to Do
1. Read the available memory context (SOUL.md, short-term memory, today's conversation history).
2. Reflect on what happened today: topics discussed, decisions made, insights gained, emotions observed.
3. Produce a structured daily report:
   - **Today's Topics** — what was discussed or worked on
   - **Key Insights** — things learned or realized today
   - **Emotional Arc** — mood or emotional patterns observed (if any)
   - **Open Loops** — things mentioned but not completed, promises made, follow-ups needed
   - **Tomorrow's Suggestions** — what to focus on next, based on today's context
4. Be warm, personal, and insightful. This is a life companion reflecting with the user.
5. Use the user's language. Keep it concise but meaningful.
6. Do NOT spawn subagents. This is a solo reflection task.
"""

_DEEP_COMPARE_PROMPT = """## Deep Compare Mode

You are in **Deep Compare** mode. Compare multiple items across dimensions with parallel, web-driven research.

### Phase 1: Define the Comparison
1. Identify what items the user wants to compare (2-5 items).
2. Define 3-6 comparison dimensions (e.g. price, quality, features, reliability, user experience, long-term value).
3. For each dimension, write a clear research brief: what data to look for, what makes a good source, and what a complete answer looks like.

### Phase 2: Parallel Research
1. **Spawn one subagent per dimension.** Launch ALL simultaneously.
2. Each subagent MUST use web search extensively to gather real-world data: prices, reviews, benchmarks, expert comparisons, user ratings, news articles.
3. Do ZERO research yourself — delegate everything.

### Phase 3: Synthesis
1. Synthesize into a comparison report:
   - **Items Compared** — brief description of each
   - **Comparison Matrix** — table of items × dimensions with ratings and brief justifications
   - **Dimension-by-Dimension Analysis** — detailed comparison per dimension, with specific data points and sources
   - **Scenario Recommendations** — best pick for different use cases/priorities
   - **Overall Winner** — which item wins overall, and why
2. Be specific. Every claim must be backed by data from web search.
3. Cite sources inline with URLs. Flag when data is estimated vs. verified.
"""

_COMPARE_SUBAGENT_PROMPT = """## Comparison Subagent

You are comparing ALL items on a SINGLE dimension. Your PRIMARY tool is web search — you MUST use it aggressively to find real data.

### Search Methodology
1. **Start broad**: search for "[dimension] comparison [item1] vs [item2]" to find existing comparisons.
2. **Go specific**: search each item individually for its data on this dimension (e.g. "[item1] price 2024", "[item2] user reviews reddit").
3. **Cross-validate**: find at least 3 independent sources for each key data point. Never rely on a single source.
4. **Go deep**: search for expert reviews, user forums, official specs, third-party benchmarks, and news articles. Different source types reveal different angles.
5. **Look for controversy**: search for negative reviews, complaints, and criticisms of each item on this dimension. The weaknesses are as important as the strengths.

### Output Requirements
- Compare ALL items on your assigned dimension. Rank them from best to worst with clear justification.
- Include specific numbers wherever possible: prices, scores, ratings, percentages, benchmarks.
- Structure your report:
  1. **Dimension** — what you're comparing and why it matters
  2. **Ranked Results** — each item's score/rating with detailed explanation and source URLs
  3. **Key Data Points** — table of specific numbers/quotes with sources
  4. **Data Sources** — all URLs consulted, with brief credibility assessment
  5. **Confidence** — how reliable the comparison is on this dimension, what data was missing
- Be fair and precise. If data is incomplete or items are too close to call, say so explicitly.
- Return your report to the main agent for synthesis.
"""

_CLAUDE_CODE_PROMPT = """## Claude Code Mode

You are in **Claude Code** mode. The user wants Cyrene to help route work through Claude Code.

### What to Do
1. First, call `CheckClaudeCode` to see if Claude Code is already running.
2. If not running, call `StartClaudeCode` to launch it in a tmux session.
3. If the user gave a concrete task for Claude Code, use `PromptClaudeCode` to prepare a stronger prompt and ask the user to confirm it.
4. After the user confirms, the system will send that prompt into Claude Code automatically.
5. If the user did not give a task, just let them know Claude Code is ready in the side panel.
6. Do NOT execute the task yourself when the user explicitly wants Claude Code to do it.
"""


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))


async def optimize_claude_code_prompt(task: str) -> str:
    raw_task = str(task or "").strip()
    if not raw_task:
        return ""

    optimizer_system = (
        "You rewrite user requests into high-signal prompts for Claude Code.\n"
        "Return only the final prompt text. No preface, no markdown fences.\n"
        "Make the prompt concrete, execution-oriented, and easy for Claude Code to act on.\n"
        "When useful, include: goal, constraints, files/areas to inspect, expected output, and verification.\n"
        "Preserve the user's language."
    )
    optimizer_user = (
        "Rewrite this request into a better Claude Code prompt.\n\n"
        f"Original request:\n{raw_task}"
    )
    try:
        response = await _call_llm(
            [
                {"role": "system", "content": optimizer_system},
                {"role": "user", "content": optimizer_user},
            ],
            tools=None,
            max_tokens=1200,
        )
        optimized = _assistant_text(response).strip()
        if optimized:
            return optimized
    except Exception:
        logger.exception("Failed to optimize Claude Code prompt")

    return _fallback_claude_code_prompt(raw_task)


def _fallback_claude_code_prompt(task: str) -> str:
    text = str(task or "").strip()
    if not text:
        return ""
    if _contains_cjk(text):
        return (
            "请帮我完成下面这项任务。\n\n"
            f"任务目标：\n{text}\n\n"
            "要求：\n"
            "1. 先阅读并定位相关代码或文件\n"
            "2. 说明你的修改计划\n"
            "3. 实施修改\n"
            "4. 运行必要的验证或测试\n"
            "5. 最后总结改动内容、影响范围和验证结果"
        )
    return (
        "Please complete the following task.\n\n"
        f"Goal:\n{text}\n\n"
        "Requirements:\n"
        "1. Inspect the relevant code or files first\n"
        "2. State the implementation plan briefly\n"
        "3. Make the changes\n"
        "4. Run relevant verification or tests\n"
        "5. Summarize what changed, impact, and validation results"
    )


def build_claude_code_question_payload(task: str, optimized_prompt: str, tmux_session: str = "") -> dict[str, Any]:
    source_task = str(task or "").strip()
    prompt = str(optimized_prompt or "").strip()
    chinese = _contains_cjk(source_task or prompt)
    text = (
        "我已经把要交给 Claude Code 的提示词优化好了。确认后我会直接发送到 Claude Code 终端并开始运行。\n\n"
        "优化后的提示词：\n"
        f"{prompt}"
        if chinese else
        "I optimized the prompt for Claude Code. After you confirm, I will send it to the Claude Code terminal and run it.\n\n"
        "Optimized prompt:\n"
        f"{prompt}"
    )
    options = ["同意并发送", "取消"] if chinese else ["Send it", "Cancel"]
    meta = {
        "kind": "claude_code_prompt_confirmation",
        "task": source_task,
        "optimized_prompt": prompt,
        "tmux_session": str(tmux_session or "").strip(),
    }
    return {
        "text": text,
        "options": options,
        "allow_custom": True,
        "meta": meta,
    }


def _spawn_policy_prompt_block(policy: str) -> str:
    if policy == "aggressive":
        return (
            "## Subagent Spawn Policy\n"
            "Current policy: aggressive.\n"
            "- Proactively look for work that can be split into independent parallel subtasks.\n"
            "- If there is clear benefit from parallel research, verification, or implementation slices, spawn subagents early.\n"
            "- Favor delegation when task boundaries are clean and multiple tracks can advance at once."
        )
    if policy == "off":
        return (
            "## Subagent Spawn Policy\n"
            "Current policy: off.\n"
            "- Do not spawn subagents.\n"
            "- Complete the task as a single main agent unless the user explicitly requests multi-agent delegation.\n"
            "- Even if parallel work seems helpful, stay in single-agent mode by default."
        )
    return (
        "## Subagent Spawn Policy\n"
        "Current policy: conservative.\n"
        "- Spawn subagents only when parallelism is clearly beneficial.\n"
        "- Prefer delegation for well-bounded independent tasks, not for tightly coupled or trivial work.\n"
        "- If the benefit is marginal, keep the work in the main agent."
    )

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _load_session_messages() -> list[dict[str, Any]]:
    state = _load_session_state()
    messages = state.get("messages", [])
    return messages if isinstance(messages, list) else []


def _load_pending_question() -> dict[str, Any]:
    state = _load_session_state()
    pending = state.get("pending_question", {})
    return dict(pending) if isinstance(pending, dict) else {}


def get_pending_question() -> dict[str, Any]:
    return _load_pending_question()


def _load_round_messages(round_id: str) -> list[dict[str, Any]]:
    target_round_id = str(round_id or "").strip()
    messages = _load_session_messages()
    if not target_round_id:
        return messages
    return [
        msg
        for msg in messages
        if str(msg.get("round_id", "")).strip() == target_round_id
    ]


def _load_session_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read state file")
        return {}
    return data if isinstance(data, dict) else {}


def _write_session_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_archive_session_id(state: dict[str, Any]) -> str:
    archive_session_id = str(state.get("archive_session_id", "")).strip()
    if not archive_session_id:
        archive_session_id = f"session_{uuid4().hex[:12]}"
        state["archive_session_id"] = archive_session_id
    return archive_session_id


def _trim_session_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(messages) <= _MAX_HISTORY_MESSAGES:
        return messages
    trimmed = messages[-_MAX_HISTORY_MESSAGES:]
    # Strip orphan tool messages from the start (their tool_calls were trimmed off)
    while trimmed and trimmed[0].get("role") == "tool":
        trimmed = trimmed[1:]
    # Strip orphan tool_calls from the end (their tool responses were trimmed off)
    for i in range(len(trimmed) - 1, -1, -1):
        if trimmed[i].get("tool_calls") and (i + 1 >= len(trimmed) or trimmed[i + 1].get("role") != "tool"):
            return trimmed[:i]
    return trimmed


def _report_title_from_text(text: str, fallback: str = "Deep Research Report") -> str:
    source = str(text or "").strip()
    if not source:
        return fallback
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        if stripped:
            return _fallback_label(stripped, limit=120)
    return _fallback_label(source, limit=120)


def _report_reference_stub(
    *,
    round_id: str,
    round_title: str,
    archive_session_id: str,
    full_text: str,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    report_title = _report_title_from_text(full_text, fallback=round_title or "Deep Research Report")
    preview = ""
    body_lines = [line.strip() for line in str(full_text or "").splitlines() if line.strip()]
    for line in body_lines[1:]:
        if line.startswith("## "):
            break
        preview = line
        if preview:
            break
    preview = _fallback_label(preview, limit=_REPORT_REF_MAX_PREVIEW) if preview else ""
    content = f"{_REPORT_REF_PREFIX} {report_title}"
    if preview:
        content += f"\n{preview}"
    content += "\n完整报告已归档；仅在明确引用这篇报告时才会重新加载全文。"
    entry: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "report_ref": True,
        "report_title": report_title,
        "report_round_id": round_id,
        "report_archive_session_id": archive_session_id,
        "report_preview": preview,
    }
    if round_title:
        entry["round_title"] = round_title
    if attachments:
        entry["attachments"] = [dict(item) for item in attachments if isinstance(item, dict)]
    return entry


def _compress_report_messages_for_storage(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state = _load_session_state()
    archive_session_id = _ensure_archive_session_id(state)
    result: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or not bool(message.get("deep_research_report")):
            result.append(message)
            continue
        compressed = _report_reference_stub(
            round_id=str(message.get("round_id", "")).strip(),
            round_title=str(message.get("round_title", "")).strip(),
            archive_session_id=archive_session_id,
            full_text=_assistant_text(message) or str(message.get("content") or ""),
            attachments=message.get("attachments") if isinstance(message.get("attachments"), list) else None,
        )
        for key in ("message_id", "client_request_id", "subagent_flow_snapshot"):
            if message.get(key):
                compressed[key] = message[key]
        result.append(compressed)
    return result


def _iter_report_refs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or not bool(message.get("report_ref")):
            continue
        if (
            str(message.get("report_archive_session_id", "")).strip()
            and str(message.get("report_round_id", "")).strip()
            and str(message.get("report_title", "")).strip()
        ):
            refs.append(message)
    return refs


def _looks_like_report_followup(user_message: str, report_refs: list[dict[str, Any]]) -> bool:
    text = str(user_message or "").strip()
    if not text or not report_refs:
        return False
    lowered = text.lower()
    direct_cues = (
        "基于", "根据", "引用", "那篇报告", "这篇报告", "之前的报告", "上次的报告",
        "研究报告", "深度研究", "那份研究", "这份研究", "继续", "延续", "接着", "展开",
        "summarize that report", "based on that report", "based on the report",
        "that report", "this report", "deep research report", "previous report",
        "continue from the report", "use the report", "refer to the report",
    )
    if any((cue in text) or (cue in lowered) for cue in direct_cues):
        return True
    for ref in reversed(report_refs):
        title = str(ref.get("report_title", "")).strip()
        if title and title.lower() in lowered:
            return True
    return False


def _select_report_ref(user_message: str, report_refs: list[dict[str, Any]]) -> dict[str, Any] | None:
    lowered = str(user_message or "").strip().lower()
    for ref in reversed(report_refs):
        title = str(ref.get("report_title", "")).strip()
        if title and title.lower() in lowered:
            return ref
    return report_refs[-1] if report_refs else None


def _expand_report_reference_history(history: list[dict[str, Any]], user_message: str) -> list[dict[str, Any]]:
    report_refs = _iter_report_refs(history)
    if not _looks_like_report_followup(user_message, report_refs):
        return history
    selected = _select_report_ref(user_message, report_refs)
    if not selected:
        return history
    archived = get_archived_round(
        str(selected.get("report_archive_session_id", "")).strip(),
        str(selected.get("report_round_id", "")).strip(),
    )
    if not archived:
        return history
    full_report = str(archived.get("assistant_body", "")).strip()
    if not full_report:
        return history
    report_title = str(selected.get("report_title", "")).strip() or "Deep Research Report"
    selected_message_id = str(selected.get("message_id", "")).strip()
    expanded_history: list[dict[str, Any]] = []
    replaced = False
    for message in history:
        if (
            isinstance(message, dict)
            and bool(message.get("report_ref"))
            and str(message.get("message_id", "")).strip() == selected_message_id
        ):
            replacement = dict(message)
            replacement["content"] = (
                f"{_REPORT_REF_PREFIX} {report_title}\n"
                "The user explicitly asked to use this archived report. "
                "The full report content is restored below for this turn only.\n\n"
                f"{full_report}"
            )
            replacement["report_expanded_for_turn"] = True
            expanded_history.append(replacement)
            replaced = True
            continue
        expanded_history.append(message)
    return expanded_history if replaced else history


def _schedule_memory_compression(messages: list[dict[str, Any]]) -> None:
    """Compress older conversation state without blocking the active request path."""
    task = asyncio.create_task(_compress_old_messages(list(messages)))
    _pending_compressors.add(task)
    task.add_done_callback(_pending_compressors.discard)


def _is_replaceable_live_message(entry: dict[str, Any], round_id: str) -> bool:
    """Return True for persisted messages that belong to the active live run.

    Queued guidance messages are intentionally excluded so they stay behind the
    current run transcript instead of being replaced by incremental saves.
    """
    if not round_id:
        return False
    if str(entry.get("round_id", "")).strip() != round_id:
        return False
    return not str(entry.get("queued_guidance_id", "")).strip()


async def _write_session_messages_locked(state: dict[str, Any], messages: list[dict[str, Any]]) -> None:
    _ensure_archive_session_id(state)
    messages = _compress_report_messages_for_storage(messages)
    messages = _ensure_message_identity(messages)
    messages = _dedupe_messages_by_id(messages)
    trimmed = _trim_session_messages(messages)
    state["messages"] = trimmed
    if not str(state.get("session_title", "")).strip():
        state.pop("session_title", None)
    _write_session_state(state)
    await debug.publish_event({
        "type": "session_update",
        "message_count": len(trimmed),
        "last_role": trimmed[-1].get("role") if trimmed else "",
        "round_id": next((str(m.get("round_id", "")).strip() for m in reversed(trimmed) if m.get("round_id")), ""),
    })

    if len(messages) >= _MAX_HISTORY_MESSAGES + 5:
        _schedule_memory_compression(messages)



async def _save_session_messages(messages: list[dict[str, Any]]) -> None:
    """保存 session 消息。如果超过上限，触发后台压缩。"""
    messages = _compress_report_messages_for_storage(messages)
    messages = _ensure_message_identity(list(messages))
    async with _session_state_lock:
        state = _load_session_state()
        effective_messages = messages
        base_messages = _persist_base_messages.get()
        if base_messages is None and _persist_merge_live_state.get():
            current = state.get("messages", [])
            base_messages = list(current) if isinstance(current, list) else []
            prefix_len = max(0, min(_persist_history_prefix_len.get(), len(messages)))
            insert_at = _persist_insert_at.get()
            if insert_at is None:
                insert_at = len(base_messages)
            insert_at = max(0, min(insert_at, len(base_messages)))
            suffix = messages[prefix_len:]
            round_id = str(_current_round_id.get() or "").strip()
            replace_end = insert_at
            while replace_end < len(base_messages) and _is_replaceable_live_message(base_messages[replace_end], round_id):
                replace_end += 1
            effective_messages = [
                *base_messages[:insert_at],
                *_merge_message_sequence(base_messages[insert_at:replace_end], suffix),
                *base_messages[replace_end:],
            ]
        elif base_messages is not None:
            current = state.get("messages", [])
            current_messages = list(current) if isinstance(current, list) else []
            prefix_len = max(0, min(_persist_history_prefix_len.get(), len(messages)))
            insert_at = _persist_insert_at.get()
            if insert_at is None:
                insert_at = len(base_messages)
            insert_at = max(0, min(insert_at, len(base_messages)))
            suffix = messages[prefix_len:]
            existing_tail = current_messages[insert_at:] if insert_at < len(current_messages) else []
            effective_messages = [
                *base_messages[:insert_at],
                *_merge_message_sequence(existing_tail or base_messages[insert_at:], suffix),
            ]
        await _write_session_messages_locked(state, effective_messages)


async def _append_session_message(entry: dict[str, Any]) -> None:
    async with _session_state_lock:
        state = _load_session_state()
        messages = state.get("messages", [])
        full_messages = list(messages) if isinstance(messages, list) else []
        full_messages.append(entry)
        _ensure_message_identity(full_messages)
        await _write_session_messages_locked(state, full_messages)


async def append_system_message(
    content: str,
    *,
    message_meta: dict[str, Any] | None = None,
    publish_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a user-visible assistant message outside an active chat round.

    This is used by scheduler-driven flows where there is no live user request
    to attach an intermediate reply to, but the Web UI still needs to display
    the message reliably from session state.
    """
    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "system_initiated": True,
    }
    if message_meta:
        assistant_entry.update(message_meta)

    _ensure_message_identity([assistant_entry])
    await _append_session_message(dict(assistant_entry))

    event = {"type": "assistant_message", "system_initiated": True}
    if publish_event:
        event.update(publish_event)
    await _publish_runtime_event(event)
    return assistant_entry


def _report_export_filename(round_id: str, fallback: str = "report") -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", str(round_id or fallback)).strip("-._") or fallback
    return f"{base}.pdf"


def _deep_research_pdf_attachment(round_id: str, user_message: str, final_text: str) -> dict[str, Any] | None:
    title = str(user_message or "").strip() or "Deep Research Report"
    export_name = _report_export_filename(round_id or "deep-research-report", fallback="deep-research-report")
    target = Path(DATA_DIR) / "generated_reports" / export_name
    try:
        from cyrene.report_export import write_report_pdf

        pdf_path = write_report_pdf(target, title=title, body=final_text)
        return build_public_attachment_payload(
            register_generated_attachment(str(pdf_path), display_name="deep-research-report.pdf")
        )
    except Exception:
        logger.exception("Failed to generate deep research PDF")
        return None


# ---------------------------------------------------------------------------
# Deep Research Phase 3 — multi-turn helper functions
# ---------------------------------------------------------------------------

_DEFAULT_TEMPLATE = """# {{title}}

> 研究问题：{{question}}

## 1. 执行摘要
## 2. 背景与上下文
## 3. 核心发现
## 4. 分析与启示
## 5. 局限性
## 6. 结论
## 7. 参考文献"""


def _load_research_template(template_path: str | None = None) -> str:
    """Load the research report template. Falls back to embedded default."""
    if template_path:
        p = Path(template_path)
        if p.exists():
            return p.read_text(encoding="utf-8")
    try:
        from importlib.resources import read_text as _read_text

        return _read_text("cyrene", "report_template.md")
    except Exception:
        return _DEFAULT_TEMPLATE


def _extract_new_references(text: str) -> tuple[str, list[str]]:
    """Split LLM section output into body text and new reference entries.

    Handles multiple heading variations:
    - ## New References
    - ### New References
    - ## References
    - 参考资料 / 参考文献 / Sources
    """
    # Try multiple patterns in order, case-insensitive
    patterns = [
        r"#{1,3}\s+New\s+References",
        r"#{1,3}\s+References",
        r"#{1,3}\s+参考资料",
        r"#{1,3}\s+参考文献",
        r"#{1,3}\s+Sources",
    ]
    best_pos = -1
    best_pat = ""
    for pat in patterns:
        matches = list(re.finditer(pat, text, re.MULTILINE | re.IGNORECASE))
        if matches:
            pos = matches[-1].start()  # use the LAST match
            if pos > best_pos:
                best_pos = pos
                best_pat = pat

    if best_pos < 0 or not best_pat:
        return text.rstrip(), []

    body = text[:best_pos].rstrip()
    ref_section = text[best_pos:]
    new_refs: list[str] = []
    for line in ref_section.splitlines():
        line = line.strip()
        if re.match(r"^\[\d+\]", line):
            new_refs.append(line)
    return body, new_refs


def _strip_stray_references(text: str) -> str:
    """Remove any stray reference headings and source blocks from body text.

    This ensures no leftover ``## New References`` or ``## Sources`` blocks
    appear in section bodies — all references go through the consolidated
    ``## 参考文献`` section at the end.

    Handles headings that may have a number prefix (e.g. ``## 7. 参考文献``).
    Uses ``re.search`` because the heading can appear mid-line.
    """
    heading_patterns = [
        r"#{1,3}\s+(?:\d+\.?\s*)?New\s+References",
        r"#{1,3}\s+(?:\d+\.?\s*)?References",
        r"#{1,3}\s+(?:\d+\.?\s*)?参考资料",
        r"#{1,3}\s+(?:\d+\.?\s*)?参考文献",
        r"#{1,3}\s+(?:\d+\.?\s*)?Sources",
    ]
    lines = text.splitlines()
    cleaned: list[str] = []
    in_ref_block = False
    for line in lines:
        stripped = line.strip()
        # Check if this line is a reference heading
        matched_heading = any(re.search(p, stripped, re.IGNORECASE) for p in heading_patterns)
        if matched_heading:
            in_ref_block = True
            continue
        if in_ref_block:
            # Skip [N] lines and blank lines inside the ref block
            if re.match(r"^\[\d+\]", stripped):
                continue
            if not stripped:
                continue
            # Non-[N] content ends the ref block
            in_ref_block = False
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _deduplicate_references(entries: list[str]) -> list[str]:
    """Deduplicate reference entries by URL and renumber sequentially.

    When two entries share the same URL the *last* one wins (more likely
    to have a complete description).  Entries without a detectable URL
    are deduplicated by their first 120 characters.
    """
    seen: dict[str, str] = {}
    for entry in entries:
        m = re.search(r"(https?://\S+)", entry)
        key = m.group(1).rstrip(".)") if m else entry[:120]
        seen[key] = entry
    result: list[str] = []
    for i, entry in enumerate(seen.values(), 1):
        result.append(re.sub(r"^\[\d+\]", f"[{i}]", entry))
    return result


def _assemble_report(sections: list[str], references: list[str], outline: dict) -> str:
    """Join section bodies, outline title, and deduplicated references.

    All references are consolidated into a single ``## 参考文献`` section
    at the end. Section bodies are pre-stripped of stray reference blocks.
    """
    title = str(outline.get("title") or "Deep Research Report").strip()
    parts: list[str] = []
    # Strip any stray ref blocks from each section
    for sec in sections:
        clean = _strip_stray_references(sec)
        if clean:
            parts.append(clean)
    if references:
        parts.append("## 参考文献\n" + "\n".join(references))
    report = "\n\n".join(parts)
    return f"# {title}\n\n{report}"


def _parse_length_preference(messages: list[dict]) -> str:
    """Scan conversation messages for the user's length preference.

    Returns one of: "short", "medium", "long", or "medium" as default.
    """
    for msg in reversed(messages):
        content = str(msg.get("content", "") or "")
        content_lower = content.lower() if isinstance(content, str) else ""
        # Check for explicit indicators
        # Long: 30+, 长
        if "30" in content or "30+" in content:
            if any(kw in content_lower for kw in ["页", "篇", "长"]):
                return "long"
        if len(content) > 5 and ("长" in content and ("30" in content or "30+" in content)):
            return "long"
        # Short: 10, 短, 精简
        if "10" in content and any(kw in content_lower for kw in ["页", "篇", "短"]):
            return "short"
        if len(content) > 3 and "短" in content:
            return "short"
        # Medium: 20, 中
        if "20" in content and any(kw in content_lower for kw in ["页", "篇", "中"]):
            return "medium"
        if len(content) > 3 and "中" in content:
            return "medium"
    return "medium"


async def _generate_deep_research_outline(
    source_material: str,
    template: str,
    question: str,
    lang: str,
    length_pref: str = "medium",
) -> dict:
    """Step 2: LLM generates a report outline as JSON."""
    sys_msg = (
        _OUTLINE_GENERATION_PROMPT.replace("{template}", template)
        .replace("{source_material}", source_material)
        .replace("{length_pref}", length_pref)
    )
    # Set outline guidance based on length preference
    if length_pref == "short":
        unit_range = "3~5 units, concise"
    elif length_pref == "medium":
        unit_range = "5~8 units, moderate detail"
    else:
        unit_range = "8~15+ units, thorough deep-dive"
    sys_msg = sys_msg.replace("{unit_range}", unit_range)

    user_msg = f"Research question: {question}\n\nPreferred language: {lang}\n\nLength preference: {length_pref}"
    messages = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = await _call_llm(messages, tools=None, max_tokens=None)
        raw = _assistant_text(resp) or ""
        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        outline = json.loads(raw)
        if not isinstance(outline, dict) or "units" not in outline:
            outline = {"title": question, "units": []}
    except Exception:
        logger.exception("Failed to generate deep research outline")
        outline = {"title": question, "units": []}
    return outline


async def _write_section(
    source_material: str,
    outline: dict,
    report_so_far: str,
    references_so_far: str,
    unit_def: dict,
    unit_no: int,
    total_units: int,
    lang: str,
    length_pref: str = "medium",
) -> str:
    """Step 3: Write one section unit and return the full LLM output.

    The caller is responsible for splitting the output into body and
    new references via ``_extract_new_references``.
    """
    if length_pref == "short":
        min_words = 200
    elif length_pref == "long":
        min_words = 800
    else:
        min_words = 500

    prompt = (
        _SECTION_WRITE_PROMPT.replace("{unit_no}", str(unit_no))
        .replace("{total_units}", str(total_units))
        .replace("{outline_json}", json.dumps(outline, ensure_ascii=False))
        .replace("{source_material}", source_material)
        .replace("{report_so_far}", report_so_far)
        .replace("{references_so_far}", references_so_far)
        .replace("{unit_heading}", unit_def.get("heading", ""))
        .replace("{brief}", unit_def.get("brief", "") or unit_def.get("prompt", ""))
        .replace("{lang}", lang)
        .replace("{min_words}", str(min_words))
    )

    user_msg = (
        f"Write unit {unit_no}/{total_units}: {unit_def.get('heading', '')}\n\n"
        f"{unit_def.get('brief', '') or unit_def.get('prompt', '')}"
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = await _call_llm(messages, tools=None, max_tokens=None)
        return _assistant_text(resp) or ""
    except Exception:
        logger.exception("Failed to write section %s", unit_def.get("heading", ""))
        return f"[该章节未生成: {unit_def.get('heading', '')}]"


async def _expansion_pass(
    source_material: str,
    outline: dict,
    sections_written: list[str],
    references: list[str],
    lang: str,
) -> list[str]:
    """Step 4 (optional): Expand thin sections when the report is too short.

    Only triggered when total report length is below threshold.
    """
    combined = "\n\n".join(sections_written)
    prompt = (
        _EXPANSION_PROMPT.replace("{final_report}", combined)
        .replace("{source_material}", source_material)
        .replace("{lang}", lang)
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Please expand thin sections as described above."},
    ]
    try:
        resp = await _call_llm(messages, tools=None, max_tokens=None)
        expansion_text = _assistant_text(resp) or ""
        if not expansion_text.strip():
            return sections_written  # no expansions needed
        # Expansion output should contain section headers matching originals.
        # Replace matching sections in place.
        result = list(sections_written)
        for i, section in enumerate(result):
            # Extract first heading line
            heading_match = re.match(r"(#{1,3}\s+[\d.]*\s*\S[^\n]*)", section)
            if not heading_match:
                continue
            heading = heading_match.group(1)
            # Find corresponding expansion
            exp_pattern = re.escape(heading)
            exp_match = re.search(exp_pattern, expansion_text)
            if exp_match:
                # Extract the expanded block (from heading to next heading or end)
                rest = expansion_text[exp_match.start():]
                next_heading = re.search(r"\n(#{1,3}\s+[\d.]*\s*\S)", rest[1:])
                expanded = rest[:next_heading.start() + 1] if next_heading else rest
                result[i] = expanded.strip()
        return result
    except Exception:
        logger.exception("Expansion pass failed")
        return sections_written


def _flush_intermediate_user_replies(messages: list[dict[str, Any]]) -> None:
    pending = _pending_intermediate_user_replies.get()
    if not pending:
        return
    existing_ids = {
        str(message.get("message_id", "")).strip()
        for message in messages
        if isinstance(message, dict)
    }
    for entry in pending:
        message_id = str(entry.get("message_id", "")).strip()
        if message_id and message_id in existing_ids:
            continue
        messages.append(dict(entry))
        if message_id:
            existing_ids.add(message_id)
    pending.clear()


async def _insert_intermediate_user_reply(
    content: str,
    round_id: str,
    client_request_id: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "round_id": round_id,
        "intermediate_reply": True,
    }
    if attachments:
        assistant_entry["attachments"] = [dict(item) for item in attachments if isinstance(item, dict)]
    if client_request_id:
        assistant_entry["client_request_id"] = client_request_id

    labels = get_session_labels(round_id)
    if labels.get("round_title"):
        assistant_entry["round_title"] = labels["round_title"]

    _ensure_message_identity([assistant_entry])

    pending = _pending_intermediate_user_replies.get()
    if pending is not None:
        pending.append(dict(assistant_entry))

    async with _session_state_lock:
        state = _load_session_state()
        existing = state.get("messages", [])
        full_messages = list(existing) if isinstance(existing, list) else []
        full_messages.append(dict(assistant_entry))
        _ensure_message_identity(full_messages)
        await _write_session_messages_locked(state, full_messages)

    await _publish_runtime_event({
        "type": "assistant_message",
        "round_id": round_id,
        "client_request_id": client_request_id,
        "intermediate": True,
        "message_id": assistant_entry.get("message_id", ""),
    })
    return assistant_entry


def _normalize_pending_question(payload: dict[str, Any]) -> dict[str, Any]:
    question_id = str(payload.get("id", "")).strip() or f"question_{uuid4().hex[:12]}"
    text = str(payload.get("text", "") or "").strip()
    round_id = str(payload.get("round_id", "") or "").strip()
    client_request_id = str(payload.get("client_request_id", "") or "").strip()
    asked_at = str(payload.get("asked_at", "") or "").strip() or datetime.now(timezone.utc).isoformat()
    allow_custom = bool(payload.get("allow_custom", True))
    options: list[dict[str, str]] = []
    raw_options = payload.get("options", [])
    if isinstance(raw_options, list):
        for index, item in enumerate(raw_options, start=1):
            if isinstance(item, dict):
                label = str(item.get("label", "") or "").strip()
                option_id = str(item.get("id", "") or "").strip() or f"option_{index}"
            else:
                label = str(item or "").strip()
                option_id = f"option_{index}"
            if not label:
                continue
            options.append({"id": option_id, "label": label})
    question: dict[str, Any] = {
        "id": question_id,
        "text": text,
        "round_id": round_id,
        "client_request_id": client_request_id,
        "options": options[:6],
        "allow_custom": allow_custom,
        "asked_at": asked_at,
    }
    round_title = str(payload.get("round_title", "") or "").strip()
    if round_title:
        question["round_title"] = round_title
    meta = payload.get("meta")
    if isinstance(meta, dict) and meta:
        question["meta"] = dict(meta)
    return question


async def _upsert_pending_question(payload: dict[str, Any]) -> dict[str, Any]:
    question = _normalize_pending_question(payload)
    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": question["text"],
        "round_id": question["round_id"],
        "question_prompt": True,
        "question_id": question["id"],
    }
    if question.get("client_request_id"):
        assistant_entry["client_request_id"] = question["client_request_id"]
    if question.get("round_title"):
        assistant_entry["round_title"] = question["round_title"]
    if question["options"]:
        assistant_entry["question_options"] = list(question["options"])

    _ensure_message_identity([assistant_entry])
    question["message_id"] = assistant_entry["message_id"]

    async with _session_state_lock:
        state = _load_session_state()
        existing = state.get("messages", [])
        full_messages = list(existing) if isinstance(existing, list) else []
        replacement_index = next(
            (
                idx
                for idx, msg in enumerate(full_messages)
                if str(msg.get("question_id", "")).strip() == question["id"]
            ),
            -1,
        )
        if replacement_index >= 0:
            assistant_entry["message_id"] = str(full_messages[replacement_index].get("message_id", "")).strip() or assistant_entry["message_id"]
            question["message_id"] = assistant_entry["message_id"]
            full_messages[replacement_index] = assistant_entry
        else:
            full_messages.append(assistant_entry)
        state["pending_question"] = question
        await _write_session_messages_locked(state, full_messages)

    await _publish_runtime_event({
        "type": "user_question",
        "question_id": question["id"],
        "client_request_id": question.get("client_request_id", ""),
        "round_id": question.get("round_id", ""),
    })
    return question


async def _restore_pending_question(question: dict[str, Any]) -> None:
    normalized = _normalize_pending_question(question)
    async with _session_state_lock:
        state = _load_session_state()
        state["pending_question"] = normalized
        _write_session_state(state)
    await _publish_runtime_event({
        "type": "user_question",
        "question_id": normalized["id"],
        "client_request_id": normalized.get("client_request_id", ""),
        "round_id": normalized.get("round_id", ""),
    })


async def _clear_pending_question(question_id: str) -> dict[str, Any]:
    target_question_id = str(question_id or "").strip()
    async with _session_state_lock:
        state = _load_session_state()
        pending = state.get("pending_question", {})
        pending_dict = dict(pending) if isinstance(pending, dict) else {}
        if not pending_dict:
            return {}
        if target_question_id and str(pending_dict.get("id", "")).strip() != target_question_id:
            return {}
        state.pop("pending_question", None)
        _write_session_state(state)

    await _publish_runtime_event({
        "type": "user_question_answered",
        "question_id": str(pending_dict.get("id", "")).strip(),
        "client_request_id": str(pending_dict.get("client_request_id", "")).strip(),
        "round_id": str(pending_dict.get("round_id", "")).strip(),
    })
    return pending_dict


async def _publish_runtime_event(event: dict[str, Any]) -> None:
    """Publish a UI/runtime event annotated with the current round when present."""
    round_id = _current_round_id.get()
    if round_id and not str(event.get("round_id", "")).strip():
        event = {**event, "round_id": round_id}
    await debug.publish_event(event)


async def _emit_reply_stream_event(event: dict[str, Any]) -> None:
    writer = _reply_stream_writer.get()
    if writer is None:
        return
    await writer(dict(event))


def _streaming_reply_requested() -> bool:
    return _reply_stream_writer.get() is not None


def _approx_token_count(text: str) -> int:
    source = str(text or "")
    if not source.strip():
        return 0
    units = re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\s]", source)
    total = 0
    for unit in units:
        if re.fullmatch(r"[A-Za-z0-9_]+", unit):
            total += max(1, (len(unit) + 3) // 4)
        else:
            total += 1
    return total


def _message_token_estimate(message: dict[str, Any]) -> int:
    total = 4
    total += _approx_token_count(_assistant_text(message) or message.get("content") or "")
    total += _approx_token_count(message.get("reasoning_content") or "")
    for tool_call in message.get("tool_calls") or []:
        fn = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
        total += _approx_token_count(fn.get("name") or "")
        total += _approx_token_count(fn.get("arguments") or "")
    total += _approx_token_count(message.get("tool_call_id") or "")
    return total


def _normalized_usage(usage: Any, messages: list[dict[str, Any]], response_message: dict[str, Any]) -> dict[str, int]:
    if isinstance(usage, dict) and any(isinstance(usage.get(key), int) for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt + completion))
        normalized = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        }
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            if isinstance(usage.get(key), int):
                normalized[key] = int(usage.get(key))
        return normalized
    prompt = sum(_message_token_estimate(message) for message in messages) + 8
    completion = _message_token_estimate(response_message) + 8
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _message_from_upstream_payload(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        message = first.get("message")
        if isinstance(message, dict):
            return message
    if isinstance(data.get("message"), dict):
        return dict(data["message"])
    output = data.get("output")
    if isinstance(output, dict):
        if isinstance(output.get("message"), dict):
            return dict(output["message"])
        if isinstance(output.get("text"), str):
            return {"role": "assistant", "content": output["text"]}
    if isinstance(data.get("response"), dict):
        return dict(data["response"])
    error_text = (
        data.get("error")
        or data.get("message")
        or data.get("detail")
        or data.get("msg")
        or json.dumps(data, ensure_ascii=False)[:400]
    )
    raise ValueError(f"Upstream response missing choices/message payload: {error_text}")


def _assistant_entry_from_response(response: dict[str, Any], round_id: str, include_tool_calls: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = {"role": "assistant", "content": response.get("content") or ""}
    if response.get("reasoning_content"):
        entry["reasoning_content"] = response["reasoning_content"]
    if include_tool_calls and response.get("tool_calls"):
        entry["tool_calls"] = response["tool_calls"]
    if response.get("usage"):
        entry["usage"] = response["usage"]
    if round_id:
        entry["round_id"] = round_id
    extra_meta = _ui_round_assistant_meta.get()
    if extra_meta:
        entry.update(extra_meta)
    return entry


def _apply_assistant_meta(entry: dict[str, Any]) -> dict[str, Any]:
    extra_meta = _ui_round_assistant_meta.get()
    if extra_meta:
        entry.update(extra_meta)
    return entry


def _fallback_label(text: str, limit: int = 48) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip().strip("[](){}<>\"'`，。！？；：,.;!?")
    return compact[:limit] or "Untitled"


def _extract_json_object(text: str) -> dict[str, Any]:
    source = str(text or "").strip()
    if not source:
        return {}
    try:
        data = json.loads(source)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", source, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _tool_result_requests_user_input(result: str) -> bool:
    payload = _extract_json_object(result)
    return str(payload.get("status", "")).strip() == "awaiting_user"


def _ensure_message_identity(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for message in messages:
        if not isinstance(message, dict):
            continue
        if not str(message.get("message_id", "")).strip():
            message["message_id"] = f"msg_{uuid4().hex}"
    return messages


def _dedupe_messages_by_id(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep message order while preferring the latest version for each message_id.

    Session state should never contain the same logical message twice. When the
    same message_id appears multiple times, later entries may carry richer
    metadata or updated content, so we keep the last version while preserving
    the position of the first occurrence.
    """
    deduped: list[dict[str, Any]] = []
    seen_index: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        message_id = str(message.get("message_id", "")).strip()
        if message_id and message_id in seen_index:
            deduped[seen_index[message_id]] = message
            continue
        if message_id:
            seen_index[message_id] = len(deduped)
        deduped.append(message)
    return deduped


def _merge_message_sequence(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge two persisted message sequences without regressing newer entries.

    ``incoming`` is usually the latest local snapshot for the active round, but
    callers may occasionally save an older partial snapshot after a newer one
    has already been persisted. When that happens we should keep the existing
    later messages instead of deleting them.
    """
    incoming_by_id = {
        str(message.get("message_id", "")).strip(): message
        for message in incoming
        if isinstance(message, dict) and str(message.get("message_id", "")).strip()
    }

    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for message in existing:
        if not isinstance(message, dict):
            continue
        message_id = str(message.get("message_id", "")).strip()
        if message_id and message_id in incoming_by_id:
            merged.append(incoming_by_id[message_id])
            seen_ids.add(message_id)
            continue
        merged.append(message)
        if message_id:
            seen_ids.add(message_id)

    for message in incoming:
        if not isinstance(message, dict):
            continue
        message_id = str(message.get("message_id", "")).strip()
        if message_id and message_id in seen_ids:
            continue
        merged.append(message)
        if message_id:
            seen_ids.add(message_id)

    return _dedupe_messages_by_id(merged)


def _round_epoch_ms(round_id: str) -> int | None:
    match = re.fullmatch(r"round_(\d+)", str(round_id or "").strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _round_started_iso(round_id: str) -> str | None:
    epoch_ms = _round_epoch_ms(round_id)
    if epoch_ms is None:
        return None
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _round_title_from_entry(entry: dict[str, Any]) -> str:
    return (
        str(entry.get("title", "")).strip()
        or _fallback_label(entry.get("last_user") or entry.get("prompt") or entry.get("id"), limit=40)
    )


def _session_round_entries() -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    messages = _load_session_messages()
    for msg in messages:
        round_id = str(msg.get("round_id", "")).strip()
        if not round_id:
            continue
        entry = entries.setdefault(round_id, {
            "id": round_id,
            "title": "",
            "prompt": "",
            "last_user": "",
            "last_assistant": "",
            "status": "done",
            "pending_guidance": 0,
            "subagent_count": 0,
            "running_subagents": 0,
            "started_at": _round_started_iso(round_id),
            "updated_at": _round_started_iso(round_id),
        })
        if msg.get("round_title"):
            entry["title"] = str(msg.get("round_title") or "").strip()
        role = str(msg.get("role") or "")
        content = str(msg.get("content") or "").strip()
        if role == "user" and content:
            if not entry["prompt"]:
                entry["prompt"] = content
            entry["last_user"] = content
        elif role == "assistant" and content:
            entry["last_assistant"] = content
            if not entry["title"] and bool(msg.get("system_initiated")):
                entry["title"] = "proactive check-in"
    return entries


def _main_inbox_pending_by_round() -> dict[str, int]:
    from cyrene.inbox import get_unread_messages

    counts: dict[str, int] = {}
    for message in get_unread_messages(_MAIN_INBOX_AGENT_ID):
        if str(message.get("type", "")).strip() != "guidance":
            continue
        round_id = str(message.get("round_id", "")).strip()
        if not round_id:
            continue
        counts[round_id] = counts.get(round_id, 0) + 1
    return counts


def _pending_question_live_entry() -> dict[str, Any]:
    pending = _load_pending_question()
    round_id = str(pending.get("round_id", "")).strip()
    if not round_id:
        return {}
    return {
        "id": round_id,
        "title": str(pending.get("round_title", "")).strip(),
        "prompt": str(pending.get("text", "")).strip(),
        "last_user": "",
        "last_assistant": str(pending.get("text", "")).strip(),
        "status": "queued",
        "pending_guidance": 0,
        "subagent_count": 0,
        "running_subagents": 0,
        "started_at": _round_started_iso(round_id),
        "updated_at": str(pending.get("asked_at", "")).strip() or datetime.now(timezone.utc).isoformat(),
    }


def get_live_rounds() -> list[dict[str, Any]]:
    """Return live round summaries for UI context selection and tooling."""
    entries = _session_round_entries()

    from cyrene.subagent import _registry  # noqa: WPS437

    for info in _registry.values():
        round_id = str(info.get("round_id", "")).strip()
        if not round_id:
            continue
        entry = entries.setdefault(round_id, {
            "id": round_id,
            "title": "",
            "prompt": str(info.get("task") or "").strip(),
            "last_user": "",
            "last_assistant": "",
            "status": "done",
            "pending_guidance": 0,
            "subagent_count": 0,
            "running_subagents": 0,
            "started_at": _round_started_iso(round_id) or info.get("created_at"),
            "updated_at": info.get("updated_at") or _round_started_iso(round_id),
        })
        entry["subagent_count"] += 1
        sub_status = str(info.get("status") or "done")
        if sub_status in ("running", "waiting", "resumed"):
            entry["running_subagents"] += 1
            entry["status"] = "running"
        if not entry.get("prompt"):
            entry["prompt"] = str(info.get("task") or "").strip()
        if info.get("updated_at"):
            entry["updated_at"] = info.get("updated_at")
        if info.get("created_at") and not entry.get("started_at"):
            entry["started_at"] = info.get("created_at")

    for round_id, pending_count in _main_inbox_pending_by_round().items():
        entry = entries.setdefault(round_id, {
            "id": round_id,
            "title": "",
            "prompt": "",
            "last_user": "",
            "last_assistant": "",
            "status": "queued",
            "pending_guidance": 0,
            "subagent_count": 0,
            "running_subagents": 0,
            "started_at": _round_started_iso(round_id),
            "updated_at": _round_started_iso(round_id),
        })
        entry["pending_guidance"] = pending_count
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        if entry["status"] != "running":
            entry["status"] = "queued"

    pending_question_entry = _pending_question_live_entry()
    if pending_question_entry:
        round_id = pending_question_entry["id"]
        entry = entries.setdefault(round_id, pending_question_entry)
        if not entry.get("title"):
            entry["title"] = pending_question_entry["title"]
        if not entry.get("prompt"):
            entry["prompt"] = pending_question_entry["prompt"]
        if not entry.get("last_assistant"):
            entry["last_assistant"] = pending_question_entry["last_assistant"]
        if entry.get("status") != "running":
            entry["status"] = "queued"
        entry["updated_at"] = pending_question_entry["updated_at"]

    if _active_main_round_id:
        entry = entries.setdefault(_active_main_round_id, {
            "id": _active_main_round_id,
            "title": "",
            "prompt": _active_main_round_public_prompt,
            "last_user": _active_main_round_public_prompt,
            "last_assistant": "",
            "status": "running",
            "pending_guidance": 0,
            "subagent_count": 0,
            "running_subagents": 0,
            "started_at": datetime.fromtimestamp(_active_main_round_started_at, tz=timezone.utc).isoformat() if _active_main_round_started_at else _round_started_iso(_active_main_round_id),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        entry["status"] = "running"
        if _active_main_round_public_prompt and not entry.get("prompt"):
            entry["prompt"] = _active_main_round_public_prompt
        if _active_main_round_started_at and not entry.get("started_at"):
            entry["started_at"] = datetime.fromtimestamp(_active_main_round_started_at, tz=timezone.utc).isoformat()
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()

    live_entries: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for entry in entries.values():
        if entry.get("status") not in ("running", "queued") and not entry.get("pending_guidance", 0):
            continue
        started_at = entry.get("started_at")
        updated_at = entry.get("updated_at")
        elapsed = "—"
        if started_at:
            try:
                started_dt = datetime.fromisoformat(str(started_at)).astimezone(timezone.utc)
                elapsed = _format_duration((now - started_dt).total_seconds())
            except Exception:
                elapsed = "—"
        live_entries.append({
            "id": entry["id"],
            "title": _round_title_from_entry(entry),
            "prompt": entry.get("prompt", ""),
            "lastUser": entry.get("last_user", ""),
            "lastAssistant": entry.get("last_assistant", ""),
            "status": entry.get("status", "queued"),
            "pendingGuidance": int(entry.get("pending_guidance", 0) or 0),
            "subagentCount": int(entry.get("subagent_count", 0) or 0),
            "runningSubagents": int(entry.get("running_subagents", 0) or 0),
            "startedAt": started_at or "",
            "updatedAt": updated_at or "",
            "elapsed": elapsed,
        })

    live_entries.sort(key=lambda item: item.get("startedAt") or "", reverse=True)
    return live_entries


def query_live_rounds(round_id: str = "") -> str:
    """Summarize currently live rounds for the main agent."""
    rounds = get_live_rounds()
    if round_id:
        rounds = [item for item in rounds if item.get("id") == round_id]
    if not rounds:
        if round_id:
            return f"No live round found for {round_id}."
        return "No live rounds are currently running."

    lines = []
    for item in rounds:
        lines.append(
            f"- {item['id']} | {item['status']} | {item['title']} | elapsed {item['elapsed']} | "
            f"subagents {item['runningSubagents']}/{item['subagentCount']} | pending guidance {item['pendingGuidance']}"
        )
        prompt = item.get("prompt") or item.get("lastUser") or ""
        if prompt:
            lines.append(f"  prompt: {_fallback_label(prompt, limit=120)}")
        last_answer = item.get("lastAssistant") or ""
        if last_answer:
            lines.append(f"  latest reply: {_fallback_label(last_answer, limit=160)}")
    return "\n".join(lines)


async def _publish_round_guidance_update(target_round_id: str) -> None:
    live = next((item for item in get_live_rounds() if item.get("id") == target_round_id), None)
    await debug.publish_event({
        "type": "round_guidance_update",
        "target_round_id": target_round_id,
        "pending_guidance": int(live.get("pendingGuidance", 0) if live else 0),
        "status": live.get("status", "") if live else "",
        "title": live.get("title", "") if live else "",
    })


def _guidance_error_text(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        reason = "the upstream model timed out"
    elif isinstance(exc, httpx.HTTPError):
        reason = "the upstream model request failed"
    else:
        reason = "an internal error occurred while applying the guidance"
    return f"Guidance could not be applied because {reason}."


def format_httpx_error(exc: Exception) -> str:
    parts: list[str] = [type(exc).__name__]
    detail = str(exc or "").strip()
    if detail:
        parts.append(detail)

    request = getattr(exc, "request", None)
    if request is not None:
        method = str(getattr(request, "method", "") or "").strip()
        url = str(getattr(request, "url", "") or "").strip()
        request_part = "request="
        if method:
            request_part += method
        if url:
            request_part += f" {url}" if method else url
        parts.append(request_part)

    response = getattr(exc, "response", None)
    if response is not None:
        parts.append(f"status={response.status_code}")
        try:
            body = str(response.text or "").strip()
        except Exception:
            body = ""
        if body:
            body_preview = re.sub(r"\s+", " ", body)[:500]
            parts.append(f"body={body_preview}")

    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        cause_text = str(cause or "").strip()
        if cause_text:
            parts.append(f"cause={type(cause).__name__}: {cause_text}")
        else:
            parts.append(f"cause={type(cause).__name__}")

    return " | ".join(parts)


def _guidance_ack_text() -> str:
    return "已接受引导。我会按这条新要求调整当前这一轮的工作，并在完成后给你更新。"


def _schedule_session_label_refresh(current_user_message: str, round_id: str) -> None:
    async def _runner() -> None:
        try:
            await _refresh_session_labels(current_user_message, round_id)
        except Exception:
            logger.warning("Async session naming failed for %s", round_id or "<unknown>", exc_info=True)

    task = asyncio.create_task(_runner())
    _pending_label_refreshes.add(task)
    task.add_done_callback(_pending_label_refreshes.discard)


def _guidance_round_context(target_round_id: str, guidance_id: str) -> dict[str, Any]:
    full_messages = _load_session_messages()
    queued_entry = next(
        (
            msg
            for msg in full_messages
            if str(msg.get("queued_guidance_id", "")).strip() == guidance_id
        ),
        {},
    )
    insert_at = next(
        (
            idx
            for idx, msg in enumerate(full_messages)
            if str(msg.get("queued_guidance_id", "")).strip() == guidance_id
        ),
        len(full_messages),
    )
    return {
        "full_messages": full_messages,
        "queued_entry": queued_entry,
        "insert_at": insert_at,
        "persist_base_messages": [
            msg
            for msg in full_messages
            if str(msg.get("queued_guidance_id", "")).strip() != guidance_id
        ],
        "round_history": [
            msg
            for msg in full_messages
            if str(msg.get("round_id", "")).strip() == target_round_id
            and not str(msg.get("queued_guidance_id", "")).strip()
        ],
        "round_title": str(queued_entry.get("round_title", "")).strip(),
        "client_request_id": str(queued_entry.get("client_request_id", "")).strip(),
    }


def _guidance_persist_context_after_ack(guidance_id: str) -> dict[str, Any]:
    full_messages = _load_session_messages()
    ack_index = next(
        (
            idx
            for idx, msg in enumerate(full_messages)
            if str(msg.get("guidance_ack_for_guidance_id", "")).strip() == guidance_id
        ),
        -1,
    )
    queued_index = next(
        (
            idx
            for idx, msg in enumerate(full_messages)
            if str(msg.get("queued_guidance_id", "")).strip() == guidance_id
        ),
        len(full_messages) - 1,
    )
    insert_at = ack_index + 1 if ack_index >= 0 else queued_index + 1
    insert_at = max(0, min(insert_at, len(full_messages)))
    return {
        "persist_base_messages": full_messages,
        "persist_insert_at": insert_at,
    }


def _pending_question_resume_context(question_id: str) -> dict[str, Any]:
    full_messages = _load_session_messages()
    pending = _load_pending_question()
    target_question_id = str(question_id or "").strip()
    if not pending:
        return {}
    if target_question_id and str(pending.get("id", "")).strip() != target_question_id:
        return {}

    target_round_id = str(pending.get("round_id", "")).strip()
    insert_at = next(
        (
            idx + 1
            for idx, msg in enumerate(full_messages)
            if str(msg.get("question_id", "")).strip() == str(pending.get("id", "")).strip()
        ),
        len(full_messages),
    )
    return {
        "pending_question": pending,
        "full_messages": full_messages,
        "persist_base_messages": full_messages,
        "persist_insert_at": insert_at,
        "round_history": [
            msg
            for msg in full_messages
            if str(msg.get("round_id", "")).strip() == target_round_id
        ],
        "round_id": target_round_id,
        "round_title": str(pending.get("round_title", "")).strip(),
        "client_request_id": str(pending.get("client_request_id", "")).strip(),
        "command": str((pending.get("meta") or {}).get("command", "") or "").strip(),
    }


async def _insert_guidance_reply(
    target_round_id: str,
    guidance_id: str,
    content: str,
    round_title: str = "",
    client_request_id: str = "",
    subagent_flow_snapshot: dict[str, Any] | None = None,
) -> None:
    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "round_id": target_round_id,
        "in_reply_to_guidance_id": guidance_id,
    }
    if round_title:
        assistant_entry["round_title"] = round_title
    if client_request_id:
        assistant_entry["client_request_id"] = client_request_id
    if subagent_flow_snapshot:
        assistant_entry["subagent_flow_snapshot"] = subagent_flow_snapshot

    async with _session_state_lock:
        state = _load_session_state()
        existing = state.get("messages", [])
        full_messages = list(existing) if isinstance(existing, list) else []
        _ensure_message_identity([assistant_entry])
        replacement_index = next(
            (
                idx
                for idx, msg in enumerate(full_messages)
                if str(msg.get("in_reply_to_guidance_id", "")).strip() == guidance_id
            ),
            -1,
        )
        if replacement_index >= 0:
            full_messages[replacement_index] = assistant_entry
        else:
            ack_index = next(
                (
                    idx
                    for idx, msg in enumerate(full_messages)
                    if str(msg.get("guidance_ack_for_guidance_id", "")).strip() == guidance_id
                ),
                -1,
            )
            insert_at = ack_index if ack_index >= 0 else next(
                (
                    idx
                    for idx, msg in enumerate(full_messages)
                    if str(msg.get("queued_guidance_id", "")).strip() == guidance_id
                ),
                len(full_messages) - 1,
            )
            full_messages.insert(max(0, insert_at + 1), assistant_entry)
        await _write_session_messages_locked(state, full_messages)
    await _publish_runtime_event({
        "type": "chat_message",
        "round_id": target_round_id,
        "client_request_id": client_request_id,
        "guidance_id": guidance_id,
    })


async def _insert_guidance_ack(
    target_round_id: str,
    guidance_id: str,
    round_title: str = "",
    client_request_id: str = "",
) -> None:
    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": _guidance_ack_text(),
        "round_id": target_round_id,
        "guidance_ack_for_guidance_id": guidance_id,
    }
    if round_title:
        assistant_entry["round_title"] = round_title
    async with _session_state_lock:
        state = _load_session_state()
        existing = state.get("messages", [])
        full_messages = list(existing) if isinstance(existing, list) else []
        _ensure_message_identity([assistant_entry])
        replacement_index = next(
            (
                idx
                for idx, msg in enumerate(full_messages)
                if str(msg.get("guidance_ack_for_guidance_id", "")).strip() == guidance_id
            ),
            -1,
        )
        if replacement_index >= 0:
            full_messages[replacement_index] = assistant_entry
        else:
            insert_at = next(
                (
                    idx
                    for idx, msg in enumerate(full_messages)
                    if str(msg.get("queued_guidance_id", "")).strip() == guidance_id
                ),
                len(full_messages) - 1,
            )
            full_messages.insert(max(0, insert_at + 1), assistant_entry)
        await _write_session_messages_locked(state, full_messages)
    await _publish_runtime_event({
        "type": "guidance_acknowledged",
        "round_id": target_round_id,
        "client_request_id": client_request_id,
        "guidance_id": guidance_id,
        "ack_text": assistant_entry["content"],
    })


async def _fan_out_guidance_to_subagents(target_round_id: str, content: str, bot: Any, chat_id: int, db_path: str) -> list[str]:
    from cyrene.inbox import send_message as _send_inbox
    from cyrene.subagent import (
        _run_subagent,
        _spawn_subagent_task,
        get_raw_messages as _sub_raw_msgs,
        get_snapshot as _sub_snapshot,
        reactivate as _sub_reactivate,
    )

    guidance_text = (
        "Main agent received new user guidance for this round.\n"
        "Adjust your work accordingly and revise your result if needed.\n\n"
        f"User guidance:\n{content}"
    )
    snapshot = await _sub_snapshot(round_id=target_round_id)
    if not snapshot:
        return []

    sent: list[str] = []
    for agent_id in snapshot:
        await _send_inbox(_MAIN_INBOX_AGENT_ID, agent_id, "guidance", guidance_text, round_id=target_round_id)
        sent.append(agent_id)

    for agent_id, info in snapshot.items():
        if info.get("status") not in ("done", "timeout"):
            continue
        if await _sub_reactivate(agent_id):
            raw_messages = await _sub_raw_msgs(agent_id)
            _spawn_subagent_task(
                _run_subagent(agent_id, str(info.get("task") or ""), bot, chat_id, db_path, resume_messages=raw_messages),
                agent_id,
            )
    return sent


async def _wait_for_subagent_round(round_id: str, bot: Any, chat_id: int, db_path: str) -> tuple[bool, str]:
    from cyrene.inbox import get_unread_count as _inbox_unread
    from cyrene.subagent import (
        _run_subagent,
        _spawn_subagent_task,
        collect_results as _sub_collect,
        get_raw_messages as _sub_raw_msgs,
        get_snapshot as _sub_snapshot,
        reactivate as _sub_reactivate,
    )

    _interrupt_event.clear()
    interrupted = False
    quiet_ticks = 0
    for _ in range(120):
        try:
            await asyncio.wait_for(_interrupt_event.wait(), timeout=5)
            _interrupt_event.clear()
            interrupted = True
            break
        except asyncio.TimeoutError:
            pass

        snapshot = await _sub_snapshot(round_id=round_id)
        if not snapshot:
            break

        resurrected = False
        for agent_id, info in snapshot.items():
            if info.get("status") not in ("done", "timeout") or _inbox_unread(agent_id) == 0:
                continue
            if await _sub_reactivate(agent_id):
                raw_messages = await _sub_raw_msgs(agent_id)
                _spawn_subagent_task(
                    _run_subagent(agent_id, str(info.get("task") or ""), bot, chat_id, db_path, resume_messages=raw_messages),
                    agent_id,
                )
                resurrected = True

        snapshot = await _sub_snapshot(round_id=round_id)
        all_truly_done = all(
            info.get("status") in ("done", "timeout") and _inbox_unread(agent_id) == 0
            for agent_id, info in snapshot.items()
        )
        if all_truly_done and not resurrected:
            quiet_ticks += 1
            if quiet_ticks >= 2:
                break
        else:
            quiet_ticks = 0

    if interrupted:
        return True, ""

    await asyncio.sleep(2)
    return False, await _sub_collect(round_id=round_id)


async def _synthesize_subagent_results(
    task: str,
    summary: str,
    round_title: str = "",
    guidance: str = "",
    round_history: list[dict[str, Any]] | None = None,
) -> str:
    # Include the main agent's own reasoning and spawn context so the LLM
    # understands what each subagent was asked to do and how we got here.
    context_lines: list[str] = []
    if round_history:
        for msg in round_history[-16:]:
            role = str(msg.get("role", "")).strip()
            if role == "system":
                continue
            content = str(msg.get("content", "")).strip()
            tool_calls = msg.get("tool_calls") or []
            if role == "user" and content:
                label = "User query" if not context_lines else "User"
                context_lines.append(f"[{label}]\n{content[:800]}")
            elif role == "assistant":
                if content:
                    context_lines.append(f"[Assistant reasoning]\n{content[:600]}")
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    args = fn.get("arguments", "{}")
                    if name == "spawn_subagent":
                        try:
                            a = json.loads(args)
                            context_lines.append(f"[Spawned subagent: {a.get('agent_id', '?')}]\nTask: {a.get('task', '')[:300]}")
                        except Exception:
                            context_lines.append(f"[Spawned subagent]")
                    elif name == "send_agent_message":
                        try:
                            a = json.loads(args)
                            context_lines.append(f"[Subagent msg: {a.get('from', '?')} -> {a.get('to', '?')}]")
                        except Exception:
                            pass
    context_block = "\n\n".join(context_lines) if context_lines else "—"

    # Build the expert findings block from subagent results
    experts_block = summary.strip() or "(No subagent results.)"

    # Only call LLM synthesis when there are actual multi-subagent findings
    if len(experts_block) < 50:
        return experts_block

    prompt_messages = [
        {
            "role": "system",
            "content": (
                "You are presenting the final answer after subagents completed their tasks.\n\n"
                "Rules:\n"
                "1. First, present EACH subagent's original output in full — verbatim, under their own heading.\n"
                "   This is mandatory. Do not rewrite, truncate, or summarize their work.\n"
                "2. After all subagent outputs, you MAY add a brief synthesis section that connects"
                " or contrasts their perspectives.\n"
                "3. For creative work (poems, code, art descriptions): quote the original completely.\n"
                "4. For research or analysis: present each expert's findings in full, then synthesize.\n\n"
                "Output format:\n"
                "--- <subagent name> ---\n"
                "<their complete original output>\n"
                "...\n"
                "--- Synthesis ---\n"
                "<your synthesis, if needed>"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Task: {task}\n\n"
                f"Round context:\n{context_block}\n\n"
                f"Expert findings from subagents:\n{experts_block}\n\n"
                "Present the final answer following the rules above."
            ),
        },
    ]
    response = await (_call_llm_stream(prompt_messages, max_tokens=None) if _streaming_reply_requested() else _call_llm(prompt_messages, tools=None, max_tokens=None))
    llm_text = _assistant_text(response).strip()
    return llm_text or experts_block


def _is_placeholder_reply(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return normalized in {
        "",
        "done",
        "done.",
        "finished",
        "finished.",
        "ok",
        "ok.",
        "okay",
        "okay.",
        "完成",
        "完成。",
        "已完成",
        "已完成。",
    }


async def _final_user_reply_from_history(messages: list[dict[str, Any]], max_tokens: int | None = None) -> str:
    last_user_text = next(
        (
            str(message.get("content") or "").strip()
            for message in reversed(messages)
            if isinstance(message, dict) and str(message.get("role") or "") == "user" and str(message.get("content") or "").strip()
        ),
        "",
    )
    prompt_messages = [
        *messages,
        {
            "role": "user",
            "content": (
                ("Now answer the user's request directly using the gathered tool results.\n" if last_user_text else
                 "The user uploaded one or more attachments without extra text. Summarize the attachment contents directly using the gathered tool results.\n")
                + "Do not call tools.\n"
                + "Do not reply with only 'Done'.\n"
                + "If the tools extracted file or attachment contents, quote or summarize those contents in your answer."
            ),
        },
    ]
    response = await (_call_llm_stream(prompt_messages, max_tokens=max_tokens) if _streaming_reply_requested() else _call_llm(prompt_messages, tools=None, max_tokens=max_tokens))
    return _assistant_text(response).strip()


async def _final_plain_reply_from_history(messages: list[dict[str, Any]], max_tokens: int | None = None) -> str:
    prompt_messages = [
        *messages,
        {
            "role": "user",
            "content": (
                "Answer the latest user message directly.\n"
                "Do not call tools.\n"
                "Do not reply with only 'Done'."
            ),
        },
    ]
    response = await (_call_llm_stream(prompt_messages, max_tokens=max_tokens) if _streaming_reply_requested() else _call_llm(prompt_messages, tools=None, max_tokens=max_tokens))
    return _assistant_text(response).strip()


def _tool_result_fallback_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if not isinstance(message, dict) or str(message.get("role") or "") != "tool":
            continue
        raw = str(message.get("content") or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            text_preview = str(payload.get("text_preview") or "").strip()
            if text_preview:
                return f"我从附件中提取到的内容是：\n\n{text_preview}"
            stdout = str(payload.get("stdout") or "").strip()
            if stdout:
                return f"我从附件中提取到的内容是：\n\n{stdout[:4000]}"
            preview = str(payload.get("preview") or "").strip()
            if preview and "no built-in parser" not in preview.lower():
                return f"我从附件中提取到的内容是：\n\n{preview}"
        elif raw and not raw.lower().startswith("tool failed:"):
            return f"我从附件中提取到的内容是：\n\n{raw[:4000]}"
    return ""


async def _final_reply_from_history(messages: list[dict[str, Any]], max_tokens: int | None = None) -> str:
    response = await (_call_llm_stream(messages, max_tokens=max_tokens) if _streaming_reply_requested() else _call_llm(messages, tools=None, max_tokens=max_tokens))
    return _assistant_text(response).strip() or "Done."


async def _process_main_inbox_message(message: dict[str, Any], bot: Any, chat_id: int, db_path: str) -> str:
    from cyrene.subagent import clear as _sub_clear, get_snapshot as _sub_snapshot

    target_round_id = str(message.get("round_id", "")).strip()
    guidance_id = str(message.get("message_id", "")).strip()
    content = str(message.get("content") or "").strip()
    if not target_round_id or not guidance_id or not content:
        return ""

    context = _guidance_round_context(target_round_id, guidance_id)
    live_round = next((live for live in get_live_rounds() if live.get("id") == target_round_id), None)
    round_title = context["round_title"] or str((live_round or {}).get("title") or "").strip() or target_round_id
    snapshot = await _sub_snapshot(round_id=target_round_id)
    await _insert_guidance_ack(
        target_round_id,
        guidance_id,
        round_title=round_title,
        client_request_id=context["client_request_id"],
    )
    has_live_subagents = bool(
        live_round
        and (
            int(live_round.get("subagentCount", 0) or 0) > 0
            or int(live_round.get("runningSubagents", 0) or 0) > 0
        )
    )
    if has_live_subagents or (live_round is None and snapshot):
        await _publish_runtime_event({
            "type": "phase_transition",
            "round_id": target_round_id,
            "from": "guidance_queue",
            "to": "subagent_guidance",
            "detail": f"Main agent is applying guidance to {len(snapshot)} subagent(s).",
        })
        await _fan_out_guidance_to_subagents(target_round_id, content, bot, chat_id, db_path)
        interrupted, _summary = await _wait_for_subagent_round(target_round_id, bot, chat_id, db_path)
        if interrupted:
            reply = "[Sub-agents are still working in the background. The guidance was delivered and the round is continuing.]"
        else:
            from cyrene.subagent import run_summary_subagent as _run_summary_subagent
            from cyrene.subagent import build_flow_snapshot as _build_subagent_flow_snapshot

            parent_task = next(
                (
                    str(msg.get("content") or "").strip()
                    for msg in context["round_history"]
                    if str(msg.get("role") or "").strip() == "user" and str(msg.get("content") or "").strip()
                ),
                content,
            )
            reply = await _run_summary_subagent(
                round_id=target_round_id,
                parent_task=parent_task,
                guidance=content,
                round_history=context["round_history"],
            )
            flow_snapshot = await _build_subagent_flow_snapshot(target_round_id)
            await _sub_clear(round_id=target_round_id)
        await _insert_guidance_reply(
            target_round_id,
            guidance_id,
            reply,
            round_title=round_title,
            client_request_id=context["client_request_id"],
            subagent_flow_snapshot=flow_snapshot if not interrupted else None,
        )
        _schedule_session_label_refresh(content, target_round_id)
        return reply

    guidance_system = (
        "This user message came from the main-agent inbox for an earlier round.\n"
        f"Target round id: {target_round_id}\n"
        f"Target round title: {round_title}\n"
        "Treat it as steering or a follow-up for that round. Continue the round instead of starting a fresh topic."
    )
    await _publish_runtime_event({
        "type": "phase_transition",
        "round_id": target_round_id,
        "from": "guidance_queue",
        "to": "guided_round_continuation",
        "detail": "Main agent is continuing the same round with the new guidance.",
    })
    persist_context = _guidance_persist_context_after_ack(guidance_id)
    return await _run_chat_agent(
        content,
        bot,
        chat_id,
        db_path,
        ephemeral_system=guidance_system,
        forced_round_id=target_round_id,
        history_override=context["round_history"],
        persist_base_messages=persist_context["persist_base_messages"],
        persist_insert_at=persist_context["persist_insert_at"],
        client_request_id=context["client_request_id"],
        persist_user_message=False,
    )


async def answer_pending_question(
    question_id: str,
    answer_text: str,
    bot: Any,
    chat_id: int,
    db_path: str,
    client_request_id: str = "",
) -> str:
    context = _pending_question_resume_context(question_id)
    pending = context.get("pending_question", {})
    if not pending:
        raise ValueError("Pending question not found.")

    content = str(answer_text or "").strip()
    if not content:
        raise ValueError("Answer cannot be empty.")

    round_id = str(context.get("round_id", "")).strip()
    if not round_id:
        raise ValueError("Pending question has no round context.")

    cleared = await _clear_pending_question(str(pending.get("id", "")).strip())
    if not cleared:
        raise ValueError("Pending question not found.")

    pending_meta = cleared.get("meta")
    if isinstance(pending_meta, dict) and str(pending_meta.get("kind", "")).strip() == "claude_code_prompt_confirmation":
        try:
            return await _handle_claude_code_prompt_answer(
                round_id=round_id,
                pending=cleared,
                answer_text=content,
                client_request_id=client_request_id,
            )
        except Exception:
            await _restore_pending_question(pending)
            raise

    answer_system = (
        "This user message answers your earlier clarification question for the same round.\n"
        f"Target round id: {round_id}\n"
        f"Original clarification question: {str(pending.get('text', '')).strip()}\n"
        "Treat the new user message as the answer and continue the same round."
    )
    try:
        return await _run_chat_agent(
            content,
            bot,
            chat_id,
            db_path,
            ephemeral_system=answer_system,
            forced_round_id=round_id,
            history_override=context.get("round_history") or [],
            persist_base_messages=context.get("persist_base_messages") or [],
            persist_insert_at=context.get("persist_insert_at"),
            client_request_id=client_request_id,
            persist_user_message=True,
            command=str(context.get("command", "") or "").strip(),
        )
    except Exception:
        await _restore_pending_question(pending)
        raise


def _is_affirmative_answer(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return normalized in {
        "同意并发送", "同意", "发送", "确认", "确认发送", "好", "好的", "可以", "行", "yes", "y", "ok", "okay", "send", "confirm",
    }


def _is_negative_answer(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return normalized in {
        "取消", "不用", "不发", "停止", "算了", "cancel", "no", "n", "stop",
    }


async def _handle_claude_code_prompt_answer(
    round_id: str,
    pending: dict[str, Any],
    answer_text: str,
    client_request_id: str = "",
) -> str:
    from cyrene.cc_bridge import send_prompt_to_cc

    meta = pending.get("meta", {})
    optimized_prompt = str(meta.get("optimized_prompt") or "").strip()
    task = str(meta.get("task") or "").strip()
    user_answer = str(answer_text or "").strip()
    chinese = _contains_cjk(task or optimized_prompt or user_answer)

    user_entry: dict[str, Any] = {
        "role": "user",
        "content": user_answer,
        "round_id": round_id,
    }
    if client_request_id:
        user_entry["client_request_id"] = client_request_id
    await _append_session_message(user_entry)

    if _is_negative_answer(user_answer):
        reply = "已取消，Claude Code 没有收到这条提示词。" if chinese else "Cancelled. The prompt was not sent to Claude Code."
        await _insert_intermediate_user_reply(reply, round_id=round_id, client_request_id=client_request_id)
        return reply

    prompt_to_send = optimized_prompt if _is_affirmative_answer(user_answer) else user_answer
    if not prompt_to_send:
        reply = "没有可发送的提示词。" if chinese else "There is no prompt to send."
        await _insert_intermediate_user_reply(reply, round_id=round_id, client_request_id=client_request_id)
        return reply

    result = send_prompt_to_cc(prompt_to_send)
    if not result.get("ok"):
        reason = str(result.get("reason") or "unknown error").strip()
        reply = (
            f"没有成功发送到 Claude Code：{reason}"
            if chinese else
            f"Failed to send the prompt to Claude Code: {reason}"
        )
        await _insert_intermediate_user_reply(reply, round_id=round_id, client_request_id=client_request_id)
        return reply

    reply = (
        "已把提示词输入到 Claude Code，任务已经开始运行。"
        if chinese else
        "I sent the prompt to Claude Code and it is now running."
    )
    await _insert_intermediate_user_reply(reply, round_id=round_id, client_request_id=client_request_id)
    await _publish_runtime_event({
        "type": "chat_message",
        "client_request_id": client_request_id,
        "round_id": round_id,
    })
    return reply


def _ensure_main_inbox_worker(bot: Any, chat_id: int, db_path: str) -> None:
    global _main_inbox_worker
    if _main_inbox_worker is None or _main_inbox_worker.done():
        _main_inbox_worker = asyncio.create_task(_drain_main_inbox(bot, chat_id, db_path))


async def queue_round_guidance(
    target_round_id: str,
    content: str,
    bot: Any,
    chat_id: int,
    db_path: str,
    client_request_id: str = "",
) -> dict[str, Any]:
    """Send a follow-up question to the main-agent inbox for a live round."""
    from cyrene.inbox import send_message as _send_inbox

    live = {item["id"]: item for item in get_live_rounds()}
    target = live.get(target_round_id)
    if target is None:
        raise ValueError(f"Round {target_round_id} is not live.")

    created_at = datetime.now(timezone.utc).isoformat()
    guidance_id = await _send_inbox("user", _MAIN_INBOX_AGENT_ID, "guidance", content, round_id=target_round_id)
    if not guidance_id:
        raise ValueError("Failed to send guidance to the main-agent inbox.")
    item = {
        "id": guidance_id,
        "target_round_id": target_round_id,
        "content": content,
        "created_at": created_at,
    }
    labels = get_session_labels(target_round_id)
    queued_user_entry: dict[str, Any] = {
        "role": "user",
        "content": content,
        "round_id": target_round_id,
        "queued_guidance_id": guidance_id,
    }
    if labels.get("round_title"):
        queued_user_entry["round_title"] = labels["round_title"]
    if client_request_id:
        queued_user_entry["client_request_id"] = client_request_id
    await _append_session_message(queued_user_entry)
    await _publish_round_guidance_update(target_round_id)
    _ensure_main_inbox_worker(bot, chat_id, db_path)
    return item


async def _drain_main_inbox(bot: Any, chat_id: int, db_path: str) -> None:
    from cyrene.conversations import archive_exchange
    from cyrene.inbox import get_unread_messages, mark_read_count

    global _main_inbox_worker
    try:
        while True:
            unread = [
                message
                for message in get_unread_messages(_MAIN_INBOX_AGENT_ID)
                if str(message.get("type", "")).strip() == "guidance"
            ]
            if not unread:
                break

            item = unread[0]
            target_round_id = str(item.get("round_id", "")).strip()
            guidance_id = str(item.get("message_id", "")).strip()
            response = ""
            try:
                await _publish_runtime_event({
                    "type": "phase_transition",
                    "round_id": target_round_id,
                    "from": "queued_guidance",
                    "to": "guidance_execution",
                    "detail": "Main agent is now applying the queued guidance.",
                })
                async with _agent_lock:
                    _interrupt_event.clear()
                    response = await _process_main_inbox_message(item, bot, chat_id, db_path)
            except Exception as exc:
                logger.exception("Failed to process main inbox guidance for %s", target_round_id or "<unknown>")
                if target_round_id and guidance_id:
                    context = _guidance_round_context(target_round_id, guidance_id)
                    round_title = context.get("round_title") or next(
                        (live["title"] for live in get_live_rounds() if live.get("id") == target_round_id),
                        target_round_id,
                    )
                    response = _guidance_error_text(exc)
                    await _insert_guidance_reply(
                        target_round_id,
                        guidance_id,
                        response,
                        round_title=round_title,
                        client_request_id=str(context.get("client_request_id") or ""),
                    )
            finally:
                await mark_read_count(_MAIN_INBOX_AGENT_ID, 1)
                if target_round_id:
                    await _publish_round_guidance_update(target_round_id)
            if response and response != _AWAITING_USER_SENTINEL:
                labels = get_session_labels(target_round_id)
                await archive_exchange(
                    str(item.get("content") or ""),
                    response,
                    chat_id,
                    session_title=labels.get("session_title", ""),
                    round_title=labels.get("round_title", ""),
                    round_id=labels.get("round_id", ""),
                    archive_session_id=labels.get("archive_session_id", ""),
                )
    except Exception:
        logger.exception("Failed to drain main inbox")
    finally:
        _main_inbox_worker = None
        if get_live_rounds() and _main_inbox_pending_by_round():
            _ensure_main_inbox_worker(bot, chat_id, db_path)


def get_session_labels(round_id: str = "") -> dict[str, str]:
    state = _load_session_state()
    messages = state.get("messages", []) if isinstance(state.get("messages"), list) else []
    last_round_id = next((str(m.get("round_id", "")).strip() for m in reversed(messages) if m.get("round_id")), "")
    target_round_id = str(round_id or "").strip() or last_round_id
    round_title = next(
        (
            str(m.get("round_title", "")).strip()
            for m in messages
            if str(m.get("round_id", "")).strip() == target_round_id and m.get("round_title")
        ),
        "",
    )
    had_archive_session_id = bool(str(state.get("archive_session_id", "")).strip())
    archive_session_id = _ensure_archive_session_id(state)
    if not had_archive_session_id:
        _write_session_state(state)
    return {
        "session_title": str(state.get("session_title", "")).strip(),
        "round_title": round_title,
        "round_id": target_round_id,
        "archive_session_id": archive_session_id,
    }


async def _refresh_session_labels(current_user_message: str, round_id: str) -> None:
    state = _load_session_state()
    messages = state.get("messages", []) if isinstance(state.get("messages"), list) else []
    if not messages:
        return

    session_user_inputs = [
        str(msg.get("content", "")).strip()
        for msg in messages
        if msg.get("role") == "user" and str(msg.get("content", "")).strip()
    ]
    round_user_inputs = [
        str(msg.get("content", "")).strip()
        for msg in messages
        if msg.get("role") == "user"
        and str(msg.get("round_id", "")).strip() == round_id
        and str(msg.get("content", "")).strip()
    ]
    if not round_user_inputs:
        round_user_inputs = [_fallback_label(current_user_message, limit=80)]
    if not session_user_inputs:
        session_user_inputs = round_user_inputs

    round_fallback = _fallback_label(" / ".join(round_user_inputs), limit=40)
    session_fallback = _fallback_label(" / ".join(session_user_inputs), limit=56)
    token = _caller_type.set("session_namer")
    try:
        response = await _call_llm([
            {
                "role": "system",
                "content": (
                    "You generate concise UI labels for chat sessions and rounds. "
                    "Return strict JSON with keys round_title and session_title only."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Summarize the following chat inputs into compact labels.\n"
                    "Rules:\n"
                    "- round_title: summarize only the current round's user input(s)\n"
                    "- session_title: summarize all user inputs in the session so far\n"
                    "- Keep each label under 12 words\n"
                    "- Use the user's language when obvious\n"
                    "- No quotes, markdown, numbering, or trailing punctuation\n\n"
                    f"Current round user inputs:\n{json.dumps(round_user_inputs, ensure_ascii=False)}\n\n"
                    f"All session user inputs:\n{json.dumps(session_user_inputs, ensure_ascii=False)}\n\n"
                    "Return JSON only."
                ),
            },
        ], tools=None)
        payload = _extract_json_object(_assistant_text(response))
    except Exception:
        logger.warning("Session naming failed", exc_info=True)
        payload = {}
    finally:
        _caller_type.reset(token)

    round_title = _fallback_label(payload.get("round_title") or round_fallback, limit=40)
    session_title = _fallback_label(payload.get("session_title") or session_fallback, limit=56)

    async with _session_state_lock:
        latest_state = _load_session_state()
        latest_messages = latest_state.get("messages", [])
        full_messages = list(latest_messages) if isinstance(latest_messages, list) else []
        for msg in full_messages:
            if str(msg.get("round_id", "")).strip() == round_id:
                msg["round_title"] = round_title
        latest_state["messages"] = full_messages
        latest_state["session_title"] = session_title
        _write_session_state(latest_state)


async def _compress_old_messages(all_messages: list[dict]) -> None:
    """
    压缩最早的一部分消息到短期记忆。
    在后台运行，不阻塞对话。
    """
    # 取前 20 条用户+助理消息
    to_compress = [m for m in all_messages[:20] if m["role"] in ("user", "assistant")]
    if not to_compress:
        return

    # 格式化成文本
    lines = []
    for m in to_compress:
        role = "User" if m["role"] == "user" else ASSISTANT_NAME
        content = m.get("content", "")[:200]
        lines.append(f"{role}: {content}")
    text = "\n".join(lines)

    # LLM 调用压缩
    prompt = f"""Extract key information from this conversation. Focus on:
1. Facts about the user (job, preferences, habits)
2. Emotional patterns or recurring topics
3. Action items or decisions made

For each finding, classify as: fact | pattern | preference | emotion

Conversation:
{text}

Output format (one per line, no explanations):
[fact] user works at a tech company
[emotion] user was frustrated about a project deadline
[preference] user likes casual short replies
"""

    try:
        response = await _call_llm([
            {"role": "system", "content": "You extract structured memories from conversations. Be concise."},
            {"role": "user", "content": prompt}
        ], tools=None)
        compressed = _assistant_text(response) or ""
    except Exception:
        logger.warning("Memory compression failed", exc_info=True)
        return

    # 解析并写入短期记忆
    for line in compressed.split("\n"):
        line = line.strip()
        if not line or not line.startswith("["):
            continue
        try:
            closing = line.index("]")
            entry_type = line[1:closing]
            content = line[closing + 1:].strip()
            if content and len(content) > 3:
                touch_entry(content, {
                    "content": content,
                    "type": entry_type,
                    "emotional_valence": -2 if "frustrat" in content.lower() or "stress" in content.lower() or "angry" in content.lower()
                    else 2 if "happy" in content.lower() or "love" in content.lower() or "excit" in content.lower()
                    else 0,
                })
        except (ValueError, IndexError):
            continue


async def clear_session_id() -> None:
    """Clear session, subagent registry, and compress conversation to short-term memory before discarding."""
    from cyrene.inbox import clear_all_inboxes

    global _main_inbox_worker
    for task in list(_pending_interrupt_clearers):
        task.cancel()
    _pending_interrupt_clearers.clear()
    for task in list(_pending_label_refreshes):
        task.cancel()
    _pending_label_refreshes.clear()
    _interrupt_event.clear()
    if _main_inbox_worker is not None:
        _main_inbox_worker.cancel()
        _main_inbox_worker = None
    global _active_main_round_id, _active_main_round_prompt, _active_main_round_public_prompt, _active_main_round_started_at
    _active_main_round_id = ""
    _active_main_round_prompt = ""
    _active_main_round_public_prompt = ""
    _active_main_round_started_at = 0.0
    await _clear_subagents()
    await clear_all_inboxes()
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
            if msgs:
                # Session reset should not block on a provider round-trip just to
                # preserve memory. Queue compression and clear the live state now.
                _schedule_memory_compression(msgs)
        except Exception:
            pass
        STATE_FILE.unlink()
    # 不清短期记忆。它用于在 session 重置后注入上下文。

    # 每次开新 session 时扫描历史行为模式；首次观察只记录，后续出现才提升为 pending script。
    try:
        from cyrene import pattern as _pattern_module
        _ = asyncio.create_task(_pattern_module.scan_for_session_start())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tool: quit (stays here to avoid circular imports — added to TOOL_HANDLERS below)
# ---------------------------------------------------------------------------


async def _tool_quit(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    return "Interaction ended."


# Add quit handler to the shared TOOL_HANDLERS dict (from tools.py)
TOOL_HANDLERS["quit"] = _tool_quit


# ---------------------------------------------------------------------------
# LLM call (accepts tools as parameter)
# ---------------------------------------------------------------------------


def _sanitize_messages_for_llm(messages: list[dict]) -> list[dict]:
    """Ensure valid tool_calls/tool message pairing with unique tool_call_ids.

    Handles three classes of corruption that cause LLM APIs to reject the
    conversation history:
    1. Duplicate tool_call_ids (e.g. after a retry round) — regenerated uniquely.
    2. Orphan tool_calls (assistant tool_calls without matching tool responses).
    3. Orphan tool messages (tool messages without a preceding tool_calls).
    """
    import uuid as _uuid

    seen_ids: set[str] = set()
    result: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = str(msg.get("role", ""))

        if role == "assistant" and msg.get("tool_calls"):
            tc_list = msg["tool_calls"]
            all_valid = True
            for j, tc in enumerate(tc_list):
                idx = i + 1 + j
                if idx >= len(messages):
                    all_valid = False
                    break
                tm = messages[idx]
                if tm.get("role") != "tool" or tm.get("tool_call_id") != tc.get("id", ""):
                    all_valid = False
                    break

            if all_valid:
                old_ids = [tc.get("id", "") for tc in tc_list]
                has_dupes = any(oid in seen_ids for oid in old_ids)

                if has_dupes:
                    new_msg = dict(msg)
                    new_tc_list = []
                    new_ids = []
                    for tc in tc_list:
                        new_tc = dict(tc)
                        new_id = f"call_{_uuid.uuid4().hex[:12]}"
                        new_tc["id"] = new_id
                        new_tc_list.append(new_tc)
                        new_ids.append(new_id)
                        seen_ids.add(new_id)
                    new_msg["tool_calls"] = new_tc_list
                    result.append(new_msg)
                    for j, new_id in enumerate(new_ids):
                        tool_msg = dict(messages[i + 1 + j])
                        tool_msg["tool_call_id"] = new_id
                        result.append(tool_msg)
                else:
                    for oid in old_ids:
                        seen_ids.add(oid)
                    result.append(msg)
                    for j in range(len(tc_list)):
                        result.append(messages[i + 1 + j])

                i += 1 + len(tc_list)
            else:
                # Orphan tool_calls — skip this assistant message
                i += 1
        elif role == "tool":
            # Orphan tool message — skip
            i += 1
        else:
            result.append(msg)
            i += 1

    return result


def _llm_phase_name(tools: list | None) -> str:
    return "phase1" if tools is _LIGHT_TOOL_DEFS else ("phase2" if tools else "no_tools")


def _build_llm_request(
    messages: list[dict],
    tools: list | None,
    max_tokens: int | None,
    *,
    stream: bool,
) -> tuple[list[str], str, dict[str, Any], dict[str, str]]:
    model = os.environ.get("OPENAI_MODEL", "deepseek-chat")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    normalized_base = base_url.rstrip("/")
    endpoints = [f"{normalized_base}/chat/completions"]
    if not normalized_base.endswith("/v1"):
        endpoints.append(f"{normalized_base}/v1/chat/completions")
    deduped_endpoints = list(dict.fromkeys(endpoints))
    payload: dict[str, Any] = {
        "model": model,
        "messages": _sanitize_messages_for_llm(messages),
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if "deepseek" in model:
        payload["thinking"] = {"type": "enabled"}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    headers = {"Content-Type": "application/json"}
    if api_key and api_key.lower() not in ("lmstudio", "dummy", ""):
        headers["Authorization"] = f"Bearer {api_key}"
    return deduped_endpoints, model, payload, headers


def _extract_stream_delta_text(delta: dict[str, Any]) -> str:
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


async def _call_llm(messages: list[dict], tools: list | None = None, max_tokens: int | None = 32000) -> dict:
    _t0 = __import__("time").monotonic()
    _phase = _llm_phase_name(tools)
    endpoints, _model, payload, headers = _build_llm_request(messages, tools, max_tokens, stream=False)

    transport = httpx.AsyncHTTPTransport(retries=1)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=120.0) as client:
            last_error: Exception | None = None
            for endpoint in endpoints:
                try:
                    resp = await client.post(
                        endpoint,
                        json=payload,
                        headers=headers,
                    )
                    if resp.status_code != 200:
                        resp.raise_for_status()
                    data = resp.json()
                    msg = _message_from_upstream_payload(data)
                    msg["usage"] = _normalized_usage(data.get("usage"), messages, msg)
                    if debug.VERBOSE:
                        debug.log_llm_call(_caller_type.get(), _phase, messages, tools, msg, (__import__("time").monotonic() - _t0) * 1000)
                    await _publish_runtime_event({
                        "type": "llm_call", "caller": _caller_type.get(), "phase": _phase,
                        "tools": [t.get("function", {}).get("name") for t in (tools or [])],
                        "messages": _sanitize_messages_for_llm(messages),
                        "response": msg,
                        "usage": msg.get("usage") or {},
                        "duration_ms": round((__import__("time").monotonic() - _t0) * 1000),
                    })
                    return msg
                except (httpx.HTTPError, ValueError) as exc:
                    last_error = exc
                    if endpoint != endpoints[-1]:
                        continue
                    if isinstance(exc, httpx.HTTPError):
                        logger.error(
                            "Upstream LLM returned non-200 [caller=%s phase=%s model=%s endpoint=%s]: %s",
                            _caller_type.get(),
                            _phase,
                            _model,
                            endpoint,
                            format_httpx_error(exc),
                        )
                    raise
            if last_error:
                raise last_error
    except httpx.TimeoutException as exc:
        logger.exception(
            "Upstream LLM timeout [caller=%s phase=%s model=%s endpoint=%s]: %s",
            _caller_type.get(),
            _phase,
            _model,
            endpoints[0],
            format_httpx_error(exc),
        )
        raise
    except httpx.HTTPError as exc:
        logger.exception(
            "Upstream LLM HTTP error [caller=%s phase=%s model=%s endpoint=%s]: %s",
            _caller_type.get(),
            _phase,
            _model,
            endpoints[0],
            format_httpx_error(exc),
        )
        raise


async def _call_llm_stream(messages: list[dict], max_tokens: int | None = 32000) -> dict[str, Any]:
    _t0 = __import__("time").monotonic()
    _phase = _llm_phase_name(None)
    endpoints, _model, payload, headers = _build_llm_request(messages, None, max_tokens, stream=True)

    accumulated: list[str] = []
    usage: dict[str, Any] = {}
    started = False
    transport = httpx.AsyncHTTPTransport(retries=1)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=120.0) as client:
            last_error: Exception | None = None
            for endpoint in endpoints:
                try:
                    async with client.stream("POST", endpoint, json=payload, headers=headers) as resp:
                        if resp.status_code != 200:
                            resp.raise_for_status()
                        async for raw_line in resp.aiter_lines():
                            line = str(raw_line or "").strip()
                            if not line:
                                continue
                            if line.startswith("data:"):
                                line = line[5:].strip()
                            if not line:
                                continue
                            if line == "[DONE]":
                                break
                            try:
                                data = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(data.get("usage"), dict):
                                usage = data["usage"]
                            for choice in data.get("choices") or []:
                                delta = choice.get("delta") or {}
                                text = _extract_stream_delta_text(delta)
                                if not text:
                                    continue
                                if not started:
                                    await _emit_reply_stream_event({"type": "reply_start"})
                                    started = True
                                accumulated.append(text)
                                await _emit_reply_stream_event({"type": "reply_delta", "delta": text})
                    break
                except httpx.HTTPError as exc:
                    last_error = exc
                    if endpoint != endpoints[-1]:
                        continue
                    logger.error(
                        "Upstream LLM returned non-200 [caller=%s phase=%s model=%s endpoint=%s stream=true]: %s",
                        _caller_type.get(),
                        _phase,
                        _model,
                        endpoint,
                        format_httpx_error(exc),
                    )
                    raise
            if last_error and not accumulated and not usage:
                raise last_error
        full_text = "".join(accumulated)
        if not started:
            await _emit_reply_stream_event({"type": "reply_start"})
        await _emit_reply_stream_event({"type": "reply_done", "response": full_text})
        msg: dict[str, Any] = {"role": "assistant", "content": full_text}
        msg["usage"] = _normalized_usage(usage, messages, msg)
        if debug.VERBOSE:
            debug.log_llm_call(_caller_type.get(), _phase, messages, None, msg, (__import__("time").monotonic() - _t0) * 1000)
        await _publish_runtime_event({
            "type": "llm_call",
            "caller": _caller_type.get(),
            "phase": _phase,
            "tools": [],
            "response": full_text[:200],
            "tool_calls": [],
            "usage": msg.get("usage") or {},
            "duration_ms": round((__import__("time").monotonic() - _t0) * 1000),
        })
        return msg
    except httpx.TimeoutException as exc:
        logger.exception(
            "Upstream LLM timeout [caller=%s phase=%s model=%s endpoint=%s stream=true]: %s",
            _caller_type.get(),
            _phase,
            _model,
            endpoints[0],
            format_httpx_error(exc),
        )
        raise
    except httpx.HTTPError as exc:
        logger.exception(
            "Upstream LLM HTTP error [caller=%s phase=%s model=%s endpoint=%s stream=true]: %s",
            _caller_type.get(),
            _phase,
            _model,
            endpoints[0],
            format_httpx_error(exc),
        )
        raise


# ---------------------------------------------------------------------------
# Main agent (assistant tone + full tools + session persistence)
# ---------------------------------------------------------------------------


# 轻量 tool：只有 use_tools + quit，用于第一阶段判断是否进重循环
_LIGHT_TOOL_DEFS = [
    {"type": "function", "function": {"name": "use_tools", "description": "MANDATORY gateway to full tool access. Call this for ANY request that involves doing things — file ops, search, web, code, shell, scheduling, sub-agents, data, etc. This is the ONLY way to reach real tools. Skip ONLY for pure conversation (opinions, greetings, conceptual explanations). IMPORTANT: set task to the user's EXACT original message, do not rewrite it.", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}},
    {"type": "function", "function": {"name": "ask_user", "description": "Ask the user a clarification question. Use this proactively whenever: the request is ambiguous, a critical detail is missing, multiple approaches exist and the choice matters, or you need confirmation before a destructive/irreversible action. Guessing is worse than asking. Use freeform text, or add a short options array when structured choices help. Do not combine with other tools in the same turn.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "quit", "description": "Call this when the interaction is done.", "parameters": {"type": "object", "properties": {}}}},
]


async def _run_main_agent(
    user_message: str,
    history: list,
    bot: Any,
    chat_id: int,
    db_path: str,
    system_prompt: str = "",
    client_request_id: str = "",
    persist_user_message: bool = True,
    public_user_message: str | None = None,
    public_attachments: list[dict[str, Any]] | None = None,
    lang: str = "",
) -> str:
    """主 Agent：先轻量判断是否需工具，再决定是否进重循环。"""
    _caller_type.set("main_agent")
    suppress_initial_detail = _ui_round_hide_initial_detail.get()
    round_id = _current_round_id.get()
    visible_user_message = user_message if public_user_message is None else str(public_user_message)
    user_message_id = f"user_{uuid4().hex}"
    user_entry = {"role": "user", "content": visible_user_message, "message_id": user_message_id}
    if public_attachments:
        user_entry["attachments"] = [dict(item) for item in public_attachments if isinstance(item, dict)]
    if round_id:
        user_entry["round_id"] = round_id
    if client_request_id:
        user_entry["client_request_id"] = client_request_id
    if persist_user_message:
        await _append_session_message(user_entry)
    effective_system = system_prompt or _MAIN_AGENT_PROMPT
    llm_user_entry = dict(user_entry)
    llm_user_entry["content"] = user_message
    phase1_decision = _PHASE1_DECISION_PROMPT
    if _current_command.get() == "quick-answer":
        phase1_decision = (
            "Decision phase rules:\n"
            "- You are in Quick Answer mode. The user wants a fast, text-only answer.\n"
            "- Call `quit` immediately with your answer. Do NOT call `use_tools`.\n"
            "- Call `ask_user` ONLY if the question is genuinely unclear.\n"
            "- This mode is for pure conversation only — no tools, no research."
        )
    phase1_messages = [{"role": "system", "content": effective_system}, *history, llm_user_entry, {"role": "user", "content": phase1_decision}]

    async def _ensure_text_reply(
        response_obj: dict[str, Any],
        base_messages: list[dict[str, Any]],
        fallback: str = "Done.",
    ) -> str:
        text = _assistant_text(response_obj).strip()
        has_tool_results = any(
            (
                str(message.get("role") or "") == "tool"
                or (
                    str(message.get("role") or "") == "assistant"
                    and bool(message.get("tool_calls"))
                )
            )
            for message in base_messages
            if isinstance(message, dict)
        )
        if text and not (has_tool_results and _is_placeholder_reply(text)):
            return text
        if has_tool_results:
            final_user_text = (await _final_user_reply_from_history(base_messages, max_tokens=None)).strip()
            if final_user_text and not _is_placeholder_reply(final_user_text):
                return final_user_text
            fallback_from_tools = _tool_result_fallback_text(base_messages).strip()
            if fallback_from_tools:
                return fallback_from_tools
        else:
            final_plain_text = (await _final_plain_reply_from_history(base_messages, max_tokens=None)).strip()
            if final_plain_text and not _is_placeholder_reply(final_plain_text):
                return final_plain_text
        final_text = (await _final_reply_from_history(base_messages, max_tokens=None)).strip()
        if final_text and not _is_placeholder_reply(final_text):
            return final_text
        return fallback

    def _session_messages_to_save(current_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        _flush_intermediate_user_replies(current_messages)
        saved: list[dict[str, Any]] = []
        for message in current_messages[1:]:
            if message["role"] == "system":
                continue
            if bool(message.get("hidden_from_ui")):
                continue
            if not persist_user_message and message.get("message_id") == user_message_id:
                continue
            if message.get("role") == "user" and message.get("message_id") == user_message_id:
                saved.append(dict(user_entry))
                continue
            saved.append(message)
        return saved

    # Phase 1: 轻量调用，无完整工具列表，只有 use_tools + quit
    response = await _call_llm(phase1_messages, tools=_LIGHT_TOOL_DEFS)
    tool_calls = response.get("tool_calls") or []
    invalid_phase1_tools = [
        str(tc.get("function", {}).get("name") or "").strip()
        for tc in tool_calls
        if str(tc.get("function", {}).get("name") or "").strip() not in {"use_tools", "ask_user", "quit", ""}
    ]
    if invalid_phase1_tools:
        retry_messages = [
            *phase1_messages,
            {
                **_assistant_entry_from_response(response, round_id="", include_tool_calls=False),
                "content": _assistant_text(response) or (response.get("content") or ""),
            },
            {
                "role": "user",
                "content": (
                    f"[Decision-phase correction] You attempted unavailable tool(s): {', '.join(invalid_phase1_tools)}. "
                    "Only `use_tools`, `ask_user`, and `quit` are available in this phase. "
                    "If real tool work is needed, call `use_tools` with the user's exact original message. "
                    "If clarification is needed before acting, call `ask_user`. "
                    "Otherwise say there is no suitable tool in this phase."
                ),
            },
        ]
        response = await _call_llm(retry_messages, tools=_LIGHT_TOOL_DEFS)
    tool_calls = response.get("tool_calls") or []
    messages = [{"role": "system", "content": effective_system}, *history, llm_user_entry]
    assistant_entry = _assistant_entry_from_response(response, round_id)
    messages.append(assistant_entry)

    # 如果 LLM 调了 use_tools → 进入重循环（含全部工具）
    use_tools_call = None
    ask_user_call = None
    for tc in tool_calls:
        name = tc.get("function", {}).get("name")
        if name == "use_tools":
            use_tools_call = tc
        elif name == "ask_user":
            ask_user_call = tc
        elif name == "quit":
            if client_request_id:
                messages[-1]["client_request_id"] = client_request_id
            await _save_session_messages(_session_messages_to_save(messages))
            return await _ensure_text_reply(response, messages)

    if ask_user_call:
        try:
            args = json.loads(ask_user_call["function"].get("arguments") or "{}")
            result = await _execute_tool("ask_user", args, bot, chat_id, db_path, None)
        except Exception as exc:
            result = f"Tool failed: {exc}"
        tool_entry: dict[str, Any] = {"role": "tool", "tool_call_id": ask_user_call["id"], "content": _truncate(result)}
        if round_id:
            tool_entry["round_id"] = round_id
        messages.append(tool_entry)
        if _tool_result_requests_user_input(result):
            return _AWAITING_USER_SENTINEL
        await _save_session_messages(_session_messages_to_save(messages))
        return (await _ensure_text_reply(response, messages, fallback=str(result)))

    if use_tools_call:
        event = {
            "type": "phase_transition",
            "from": "phase1_decision",
            "to": "phase2_execution",
        }
        if not suppress_initial_detail:
            event["detail"] = f"Phase 1 decided to use tools. Task: {user_message[:120]}"
        await _publish_runtime_event(event)
        # Phase 2: 重循环 — LLM 使用带附件提示的用户消息，持久化仍使用可见版用户消息。
        messages = [{"role": "system", "content": effective_system}, *history, dict(llm_user_entry)]

        for _ in range(_MAX_TOOL_ROUNDS):
            response = await _call_llm(messages, tools=get_active_tool_defs())
            entry: dict = {"role": "assistant", "content": response.get("content") or ""}
            if response.get("reasoning_content"):
                entry["reasoning_content"] = response["reasoning_content"]
            if response.get("tool_calls"):
                entry["tool_calls"] = response["tool_calls"]
            if response.get("usage"):
                entry["usage"] = response["usage"]
            if round_id:
                entry["round_id"] = round_id
            messages.append(_apply_assistant_meta(entry))

            tcs = response.get("tool_calls") or []
            if any(t.get("function", {}).get("name") == "quit" for t in tcs):
                await _publish_runtime_event({
                    "type": "phase_transition",
                    "from": "execution",
                    "to": "done",
                    "detail": "Agent called quit",
                })
                if _streaming_reply_requested():
                    messages.pop()
                    final_text = await _final_reply_from_history(messages, max_tokens=None)
                    final_entry: dict[str, Any] = {"role": "assistant", "content": final_text}
                    if client_request_id:
                        final_entry["client_request_id"] = client_request_id
                    if round_id:
                        final_entry["round_id"] = round_id
                    messages.append(_apply_assistant_meta(final_entry))
                    await _save_session_messages(_session_messages_to_save(messages))
                    return final_text
                if client_request_id:
                    messages[-1]["client_request_id"] = client_request_id
                await _save_session_messages(_session_messages_to_save(messages))
                return await _ensure_text_reply(response, messages)
            if not tcs:
                if _streaming_reply_requested():
                    messages.pop()
                    final_text = await _final_reply_from_history(messages, max_tokens=None)
                    final_entry = {"role": "assistant", "content": final_text}
                    if client_request_id:
                        final_entry["client_request_id"] = client_request_id
                    if round_id:
                        final_entry["round_id"] = round_id
                    messages.append(_apply_assistant_meta(final_entry))
                    await _save_session_messages(_session_messages_to_save(messages))
                    return final_text
                if client_request_id:
                    messages[-1]["client_request_id"] = client_request_id
                await _save_session_messages(_session_messages_to_save(messages))
                return await _ensure_text_reply(response, messages)

            awaiting_user = False
            spawned = False
            for index, t in enumerate(tcs):
                tool_name = t.get("function", {}).get("name")
                if awaiting_user:
                    skipped_tool_entry: dict[str, Any] = {
                        "role": "tool",
                        "tool_call_id": t["id"],
                        "content": "Skipped because ask_user paused the round until the user answers.",
                    }
                    if round_id:
                        skipped_tool_entry["round_id"] = round_id
                    messages.append(skipped_tool_entry)
                    continue
                try:
                    args = json.loads(t["function"].get("arguments") or "{}")
                    result = await _execute_tool(tool_name, args, bot, chat_id, db_path, None)
                except Exception as e:
                    result = f"Tool failed: {e}"
                tool_entry: dict[str, Any] = {"role": "tool", "tool_call_id": t["id"], "content": _truncate(result)}
                if round_id:
                    tool_entry["round_id"] = round_id
                messages.append(tool_entry)
                if tool_name == "ask_user" and _tool_result_requests_user_input(str(result)):
                    awaiting_user = True
                if tool_name == "spawn_subagent":
                    spawned = True
            if awaiting_user:
                return _AWAITING_USER_SENTINEL
            await _save_session_messages(_session_messages_to_save(messages))

            # 调用了 spawn_subagent → 进入监控模式，不调 LLM，等 subagent 全部安静
            if spawned:
                await _publish_runtime_event({
                    "type": "phase_transition",
                    "from": "phase2_execution",
                    "to": "subagent_monitoring",
                    "detail": "Subagents spawned, entering monitoring loop",
                })
                from cyrene.subagent import (
                    _run_subagent,
                    _spawn_subagent_task,
                    build_deep_research_source as _build_deep_research_source,
                    build_flow_snapshot as _build_subagent_flow_snapshot,
                    clear as _sub_clear,
                    get_snapshot as _sub_snapshot,
                    get_raw_messages as _sub_raw_msgs,
                    reactivate as _sub_reactivate,
                    run_summary_subagent as _run_summary_subagent,
                )
                from cyrene.inbox import get_unread_count as _inbox_unread

                # 新退出条件：所有 agent 都 DONE/TIMEOUT 且 inbox 全部清空。
                # 监控期间，DONE agent 如果收到消息就唤醒它继续处理。
                # 如果用户发来新消息，中断监控让主 agent 立即处理。
                _interrupt_event.clear()
                interrupted = False
                quiet_ticks = 0
                for _ in range(120):  # max 10 min 硬上限
                    try:
                        await asyncio.wait_for(_interrupt_event.wait(), timeout=5)
                        _interrupt_event.clear()
                        interrupted = True
                        break
                    except asyncio.TimeoutError:
                        pass
                    snap = await _sub_snapshot(round_id=round_id)
                    if not snap:
                        break

                    # 1) 唤醒：DONE/TIMEOUT 的 agent 有未读消息 → 重启它的 loop
                    resurrected = False
                    for aid, info in snap.items():
                        if info["status"] in ("done", "timeout") and _inbox_unread(aid) > 0:
                            if await _sub_reactivate(aid):
                                raw = await _sub_raw_msgs(aid)
                                _spawn_subagent_task(
                                    _run_subagent(aid, info["task"], bot, chat_id, db_path, resume_messages=raw),
                                    aid,
                                )
                                resurrected = True

                    # 2) 真正退出条件：所有 agent 都 DONE/TIMEOUT 且没有未读消息
                    snap2 = await _sub_snapshot(round_id=round_id)
                    all_truly_done = all(
                        info["status"] in ("done", "timeout") and _inbox_unread(aid) == 0
                        for aid, info in snap2.items()
                    )
                    if all_truly_done and not resurrected:
                        quiet_ticks += 1
                        if quiet_ticks >= 2:  # 连续两次 tick 都安静 → 真退出
                            break
                    else:
                        quiet_ticks = 0
                if interrupted:
                    await _save_session_messages(_session_messages_to_save(messages))
                    return "[Sub-agents are still working in the background. You can continue the conversation.]"
                # 等 quiescent 后，收集结果
                await asyncio.sleep(2)  # 给 subagent 一点时间写 registry
                await _publish_runtime_event({
                    "type": "phase_transition",
                    "from": "subagent_monitoring",
                    "to": "synthesis",
                    "detail": "All subagents done, starting summary subagent",
                })
                summary_result = await _run_summary_subagent(
                    round_id=round_id,
                    parent_task=user_message,
                    round_history=messages,
                )

                # Deep research Phase 3: 多轮报告生成（大纲 → 逐章节填写 → 引用累积）
                if _deep_research_mode.get():
                    source_material = await _build_deep_research_source(round_id)
                    template = _load_research_template()

                    # ① 生成大纲（含篇幅偏好）
                    length_pref = _parse_length_preference(messages)
                    outline = await _generate_deep_research_outline(
                        source_material, template, user_message, lang, length_pref
                    )
                    units: list[dict] = outline.get("units", [])
                    if not units:
                        logger.warning("Deep research outline has no units, falling back to research materials")
                        final_text = source_material
                        synthesis_entry = {"role": "assistant", "content": final_text}
                    else:
                        # ② 逐写作单元填写
                        sections_written: list[str] = []
                        references_accumulated: list[str] = []

                        for unit_no, unit_def in enumerate(units, 1):
                            section_text = await _write_section(
                                source_material=source_material,
                                outline=outline,
                                report_so_far="\n\n".join(sections_written),
                                references_so_far="\n".join(references_accumulated),
                                unit_def=unit_def,
                                unit_no=unit_no,
                                total_units=len(units),
                                lang=lang,
                                length_pref=length_pref,
                            )
                            body, new_refs = _extract_new_references(section_text)
                            sections_written.append(body)
                            references_accumulated.extend(new_refs)

                        # ③ 可选扩展扫描（根据篇幅调整阈值）
                        total_len = sum(len(s) for s in sections_written)
                        expand_threshold = {"short": 4000, "medium": 8000, "long": 15000}.get(length_pref, 8000)
                        if total_len < expand_threshold:
                            sections_written = await _expansion_pass(
                                source_material, outline,
                                sections_written, references_accumulated, lang,
                            )

                        # ④ 去重引用 + 组装
                        references_accumulated = _deduplicate_references(references_accumulated)
                        final_text = _assemble_report(sections_written, references_accumulated, outline)

                    synthesis_entry = {"role": "assistant", "content": final_text, "deep_research_report": True}
                    pdf_attachment = _deep_research_pdf_attachment(round_id, user_message, final_text)
                    if pdf_attachment:
                        synthesis_entry["attachments"] = [pdf_attachment]
                else:
                    final_text = summary_result
                    synthesis_entry = {"role": "assistant", "content": final_text}

                flow_snapshot = await _build_subagent_flow_snapshot(round_id)
                if client_request_id:
                    synthesis_entry["client_request_id"] = client_request_id
                if round_id:
                    synthesis_entry["round_id"] = round_id
                if flow_snapshot:
                    synthesis_entry["subagent_flow_snapshot"] = flow_snapshot
                messages.append(_apply_assistant_meta(synthesis_entry))
                # 清空 registry，避免下一轮 spawn 把旧结果混入新 context
                await _sub_clear(round_id=round_id)
                await _save_session_messages(_session_messages_to_save(messages))
                return final_text

        await _save_session_messages(_session_messages_to_save(messages))
        return "Stopped after hitting the tool loop limit."

    event = {
        "type": "phase_transition",
        "from": "phase1_decision",
        "to": "chat_only",
    }
    if not suppress_initial_detail:
        event["detail"] = "Phase 1 decided chat-only, no tools needed"
    await _publish_runtime_event(event)
    # Phase 1 结束：纯聊天，无工具需要
    if _streaming_reply_requested():
        messages = [{"role": "system", "content": effective_system}, *history, user_entry]
        final_text = await _final_reply_from_history(messages, max_tokens=None)
        final_entry = {"role": "assistant", "content": final_text}
        if client_request_id:
            final_entry["client_request_id"] = client_request_id
        if round_id:
            final_entry["round_id"] = round_id
        messages.append(_apply_assistant_meta(final_entry))
        await _save_session_messages(_session_messages_to_save(messages))
        return final_text
    if client_request_id:
        messages[-1]["client_request_id"] = client_request_id
    await _save_session_messages(_session_messages_to_save(messages))
    return await _ensure_text_reply(response, messages)


# ---------------------------------------------------------------------------
# Execution agent (internal, all tools)
# ---------------------------------------------------------------------------


async def _run_execution_agent(task: str, bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> str:
    _caller_type.set("execution_agent")
    """Execution agent with all tools. Used internally by chat agent."""
    messages = [
        {"role": "system", "content": _EXECUTION_SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    final_text = "Done."
    for _ in range(_MAX_TOOL_ROUNDS):
        response = await _call_llm(messages, tools=get_active_tool_defs())

        assistant_entry: dict[str, Any] = {"role": "assistant"}
        if response.get("content"):
            assistant_entry["content"] = response["content"]
        else:
            assistant_entry["content"] = ""
        if response.get("tool_calls"):
            assistant_entry["tool_calls"] = response["tool_calls"]
        if response.get("reasoning_content"):
            assistant_entry["reasoning_content"] = response["reasoning_content"]
        if response.get("usage"):
            assistant_entry["usage"] = response["usage"]
        messages.append(assistant_entry)

        tool_calls = response.get("tool_calls") or []

        # Check for quit
        if any(tc.get("function", {}).get("name") == "quit" for tc in tool_calls):
            final_text = _assistant_text(response) or "Done."
            break

        if not tool_calls:
            return _assistant_text(response) or "Done."

        for tc in tool_calls:
            call_id = tc["id"]
            fn = tc["function"]
            name = fn["name"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
                result = await _execute_tool(name, args, bot, chat_id, db_path, notify_state)
            except Exception as e:
                result = f"Tool {name} failed: {e}"
            messages.append({"role": "tool", "tool_call_id": call_id, "content": _truncate(result)})

    return final_text


# ---------------------------------------------------------------------------
# Chat agent (entry point)
# ---------------------------------------------------------------------------


async def run_agent(
    user_message: str,
    bot: Any,
    chat_id: int,
    db_path: str,
    client_request_id: str = "",
    lang: str = "",
    command: str = "",
    public_user_message: str | None = None,
    public_attachments: list[dict[str, Any]] | None = None,
) -> str:
    """Main entry point. Runs the main agent loop with full tools."""
    if _agent_lock.locked():
        interrupt_active_run()
    async with _agent_lock:
        _interrupt_event.clear()
        if client_request_id:
            return await _run_chat_agent(
                user_message,
                bot,
                chat_id,
                db_path,
                client_request_id=client_request_id,
                lang=lang,
                command=command,
                public_user_message=public_user_message,
                public_attachments=public_attachments,
            )
        return await _run_chat_agent(
            user_message,
            bot,
            chat_id,
            db_path,
            lang=lang,
            command=command,
            public_user_message=public_user_message,
            public_attachments=public_attachments,
        )


async def _clear_interrupt_when_idle() -> None:
    try:
        while _agent_lock.locked():
            await asyncio.sleep(0.05)
    finally:
        _interrupt_event.clear()


def interrupt_active_run() -> bool:
    """Best-effort interrupt for the currently running main-agent request."""
    if not _agent_lock.locked():
        _interrupt_event.clear()
        return False
    _interrupt_event.set()
    task = asyncio.create_task(_clear_interrupt_when_idle())
    _pending_interrupt_clearers.add(task)
    task.add_done_callback(_pending_interrupt_clearers.discard)
    return True


async def _run_chat_agent(
    user_message: str,
    bot: Any,
    chat_id: int,
    db_path: str,
    ephemeral_system: str = "",
    forced_round_id: str = "",
    history_override: list[dict[str, Any]] | None = None,
    persist_base_messages: list[dict[str, Any]] | None = None,
    persist_insert_at: int | None = None,
    client_request_id: str = "",
    persist_user_message: bool = True,
    public_user_message: str | None = None,
    public_attachments: list[dict[str, Any]] | None = None,
    public_prompt: str | None = None,
    refresh_labels: bool = True,
    hide_initial_detail: bool = False,
    assistant_message_meta: dict[str, Any] | None = None,
    lang: str = "",
    command: str = "",
) -> str:
    """Coordinator: main agent loop."""
    import time as _time

    round_id = str(forced_round_id or "").strip() or f"round_{int(_time.time() * 1000)}"
    round_token = _current_round_id.set(round_id)
    full_session_messages = _load_session_messages()
    global _active_main_round_id, _active_main_round_prompt, _active_main_round_public_prompt, _active_main_round_started_at
    _active_main_round_id = round_id
    _active_main_round_prompt = user_message
    _active_main_round_public_prompt = user_message if public_prompt is None else str(public_prompt)
    _active_main_round_started_at = _time.time()
    raw_history = list(history_override) if history_override is not None else _load_session_messages()
    history = _expand_report_reference_history(raw_history, user_message)
    merge_base = persist_base_messages
    merge_insert_at = persist_insert_at
    merge_live_state = history_override is None
    if history_override is not None and merge_base is None:
        merge_base = list(full_session_messages)
        merge_insert_at = len(merge_base)
        merge_live_state = False
    elif merge_live_state and merge_insert_at is None:
        merge_insert_at = len(history)
    base_token = _persist_base_messages.set(merge_base)
    merge_live_token = _persist_merge_live_state.set(merge_live_state and merge_base is None)
    prefix_token = _persist_history_prefix_len.set(len(history) if (merge_base is not None or merge_live_state) else 0)
    insert_token = _persist_insert_at.set(merge_insert_at if (merge_base is not None or merge_live_state) else None)
    client_request_token = _current_client_request_id.set(client_request_id)
    intermediate_reply_token = _pending_intermediate_user_replies.set([])
    hide_initial_detail_token = _ui_round_hide_initial_detail.set(bool(hide_initial_detail))
    assistant_meta_token = _ui_round_assistant_meta.set(dict(assistant_message_meta) if assistant_message_meta else None)
    try:
        # 如果 history 为空（session 被重置），注入短期记忆
        restored_short_term = False
        if not history:
            st = get_context(max_chars=5000)
            if st:
                history = [{"role": "system", "content": "[Restored context]\n" + st}]
                restored_short_term = True
        if ephemeral_system:
            history = [*history, {"role": "system", "content": ephemeral_system}]

        # 组装记忆上下文注入主 Agent 的 system prompt
        try:
            memory_context = get_memory_context(include_short_term=not restored_short_term)
        except TypeError as exc:
            if "include_short_term" not in str(exc):
                raise
            memory_context = get_memory_context()
        main_system = _MAIN_AGENT_PROMPT
        if lang and lang != "en":
            main_system += f"\n\nThe user has set their preferred language to {lang}. Reply in this language."
        if memory_context:
            main_system = main_system + "\n\n## Memory Context\n" + memory_context
        skill_prompt_block = build_skill_prompt_block()
        if skill_prompt_block:
            main_system = main_system + "\n\n" + skill_prompt_block

        is_deep_research = command == "deep-research"
        dr_token = _deep_research_mode.set(is_deep_research)
        cmd_token = _current_command.set(command)

        if command == "deep-research":
            main_system = main_system + "\n\n" + _DEEP_RESEARCH_PROMPT
            main_system += (
                "\n\n## Subagent Spawn Policy\n"
                "Current policy: deep-research (maximum parallelism).\n"
                "- You MUST spawn subagents for EVERY research track. Never do research yourself — your only job is to decompose, delegate, and synthesize.\n"
                "- Launch ALL subagents at once in a single batch. Do not wait for some to finish before spawning others.\n"
                "- If a research track is broad, split it further into narrower sub-tracks and spawn additional subagents.\n"
                "- Err on the side of MORE subagents. 5–10 subagents is normal; 10+ is acceptable for complex questions.\n"
                "- Even small, focused questions within a track deserve their own subagent. Granularity beats breadth per agent.\n"
                "- If any subagent result is thin, contradictory, or incomplete, immediately spawn follow-up subagents to fill the gap.\n"
                "- The ONLY reason not to spawn a subagent is if the task is already fully answered with high confidence. When in doubt, spawn."
            )
        elif command == "quick-answer":
            main_system = main_system + "\n\n" + _QUICK_ANSWER_PROMPT
        elif command == "help-me-decide":
            main_system = main_system + "\n\n" + _HELP_ME_DECIDE_PROMPT
            main_system += (
                "\n\n## Subagent Spawn Policy\n"
                "Current policy: help-me-decide.\n"
                "- Spawn exactly ONE subagent per option. Launch all simultaneously.\n"
                "- Do NOT do any option research yourself — delegate every option to its own subagent.\n"
                "- After all subagents return, synthesize into a decision report."
            )
        elif command == "learning-plan":
            main_system = main_system + "\n\n" + _LEARNING_PLAN_PROMPT
            main_system += (
                "\n\n## Subagent Spawn Policy\n"
                "Current policy: learning-plan.\n"
                "- Spawn exactly ONE subagent per knowledge module. Launch all simultaneously.\n"
                "- Do NOT research learning resources yourself — delegate every module to its own subagent.\n"
                "- After all subagents return, synthesize into a structured learning plan."
            )
        elif command == "daily-review":
            main_system = main_system + "\n\n" + _DAILY_REVIEW_PROMPT
            main_system = main_system + "\n\n" + _spawn_policy_prompt_block("off")
        elif command == "deep-compare":
            main_system = main_system + "\n\n" + _DEEP_COMPARE_PROMPT
            main_system += (
                "\n\n## Subagent Spawn Policy\n"
                "Current policy: deep-compare.\n"
                "- Spawn exactly ONE subagent per comparison dimension. Launch all simultaneously.\n"
                "- Do NOT do any comparison research yourself — delegate every dimension to its own subagent.\n"
                "- After all subagents return, synthesize into a comparison matrix and recommendation."
            )
        elif command == "claude-code":
            main_system = main_system + "\n\n" + _CLAUDE_CODE_PROMPT
        else:
            main_system = main_system + "\n\n" + _spawn_policy_prompt_block(get_spawn_policy())

        # ====== 主 Agent ======
        main_text = await _run_main_agent(
            user_message,
            history,
            bot,
            chat_id,
            db_path,
            main_system,
            client_request_id=client_request_id,
            persist_user_message=persist_user_message,
            public_user_message=public_user_message,
            public_attachments=public_attachments,
            lang=lang,
        )

        if refresh_labels:
            await _refresh_session_labels(user_message, round_id)
        if main_text == _AWAITING_USER_SENTINEL:
            return main_text
        await _publish_runtime_event({
            "type": "chat_message",
            "client_request_id": client_request_id,
        })
        return main_text or "Done."
    finally:
        _current_command.reset(cmd_token)
        _deep_research_mode.reset(dr_token)
        _ui_round_assistant_meta.reset(assistant_meta_token)
        _ui_round_hide_initial_detail.reset(hide_initial_detail_token)
        _pending_intermediate_user_replies.reset(intermediate_reply_token)
        _current_client_request_id.reset(client_request_token)
        _persist_insert_at.reset(insert_token)
        _persist_history_prefix_len.reset(prefix_token)
        _persist_merge_live_state.reset(merge_live_token)
        _persist_base_messages.reset(base_token)
        _active_main_round_id = ""
        _active_main_round_prompt = ""
        _active_main_round_public_prompt = ""
        _active_main_round_started_at = 0.0
        _current_round_id.reset(round_token)


# ---------------------------------------------------------------------------
# Backward-compatible public API
# ---------------------------------------------------------------------------


async def run_task_agent(prompt: str, bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> str:
    """Alias for execution agent (no session). Used by scheduler."""
    return await _run_execution_agent(prompt, bot, chat_id, db_path, notify_state=notify_state)


async def run_heartbeat_agent(prompt: str, bot: Any, chat_id: int, db_path: str) -> str:
    """Run a full main-agent loop for proactive user-visible check-ins.

    The internal scheduler prompt is hidden from the Web UI. The final reply is
    persisted as a normal assistant message and must read like a direct message
    to the user, not a report about the hidden task.
    """
    proactive_system = (
        "This round was initiated by the scheduler, not by a user chat message.\n"
        "The hidden task you receive is internal guidance, not text to answer literally.\n"
        "Your final assistant reply will be shown directly to the user in the Web UI.\n"
        "Write to the user in a natural, user-facing voice.\n"
        "Match the user's preferred language based on their past messages.\n"
        "Do not mention the scheduler, heartbeat, lottery, hidden prompt, or internal instructions.\n"
        "If you decide to speak, send one concise, useful proactive message to the user.\n"
        "If tools are useful, use the normal main-agent loop and let the UI show the later details."
    )
    if _agent_lock.locked():
        return ""
    async with _agent_lock:
        _interrupt_event.clear()
        return await _run_chat_agent(
            prompt,
            bot,
            chat_id,
            db_path,
            ephemeral_system=proactive_system,
            persist_user_message=False,
            public_prompt="",
            refresh_labels=False,
            hide_initial_detail=True,
            assistant_message_meta={"proactive": True, "system_initiated": True},
        )


async def run_steward_agent(conversation_text: str, soulmd_content: str, bot: Any, chat_id: int, db_path: str) -> str:
    """Steward Agent call. Reads recent conversation + current SOUL.md, outputs modification instructions.
    Uses a different system prompt and no session persistence.
    """
    steward_prompt = f"""You are a memory steward. Your job is to update Cyrene's SOUL.md based on recent conversations.

Read the recent conversation and current SOUL.md, then output:
- APPEND: what new information to add
- ERASE: what old information to remove
- MERGE: what to consolidate
- Or SKIP if nothing important

SOUL.md:
{soulmd_content}

Recent conversation:
{conversation_text}

Output only the modifications needed, one per line, prefixed with APPEND/ERASE/MERGE/SKIP."""

    return await _run_execution_agent(steward_prompt, bot, chat_id, db_path)
