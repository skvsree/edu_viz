"""Revision-notes PDF generation.

Takes the extracted text from a BulkAIUploadFile (already on disk) and
renders a print-ready, low-ink PDF that summarises the chapter into
6-9 topics.

Design principles (lesson from past incidents):

* Be honest about what we know. The input is the *real* extracted text,
  not LLM-invented content. Topic titles can come from the original
  headings; summaries come from the original paragraphs.
* The **only** AI call is to *organise* the source into 6-9 topic
  buckets. We never ask the model to invent facts.
* If heuristic chunking already produces 4-9 sections, skip AI entirely.
* The renderer in ``revision_palette`` uses zero coloured fills (only
  ink for text + hairline rules) so the PDF prints without eating ink.

The output is rendered into a BytesIO and uploaded to the configured
storage backend under ``bulk_uploads/{bulk_id}/revision_notes/{child}.pdf``.
"""
from __future__ import annotations

import io
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import (
    BulkAIUploadFile,
    BulkAIUploadRevisionNote,
    BulkRevisionNoteStatus,
    Deck,
)
from app.services import revision_palette as P
from app.services.ai_generation import get_study_pack_provider
from app.services.storage import StorageError, get_storage, guess_content_type

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AI-as-selector: extract verbatim key points from the source for each topic
# ---------------------------------------------------------------------------
#
# The AI is only allowed to PICK sentences from the source, never to
# rewrite, paraphrase, or invent. The validator below enforces this by
# rejecting any returned bullet that does not appear character-for-
# character (modulo internal whitespace) in the supplied paragraphs.
#
# Failure modes handled:
#   * AI call times out / non-JSON -> fall back to heuristic-only bullets
#   * Validator strips bullets down to <3 -> fall back to heuristic-only
#   * Topic has too few source paragraphs (<200 chars) -> skip AI entirely
def build_topic_recall_prompt(topic_title: str, paragraphs: list[str]) -> str:
    """Prompt the model to return 3-5 verbatim quotes from the source."""
    numbered = "\n\n".join(
        f"[{i + 1}] {p.strip()}" for i, p in enumerate(paragraphs) if p.strip()
    )
    return (
        "You are a careful editor selecting recall points for a revision sheet.\n"
        f"Topic: {topic_title}\n\n"
        "From the numbered source paragraphs below, pick the 3 to 5 most "
        "important points a student would need to recall.\n\n"
        "STRICT RULES:\n"
        "- Each bullet MUST be a verbatim quote from the source. A bullet is a "
        "contiguous sequence of words that appears exactly in the source.\n"
        "- Do NOT paraphrase. Do NOT summarise in your own words.\n"
        "- Do NOT invent names, dates, places, or facts.\n"
        "- Do NOT add connecting words or explanations around the quote.\n"
        "- Prefer whole sentences over short fragments when the sentence carries the meaning.\n"
        "- If the source has fewer than 3 useful points, return fewer.\n"
        "- Return strict JSON only, no markdown, no commentary.\n\n"
        'Output shape: {"bullets": ["verbatim quote 1", "verbatim quote 2", ...]}\n\n'
        f"Source paragraphs:\n{numbered}"
    )


def _normalise_for_match(s: str) -> str:
    """Collapse internal whitespace; used for substring/equality checks
    that tolerate the AI slightly rewrapping lines."""
    return " ".join((s or "").split())


def validate_verbatim_bullets(
    bullets: list[str], source_paragraphs: list[str]
) -> list[str]:
    """Drop any bullet that does not appear character-for-character
    (after whitespace normalisation) in the concatenated source."""
    if not bullets or not source_paragraphs:
        return []
    src_blob = "\n".join(_normalise_for_match(p) for p in source_paragraphs)
    out: list[str] = []
    for b in bullets:
        if not b:
            continue
        if _normalise_for_match(b) in src_blob:
            out.append(b.strip())
    return out


def _parse_recall_bullets_json(raw: str) -> list[str]:
    """Lenient JSON parser for the model's response. Accepts:
       {"bullets": [...]}
       or just a JSON array of strings
    """
    import json

    text = (raw or "").strip()
    if not text:
        return []
    # Strip code fences if the model ignored the rule
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
        text = text.strip()
    try:
        data = json.loads(text)
    except Exception:
        # Try to salvage by finding the first { ... } block
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except Exception:
                return []
        else:
            return []
    if isinstance(data, list):
        return [str(x) for x in data if x]
    if isinstance(data, dict) and "bullets" in data:
        val = data["bullets"]
        if isinstance(val, list):
            return [str(x) for x in val if x]
    return []


