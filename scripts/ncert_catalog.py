"""NCERT online-textbook catalogue: fetch + parse ``ncert.nic.in/textbook.php``.

The NCERT textbook browser is a single server-rendered PHP page whose entire
class -> subject -> book tree lives in **inline JavaScript**:

* ``function change()``          -> populates the subject <select> per class
* ``function change1(sind)``     -> populates the book <select> per (class, subject)

Every book option carries a ``.text`` (title) and a ``.value`` (a relative URL of
the form ``textbook.php?<book_code>=0-<n>``). The book page itself then emits one
row per section (Prelims, chapters, ...), each linking to
``textbook.php?<book_code>=<key>-<n>``.

Book-code layout (verified across all 1116 books in the 2026-09-18 snapshot)::

    <class letter><language><book abbrev><part>
       a=Class I ... l=Class XII
       e=English, h=Hindi, u=Urdu, mr=Marathi, sk=Sanskrit, ...

English-medium selection is therefore ``book_code[1] == "e"``.

This module is deliberately free of DB and HTTP side effects so the parsers can
be unit-tested against saved HTML. Fetching is isolated in ``fetch_html``.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BASE_URL = "https://ncert.nic.in"

CLASS_NAMES: dict[int, str] = {
    1: "Class I",
    2: "Class II",
    3: "Class III",
    4: "Class IV",
    5: "Class V",
    6: "Class VI",
    7: "Class VII",
    8: "Class VIII",
    9: "Class IX",
    10: "Class X",
    11: "Class XI",
    12: "Class XII",
    13: "Class XI & XII Combined",
}

#: NCERT codes two Hindi-medium Class XII Home Science books with an English
#: language char. Evidence: their Class XI counterparts (``khhe1``/``khhe2``)
#: carry the correct ``h``, and the English editions of the same parts already
#: exist as ``lehe1``/``lehe2``.
MISCODED_NON_ENGLISH_BOOK_CODES: frozenset[str] = frozenset({"lehh1", "lehh2"})

ENGLISH_LANGUAGE_CHAR = "e"


class NcertCatalogError(RuntimeError):
    """Raised when the NCERT index cannot be fetched or parsed."""


@dataclass(frozen=True)
class NcertBook:
    title: str
    code: str
    url: str  # absolute
    range_end: int | None


@dataclass(frozen=True)
class NcertSubject:
    name: str
    books: list[NcertBook] = field(default_factory=list)


@dataclass(frozen=True)
class NcertClass:
    class_no: int
    name: str
    subjects: list[NcertSubject] = field(default_factory=list)


@dataclass(frozen=True)
class NcertSection:
    """One row on a book page: Prelims, a chapter, or a named section."""

    label: str
    key: str  # the value substituted into ``textbook.php?<code>=<key>-<n>``
    kind: str  # "chapter" | "prelims" | "section" | "link"
    number: int | None = None


# --------------------------------------------------------------------------- #
# low-level helpers
# --------------------------------------------------------------------------- #

_TS_CLASS = re.compile(r"tclass\.value\s*==\s*(\d+)")
_TS_SUBJECT = re.compile(r'tsubject\.options\[(\d+)\]\.text\s*=\s*"([^"]*)"')
_TB_TEXT = re.compile(r'tbook\.options\[(\d+)\]\.text\s*=\s*"([^"]*)"')
_TB_VALUE = re.compile(r'tbook\.options\[(\d+)\]\.value\s*=\s*"([^"]*)"')


def _live_lines(js: str):
    """Yield code lines from the page JS, dropping ``//``-commented statements."""
    for line in js.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        yield stripped


