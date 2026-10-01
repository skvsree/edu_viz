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

# A page often prints its section header on the same extracted line as the title
# ("My Bicycle Let us recite"), so the header is stripped off the end of a title.
_SECTION_LABELS = (
    "let us recite", "let us read", "let us speak", "let us write", "let us draw",
    "let us talk", "let us sing", "let us do", "new words", "sight words",
)
# The shortest thing left after stripping a header that is still worth keeping.
MIN_TITLE_LENGTH = 3

# AI/fallback titles carry their own numbering ("Chapter 03 - Between Home and
# School"), which for a book that restarts numbering inside every unit is often
# the wrong number — it is dropped when the unit is prefixed instead.
_LEADING_NUMBERING = re.compile(
    r"^\s*(?:chapter|unit|lesson)\s+\d{1,3}\s*[-–—:.]\s*", re.IGNORECASE
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


def _collapse_doubled(value: str) -> str:
    """Undo pypdf's doubled-run artefacts: ``Unit 5Unit 5`` -> ``Unit 5``.

    NCERT's *Mridang II* draws some runs twice, so the extracted text repeats a
    whole line (``Unit 5Unit 5``, ``Picture ReadingPicture Reading``) — and a
    doubled navigation label would otherwise look like no label at all.
    """
    for _ in range(4):
        half, remainder = divmod(len(value), 2)
        if not remainder and half >= MIN_TITLE_LENGTH and value[:half] == value[half:]:
            value = value[:half]
            continue
        break
    return value


def _clean(line: str | None) -> str:
    """Collapse whitespace and undo the doubled-run artefacts."""
    return _collapse_doubled(re.sub(r"\s+", " ", (line or "")).strip())


def _strip_section_label(title: str) -> str:
    """``My Bicycle Let us recite`` -> ``My Bicycle``."""
    lowered = title.lower()
    for label in _SECTION_LABELS:
        if lowered.endswith(label):
            trimmed = title[: len(title) - len(label)].strip(" -–—·:|.")
            if len(trimmed) >= MIN_TITLE_LENGTH:
                return trimmed
    return title


def _title_text(line: str | None) -> str:
    """The printable title form of an extracted line."""
    return _strip_section_label(_clean(line))[:MAX_TITLE_LENGTH]


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
        return _title_text(candidate)
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
                candidate = _title_text(inline.group(2))
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
                candidate = _title_text(inline.group(2))
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


def unit_prefix(block: NavBlock | None, carried_unit: int | None = None) -> str | None:
    """``Unit 2`` — the unit as label + number, or ``None`` when it is unknown."""
    unit = block.unit_no if block is not None and block.unit_no is not None else carried_unit
    return f"Unit {unit}" if unit is not None else None


def build_deck_title(block: NavBlock | None, carried_unit: int | None = None) -> str | None:
    """Render ``Unit 2 · Ch 1 · Picture Time``.

    ``carried_unit`` supplies the unit for a chapter whose PDF omits the unit
    line; callers walking a book in order pass the last unit they saw.
    """
    if block is None or block.is_empty:
        return None

    parts: list[str] = []
    prefix = unit_prefix(block, carried_unit)
    if prefix:
        parts.append(prefix)
    if block.chapter_no is not None:
        parts.append(f"Ch {block.chapter_no}")
    if block.chapter_title:
        parts.append(block.chapter_title)
    elif block.unit_title and block.chapter_no is None:
        parts.append(block.unit_title)

    title = TITLE_SEPARATOR.join(parts).strip()
    return title[:MAX_TITLE_LENGTH] or None


def compose_deck_title(block: NavBlock | None, *, carried_unit: int | None = None,
                       derived_title: str | None = None) -> str | None:
    """The deck title: the printed block when it names the chapter, else a prefix.

    Call this once the derived (AI/fallback) title is known. Some chapter PDFs
    print no usable ``Chapter N`` line at all — Class II *Mridang*'s chapters 6
    and 7 extract as ``Chapter 2Chapter 2 Let us read`` — so the only navigation
    fact left is the unit carried from the unit's opening chapter. Those decks
    must read ``Unit 3 · Between Home and School``, not keep the AI's wrong
    ``Chapter 03 - Between Home and School`` (and not lose the chapter's name to
    the unit's theme, which is what a plain block render would do).
    """
    printed = build_deck_title(block, carried_unit=carried_unit)
    if block is not None and block.chapter_title:
        # The page printed its own chapter title; that title wins outright.
        return printed
    unit = unit_prefix(block, carried_unit)
    if unit is None or not derived_title:
        return printed
    cleaned = _LEADING_NUMBERING.sub("", derived_title).strip()
    if len(cleaned) < MIN_TITLE_LENGTH:
        return printed
    return f"{unit}{TITLE_SEPARATOR}{cleaned}"[:MAX_TITLE_LENGTH]


def derive_nav_title(text: str | None, carried_unit: int | None = None) -> tuple[str | None, int | None]:
    """Convenience wrapper: ``(title, unit_no)`` for the caller's carry-forward.

    ``unit_no`` is the unit the document itself declared, or ``None`` when it
    declared none — callers keep their previous value in that case.
    """
    block = extract_nav_block(text)
    if block is None:
        return None, None
    return build_deck_title(block, carried_unit=carried_unit), block.unit_no
