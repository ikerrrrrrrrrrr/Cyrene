"""Deep Research Phase 3 — report assembly, section writing, and citation management.

This module contains the pure-data helpers and the LLM-driven writing
functions for the final deep research report.  It depends on the agent
subpackage only for ``_call_llm`` and prompt constants.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

from cyrene.agent.prompts import (
    _DEFAULT_TEMPLATE,
    _EXPANSION_PROMPT,
    _OUTLINE_GENERATION_PROMPT,
    _SECTION_WRITE_PROMPT,
)
from cyrene.config import DATA_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def report_export_filename(round_id: str, fallback: str = "report") -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", str(round_id or fallback)).strip("-._") or fallback
    return f"{base}.pdf"


def load_research_template(template_path: str | None = None) -> str:
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


def extract_new_references(text: str) -> tuple[str, list[str]]:
    """Split LLM section output into body text and new reference entries."""
    patterns = [
        r"#{1,3}\s+(?:\d+\.?\s*)?New\s+References",
        r"#{1,3}\s+(?:\d+\.?\s*)?References",
        r"#{1,3}\s+(?:\d+\.?\s*)?Sources",
        r"#{1,3}\s+(?:\d+\.?\s*)?参考资料(?:和来源)?",
        r"#{1,3}\s+(?:\d+\.?\s*)?参考文献(?:和来源)?",
        r"#{1,3}\s+(?:\d+\.?\s*)?参考来源",
        r"#{1,3}\s+(?:\d+\.?\s*)?参考链接",
    ]
    best_pos = -1
    for pat in patterns:
        matches = list(re.finditer(pat, text, re.MULTILINE | re.IGNORECASE))
        if matches:
            pos = matches[-1].start()
            if pos > best_pos:
                best_pos = pos

    new_refs: list[str] = []
    body = text.rstrip()

    if best_pos >= 0:
        body = text[:best_pos].rstrip()
        ref_section = text[best_pos:]
        for line in ref_section.splitlines():
            line = line.strip()
            if re.match(r"^\[\d+\]", line):
                new_refs.append(line)

    # Fallback: orphan [N] entries
    if not new_refs:
        lines = text.strip().splitlines()
        orphan: list[str] = []
        for line in reversed(lines):
            stripped = line.strip()
            if re.match(r"^\[\d+\]", stripped):
                orphan.append(stripped)
            elif orphan and stripped:
                break
        if orphan:
            orphan.reverse()
            body_lines = text.strip().splitlines()
            filter_start = len(body_lines) - len(orphan)
            if filter_start >= 0:
                body = "\n".join(body_lines[:filter_start]).rstrip()
            new_refs = orphan

    return body, new_refs


def strip_stray_references(text: str) -> str:
    """Remove stray reference headings and source blocks from body text."""
    heading_patterns = [
        r"#{1,3}\s+(?:\d+\.?\s*)?New\s+References",
        r"#{1,3}\s+(?:\d+\.?\s*)?References",
        r"#{1,3}\s+(?:\d+\.?\s*)?Sources",
        r"#{1,3}\s+(?:\d+\.?\s*)?参考资料(?:和来源)?",
        r"#{1,3}\s+(?:\d+\.?\s*)?参考文献(?:和来源)?",
        r"#{1,3}\s+(?:\d+\.?\s*)?参考来源",
        r"#{1,3}\s+(?:\d+\.?\s*)?参考链接",
    ]
    lines = text.splitlines()
    cleaned: list[str] = []
    in_ref_block = False
    for line in lines:
        stripped = line.strip()
        matched_heading = any(re.search(p, stripped, re.IGNORECASE) for p in heading_patterns)
        if matched_heading:
            in_ref_block = True
            continue
        if in_ref_block:
            if re.match(r"^\[\d+\]", stripped):
                continue
            if not stripped:
                continue
            in_ref_block = False
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def deduplicate_references(entries: list[str]) -> tuple[list[str], dict[int, int]]:
    """Deduplicate reference entries by URL and renumber sequentially.

    Returns (deduplicated_entries, {old_number: new_number} mapping).
    """
    seen: dict[str, tuple[str, int]] = {}
    for entry in entries:
        m = re.search(r"(https?://\S+)", entry)
        key = m.group(1).rstrip(".)") if m else entry[:120]
        orig_num = 0
        num_m = re.match(r"\[(\d+)\]", entry)
        if num_m:
            orig_num = int(num_m.group(1))
        seen[key] = (entry, orig_num)
    old_to_new: dict[int, int] = {}
    result: list[str] = []
    for i, (entry, orig_num) in enumerate(seen.values(), 1):
        new_entry = re.sub(r"^\[\d+\]", f"[{i}]", entry)
        result.append(new_entry)
        if orig_num:
            old_to_new[orig_num] = i
    return result, old_to_new


def fill_missing_references(body: str, references: list[str]) -> list[str]:
    """Scan body for [N] citations and ensure each N has a definition."""
    if not references:
        return references
    ref_nums: set[int] = set()
    for ref in references:
        m = re.match(r"\[(\d+)\]", ref)
        if m:
            ref_nums.add(int(m.group(1)))
    body_nums: set[int] = set()
    for m in re.finditer(r"\[(\d+)\]", body):
        body_nums.add(int(m.group(1)))
    missing = sorted(n for n in body_nums if n not in ref_nums)
    if not missing:
        return references
    result = list(references)
    for n in missing:
        result.append(f"[{n}] Source — citation used in report")
    return result


def renumber_citations(text: str, mapping: dict[int, int]) -> str:
    """Renumber [N] citations in text according to old→new mapping."""

    def _replace(m: re.Match) -> str:
        num = int(m.group(1))
        new_num = mapping.get(num)
        if new_num is not None and new_num != num:
            return f"[{new_num}]"
        return m.group(0)

    return re.sub(r"\[(\d+)\]", _replace, text)


def assemble_report(sections: list[str], references: list[str], outline: dict, dedup_mapping: dict[int, int] | None = None) -> str:
    """Join section bodies, outline title, and deduplicated references."""
    title = str(outline.get("title") or "Deep Research Report").strip()
    parts: list[str] = []
    for sec in sections:
        clean = strip_stray_references(sec)
        if clean:
            parts.append(clean)
    body_text = "\n\n".join(parts)

    if dedup_mapping:
        body_text = renumber_citations(body_text, dedup_mapping)

    references = fill_missing_references(body_text, references)
    if references:
        parts = [body_text, "## 参考文献\n" + "\n".join(references)]
    else:
        parts = [body_text]
    report = "\n\n".join(parts)
    return f"# {title}\n\n{report}"


def parse_length_preference(messages: list[dict]) -> str:
    """Scan conversation messages for the user's length preference."""
    for msg in reversed(messages):
        content = str(msg.get("content", "") or "")
        content_lower = content.lower() if isinstance(content, str) else ""
        if "30" in content or "30+" in content:
            if any(kw in content_lower for kw in ["页", "篇", "长"]):
                return "long"
        if len(content) > 5 and ("长" in content and ("30" in content or "30+" in content)):
            return "long"
        if "10" in content and any(kw in content_lower for kw in ["页", "篇", "短"]):
            return "short"
        if len(content) > 3 and "短" in content:
            return "short"
        if "20" in content and any(kw in content_lower for kw in ["页", "篇", "中"]):
            return "medium"
        if len(content) > 3 and "中" in content:
            return "medium"
    return "medium"


