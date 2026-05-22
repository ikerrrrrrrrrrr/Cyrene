from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.pdfmetrics import registerFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


def _prepare_styles():
    registerFont(UnicodeCIDFont("STSong-Light"))
    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "CyreneBody",
        parent=styles["BodyText"],
        fontName="STSong-Light",
        fontSize=10.5,
        leading=16,
        textColor=HexColor("#1f2937"),
        spaceAfter=8,
    )
    return {
        "title": ParagraphStyle(
            "CyreneTitle",
            parent=base,
            fontSize=20,
            leading=26,
            textColor=HexColor("#111827"),
            spaceAfter=14,
        ),
        "h1": ParagraphStyle("CyreneH1", parent=base, fontSize=15, leading=21, textColor=HexColor("#111827"), spaceBefore=10, spaceAfter=8),
        "h2": ParagraphStyle("CyreneH2", parent=base, fontSize=12.5, leading=18, textColor=HexColor("#111827"), spaceBefore=8, spaceAfter=6),
        "body": base,
        "bullet": ParagraphStyle("CyreneBullet", parent=base, leftIndent=12, firstLineIndent=-8),
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
    escaped = _escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<font face='Courier'>\1</font>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", escaped)
    return escaped


def write_report_pdf(path: str | Path, title: str, body: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    styles = _prepare_styles()
    doc = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="Cyrene",
    )

    story = [Paragraph(_inline_markup(title), styles["title"]), Spacer(1, 4)]
    for raw_line in str(body or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            story.append(Spacer(1, 4))
            continue
        if stripped.startswith("### "):
            story.append(Paragraph(_inline_markup(stripped[4:]), styles["h2"]))
            continue
        if stripped.startswith("## "):
            story.append(Paragraph(_inline_markup(stripped[3:]), styles["h1"]))
            continue
        if stripped.startswith("# "):
            story.append(Paragraph(_inline_markup(stripped[2:]), styles["title"]))
            continue
        if re.match(r"^[-*]\s+", stripped):
            story.append(Paragraph("• " + _inline_markup(re.sub(r"^[-*]\s+", "", stripped)), styles["bullet"]))
            continue
        story.append(Paragraph(_inline_markup(stripped).replace("\n", "<br/>"), styles["body"]))

    doc.build(story)
    return target