def select_topic_recall_bullets(
    db: Session,
    *,
    topic_title: str,
    source_paragraphs: list[str],
    credential_provider_name: str,
    credential,
) -> tuple[list[str], bool]:
    """Call the AI to select verbatim bullets from the source for a topic.

    Returns (bullets, used_ai). used_ai=False means the AI was bypassed
    or fell back; the caller should use heuristic-only bullets instead.
    """
    if not source_paragraphs:
        return [], False
    total_chars = sum(len(p) for p in source_paragraphs)
    # AI selector needs enough material to be useful. Sub-100 char
    # sources are typically mnemonic lines or one-word headings; the
    # verbatim validator would also reject anything that short. We use
    # a soft threshold so we don't waste tokens on topics that have
    # only headings.
    if total_chars < 100:
        # Too short to be worth an AI call
        return [], False

    try:
        provider = get_study_pack_provider(credential_provider_name)
        prompt = build_topic_recall_prompt(topic_title, source_paragraphs)
        raw = provider.generate_text(prompt, credential)
        candidates = _parse_recall_bullets_json(raw)
        if not candidates:
            logger.warning(
                "revision_notes: AI returned no parseable bullets for topic %r",
                topic_title,
            )
            return [], False
        accepted = validate_verbatim_bullets(candidates, source_paragraphs)
        if len(accepted) < 3:
            logger.info(
                "revision_notes: AI returned %d parseable, %d verbatim (>=3 required) "
                "for topic %r — falling back to heuristic",
                len(candidates),
                len(accepted),
                topic_title,
            )
            return [], False
        return accepted, True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "revision_notes: AI selector failed for topic %r: %s",
            topic_title,
            exc,
        )
        return [], False


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------
@dataclass
class RevisionTopic:
    """One topic on the revision sheet.

    Holds the original heading + source paragraphs so the AI selector can
    pick verbatim bullets, plus the chosen recall bullets (AI-selected
    verbatim, or heuristic fallback when AI was skipped/failed).
    """

    title: str
    subtitle: Optional[str]
    sections: list[tuple[str, str]]  # (heading, kind) - kind in:
    #   "bullets" -> list of paragraphs under bullets()
    #   "callout" -> list of paragraphs under callout(title, ...)
    #   "table"   -> comparison-table rows

    # NEW: source paragraphs for this topic (used by AI selector + fallback).
    source_paragraphs: list[str] = None  # type: ignore[assignment]
    # NEW: AI-selected verbatim bullets; None means "not attempted yet".
    # Empty list means "AI attempted and produced nothing usable".
    ai_bullets: Optional[list[str]] = None
    # NEW: did AI help for this topic? drives the per-page footer.
    ai_used: bool = False

    def __post_init__(self) -> None:
        if self.source_paragraphs is None:
            self.source_paragraphs = []


@dataclass
class RevisionDoc:
    """Top-level revision document."""

    chapter_label: str
    title: str
    subtitle: str
    epigraph_title: str
    epigraph_lines: list[str]
    topics: list[RevisionTopic]
    final_sentence: str


# ---------------------------------------------------------------------------
# Heuristic organiser (no LLM)
# ---------------------------------------------------------------------------
_PAGE_BREAK_RE = re.compile(r"\f+", re.MULTILINE)


def _split_paragraphs(text: str) -> list[str]:
    """Split into non-empty paragraphs; preserve page-break markers as
    a sentinel so the renderer can flow content naturally."""
    out: list[str] = []
    for raw in _PAGE_BREAK_RE.split(text or ""):
        for p in (raw or "").split("\n\n"):
            p = p.strip()
            if p:
                out.append(p)
    return out


_HEADING_BLOCKLIST = {
    # Common article/connector words that, when present alongside an
    # otherwise-title-cased line, indicate it's a SUBTITLE/PHRASE rather
    # than a heading. A heading can have several of these (e.g. "Iron
    # Age and the Second Urbanisation"), so we use a soft rule: reject
    # only when >50% of words are common connectors, AND the line has
    # at least one strong connector (and/with/from/into).
    "the", "of", "and", "for", "with", "from", "into", "to", "a", "an",
    "in", "on", "at", "by", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "shall", "should", "may", "might", "must", "can", "could",
}
_STRONG_CONNECTORS = {"and", "with", "from", "into", "for", "of"}