# ---------------------------------------------------------------------------
# LLM-driven helpers
# ---------------------------------------------------------------------------

async def generate_deep_research_outline(
    source_material: str,
    template: str,
    question: str,
    lang: str,
    length_pref: str = "medium",
) -> dict:
    """LLM generates a report outline as JSON."""
    from cyrene.agent.state import _call_llm
    from cyrene.llm import _assistant_text

    sys_msg = (
        _OUTLINE_GENERATION_PROMPT.replace("{template}", template)
        .replace("{source_material}", source_material)
        .replace("{length_pref}", length_pref)
    )
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
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        outline = json.loads(raw)
        if not isinstance(outline, dict) or "units" not in outline:
            outline = {"title": question, "units": []}
    except Exception:
        logger.exception("Failed to generate deep research outline")
        outline = {"title": question, "units": []}
    return outline


async def write_section(
    source_material: str,
    outline: dict,
    unit_def: dict,
    unit_no: int,
    total_units: int,
    all_units: list[dict],
    lang: str,
    length_pref: str = "medium",
) -> str:
    """Write one section unit and return the full LLM output.

    All sections can be written in parallel — each writer sees the full
    outline + all section headings for context, without depending on prior
    sections' actual text.
    """
    from cyrene.agent.state import _call_llm
    from cyrene.llm import _assistant_text

    if length_pref == "short":
        min_words = 200
    elif length_pref == "long":
        min_words = 800
    else:
        min_words = 500

    # Build a preview of ALL sections (not just the current one)
    all_sections_preview = "\n".join(
        f"- {u.get('heading', '')}: {u.get('brief', '') or u.get('prompt', '')}"
        for u in all_units
    )

    # Build system prompt with SHARED content only (for DeepSeek KV cache efficiency)
    system_prompt = (
        _SECTION_WRITE_PROMPT.replace("{outline_json}", json.dumps(outline, ensure_ascii=False))
        .replace("{source_material}", source_material)
        .replace("{all_sections_preview}", all_sections_preview)
        .replace("{lang}", lang)
        .replace("{min_words}", str(min_words))
        .replace("{total_units}", str(total_units))
    )

    # Build user message with UNIT-SPECIFIC content
    user_msg = (
        f"Write unit {unit_no}/{total_units}: {unit_def.get('heading', '')}\n\n"
        f"{unit_def.get('brief', '') or unit_def.get('prompt', '')}"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = await _call_llm(messages, tools=None, max_tokens=None)
        return _assistant_text(resp) or ""
    except Exception:
        logger.exception("Failed to write section %s", unit_def.get("heading", ""))
        return f"[该章节未生成: {unit_def.get('heading', '')}]"


async def expansion_pass(
    outline: dict,
    sections_written: list[str],
    references: list[str],
    lang: str,
) -> list[str]:
    """Expand thin sections when the report is too short."""
    from cyrene.agent.state import _call_llm
    from cyrene.llm import _assistant_text

    combined = "\n\n".join(sections_written)
    prompt = (
        _EXPANSION_PROMPT.replace("{final_report}", combined)
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
            return sections_written
        result = list(sections_written)
        for i, section in enumerate(result):
            heading_match = re.match(r"(#{1,3}\s+[\d.]*\s*\S[^\n]*)", section)
            if not heading_match:
                continue
            heading = heading_match.group(1)
            exp_pattern = re.escape(heading)
            exp_match = re.search(exp_pattern, expansion_text)
            if exp_match:
                rest = expansion_text[exp_match.start():]
                next_heading = re.search(r"\n(#{1,3}\s+[\d.]*\s*\S)", rest[1:])
                expanded = rest[:next_heading.start() + 1] if next_heading else rest
                result[i] = expanded.strip()
        return result
    except Exception:
        logger.exception("Expansion pass failed")
        return sections_written


def deep_research_pdf_attachment(round_id: str, user_message: str, final_text: str) -> dict[str, Any] | None:
    """Generate a PDF attachment for a deep research report."""
    from cyrene.attachments import build_public_attachment_payload, register_generated_attachment

    title_match = re.search(r"^#\s+(.+)$", str(final_text or ""), re.MULTILINE)
    title = title_match.group(1).strip() if title_match else str(user_message or "").strip() or "Deep Research Report"
    title = re.sub(r"<[^>]+>", "", title).strip()
    export_name = report_export_filename(round_id or "deep-research-report", fallback="deep-research-report")
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


def report_title_from_text(text: str, fallback: str = "Deep Research Report") -> str:
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
            from cyrene.agent.message import _fallback_label
            return _fallback_label(stripped, limit=120)
    from cyrene.agent.message import _fallback_label
    return _fallback_label(source, limit=120)
