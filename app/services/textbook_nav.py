"""Deterministic deck titles taken from a textbook's own navigation block.

Why this exists
---------------
The deck name for an uploaded chapter PDF used to come from an AI title (or a
generic "best line" fallback), and both of them pick whatever heading looks
promising. On NCERT *Mridang* (Class I English) that produced names like
"Chapter 02 - Life Around Us" — the **unit** number glued onto the unit's title,
for a chapter that is actually *Chapter 1 of Unit 2* — and four different decks
all reading "Chapter 2", because Mridang restarts chapter numbering inside every
unit (Unit 1: 1-2, Unit 2: 1-3, Unit 3: 1-2, Unit 4: 1-2).

A textbook page prints its own identity in the navigation block::

    Let us speak
    Unit 2
    Life Around Us
    Chapter 1
    Picture Time

so the title can be read off the text instead of guessed:

    Unit 2 · Ch 1 · Picture Time

The unit line is the one piece that a chapter PDF sometimes omits (a chapter
can start with running text and print only ``Chapter 2 / Greetings``). Callers
that walk a book in order therefore carry the last seen unit forward —
see ``build_deck_title(block, carried_unit=...)``.

Deliberately NOT used as a source: the InDesign slug that most NCERT PDFs carry
on every page (``Chapter 2.indd 47 12-01-2024 04:36:26``). It follows the source
document grouping, not the printed chapter: in ``aemr103.pdf`` every page says
``Chapter 2`` while the navigation block correctly says *Unit 2 / Chapter 1*.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TITLE_SEPARATOR = " · "
MAX_TITLE_LENGTH = 250

# A navigation line is the label alone on its own line; the next line is the title.
_UNIT_LINE = re.compile(r"^unit\s+(\d{1,2})$", re.IGNORECASE)
_CHAPTER_LINE = re.compile(r"^(?:chapter|lesson)\s+(\d{1,3})$", re.IGNORECASE)
# Some books put label and title on one line: "Chapter 2 - Greetings".
_INLINE_CHAPTER = re.compile(
    r"^(?:chapter|lesson)\s+(\d{1,3})\s*[:\-–—.]\s*(\S.*)$", re.IGNORECASE
)
_INLINE_UNIT = re.compile(r"^unit\s+(\d{1,2})\s*[:\-–—.]\s*(\S.*)$", re.IGNORECASE)

# Lines that are page furniture rather than a title.
_NOISE_PATTERNS = (
    re.compile(r"^\d{1,4}$"),  # a bare page number
    re.compile(r"indd", re.IGNORECASE),
    re.compile(r"^reprint", re.IGNORECASE),
    re.compile(r"^isbn\b", re.IGNORECASE),
)

# Only the head of the document is searched: the navigation block is printed at
# the start of a chapter, and mid-document text can legitimately mention a
# chapter number.
NAV_SEARCH_LINES = 400


@dataclass(frozen=True)
class NavBlock:
    """The identity a textbook page prints about itself."""

    unit_no: int | None = None
    unit_title: str | None = None
    chapter_no: int | None = None
    chapter_title: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.chapter_no is None and self.unit_no is None


def _clean(line: str | None) -> str:
    return re.sub(r"\s+", " ", (line or "")).strip()


def _is_noise(line: str) -> bool:
    if not line or len(line) < 3:
        return True
    if any(pattern.search(line) for pattern in _NOISE_PATTERNS):
        return True
    # A title has at least one letter.
    return not re.search(r"[A-Za-z\u0900-\u097F]", line)


def _next_title(lines: list[str], index: int) -> str | None:
    """The first non-furniture line after ``index`` (usually index + 1)."""
    for candidate in lines[index + 1: index + 4]:
        cleaned = _clean(candidate)
        if _is_noise(cleaned):
            continue
        return cleaned[:MAX_TITLE_LENGTH]
    return None


def extract_nav_block(text: str | None) -> NavBlock | None:
    """Read ``Unit N`` / ``Chapter N`` (+ their titles) off the document head.

    Returns ``None`` when the document prints no usable navigation block, which
    is the signal for callers to keep their existing title derivation.

    Page furniture is skipped outright — most NCERT PDFs carry an InDesign slug
    on every page (``Chapter 1.indd 1 1/17/2025 12:09:01 PM``) whose number
    follows the source document, not the printed chapter, and whose appearance
    order relative to the real navigation block is not stable.
    """
    if not text:
        return None
    lines = [_clean(line) for line in text.splitlines()]
    lines = [line for line in lines if line][:NAV_SEARCH_LINES]

    unit_no = unit_title = chapter_no = chapter_title = None

    for index, line in enumerate(lines):
        if _is_noise(line):
            continue
        if unit_no is None:
            match = _UNIT_LINE.match(line)
            if match:
                unit_no = int(match.group(1))
                unit_title = _next_title(lines, index)
                continue
            inline = _INLINE_UNIT.match(line)
            if inline:
                candidate = _clean(inline.group(2))[:MAX_TITLE_LENGTH]
                if candidate and not _is_noise(candidate):
                    unit_no = int(inline.group(1))
                    unit_title = candidate
                continue
        if chapter_no is None:
            match = _CHAPTER_LINE.match(line)
            if match:
                chapter_no = int(match.group(1))
                chapter_title = _next_title(lines, index)
                continue
            inline = _INLINE_CHAPTER.match(line)
            if inline:
                candidate = _clean(inline.group(2))[:MAX_TITLE_LENGTH]
                if candidate and not _is_noise(candidate):
                    chapter_no = int(inline.group(1))
                    chapter_title = candidate
        if unit_no is not None and chapter_no is not None:
            break

    block = NavBlock(
        unit_no=unit_no,
        unit_title=unit_title,
        chapter_no=chapter_no,
        chapter_title=chapter_title,
    )
    if block.is_empty:
        return None
    # A chapter number without a title is not an improvement on the AI title —
    # that is the Joyful Mathematics shape (slug-only numbering).
    if block.chapter_no is not None and not block.chapter_title:
        return None
    if block.chapter_no is None and not block.unit_title:
        return None
    return block


def build_deck_title(block: NavBlock | None, carried_unit: int | None = None) -> str | None:
    """Render ``Unit 2 · Ch 1 · Picture Time``.

    ``carried_unit`` supplies the unit for a chapter whose PDF omits the unit
    line; callers walking a book in order pass the last unit they saw.
    """
    if block is None or block.is_empty:
        return None

    parts: list[str] = []
    unit = block.unit_no if block.unit_no is not None else carried_unit
    if unit is not None:
        parts.append(f"Unit {unit}")
    if block.chapter_no is not None:
        parts.append(f"Ch {block.chapter_no}")
    if block.chapter_title:
        parts.append(block.chapter_title)
    elif block.unit_title and block.chapter_no is None:
        parts.append(block.unit_title)

    title = TITLE_SEPARATOR.join(parts).strip()
    return title[:MAX_TITLE_LENGTH] or None


def derive_nav_title(text: str | None, carried_unit: int | None = None) -> tuple[str | None, int | None]:
    """Convenience wrapper: ``(title, unit_no)`` for the caller's carry-forward.

    ``unit_no`` is the unit the document itself declared, or ``None`` when it
    declared none — callers keep their previous value in that case.
    """
    block = extract_nav_block(text)
    if block is None:
        return None, None
    return build_deck_title(block, carried_unit=carried_unit), block.unit_no
