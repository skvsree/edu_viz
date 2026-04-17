#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib import error, request


DEFAULT_ANKI_ROOT = Path("/root/.openclaw/workspace/ncert-books-science/anki_all_books")
DEFAULT_MCQ_ROOT = Path("/root/.openclaw/workspace/ncert-books-science/grounded_mcq_pipeline/work/chapters")
DEFAULT_MCQ_OUTPUT_ROOT = Path("/root/.openclaw/workspace/ncert-books-science/grounded_mcq_pipeline/outputs")
API_PATH = "/api/v1/import/deck"


class ImportErrorRuntime(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import NCERT Anki + MCQ chapter decks into edu_viz via API")
    parser.add_argument("--base-url", required=True, help="Base URL, e.g. https://qa.edu.selviz.in or https://edu.selviz.in")
    parser.add_argument("--api-key", required=True, help="Bulk import API key")
    parser.add_argument("--anki-root", default=str(DEFAULT_ANKI_ROOT))
    parser.add_argument("--mcq-root", default=str(DEFAULT_MCQ_ROOT), help="Preferred MCQ discovery root; work/chapters accepted, final MCQ JSONs auto-resolved when available")
    parser.add_argument("--mcq-output-root", default=str(DEFAULT_MCQ_OUTPUT_ROOT), help="Final MCQ JSON output root")
    parser.add_argument("--extra-tag", action="append", default=[], help="Additional deck tag; can be repeated")
    parser.add_argument("--include-full-grade-decks", action="store_true", help="Also create combined per-grade full decks")
    parser.add_argument("--grade", type=int, action="append", help="Limit import to one or more grades")
    parser.add_argument("--chapter", type=int, action="append", help="Limit import to one or more chapter numbers")
    parser.add_argument("--dry-run", action="store_true", help="Build payloads and print summary without POSTing")
    return parser.parse_args()


def parse_grade_from_book_slug(book_slug: str) -> int:
    match = re.match(r"class_(\d+)(__.*)?$", book_slug)
    if not match:
        raise ImportErrorRuntime(f"Unable to parse grade from book slug: {book_slug}")
    return int(match.group(1))


def parse_chapter_no(value: str) -> int:
    chapter_match = re.match(r"\s*Chapter\s+(\d+)", value, flags=re.IGNORECASE)
    if chapter_match:
        return int(chapter_match.group(1))
    slug_match = re.match(r"(\d+)_+", value)
    if slug_match:
        return int(slug_match.group(1))
    raise ImportErrorRuntime(f"Unable to parse chapter number from: {value}")


def load_anki_by_deck(anki_root: Path) -> dict[tuple[int, int], list[dict]]:
    decks: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for csv_path in sorted(anki_root.glob("class_*.csv")):
        book_slug = csv_path.stem
        grade_no = parse_grade_from_book_slug(book_slug)
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                chapter_value = (row.get("chapter") or "").strip()
                front = (row.get("front") or "").strip()
                back = (row.get("back") or "").strip()
                if not chapter_value or not front or not back:
                    continue
                chapter_no = parse_chapter_no(chapter_value)
                decks[(grade_no, chapter_no)].append(
                    {
                        "front": front,
                        "back": back,
                        "source_label": f"anki:{book_slug}",
                    }
                )
    return decks


def load_mcqs_by_deck(mcq_root: Path, mcq_output_root: Path) -> dict[tuple[int, int], list[dict]]:
    decks: dict[tuple[int, int], list[dict]] = defaultdict(list)
    candidate_roots: list[Path] = []
    if mcq_root.exists():
        candidate_roots.append(mcq_root)
    if mcq_output_root.exists() and mcq_output_root not in candidate_roots:
        candidate_roots.append(mcq_output_root)

    seen_files: set[Path] = set()
    for root in candidate_roots:
        for json_path in sorted(root.glob("class_*/*.json")):
            seen_files.add(json_path.resolve())
            book_slug = json_path.parent.name
            grade_no = parse_grade_from_book_slug(book_slug)
            chapter_no = parse_chapter_no(json_path.stem)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            for item in payload.get("mcqs", []):
                question = (item.get("question") or "").strip()
                options = item.get("options") or []
                if not question or len(options) != 4:
                    continue
                decks[(grade_no, chapter_no)].append(
                    {
                        "question": question,
                        "explanation": (item.get("explanation") or "").strip(),
                        "options": [str(option).strip() for option in options],
                        "answer_index": int(item.get("answer_index", 0)),
                        "source_label": f"mcq:{book_slug}",
                    }
                )
    return decks


def build_payloads(
    anki_root: Path,
    mcq_root: Path,
    mcq_output_root: Path,
    grades: set[int] | None,
    chapters: set[int] | None,
    extra_tags: list[str],
    include_full_grade_decks: bool,
) -> list[dict]:
    anki_map = load_anki_by_deck(anki_root)
    mcq_map = load_mcqs_by_deck(mcq_root, mcq_output_root)
    keys = sorted(set(anki_map) | set(mcq_map))
    payloads: list[dict] = []
    per_grade_flashcards: dict[int, list[dict]] = defaultdict(list)
    per_grade_mcqs: dict[int, list[dict]] = defaultdict(list)

    for grade_no, chapter_no in keys:
        if grades and grade_no not in grades:
            continue
        if chapters and chapter_no not in chapters:
            continue
        flashcards = anki_map.get((grade_no, chapter_no), [])
        mcqs = mcq_map.get((grade_no, chapter_no), [])
        if not flashcards and not mcqs:
            continue
        per_grade_flashcards[grade_no].extend(flashcards)
        per_grade_mcqs[grade_no].extend(mcqs)
        payloads.append(
            {
                "grade_no": grade_no,
                "chapter_no": chapter_no,
                "description": f"NCERT Grade {grade_no} Science Chapter {chapter_no}",
                "tags": extra_tags,
                "flashcards": flashcards,
                "mcqs": mcqs,
            }
        )

    if include_full_grade_decks:
        for grade_no in sorted(set(per_grade_flashcards) | set(per_grade_mcqs)):
            flashcards = per_grade_flashcards.get(grade_no, [])
            mcqs = per_grade_mcqs.get(grade_no, [])
            if not flashcards and not mcqs:
                continue
            payloads.append(
                {
                    "grade_no": grade_no,
                    "chapter_no": None,
                    "description": f"NCERT Grade {grade_no} Science Full",
                    "tags": extra_tags,
                    "flashcards": flashcards,
                    "mcqs": mcqs,
                }
            )

    return payloads


def post_payload(base_url: str, api_key: str, payload: dict) -> dict:
    url = base_url.rstrip("/") + API_PATH
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise ImportErrorRuntime(f"HTTP {exc.code} for {url}: {detail}") from exc


def main() -> int:
    args = parse_args()
    anki_root = Path(args.anki_root)
    mcq_root = Path(args.mcq_root)
    mcq_output_root = Path(args.mcq_output_root)
    grades = set(args.grade or []) or None
    chapters = set(args.chapter or []) or None

    payloads = build_payloads(
        anki_root,
        mcq_root,
        mcq_output_root,
        grades,
        chapters,
        args.extra_tag,
        args.include_full_grade_decks,
    )
    if not payloads:
        print("No payloads found", file=sys.stderr)
        return 1

    print(f"Prepared {len(payloads)} deck payload(s)")
    for payload in payloads[:10]:
        print(
            f"- {'grade_%s_science_full' % payload['grade_no'] if payload['chapter_no'] is None else 'grade_%s_science_chapter_%s' % (payload['grade_no'], payload['chapter_no'])}: "
            f"{len(payload['flashcards'])} flashcards, {len(payload['mcqs'])} mcqs"
        )
    if len(payloads) > 10:
        print(f"... and {len(payloads) - 10} more")

    if args.dry_run:
        return 0

    total_cards = 0
    for index, payload in enumerate(payloads, start=1):
        result = post_payload(args.base_url, args.api_key, payload)
        total_cards += int(result.get("total_cards_imported", 0))
        print(
            f"[{index}/{len(payloads)}] {result['deck_name']}: "
            f"{result['flashcards_imported']} flashcards, {result['mcqs_imported']} mcqs, "
            f"created={result['created']}"
        )

    print(f"Imported {total_cards} cards across {len(payloads)} deck(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