def _slice_function(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    if start < 0:
        raise NcertCatalogError(f"marker not found in NCERT index: {start_marker!r}")
    end = source.find(end_marker, start + len(start_marker))
    if end < 0:
        raise NcertCatalogError(f"unterminated block for {start_marker!r}")
    return source[start:end]


def book_code_from_url(url: str) -> str:
    """``textbook.php?aemr1=0-9`` -> ``aemr1``."""
    query = url.split("?")[-1]
    return query.split("=")[0].strip()


def range_end_from_url(url: str) -> int | None:
    """``textbook.php?aemr1=0-9`` -> ``9`` (the page's own upper bound)."""
    query = url.split("?")[-1]
    if "=" not in query:
        return None
    try:
        return int(query.split("=")[-1].split("-")[-1])
    except ValueError:
        return None


def is_english_medium(book_code: str) -> bool:
    if len(book_code) < 2:
        return False
    if book_code in MISCODED_NON_ENGLISH_BOOK_CODES:
        return False
    return book_code[1] == ENGLISH_LANGUAGE_CHAR


def absolute_url(url: str, base_url: str = DEFAULT_BASE_URL) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{base_url.rstrip('/')}/{url.lstrip('/')}"


# --------------------------------------------------------------------------- #
# index parsing
# --------------------------------------------------------------------------- #


def parse_textbook_index(html: str, base_url: str = DEFAULT_BASE_URL) -> list[NcertClass]:
    """Parse ``textbook.php`` into classes -> subjects -> English-medium books."""
    change_js = _slice_function(html, "function change()", "function change1(")
    change1_js = _slice_function(html, "function change1(sind)", "</script>")

    subjects_by_class: dict[int, list[str]] = {}
    current_class: int | None = None
    for line in _live_lines(change_js):
        match = _TS_CLASS.search(line)
        if match:
            current_class = int(match.group(1))
            subjects_by_class.setdefault(current_class, [])
            continue
        match = _TS_SUBJECT.search(line)
        if match and current_class is not None:
            index, text = int(match.group(1)), match.group(2).strip()
            if index == 0 or not text:
                continue
            if text not in subjects_by_class[current_class]:
                subjects_by_class[current_class].append(text)

    books_by_branch: dict[tuple[int, str], list[dict[str, Any]]] = {}
    current_branch: tuple[int, str] | None = None
    pending: dict[str, Any] | None = None
    for line in _live_lines(change1_js):
        match = re.search(
            r'tclass\.value\s*==\s*(\d+)\)\s*&&\s*'
            r'\(document\.test\.tsubject\.options\[sind\]\.text\s*==\s*"([^"]*)"',
            line,
        )
        if match:
            current_branch = (int(match.group(1)), match.group(2))
            books_by_branch.setdefault(current_branch, [])
            pending = None
            continue
        if current_branch is None:
            continue
        match = _TB_TEXT.search(line)
        if match:
            index, title = int(match.group(1)), match.group(2).strip()
            if index == 0 or not title:
                pending = None
                continue
            pending = {"index": index, "title": title, "url": None}
            books_by_branch[current_branch].append(pending)
            continue
        match = _TB_VALUE.search(line)
        if match:
            index, value = int(match.group(1)), match.group(2)
            target = pending if pending and pending["index"] == index else None
            if target is None:
                target = next(
                    (b for b in books_by_branch[current_branch]
                     if b["index"] == index and b["url"] is None),
                    None,
                )
            if target is not None:
                target["url"] = value

    classes: list[NcertClass] = []
    for class_no in sorted(subjects_by_class):
        subjects: list[NcertSubject] = []
        for subject_name in subjects_by_class[class_no]:
            books: list[NcertBook] = []
            for raw in books_by_branch.get((class_no, subject_name), []):
                url = raw.get("url")
                if not url:
                    continue
                code = book_code_from_url(url)
                if not is_english_medium(code):
                    continue
                books.append(
                    NcertBook(
                        title=raw["title"],
                        code=code,
                        url=absolute_url(url, base_url),
                        range_end=range_end_from_url(url),
                    )
                )
            if books:
                subjects.append(NcertSubject(name=subject_name, books=books))
        if subjects:
            classes.append(
                NcertClass(
                    class_no=class_no,
                    name=CLASS_NAMES.get(class_no, f"Class {class_no}"),
                    subjects=subjects,
                )
            )
    if not classes:
        raise NcertCatalogError("no classes parsed from the NCERT index page")
    return classes


# --------------------------------------------------------------------------- #
# book page parsing
# --------------------------------------------------------------------------- #

_WRITE = re.compile(r'document\.write\(\s*((?:"(?:[^"\\]|\\.)*"|[^"()])*)\)', re.S)
_GUARD = re.compile(r"(?:\belse\s+)?\bif\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
_FOR_LOOP = re.compile(r"for\s*\(\s*i\s*=\s*(\d+)\s*;\s*i\s*<=\s*([^;]+?)\s*;")
_LABEL = re.compile(r"sty1\\?\"?\s*>([^<]*)<")
_STRONG = re.compile(r"<strong>([^<]*)</strong>")
_HREF_KEY = re.compile(r"textbook\.php\?(\w+)=([^\s'\"<]*)")
_JS_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')
_LOOP_TOKEN = "\x00"


def _write_template(argument: str, book_code: str) -> str:
    """Collapse a concatenation argument into a single template string.

    NCERT builds rows like ``"<a href='textbook.php?" + pm + "=" + i + "-" + cha + "'>"``,
    so the argument to ``document.write`` is a concatenation expression. We splice
    the known ``pm`` value in directly and mark the loop variable with a token so
    the caller can expand it per chapter.
    """
    parts: list[str] = []
    cursor = 0
    for literal in _JS_LITERAL.finditer(argument):
        between = argument[cursor:literal.start()]
        if between.strip():
            if "pm" in between:
                parts.append(book_code)
            elif "i" in between:
                parts.append(_LOOP_TOKEN)
        try:
            parts.append(_unescape_js_string(literal.group(0)))
        except ValueError:
            return ""
        cursor = literal.end()
    return "".join(parts)


def _eval_pm_condition(condition: str, code: str) -> bool | None:
    """Evaluate an NCERT book-page guard that only ever tests ``pm``.

    Returns ``None`` when the guard uses anything we do not understand, so the
    caller can decide how to treat an unknown branch.
    """
    text = condition.replace("&&", "\u0000").replace("||", "\u0001")
    if "\u0000" in text and "\u0001" in text:
        return None
    parts = re.split(r"[\u0000\u0001]", text)
    operators = re.findall(r"[\u0000\u0001]", text)
    values: list[bool] = []
    for part in parts:
        match = re.fullmatch(r'\s*pm\s*(==|!=)\s*"([^"]*)"\s*', part)
        if not match:
            return None
        op, literal = match.group(1), match.group(2)
        values.append((code == literal) if op == "==" else (code != literal))
    if not values:
        return None
    result = values[0]
    for op, value in zip(operators, values[1:]):
        result = (result and value) if op == "\u0000" else (result or value)
    return result


def _brace_pairs(js: str) -> dict[int, int]:
    """Map each ``{`` index to its matching ``}`` index (one stack pass, O(n))."""
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for index, char in enumerate(js):
        if char == "{":
            stack.append(index)
        elif char == "}" and stack:
            pairs[stack.pop()] = index
    return pairs


def _block_span(js: str, after: int, pairs: dict[int, int]) -> tuple[int, int] | None:
    """Span of the brace block that starts at the first ``{`` after ``after``."""
    opening = js.find("{", after)
    if opening < 0 or opening not in pairs:
        return None
    return opening, pairs[opening]


def _enclosing(entries: list[tuple[int, int, Any]], position: int):
    """Innermost entry whose brace block contains ``position``."""
    best_depth = -1
    best = None
    for start, end, payload in entries:
        if start < position < end and start > best_depth:
            best_depth = start
            best = payload
    return best


def parse_book_sections(html: str, book_code: str) -> list[NcertSection]:
    """Return the section rows a book page renders for ``book_code``.

    The page builds rows with ``document.write`` guarded by ``if(pm=="<code>")``
    style conditions and expands chapters with ``for(i=1;i<=N;i++)`` loops. We
    walk every write in source order, keep the ones whose innermost guard
    evaluates true for this code, and expand any enclosing loop.

    Guards/loops are resolved by brace-block containment: a write belongs to the
    innermost ``if``/``for`` whose ``{...}`` span encloses it.
    """
    start = html.find("var pm=sss[0]")
    js = html[start:] if start >= 0 else html
    pairs = _brace_pairs(js)

    guard_entries: list[tuple[int, int, str]] = []
    for match in _GUARD.finditer(js):
        span = _block_span(js, match.end(), pairs)
        if span is not None:
            guard_entries.append((span[0], span[1], match.group(1)))

    loop_entries: list[tuple[int, int, tuple[int, str]]] = []
    for match in _FOR_LOOP.finditer(js):
        span = _block_span(js, match.end(), pairs)
        if span is not None:
            loop_entries.append(
                (span[0], span[1], (int(match.group(1)), match.group(2)))
            )

    sections: list[NcertSection] = []
    seen: set[str] = set()

    for write in _WRITE.finditer(js):
        position = write.start()

        condition = _enclosing(guard_entries, position)
        if condition is not None and _eval_pm_condition(condition, book_code) is False:
            continue

        template = _write_template(write.group(1), book_code)
        if not template:
            continue
        if "textbook.php?" not in template and "<strong>" not in template:
            continue

        loop = _enclosing(loop_entries, position)
        indices: list[int | None] = [None]
        if loop is not None:
            try:
                upper = int(loop[1])
            except ValueError:
                indices = [None]  # bound depends on a runtime value (e.g. `cha`)
            else:
                indices = list(range(loop[0], upper + 1))

        for index in indices:
            text = template if index is None else template.replace(_LOOP_TOKEN, str(index))
            if _LOOP_TOKEN in text:
                continue
            section = _section_from_literal(text, index)
            if section is None or section.key in seen:
                continue
            seen.add(section.key)
            sections.append(section)

    return sections


def _unescape_js_string(raw: str) -> str:
    body = raw[1:-1]
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            nxt = body[index + 1]
            mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "\\": "\\"}
            if nxt in mapping:
                out.append(mapping[nxt])
                index += 2
                continue
            raise ValueError("unsupported escape")
        out.append(char)
        index += 1
    return "".join(out)


def _section_from_literal(text: str, index: int | None) -> NcertSection | None:
    key_match = _HREF_KEY.search(text)
    if not key_match:
        strong = _STRONG.search(text)
        if strong:
            label = _clean(strong.group(1))
            if label:
                return NcertSection(label=label, key=f"section::{label}", kind="section")
        return None

    key = _clean(key_match.group(2)).rstrip("-").strip()
    if not key:
        return None

    label_match = _LABEL.search(text)
    if label_match:
        label = _clean(label_match.group(1))
    else:
        strong = _STRONG.search(text)
        label = _clean(strong.group(1)) if strong else key

    if not label:
        label = key

    kind = "chapter"
    number: int | None = index
    if key == "ps":
        kind, number = "prelims", None
    elif not key.isdigit():
        kind, number = "link", None

    return NcertSection(label=label, key=key, kind=kind, number=number)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\\", "").replace("&nbsp;", " ")).strip()


def section_url(book_code: str, section: NcertSection, base_url: str = DEFAULT_BASE_URL,
                range_end: int | None = None) -> str:
    tail = range_end if range_end is not None else ""
    return f"{base_url.rstrip('/')}/textbook.php?{book_code}={section.key}-{tail}"


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #


def _retry(callable_, *, attempts: int = 3, base_delay: float = 1.5, label: str = "request"):
    """Retry a network call. ncert.nic.in intermittently resets connections
    (observed 2026-09-23: a whole outage window, then sporadic
    ``ConnectionResetError`` between successful 200s), so a single failure must
    not be read as 'not found'."""
    import time

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return callable_()
        except urllib.error.HTTPError:
            raise  # a real HTTP status is definitive; do not retry
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(base_delay * attempt)
    raise NcertCatalogError(f"{label} failed after {attempts} attempts: {last}")


def fetch_html(url: str, timeout: float = 30.0) -> str:
    """Fetch a page with the stdlib only (no third-party dependency)."""
    def _once() -> str:
        request_obj = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(request_obj, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    return _retry(_once, label=f"GET {url}")


def load_catalog(base_url: str = DEFAULT_BASE_URL, timeout: float = 30.0) -> list[NcertClass]:
    return parse_textbook_index(fetch_html(f"{base_url.rstrip('/')}/textbook.php", timeout), base_url)


def load_book_sections(book_code: str, range_end: int | None = None,
                       base_url: str = DEFAULT_BASE_URL, timeout: float = 30.0) -> list[NcertSection]:
    tail = range_end if range_end is not None else 0
    url = f"{base_url.rstrip('/')}/textbook.php?{book_code}=0-{tail}"
    return parse_book_sections(fetch_html(url, timeout), book_code)


# --------------------------------------------------------------------------- #
# chapter discovery by probing the PDF path
# --------------------------------------------------------------------------- #
#
# The book page only enumerates chapters for a handful of book codes (verified
# live 2026-09-23: ``textbook.php?fecu1=0-12`` returns the catalogue document and
# yields no chapter rows). The chapter PDFs themselves live at a deterministic
# path, so the reliable way to enumerate a book's chapters is to probe that path:
#
#     fecu1 -> 01-12 present, 13+ 404      (matches the catalogue's range end)
#     hehd1 -> 01-05 present, 10+ 404
#     lekl1 -> 01-05, 11-15, 22-23 present (non-contiguous)
#
# Probing every index in the window (rather than assuming 1..N) is what makes the
# non-contiguous books correct. A HEAD is cheap: no multi-megabyte body.


def chapter_pdf_url(book_code: str, number: int, base_url: str = DEFAULT_BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/textbook/pdf/{book_code}{number:02d}.pdf"


def pdf_exists(url: str, timeout: float = 20.0) -> bool:
    """True when the URL serves a PDF.

    404/410 are definitive ('absent'). Transport errors are retried and then
    raised, so a flaky probe can never be mistaken for a missing chapter.
    """
    def _once() -> bool:
        probe = urllib.request.Request(
            url,
            method="HEAD",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            },
        )
        try:
            with urllib.request.urlopen(probe, timeout=timeout) as response:
                return response.status == 200
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                return False
            raise

    return _retry(_once, label=f"HEAD {url}")


def probe_chapter_numbers(book_code: str, range_end: int | None,
                          base_url: str = DEFAULT_BASE_URL, timeout: float = 20.0,
                          margin: int = 3, max_probe: int = 40,
                          workers: int = 8) -> list[int]:
    """Return the chapter numbers whose PDF exists, in ascending order.

    Probing is parallelised: each probe is an independent HEAD, and serially they
    cost ~2-3 s apiece (a TLS handshake each), which would make a whole-class
    scan take hours.
    """
    from concurrent.futures import ThreadPoolExecutor

    upper = min((range_end or 30) + margin, max_probe)
    numbers = list(range(1, upper + 1))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(
            pool.map(
                lambda n: pdf_exists(chapter_pdf_url(book_code, n, base_url), timeout),
                numbers,
            )
        )
    return [n for n, ok in zip(numbers, results) if ok]


def discover_chapters(book_code: str, range_end: int | None,
                      base_url: str = DEFAULT_BASE_URL,
                      page_timeout: float = 30.0, probe_timeout: float = 20.0,
                      margin: int = 3, max_probe: int = 40) -> list[NcertSection]:
    """Enumerate a book's chapters, preferring real labels where NCERT has them.

    Unions the page-parsed chapters (which carry proper labels but exist for only
    a few books) with the probed PDF set (authoritative but unlabelled), so every
    downloadable chapter is found and labelled wherever a label is known.
    """
    labels: dict[int, str] = {}
    try:
        for section in load_book_sections(
            book_code, range_end=range_end, base_url=base_url, timeout=page_timeout
        ):
            if section.kind == "chapter" and section.number is not None:
                labels[section.number] = section.label
    except Exception:  # noqa: BLE001 - the page is optional; probing is the source of truth
        pass

    chapters: list[NcertSection] = []
    for number in probe_chapter_numbers(
        book_code, range_end, base_url, probe_timeout, margin, max_probe
    ):
        label = labels.get(number) or f"Chapter {number}"
        chapters.append(
            NcertSection(label=label, key=str(number), kind="chapter", number=number)
        )
    return chapters
