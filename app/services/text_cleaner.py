"""Zero-config NCERT text cleaning for the edu_viz revision pipeline.

Strips the artifact classes that PDF extraction (pdftotext -layout or
pypdf) leaves behind in NCERT textbook text:

  1. Running page headers / mastheads / footers that repeat the same
     short standalone line on >=30% of pages (e.g. "Prime Time",
     "Curiosity | Textbook of Science | Grade 6", "9 - Family and
     Community"). Detected by page frequency — NO per-book regexes.
  2. InDesign metadata: "Chapter 6.indd 101", "Reprint 2025-26",
     timestamps in both DD-MM-YYYY HH:MM:SS and MM/DD/YYYY h:mm:ss AM/PM.
  3. Lone page numbers and purely-numeric figure rows (number grids,
     peg diagrams). Lines with 'x', '=' or letters are KEPT (real math
     content like "72 = 2 x 2 x 3 x 2 x 3").
  4. Form-feed page breaks are normalised to paragraph breaks.

Page-frequency detection is page-marker-agnostic: if the text has '\f'
form feeds (pdftotext) they define pages; otherwise (pypdf) the text is
chunked into ~40-line pseudo-pages so the >=30% threshold still works.
"""
from __future__ import annotations

import re
from collections import Counter

# Timestamps: DD-MM-YYYY HH:MM:SS  OR  MM/DD/YYYY h:mm:ss AM/PM
_TS_RE = re.compile(
    r"\d{1,2}[/-]\d{1,2}[/-]\d{4}\s+\d{1,2}:\d{2}:\d{2}(\s*(AM|PM))?"
)
_LINES_PER_PSEUDO_PAGE = 40


def _split_pages(raw: str) -> list[str]:
    """Split text into page-sized units for frequency counting.

    Prefers real '\f' page breaks (pdftotext -layout output). When the
    extractor does not emit form feeds (pypdf), chunk by line count so
    the >=30%-of-pages heuristic still has meaningful units.
    """
    if "\f" in raw:
        return [p for p in raw.split("\f") if p.strip()]
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return []
    chunks: list[str] = []
    for i in range(0, len(lines), _LINES_PER_PSEUDO_PAGE):
        chunks.append("\n".join(lines[i:i + _LINES_PER_PSEUDO_PAGE]))
    return chunks


def _detect_repeated_lines(raw: str) -> set[str]:
    """Running headers/mastheads/footers: short standalone header-like
    lines appearing on >=30% of pages (or pseudo-pages). Real sub-headings
    rarely repeat this much; page headers always do."""
    pages = _split_pages(raw)
    if not pages:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        seen: set[str] = set()
        for line in page.splitlines():
            s = line.strip()
            if not s or len(s) > 80:
                continue
            if s.endswith((".", "!", ":", ",")):
                continue
            if re.fullmatch(r"[\d\s]+", s):  # pure numbers handled elsewhere
                continue
            words = s.split()
            if len(words) > 8:
                continue
            # Header-like: starts uppercase / all-caps / starts with digit
            if not (s[0].isupper() or s.isupper() or s[0].isdigit()):
                continue
            seen.add(s)
        for s in seen:
            counts[s] += 1
    threshold = max(2, len(pages) // 3)  # >= ~30% of pages
    return {s for s, n in counts.items() if n >= threshold}


def clean_ncert_text(raw: str) -> str:
    """Strip pdftotext/pypdf artifacts from extracted NCERT text."""
    if not raw:
        return ""
    # Footer + InDesign metadata (generic: both timestamp formats).
    # .indd markers vary: "Chapter 6.indd 101", "chapter9.indd 137",
    # "Chapter 5_Prime Time.indd 107" — case-insensitive, number may be
    # glued to the word or separated by a space.
    raw = re.sub(r"Reprint \d{4}-\d{2}", "", raw)
    raw = re.sub(
        r"chapter\s*\d+(?:_[A-Za-z0-9 ]+)?\.indd\s*\d+", "", raw, flags=re.I
    )
    raw = _TS_RE.sub("", raw)
    # Auto-detected running headers / mastheads / footers (>=30% of pages)
    for line in sorted(_detect_repeated_lines(raw), key=len, reverse=True):
        raw = re.sub(rf"(?m)^\s*{re.escape(line)}\s*$", "", raw)
    # Lone page numbers
    raw = re.sub(r"^\s*\d{1,3}\s*$", "", raw, flags=re.M)
    # Purely-numeric figure rows (number grids, peg diagrams). Keep lines
    # with x, = or letters (real content like "72 = 2 x 2 x 3 x 2 x 3").
    raw = re.sub(r"^\s*[\d\s]+\s*$", "", raw, flags=re.M)
    # Form feeds -> paragraph break
    raw = re.sub(r"\f+", "\n\n", raw)
    # Collapse blank-line runs
    raw = re.sub(r"[ \t]+\n", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()
