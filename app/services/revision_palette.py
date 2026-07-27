"""LOW-INK PDF palette for revision notes.

A4, text-only PDFs with **no coloured fills**: hierarchy comes from type
weight, all-caps tags, hairline rules, and hairline-border callout boxes.
Prints cleanly without eating ink.

This is the same palette used in the four-chapter repack the user
validated (Ch 1 / 4 / 8 / 9). Keep the rules simple: only INK for type
and rules, only SOFT_RULE for hairlines and borders, no fills.
"""
from __future__ import annotations

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


# --- Colour palette (intentionally tiny) ----------------------------------
INK = HexColor("#222222")          # body & head text
HEAD_RULE = HexColor("#222222")     # topic banner rule
SOFT_RULE = HexColor("#888888")     # callout borders, hairlines, table grid
ACCENT_LINE = HexColor("#666666")   # cover rule
DEEP_RULE = HexColor("#000000")     # chapter divider rule (heavier than HEAD_RULE)
PAGE_NUM = HexColor("#666666")      # footer


# --- Font registration --------------------------------------------------
# DejaVu is used because the rest of the app stack tolerates it and it
# carries the full Unicode range (Sanskrit diacritics, em-dashes, etc.).
# Falls back to the ReportLab default Helvetica if DejaVu is missing.
try:
    pdfmetrics.registerFont(
        TTFont("DJV", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    )
    pdfmetrics.registerFont(
        TTFont("DJVB", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    )
    registerFontFamily(
        "DJV", normal="DJV", bold="DJVB", italic="DJV", boldItalic="DJVB"
    )
    FONT, FONT_BOLD = "DJV", "DJVB"
except Exception:  # pragma: no cover - container without DejaVu
    FONT, FONT_BOLD = "Helvetica", "Helvetica-Bold"


# --- Style sheet --------------------------------------------------------
styles = getSampleStyleSheet()

PAGESIZE = A4
MARGIN_L = 1.8 * cm
MARGIN_R = 1.8 * cm
MARGIN_T = 1.4 * cm
MARGIN_B = 1.6 * cm
BODY_WIDTH = PAGESIZE[0] - MARGIN_L - MARGIN_R  # usable column width

H1 = ParagraphStyle(
    "H1",
    parent=styles["Heading1"],
    fontName=FONT_BOLD,
    fontSize=20,
    leading=24,
    textColor=INK,
    spaceAfter=4,
)
SECT_TAG = ParagraphStyle(
    "SECT_TAG",
    parent=styles["Normal"],
    fontName=FONT_BOLD,
    fontSize=8.5,
    leading=11,
    textColor=SOFT_RULE,
    spaceAfter=1,
    spaceBefore=10,
)
H2 = ParagraphStyle(
    "H2",
    parent=styles["Heading2"],
    fontName=FONT_BOLD,
    fontSize=14.5,
    leading=18,
    textColor=INK,
    spaceAfter=2,
)
H3 = ParagraphStyle(
    "H3",
    parent=styles["Heading3"],
    fontName=FONT_BOLD,
    fontSize=11.5,
    leading=14,
    textColor=INK,
    spaceBefore=6,
    spaceAfter=2,
)
BODY = ParagraphStyle(
    "BODY",
    parent=styles["Normal"],
    fontName=FONT,
    fontSize=10,
    leading=14,
    textColor=INK,
    alignment=TA_JUSTIFY,
    spaceAfter=3,
)
BULLET = ParagraphStyle(
    "BULLET", parent=BODY, leftIndent=10, spaceAfter=1, leading=13
)
CALLOUT = ParagraphStyle(
    "CALLOUT",
    parent=BODY,
    fontSize=9.8,
    leading=13,
    leftIndent=6,
    rightIndent=4,
    spaceAfter=1,
)
CALLOUT_TITLE = ParagraphStyle(
    "CALLOUT_TITLE",
    parent=BODY,
    fontName=FONT_BOLD,
    fontSize=9.5,
    leading=12,
    textColor=INK,
    spaceAfter=2,
)
SMALL_MUTED = ParagraphStyle(
    "small_muted",
    parent=BODY,
    fontSize=8.8,
    leading=11.5,
    textColor=SOFT_RULE,
)
FOOTER = ParagraphStyle(
    "footer",
    parent=BODY,
    fontSize=8,
    leading=10,
    textColor=PAGE_NUM,
    alignment=0,
)
SUB_STYLE = ParagraphStyle(
    "sub",
    parent=BODY,
    fontSize=9.8,
    leading=12.5,
    textColor=SOFT_RULE,
    spaceAfter=2,
)


# --- Helpers ------------------------------------------------------------
def topic_banner(title: str, subtitle: str | None = None) -> list:
    """Topic banner: small uppercase tag + bold title + thin rule.

    Returns a list of flowables (no fill, no colour block).
    """
    out = [
        Paragraph("TOPIC", SECT_TAG),
        Paragraph(title, H2),
    ]
    if subtitle:
        out.append(Paragraph(subtitle, SUB_STYLE))
    out.append(
        HRFlowable(
            width="100%",
            thickness=0.8,
            color=HEAD_RULE,
            spaceBefore=1,
            spaceAfter=6,
        )
    )
    return out


def chapter_divider(chapter_title: str) -> list:
    """Top-level chapter divider band. Renders above the first topic of a
    new narrative chapter (story / poem). Tag + bold chapter title + thin
    double rule. Visual hierarchy: chapter > topic.
    """
    return [
        Spacer(1, 0.4 * cm),
        Paragraph("CHAPTER", SECT_TAG),
        Paragraph(chapter_title, H1),
        HRFlowable(
            width="100%",
            thickness=1.2,
            color=DEEP_RULE,
            spaceBefore=1,
            spaceAfter=2,
        ),
        HRFlowable(
            width="100%",
            thickness=0.4,
            color=DEEP_RULE,
            spaceBefore=0,
            spaceAfter=8,
        ),
    ]


def callout(title: str, body_lines: list, icon: str | None = None) -> Table:
    """Hairline-bordered callout. White interior, bold title, no fill."""
    full_title = f"{icon} {title}" if icon else title
    rows = [[Paragraph(full_title, CALLOUT_TITLE)]]
    for line in body_lines:
        rows.append([Paragraph(line, CALLOUT)])
    t = Table(rows, colWidths=[BODY_WIDTH])
    t.setStyle(
        TableStyle(  # type: ignore[arg-type]
            [
                ("BOX", (0, 0), (-1, -1), 0.5, SOFT_RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t


def bullets(items: list, style=BULLET) -> ListFlowable:
    """Nested-aware bullet flowable."""
    out = []
    for it in items:
        if isinstance(it, list):
            nested = ListFlowable(
                [
                    ListItem(Paragraph(sub, style), leftIndent=12, value="-")
                    for sub in it
                ],
                bulletType="bullet",
                leftIndent=18,
                bulletFontSize=8,
            )
            out.append(ListItem(nested, leftIndent=12, value="•"))
        else:
            out.append(
                ListItem(Paragraph(it, style), leftIndent=12, value="•")
            )
    return ListFlowable(
        out, bulletType="bullet", leftIndent=10, bulletFontSize=9
    )


def comp_table(rows: list, col_widths: list) -> Table:
    """Comparison table: ink header rule + hairline rows. No fills."""
    t = Table(rows, colWidths=col_widths)
    t.setStyle(
        TableStyle(  # type: ignore[arg-type]
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
                ("FONTSIZE", (0, 0), (-1, 0), 10),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK),
                ("LINEABOVE", (0, 1), (-1, 1), 0.4, SOFT_RULE),
                ("LINEBELOW", (0, 0), (-1, -2), 0.25, SOFT_RULE),
            ]
        )
    )
    return t


def section_rule() -> HRFlowable:
    """Thin separator between sections. No fill."""
    return HRFlowable(
        width="100%",
        thickness=0.4,
        color=SOFT_RULE,
        spaceBefore=4,
        spaceAfter=4,
    )


def page_footer(chapter_label: str, deck_name: str) -> Paragraph:
    """Centered footer text; no fill."""
    return Paragraph(
        f"{chapter_label} · {deck_name}",
        FOOTER,
    )


# A bit of hygiene: strip emoji icons (which DejaVu can render but the
# mapping is inconsistent) and substitute plain text prefixes.
_EMOJI_PREFIXES = {
    "🧠 ": "MNEMONIC · ",
    "🗺️ ": "MAP · ",
    "📚 ": "EPIGRAPH · ",
    "🗳️ ": "NOTE · ",
    "📝 ": "NOTE · ",
    "❓ ": "RECALL · ",
    "🕰️ ": "TIMELINE · ",
    "🔍 ": "KEY TERM · ",
}


def sanitise_text(value: str) -> str:
    """Drop problematic emoji glyphs and substitute text labels."""
    if not value:
        return ""
    out = value
    for emo, repl in _EMOJI_PREFIXES.items():
        out = out.replace(emo, repl)
    # Drop remaining high-plane unicode (anything > 0x2740) so missing
    # glyphs cannot render as missing-glyph boxes in print.
    return "".join(ch if ord(ch) < 0x2740 else "" for ch in out)