def _looks_like_heading(line: str) -> bool:
    """A line that is short, mostly title-case or all-caps, no terminal
    punctuation — likely a heading.

    Three filters, applied in order:

    1. Length & punctuation: 4-90 chars, no terminal `.?!:`
    2. Title-case: ≥half the words start with uppercase
    3. Not a phrase: rejects lines where >50% of words are common
       connectors AND at least one strong connector is present
       (otherwise legitimate headings like "Innovations of the Age"
       would be wrongly dropped).
    """
    line = (line or "").strip()
    if not (4 <= len(line) <= 90):
        return False
    if line.endswith((".", "?", "!", ":")):
        return False
    words = line.split()
    if len(words) < 2:
        return False
    title_caps = sum(1 for w in words if w[:1].isupper())
    if title_caps < max(2, len(words) // 2):
        return False
    cleaned = [w.lower().strip(",.;:!?\"'()") for w in words]
    blocklist_hits = sum(1 for w in cleaned if w in _HEADING_BLOCKLIST)
    strong_hits = sum(1 for w in cleaned if w in _STRONG_CONNECTORS)
    # Reject if >=60% common connectors AND at least one strong connector.
    # 0.6 lets through "Innovations of the Age" (2/4=0.5) while still
    # filtering "From a 'dark age' back to cities" (3/6 strong).
    if (blocklist_hits / max(len(words), 1) >= 0.6) and strong_hits >= 1:
        return False
    return True


def _bucket_by_heading(source_text: str) -> dict[Optional[str], list[str]]:
    """Walk paragraphs and bucket them under detected headings.

    Handles two shapes:

    1. Heading + body in the same paragraph (markdown-ish).
    2. Heading on its own line, body in the next paragraph (NCERT-style).

    Returns a dict keyed by heading (None for the unnamed pre-heading
    bucket). Each value is the list of body paragraphs that belong to
    that heading. Headings with empty bodies get folded into the next
    heading so the topic list doesn't fragment into one-liner stubs.
    """
    paragraphs = _split_paragraphs(source_text)
    classified: list[tuple[str, str]] = []
    for para in paragraphs:
        first_line = para.split("\n", 1)[0].strip()
        is_h = _looks_like_heading(first_line)
        if is_h and len(para) > len(first_line) + 80:
            rest = para[len(first_line):].strip()
            classified.append(("h", first_line))
            classified.append(("b", rest))
        elif is_h:
            classified.append(("h", first_line))
        else:
            classified.append(("b", para))

    raw: list[tuple[Optional[str], str]] = []
    i = 0
    while i < len(classified):
        kind, txt = classified[i]
        if kind == "h":
            body = ""
            if i + 1 < len(classified) and classified[i + 1][0] == "b":
                body = classified[i + 1][1]
                i += 2
            else:
                i += 1
            raw.append((txt, body))
        else:
            raw.append((None, txt))
            i += 1

    # Fold entries with empty bodies into the next entry's bucket
    folded: list[tuple[Optional[str], list[str]]] = []
    j = 0
    while j < len(raw):
        h, body = raw[j]
        # Gather all consecutive empty-body headings, then attach to next
        # non-empty entry. This collapses mnemonic/one-liner headings
        # like "Magadha — Rajgir" into the surrounding topic.
        if not body:
            # Look ahead for the next entry with a body
            k = j + 1
            extras = []
            while k < len(raw) and not raw[k][1]:
                extras.append(raw[k][0])
                k += 1
            if k < len(raw):
                target_h, target_body = raw[k]
                # Merge: keep first non-None heading, prepend extras as body
                real_h = h if h else target_h
                merged_body = list(filter(None, [body])) + [
                    e for e in extras if e
                ] + ([target_body] if target_body else [])
                folded.append((real_h, merged_body))
                j = k + 1
                continue
            else:
                # No body follows - drop
                j = k
                continue
        folded.append((h, [body] if body else []))
        j += 1

    bucket: dict[Optional[str], list[str]] = {}
    for h, bodies in folded:
        bucket.setdefault(h, []).extend(b for b in bodies if b)
    return bucket


def heuristic_topics(
    text: str, *, max_topics: int = 9
) -> list[RevisionTopic]:
    """Walk paragraphs, group under detected headings.

    Falls back to chunked paragraph groups if not enough headings.
    """
    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return []

    bucket = _bucket_by_heading(text)
    named_buckets = [(k, v) for k, v in bucket.items() if k is not None]
    if len(named_buckets) < 4:
        # Not enough headings -> fall back to chunked paragraph groups
        return _chunked_topics(paragraphs, max_topics=max_topics)

    topics: list[RevisionTopic] = []
    for title, paras in named_buckets[:max_topics]:
        topics.append(
            RevisionTopic(
                title=P.sanitise_text(title),
                subtitle=(
                    P.sanitise_text(_first_meaningful(paras[0]))
                    if paras and paras[0]
                    else None
                ),
                sections=_digest_paras_to_sections(
                    [p for p in paras[1:] if p]
                ),
            )
        )
    return topics


def _first_meaningful(text: str) -> str:
    return P.sanitise_text((text or "").strip().split("\n", 1)[0])[:200]


def _chunked_topics(
    paragraphs: list[str], *, max_topics: int
) -> list[RevisionTopic]:
    """Used when heuristic heading detection failed. Group paragraphs
    into 6-9 topics by simple chunking."""
    cleaned = [
        P.sanitise_text(p) for p in paragraphs if p and len(p.strip()) >= 60
    ]
    if not cleaned:
        return []
    n = min(max_topics, max(4, len(cleaned) // 3 or 4))
    n = min(n, len(cleaned))
    chunk_size = max(1, len(cleaned) // n)
    topics: list[RevisionTopic] = []
    for i in range(n):
        start = i * chunk_size
        end = (i + 1) * chunk_size if i < n - 1 else len(cleaned)
        chunk = cleaned[start:end]
        if not chunk:
            continue
        title = f"Section {i + 1}"
        first = chunk[0]
        subtitle = re.split(r"(?<=[.!?])\s+", first)[0][:200]
        topics.append(
            RevisionTopic(
                title=title,
                subtitle=subtitle,
                sections=_digest_paras_to_sections(chunk),
            )
        )
    return topics


def _digest_paras_to_sections(paras: list[str]) -> list[tuple[str, str]]:
    """Turn a paragraph list into (heading, kind) tuples.

    Heuristics:
      * If 3 or more consecutive short paragraphs with similar opening
        words, treat as a bullet group.
      * If first paragraph looks like a definition ("X is Y."), wrap as
        a KEY-TERM callout.
      * Otherwise, treat as a body block under a generic "Notes" heading.
    """
    if not paras:
        return []

    out: list[tuple[str, str]] = []
    cur_body: list[str] = []

    def flush():
        nonlocal cur_body
        if not cur_body:
            return
        if len(cur_body) <= 6 and all(len(p) <= 250 for p in cur_body):
            out.append(("Notes", "bullets"))
        else:
            out.append(("Notes", "body"))
        cur_body = []

    first = paras[0]
    # KEY-TERM callout if first paragraph looks like a definition.
    if re.match(r"^[A-Z][\w'\-]{1,40}\s+(?:is|are|refers? to|means?)\b", first):
        keyword = first.split(" ", 1)[0]
        # Pull a one-line summary + remaining bullets
        summary = first[:240]
        rest = paras[1:]
        cur_pairs: list[str] = [summary]
        for r in rest[:8]:
            if len(r) <= 200:
                cur_pairs.append(r)
        out.append((f"Key term — {keyword}", "callout"))
        # The first item in cur_pairs goes into the callout title block;
        # the rest goes into "Notes" bullets handled below.
        out.append(("Notes", "bullets"))
        cur_body = list(cur_pairs)
        flush()
    else:
        cur_body = list(paras)
        flush()
    return out


# ---------------------------------------------------------------------------
# Document builder
# ---------------------------------------------------------------------------
def _build_revision_doc(
    *,
    deck_name: str,
    chapter_label: str,
    source_title: str,
    subtitle: str,
    topics: list[RevisionTopic],
    final_sentence: str,
) -> RevisionDoc:
    epigraph_title = "What this chapter is about"
    epigraph_lines = [
        P.sanitise_text(
            f"These notes summarise {source_title} into {len(topics)} "
            "high-yield topics, organised so each one fits cleanly on an "
            "A4 page. Use them as a revision sheet before tests."
        )
    ]
    return RevisionDoc(
        chapter_label=P.sanitise_text(chapter_label),
        title=P.sanitise_text(deck_name),
        subtitle=P.sanitise_text(subtitle),
        epigraph_title=epigraph_title,
        epigraph_lines=epigraph_lines,
        topics=topics,
        final_sentence=P.sanitise_text(final_sentence),
    )


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------
def render_revision_pdf(doc: RevisionDoc, deck_name: str) -> tuple[bytes, int]:
    """Render the doc to bytes via the low-ink palette.

    Returns (pdf_bytes, page_count).
    """
    from reportlab.platypus import (  # noqa: F401
        HRFlowable,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )
    from reportlab.lib.units import cm  # noqa: F401

    buf = io.BytesIO()
    doc_template = SimpleDocTemplate(
        buf,
        pagesize=P.PAGESIZE,
        leftMargin=P.MARGIN_L,
        rightMargin=P.MARGIN_R,
        topMargin=P.MARGIN_T,
        bottomMargin=P.MARGIN_B,
        title=f"{doc.chapter_label} Revision Notes - {doc.title}",
        author="EduViz",
    )

    story: list = []

    # ---------- Cover ----------
    story.append(P.Paragraph(f"{doc.chapter_label.upper()}", P.SECT_TAG))
    story.append(P.Paragraph(f"{doc.chapter_label} revision notes", P.SMALL_MUTED))
    story.append(Spacer(1, 0.4 * cm))
    story.append(P.Paragraph(doc.title, P.H1))
    story.append(
        P.Paragraph(
            "Revision sheet generated from your deck \u2014 low-ink print version",
            P.SMALL_MUTED,
        )
    )
    story.append(Spacer(1, 0.15 * cm))
    if doc.subtitle:
        story.append(
            P.Paragraph(
                doc.subtitle,
                P.SUB_STYLE,
            )
        )
    from reportlab.platypus import HRFlowable as _HR  # noqa: F401

    story.append(
        _HR(
            width="100%",
            thickness=0.9,
            color=P.ACCENT_LINE,
            spaceBefore=2,
            spaceAfter=8,
        )
    )

    # ---------- Contents (hairline-framed, no fill) ----------
    story.append(P.Paragraph("What's inside", P.H3))
    contents_lines = [
        f"<b>{i + 1}.</b> {t.title}" for i, t in enumerate(doc.topics)
    ]
    contents_box = P.Table(
        [[P.Paragraph(line, P.BULLET)] for line in contents_lines],
        colWidths=[P.BODY_WIDTH],
    )
    contents_box.setStyle(
        P.TableStyle(  # type: ignore[arg-type]
            [
                ("BOX", (0, 0), (-1, -1), 0.4, P.SOFT_RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(contents_box)
    story.append(Spacer(1, 0.2 * cm))

    # ---------- Epigraph callout ----------
    story.append(
        P.callout(
            doc.epigraph_title,
            doc.epigraph_lines,
            icon="EPIGRAPH ·",
        )
    )
    story.append(Spacer(1, 0.15 * cm))

    # ---------- Per-topic rendering ----------
    # Each topic renders either:
    #   * AI-selected verbatim bullets (if select_topic_recall_bullets
    #     succeeded) - rendered with a left quote-mark to flag them as
    #     direct source quotes, OR
    #   * Heuristic payload (first line as subtitle, rest as bullets) -
    #     rendered normally.
    any_ai_used = any(getattr(t, "ai_used", False) for t in doc.topics)
    for topic in doc.topics:
        for flow in P.topic_banner(topic.title, topic.subtitle):
            story.append(flow)
        if getattr(topic, "ai_used", False) and topic.ai_bullets:
            story.append(
                P.Paragraph(
                    "<i>Key points — verbatim from your source:</i>",
                    P.SMALL_MUTED,
                )
            )
            story.append(_verbatim_bullets_flowable(topic.ai_bullets))
        else:
            payload = getattr(topic, "_payload", {})
            flat_lines = _flatten_payload(payload)
            if not flat_lines and topic.sections:
                flat_lines = [f"{h}." for h, _ in topic.sections]
            if flat_lines:
                short = [ln for ln in flat_lines if len(ln) <= 220]
                long = [ln for ln in flat_lines if len(ln) > 220]
                if short:
                    story.append(P.bullets(short))
                for ln in long:
                    story.append(P.Paragraph(ln, P.BODY))
        story.append(Spacer(1, 0.2 * cm))

    # ---------- Quick-recall footer (no fill, hairline box) ----------
    if doc.topics:
        story.append(P.section_rule())
        story.append(P.Paragraph("Quick recall", P.H3))
        recall_lines = [f"• {t.title}" for t in doc.topics]
        recall_box = P.Table(
            [[P.Paragraph(line, P.CALLOUT)] for line in recall_lines],
            colWidths=[P.BODY_WIDTH],
        )
        recall_box.setStyle(
            P.TableStyle(  # type: ignore[arg-type]
                [
                    ("BOX", (0, 0), (-1, -1), 0.4, P.SOFT_RULE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(recall_box)

    # ---------- Final sentence ----------
    # Wrap in KeepTogether so it doesn't orphan to a half-empty page
    # if the topic section ends near a page break.
    if doc.final_sentence:
        from reportlab.platypus import KeepTogether
        story.append(Spacer(1, 0.3 * cm))
        story.append(
            KeepTogether([
                P.Paragraph(f"<i>{doc.final_sentence}</i>", P.SMALL_MUTED)
            ])
        )

    # ---------- Build with footer ----------
    # Decide the honesty footer text once per render based on whether
    # ANY topic in the doc used AI selection. Mixed docs (some AI, some
    # heuristic) still get the AI footer so the user knows AI touched it
    # at all - the per-topic quote marks already flag which parts were
    # AI-selected.
    if any_ai_used:
        footer_text = (
            "Key points extracted verbatim from your source using AI "
            "selection. No AI rewriting - verify against original if uncertain."
        )
    else:
        footer_text = (
            "Key points are direct excerpts from your source text. "
            "AI-assisted extraction was not used for this file."
        )

    def on_page(canvas, document):
        canvas.saveState()
        canvas.setFont(P.FONT, 7.5)
        canvas.setFillColor(P.PAGE_NUM)
        canvas.drawCentredString(
            P.PAGESIZE[0] / 2.0,
            0.55 * 28.35,  # 0.55 cm from bottom
            footer_text,
        )
        canvas.drawRightString(
            P.PAGESIZE[0] - P.MARGIN_R,
            P.PAGESIZE[1] - P.MARGIN_T + 0.25 * 28.35,  # top-right
            f"{doc.chapter_label} \u00b7 p {document.page}",
        )
        canvas.restoreState()

    doc_template.build(story, onFirstPage=on_page, onLaterPages=on_page)
    buf.seek(0)
    # Cheap page count: ask the rendered PDF for /Count via PyPDF.
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(buf.getvalue()))
        page_count = len(reader.pages)
    except Exception:  # pragma: no cover
        page_count = 0
    return buf.getvalue(), page_count


def _section_lines(
    topic: RevisionTopic, heading: str, kind: str
) -> list[str]:
    """Pull lines for a section. Heading is used as a marker; the real
    content lives in topic.sections[heading, *]. For the heuristic
    organiser, multiple sections share the same heading, so we flatten
    and dedupe across all matching tuples."""
    seen: set[str] = set()
    out: list[str] = []
    for h, k in topic.sections:
        if k != kind:
            continue
        payload = getattr(topic, "_payload", {}).get((h, k), [])
        for line in payload:
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                out.append(line)
    return out


def _flatten_payload(payload: dict[tuple[str, str], list[str]]) -> list[str]:
    """Return a flat list of paragraphs for the renderer.

    De-dupes trivial duplicates and drops anything below 25 chars so the
    page does not collapse into a sea of one-word bullets.
    """
    seen: set[str] = set()
    out: list[str] = []
    for lines in payload.values():
        for line in lines:
            line = line.strip()
            if len(line) < 25:
                continue
            key = P.sanitise_text(line)[:120]
            if key in seen:
                continue
            seen.add(key)
            out.append(line)
    return out


def _verbatim_bullets_flowable(bullets: list[str]):
    """Render AI-selected bullets as a single ``KeepTogether`` block of
    paragraphs with a leading typographic quote-mark, so the user can
    see at a glance that these are direct quotes from the source.

    Returns a single flowable (not a list) so it can be appended
    directly to a ReportLab story.

    We do NOT use ListFlowable bullets here because the hairline list
    markers in our palette are designed for short bullets; verbatim
    sentences can run long and we want them justified like body text.
    """
    from reportlab.platypus import KeepTogether, Paragraph, Spacer

    quote_style = P.ParagraphStyle(
        "verbatim_quote",
        parent=P.BODY,
        leftIndent=14,
        rightIndent=4,
        fontSize=9.8,
        leading=13.5,
        spaceAfter=3,
    )
    items = []
    for line in bullets:
        text = (line or "").strip()
        if not text:
            continue
        items.append(Paragraph(f'&ldquo;{P.sanitise_text(text)}&rdquo;', quote_style))
    if not items:
        return Spacer(1, 0.01 * 28.35)
    return KeepTogether(items)


def _store_section_payload(
    topic: RevisionTopic, payloads: dict[tuple[str, str], list[str]]
) -> None:
    """Persist inline payload for the section renderer to pull."""
    setattr(topic, "_payload", payloads)


def build_topic_payload_map(
    paras: list[str],
) -> dict[tuple[str, str], list[str]]:
    """Build the (heading, kind) -> lines map for use at render time.

    Mirrors the section decisions in _digest_paras_to_sections but keeps
    the actual line contents so the renderer can put text on the page.
    """
    out: dict[tuple[str, str], list[str]] = {}
    if not paras:
        return out
    first = paras[0]
    if re.match(r"^[A-Z][\w'\-]{1,40}\s+(?:is|are|refers? to|means?)\b", first):
        lines = [first] + list(paras[1:])
        out[("Notes", "bullets")] = lines
    else:
        lines = list(paras)
        if len(lines) <= 6 and all(len(p) <= 250 for p in lines):
            out[("Notes", "bullets")] = lines
        else:
            out[("Notes", "body")] = lines
    return out


# ---------------------------------------------------------------------------
# High-level entry: orchestrate extraction -> render -> persist
# ---------------------------------------------------------------------------
def _read_storage_bytes(storage_key: str) -> bytes:
    storage = get_storage()
    data, _ctype = storage.open_bytes(key=storage_key)
    return data


def _decode_pdf_to_text(pdf_bytes: bytes) -> str:
    """Re-use the existing extraction helper from bulk_ai_upload."""
    from app.api.routers.bulk_ai_upload import extract_text_from_pdf

    return extract_text_from_pdf(pdf_bytes)


def _extracted_payload(file_row: BulkAIUploadFile) -> str:
    """Get the source text. Prefer the DB column (already in memory);
    fall back to re-reading from storage if needed."""
    if getattr(file_row, "content_text", None):
        return file_row.content_text or ""
    if getattr(file_row, "storage_key", None):
        try:
            data = _read_storage_bytes(file_row.storage_key)
            if (file_row.storage_key or "").lower().endswith(".pdf"):
                return _decode_pdf_to_text(data)
            return data.decode("utf-8", errors="replace")
        except StorageError as exc:
            logger.warning(
                "revision_notes: storage read failed for %s: %s",
                file_row.storage_key,
                exc,
            )
    return ""


def generate_revision_notes_for_child(
    db: Session,
    *,
    child_file_id: uuid.UUID,
    note: BulkAIUploadRevisionNote,
    credential_provider_name: str | None = None,
    credential=None,
) -> BulkAIUploadRevisionNote:
    """Render the revision-notes PDF for a child and persist it.

    Optional ``credential_provider_name`` and ``credential`` enable the
    AI-as-selector pass. When omitted, the function falls back to
    heuristic-only bullets for every topic.
    """
    from app.models import BulkAIUploadChildFile

    # Mark processing
    child = db.get(BulkAIUploadChildFile, child_file_id)
    if child is None:
        note.status = BulkRevisionNoteStatus.FAILED.value
        note.error_message = "Child file not found"
        db.commit()
        return note
    deck = db.get(Deck, child.bulk_upload.deck_id if child.bulk_upload else None) \
        if child.bulk_upload else db.get(Deck, note.deck_id)
    if deck is None:
        deck = db.get(Deck, note.deck_id)
    if deck is None:
        note.status = BulkRevisionNoteStatus.FAILED.value
        note.error_message = "Deck not found"
        db.commit()
        return note

    note.status = BulkRevisionNoteStatus.PROCESSING.value
    note.started_at = datetime.utcnow()
    db.commit()

    attempt = child.latest_attempt
    if attempt is None:
        note.status = BulkRevisionNoteStatus.FAILED.value
        note.error_message = "No successful attempt for this child"
        db.commit()
        return note

    source_text = _extracted_payload(attempt)
    if not source_text or len(source_text.strip()) < 200:
        note.status = BulkRevisionNoteStatus.FAILED.value
        note.error_message = (
            "Source text too short to build notes from "
            f"({len(source_text)} chars)."
        )
        db.commit()
        return note

    # Heuristic topic extraction. Topic titles come from real headings.
    raw_topics = heuristic_topics(source_text, max_topics=9)
    topics: list[RevisionTopic] = []
    for t in raw_topics:
        if not t.sections:
            continue
        topics.append(t)
    if not topics:
        note.status = BulkRevisionNoteStatus.FAILED.value
        note.error_message = "Could not extract topics from source text"
        db.commit()
        return note

    # Attach full source paragraphs to each topic (used by AI selector).
    topics = _attach_payloads(source_text, topics)

    # AI-as-selector pass: per topic, ask the model to PICK 3-5 verbatim
    # sentences from the source. Validator enforces they appear in source.
    if credential_provider_name and credential is not None:
        for t in topics:
            bullets, used = select_topic_recall_bullets(
                db,
                topic_title=t.title,
                source_paragraphs=t.source_paragraphs,
                credential_provider_name=credential_provider_name,
                credential=credential,
            )
            if used and bullets:
                t.ai_bullets = bullets
                t.ai_used = True
            else:
                t.ai_bullets = []
                t.ai_used = False
        ai_topics_used = sum(1 for t in topics if t.ai_used)
        logger.info(
            "revision_notes: AI selector used for %d/%d topics on child %s",
            ai_topics_used,
            len(topics),
            child_file_id,
        )

    note.topic_count = len(topics)

    subtitle = ""
    if attempt.extracted_description:
        subtitle = attempt.extracted_description[:240]
    elif deck.description:
        subtitle = deck.description[:240]

    chapter_label = (
        attempt.extracted_title
        or child.display_title
        or child.original_filename.rsplit(".", 1)[0]
    )[:80]

    doc = _build_revision_doc(
        deck_name=deck.name or chapter_label,
        chapter_label=chapter_label,
        source_title=chapter_label,
        subtitle=subtitle,
        topics=topics,
        final_sentence=f"Generated on {datetime.utcnow().strftime('%Y-%m-%d')} from your uploaded deck.",
    )

    try:
        pdf_bytes, page_count = render_revision_pdf(doc, deck.name or chapter_label)
    except Exception as exc:  # noqa: BLE001
        logger.exception("revision_notes: render failed for child %s", child_file_id)
        note.status = BulkRevisionNoteStatus.FAILED.value
        note.error_message = f"PDF render failed: {exc}"
        db.commit()
        return note

    storage = get_storage()
    storage_key = (
        f"bulk_uploads/{child.bulk_upload_id}/revision_notes/"
        f"{child_file_id}.pdf"
    )
    content_type = guess_content_type(".pdf") or "application/pdf"
    try:
        storage.save_bytes(
            key=storage_key,
            data=pdf_bytes,
            content_type=content_type,
        )
    except StorageError as exc:
        logger.exception("revision_notes: storage write failed for %s", storage_key)
        note.status = BulkRevisionNoteStatus.FAILED.value
        note.error_message = f"Storage write failed: {exc}"
        db.commit()
        return note

    note.storage_key = storage_key
    note.file_size = len(pdf_bytes)
    note.page_count = page_count
    note.status = BulkRevisionNoteStatus.READY.value
    note.completed_at = datetime.utcnow()
    db.commit()
    logger.info(
        "revision_notes: ready child=%s bytes=%s pages=%s topics=%s",
        child_file_id,
        len(pdf_bytes),
        page_count,
        note.topic_count,
    )
    return note


def _attach_payloads(
    source_text: str, topics: list[RevisionTopic]
) -> list[RevisionTopic]:
    """Walk the source again, capture each topic's paragraph list so the
    renderer can put real text on the page. Returns topics with
    ``_payload`` attribute populated, and ``source_paragraphs`` set for
    AI selection.
    """
    bucket = _bucket_by_heading(source_text)
    named = [(k, v) for k, v in bucket.items() if k is not None]
    by_title = {k: v for k, v in named}

    for t in topics:
        paras_for_topic = by_title.get(t.title, [])
        # Filter empty entries (paired heading with no body)
        paras_for_topic = [p for p in paras_for_topic if p]
        t.source_paragraphs = list(paras_for_topic)
        _store_section_payload(t, build_topic_payload_map(paras_for_topic))
    return topics
