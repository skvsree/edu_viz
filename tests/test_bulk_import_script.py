from __future__ import annotations

import json
from pathlib import Path

from scripts.bulk_import_ncert import build_payloads, load_anki_by_deck, load_mcqs_by_deck



def test_load_anki_by_deck_groups_by_grade_and_chapter(tmp_path: Path):
    csv_path = tmp_path / "class_6__curiosity.csv"
    csv_path.write_text(
        "chapter,front,back,tags\n"
        "Chapter 1 - Test,F1,B1,t1\n"
        "Chapter 2 - Test,F2,B2,t2\n",
        encoding="utf-8",
    )

    result = load_anki_by_deck(tmp_path)

    assert len(result[(6, 1)]) == 1
    assert result[(6, 1)][0]["front"] == "F1"
    assert len(result[(6, 2)]) == 1
    assert result[(6, 2)][0]["back"] == "B2"



def test_load_mcqs_by_deck_groups_by_grade_and_chapter(tmp_path: Path):
    chapters_dir = tmp_path / "work" / "chapters"
    output_dir = tmp_path / "outputs"
    chapters_dir.mkdir(parents=True)
    book_dir = output_dir / "class_6__curiosity"
    book_dir.mkdir(parents=True)
    (book_dir / "01__chapter_one.json").write_text(
        json.dumps(
            {
                "mcqs": [
                    {
                        "question": "Q1",
                        "options": ["A", "B", "C", "D"],
                        "answer_index": 2,
                        "explanation": "E1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = load_mcqs_by_deck(chapters_dir, output_dir)

    assert len(result[(6, 1)]) == 1
    assert result[(6, 1)][0]["question"] == "Q1"
    assert result[(6, 1)][0]["answer_index"] == 2



def test_build_payloads_merges_sources_and_builds_full_grade_deck(tmp_path: Path):
    anki_root = tmp_path / "anki"
    mcq_root = tmp_path / "work" / "chapters"
    mcq_output_root = tmp_path / "outputs"
    anki_root.mkdir(parents=True)
    mcq_root.mkdir(parents=True)
    mcq_book_dir = mcq_output_root / "class_6__curiosity"
    mcq_book_dir.mkdir(parents=True)

    (anki_root / "class_6__curiosity.csv").write_text(
        "chapter,front,back,tags\n"
        "Chapter 1 - Test,F1,B1,t1\n",
        encoding="utf-8",
    )
    (mcq_book_dir / "01__chapter_one.json").write_text(
        json.dumps(
            {
                "mcqs": [
                    {
                        "question": "Q1",
                        "options": ["A", "B", "C", "D"],
                        "answer_index": 2,
                        "explanation": "E1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    payloads = build_payloads(anki_root, mcq_root, mcq_output_root, None, None, ["ncert"], True)

    assert len(payloads) == 2
    chapter_payload = next(item for item in payloads if item["chapter_no"] == 1)
    full_payload = next(item for item in payloads if item["chapter_no"] is None)
    assert chapter_payload["grade_no"] == 6
    assert len(chapter_payload["flashcards"]) == 1
    assert len(chapter_payload["mcqs"]) == 1
    assert chapter_payload["tags"] == ["ncert"]
    assert full_payload["grade_no"] == 6
    assert len(full_payload["flashcards"]) == 1
    assert len(full_payload["mcqs"]) == 1
