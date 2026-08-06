#!/usr/bin/env python3
"""Generate revision notes for an NCERT chapter PDF using the edu_viz
revision-notes pipeline — zero-config edition.

Usage:
    python run_revision_notes.py <chapter.pdf> <out.pdf> \
        [chapter_label] [deck_name] [source_title]

If chapter_label is omitted it is derived from the filename (e.g.
fecu106.pdf -> "Chapter 6"). Deck name defaults to "NCERT <stem>".

Pipeline (mirrors generate_revision_notes_for_child):
    pdftotext -> auto-detect & strip artifacts -> AI-first
    (deepseek-v4-pro via opencode.ai) -> heuristic + verbatim-selector
    fallback -> render_revision_pdf (low-ink A4).

Preprocessing is ZERO-CONFIG: instead of per-book regexes, repeated
standalone short lines (running headers, mastheads, footers) are detected
by page frequency (>=30% of pages), timestamps are matched generically,
and numeric figure rows / lone page numbers / editorial markers are
stripped by pattern. No per-book args needed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env",
            override=False)

from app.core.config import settings  # noqa: E402
from app.services.ai_generation import AICredential  # noqa: E402
from app.services import revision_notes as RN  # noqa: E402
from app.services.text_cleaner import clean_ncert_text  # noqa: E402


def _run_pdftotext(pdf_path: str) -> str:
    res = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True, text=True, timeout=120,
    )
    if res.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {res.stderr[:300]}")
    return res.stdout


def _chapter_label_from_stem(stem: str) -> str:
    m = re.search(r"(\d+)$", stem)
    return f"Chapter {m.group(1)}" if m else stem.upper()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def run_pipeline(pdf_path: str, out_pdf: str, chapter_label: str,
                 deck_name: str, source_title: str) -> None:
    api_key = settings.ai_api_key
    if not api_key:
        print("FATAL: AI_API_KEY missing from /opt/edu_viz/.env")
        sys.exit(2)
    cred = AICredential(
        provider="deepseek",
        auth_type="api_key",
        secret=api_key,
        source="env",
    )

    raw_text = _run_pdftotext(pdf_path)
    source_text = clean_ncert_text(raw_text)
    print(f"raw chars: {len(raw_text)}  ->  cleaned chars: {len(source_text)}")
    print(f"chapter_label: {chapter_label} | deck: {deck_name}")
    print(f"max source chars (config): {settings.revision_notes_max_source_chars}")

    topics: list = []
    final_sentence = ""
    ai_generated = False

    # ---- AI-first path (preferred) ----
    print(">> Attempting AI-driven generation (deepseek-v4-pro)...", flush=True)
    try:
        topics, final_sentence, ai_generated = RN.generate_ai_topics(
            None,
            source_text=source_text,
            chapter_label=chapter_label,
            credential_provider_name="deepseek",
            credential=cred,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"!! generate_ai_topics raised: {exc}")
        topics, final_sentence, ai_generated = [], "", False

    if ai_generated:
        print(f">> AI path OK: {len(topics)} topics, "
              f"{sum(len(t.ai_bullets or []) for t in topics)} bullets")
    else:
        # ---- Heuristic fallback + per-topic verbatim selector (legacy) ----
        print(">> AI path failed — falling back to heuristic + verbatim "
              "selector", flush=True)
        raw_topics = RN.heuristic_topics(source_text, max_topics=9)
        topics = [t for t in raw_topics if t.sections]
        if not topics:
            print("FATAL: no topics extracted from source")
            sys.exit(1)
        topics = RN._attach_payloads(source_text, topics)

        try:
            for t in topics:
                bullets, used = RN.select_topic_recall_bullets(
                    None,
                    topic_title=t.title,
                    source_paragraphs=t.source_paragraphs,
                    credential_provider_name="deepseek",
                    credential=cred,
                )
                if used and bullets:
                    t.ai_bullets = bullets
                    t.ai_used = True
                else:
                    t.ai_bullets = []
                    t.ai_used = False
            ai_used_count = sum(1 for t in topics if t.ai_used)
            print(f">> Verbatim selector used for "
                  f"{ai_used_count}/{len(topics)} topics")
        except Exception as exc:  # noqa: BLE001
            print(f"!! verbatim selector raised: {exc} — heuristic-only bullets")
        final_sentence = (
            f"Generated on {datetime.utcnow().strftime('%Y-%m-%d')} "
            "from your uploaded deck."
        )

    # ---- Build doc + render ----
    doc = RN._build_revision_doc(
        deck_name=deck_name,
        chapter_label=chapter_label,
        source_title=source_title,
        subtitle=f"{deck_name} — {chapter_label}",
        topics=topics,
        final_sentence=final_sentence,
    )
    pdf_bytes, page_count = RN.render_revision_pdf(doc, deck_name)
    Path(out_pdf).write_bytes(pdf_bytes)

    print("\n==== RESULT ====")
    print(f"path:        {out_pdf}")
    print(f"bytes:       {len(pdf_bytes):,}")
    print(f"pages:       {page_count}")
    print(f"ai_generated:{ai_generated}")
    print(f"topics:      {len(topics)}")
    for i, t in enumerate(topics, 1):
        tag = "AI" if getattr(t, "ai_used", False) else "HEUR"
        print(f"  {i:2d}. [{tag}] {t.title}  ({len(t.ai_bullets or [])} bullets)")


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: run_revision_notes.py <chapter.pdf> <out.pdf> "
              "[chapter_label] [deck_name] [source_title]")
        sys.exit(2)

    pdf_path = sys.argv[1]
    out_pdf = sys.argv[2]
    stem = Path(pdf_path).stem  # e.g. fecu106
    chapter_label = sys.argv[3] if len(sys.argv) > 3 else _chapter_label_from_stem(stem)
    deck_name = sys.argv[4] if len(sys.argv) > 4 else f"NCERT {stem.upper()}"
    source_title = sys.argv[5] if len(sys.argv) > 5 else f"{stem.upper()} — {chapter_label}"
    run_pipeline(pdf_path, out_pdf, chapter_label, deck_name, source_title)


if __name__ == "__main__":
    main()
