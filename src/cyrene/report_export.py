from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

# ---------------------------------------------------------------------------
# Font setup — register CJK fonts
# ---------------------------------------------------------------------------


def _register_cjk_font() -> str:
    """Register a suitable CJK font and return its name.

    Tries Noto Sans CJK SC first, then STSong-Light as fallback.
    """
    import os as _os

    # Common locations for Noto Sans CJK SC
    _candidates = [
        "/System/Library/Fonts/NotoSansCJKsc-Regular.otf",
        "/System/Library/Fonts/Noto Sans CJK SC Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.ttf",
        _os.path.expanduser("~/Library/Fonts/NotoSansCJKsc-Regular.otf"),
    ]
    for _path in _candidates:
        if _os.path.exists(_path):
            try:
                pdfmetrics.registerFont(TTFont("NotoSansCJK", _path))
                return "NotoSansCJK"
            except Exception:
                continue
    # Fallback: STSong-Light (CID font, bundled with ReportLab)
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    except Exception:
        pass
    return "STSong-Light"


def _prepare_styles():
    _font_name = _register_cjk_font()
    _s = getSampleStyleSheet()

    _body = ParagraphStyle(
        "CyreneBody",
        parent=_s["BodyText"],
        fontName=_font_name,
        fontSize=10,
        leading=16,
        textColor=HexColor("#1f2937"),
        spaceAfter=6,
        alignment=TA_LEFT,
    )
    return {
        "title": ParagraphStyle(
            "CyreneTitle",
            parent=_body,
            fontSize=22,
            leading=30,
            textColor=HexColor("#111827"),
            spaceAfter=4,
            alignment=TA_CENTER,
        ),
        "subtitle": ParagraphStyle(
            "CyreneSubtitle",
            parent=_body,
            fontSize=11,
            leading=16,
            textColor=HexColor("#6b7280"),
            spaceAfter=20,
            alignment=TA_CENTER,
        ),
        "h1": ParagraphStyle(
            "CyreneH1",
            parent=_body,
            fontSize=14,
            leading=20,
            textColor=HexColor("#111827"),
            spaceBefore=14,
            spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "CyreneH2",
            parent=_body,
            fontSize=12,
            leading=17,
            textColor=HexColor("#1f2937"),
            spaceBefore=10,
            spaceAfter=4,
        ),
        "body": _body,
        "bullet": ParagraphStyle(
            "CyreneBullet",
            parent=_body,
            leftIndent=14,
            firstLineIndent=-10,
            spaceAfter=3,
        ),
        "ref": ParagraphStyle(
            "CyreneRef",
            parent=_body,
            fontSize=8.5,
            leading=13,
            leftIndent=18,
            firstLineIndent=-18,
            spaceAfter=2,
            textColor=HexColor("#4b5563"),
        ),
    }


def _escape(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\t", "    ")
    )


def _inline_markup(text: str) -> str:
    """Convert markdown inline markup to ReportLab XML tags."""
    escaped = _escape(text)
    # Inline code with Courier
    escaped = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', escaped)
    # Bold
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    # Italic
    escaped = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", escaped)
    # Remove escaped brackets around citation numbers — they look bad in PDF
    # Keep the [N] format as-is since ReportLab handles it fine
    return escaped


def _parse_references_section(body: str) -> tuple[list[str], str]:
    """Extract the final references section from body text.

    Returns (reference_lines, body_without_refs).
    Any heading matching 参考文献/References/Sources at ## level is handled,
    and everything after it is treated as references.
    """
    # Try to find the final references section
    # Match both Chinese and English reference headings
    patterns = [
        r"\n##\s+\d*\.?\s*New\s+References\s*$",
        r"\n##\s+\d*\.?\s*References\s*$",
        r"\n##\s+\d*\.?\s*Sources\s*$",
        r"\n##\s+\d*\.?\s*参考资料(?:和来源)?\s*$",
        r"\n##\s+\d*\.?\s*参考文献(?:和来源)?\s*$",
        r"\n##\s+\d*\.?\s*参考来源\s*$",
        r"\n##\s+\d*\.?\s*参考链接\s*$",
    ]
    best_pos = -1
    for pat in patterns:
        matches = list(re.finditer(pat, body, re.MULTILINE | re.IGNORECASE))
        if matches:
            pos = matches[-1].start()  # Use the LAST occurrence
            if pos > best_pos:
                best_pos = pos

    if best_pos < 0:
        return [], body

    ref_block = body[best_pos:].strip()
    body_clean = body[:best_pos].rstrip()

    lines = []
    for line in ref_block.splitlines():
        stripped = line.strip()
        if re.match(r"^\[\d+\]", stripped):
            lines.append(stripped)

    return lines, body_clean


def write_report_pdf(path: str | Path, title: str, body: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    styles = _prepare_styles()

    doc = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=22 * mm,
        rightMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=22 * mm,
        title=title,
        author="Cyrene",
    )

    story: list = []

    # ---- Title block ----
    story.append(Spacer(1, 40 * mm))
    story.append(Paragraph(_inline_markup(f"<b>{title}</b>"), styles["title"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Deep Research Report", styles["subtitle"]))
    story.append(Spacer(1, 20 * mm))

    # ---- Body ----
    body_lines = body.strip().splitlines()
    i = 0
    while i < len(body_lines):
        raw_line = body_lines[i]
        stripped = raw_line.strip()

        if not stripped:
            story.append(Spacer(1, 3))
            i += 1
            continue

        # Skip the title line (already rendered above)
        if stripped.startswith("# ") or stripped.startswith("#{title}"):
            i += 1
            continue

        # Skip standalone reference headings — they'll be handled at the end
        if re.match(r"^##\s+\d*\.?\s*(?:New\s+References|References|Sources|参考资料(?:和来源)?|参考文献(?:和来源)?|参考来源|参考链接)\s*$", stripped, re.IGNORECASE):
            i += 1
            continue

        # h3 — subsection
        if stripped.startswith("### "):
            story.append(Paragraph(_inline_markup(stripped[4:]), styles["h2"]))
            i += 1
            continue

        # h2 — section
        if stripped.startswith("## "):
            story.append(Paragraph(_inline_markup(stripped[3:]), styles["h1"]))
            i += 1
            continue

        # h1 — should only be the title, already handled
        if stripped.startswith("# "):
            i += 1
            continue

        # Bullet list
        if re.match(r"^[-*]\s+", stripped):
            text = re.sub(r"^[-*]\s+", "", stripped)
            story.append(Paragraph("• " + _inline_markup(text), styles["bullet"]))
            i += 1
            continue

        # Reference line [N] ... → render in small font
        if re.match(r"^\[\d+\]", stripped):
            story.append(Paragraph(_inline_markup(stripped), styles["ref"]))
            i += 1
            continue

        # Regular paragraph
        story.append(Paragraph(_inline_markup(stripped).replace("\n", "<br/>"), styles["body"]))
        i += 1

    doc.build(story)
    return target
