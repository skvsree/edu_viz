from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import deps
from app.api.routers import bulk_import
from app.services.access import ROLE_SYSTEM_ADMIN


class RecordingResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def first(self):
        return self.values[0] if self.values else None

    def all(self):
        return self.values


class RecordingDB:
    def __init__(
        self,
        *,
        system_admin,
        existing_deck=None,
        existing_cards=None,
        existing_tags=None,
    ):
        self.system_admin = system_admin
        self.existing_deck = existing_deck
        self.existing_cards = existing_cards or []
        self.existing_tags = existing_tags or []
        self.added = []
        self.flushed = 0
        self.committed = False

    def execute(self, stmt):
        text = str(stmt)
        if "FROM users" in text:
            return RecordingResult([self.system_admin] if self.system_admin else [])
        if "FROM decks" in text:
            return RecordingResult([self.existing_deck] if self.existing_deck else [])
        if "FROM cards" in text:
            return RecordingResult(self.existing_cards)
        if "FROM tags" in text:
            return RecordingResult(self.existing_tags)
        return RecordingResult([])

    def add(self, item):
        self.added.append(item)
        if getattr(item, "id", None) is None:
            item.id = uuid4()

    def add_all(self, items):
        items = list(items)
        self.added.extend(items)
        for item in items:
            if getattr(item, "id", None) is None:
                item.id = uuid4()

    def flush(self):
        self.flushed += 1
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid4()

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


def _system_admin(org_id=None):
    return SimpleNamespace(
        id=uuid4(),
        role=ROLE_SYSTEM_ADMIN,
        organization_id=org_id,
        created_at=None,
    )


def test_bulk_import_api_key_rejects_when_env_missing(monkeypatch):
    monkeypatch.setattr(deps.settings, "bulk_import_api_key", None)

    with pytest.raises(HTTPException) as exc:
        deps.require_bulk_import_api_key("abc")

    assert exc.value.status_code == 503
    assert exc.value.detail == "Bulk import API is not configured"


def test_bulk_import_api_key_rejects_missing_header(monkeypatch):
    monkeypatch.setattr(deps.settings, "bulk_import_api_key", "secret")

    with pytest.raises(HTTPException) as exc:
        deps.require_bulk_import_api_key(None)

    assert exc.value.status_code == 401
    assert exc.value.detail == "Missing API key"


def test_bulk_import_api_key_rejects_invalid_header(monkeypatch):
    monkeypatch.setattr(deps.settings, "bulk_import_api_key", "secret")

    with pytest.raises(HTTPException) as exc:
        deps.require_bulk_import_api_key("wrong")

    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid API key"


def test_bulk_import_creates_global_chapter_science_deck_and_cards_with_default_tags():
    db = RecordingDB(system_admin=_system_admin(org_id=uuid4()))
    payload = bulk_import.BulkImportDeckPayload(
        grade_no=6,
        chapter_no=3,
        flashcards=[bulk_import.BulkImportFlashcardItem(front="Q1", back="A1")],
        mcqs=[
            bulk_import.BulkImportMcqItem(
                question="Which one?",
                explanation="Because",
                options=["A", "B", "C", "D"],
                answer_index=1,
            )
        ],
    )

    response = bulk_import.import_chapter_deck(payload, db=db)

    assert response.deck_name == "grade_6_science_chapter_3"
    assert response.created is True
    assert response.flashcards_imported == 1
    assert response.mcqs_imported == 1
    assert response.total_cards_imported == 2
    assert db.committed is True
    created_deck = next(
        item
        for item in db.added
        if getattr(item, "name", None) == "grade_6_science_chapter_3"
    )
    assert created_deck.is_global is True
    assert {tag.name for tag in created_deck.tags} == {
        "grade_6",
        "chapter_3",
        "science",
    }


def test_bulk_import_creates_full_grade_science_deck_name():
    db = RecordingDB(system_admin=_system_admin(org_id=uuid4()))
    payload = bulk_import.BulkImportDeckPayload(
        grade_no=6,
        chapter_no=None,
        flashcards=[bulk_import.BulkImportFlashcardItem(front="Q1", back="A1")],
        tags=["full_book"],
    )

    response = bulk_import.import_chapter_deck(payload, db=db)

    assert response.deck_name == "grade_6_science_full"
    created_deck = next(
        item
        for item in db.added
        if getattr(item, "name", None) == "grade_6_science_full"
    )
    assert {tag.name for tag in created_deck.tags} == {
        "grade_6",
        "science",
        "full_book",
    }


def test_bulk_import_creates_subject_full_deck_name():
    db = RecordingDB(system_admin=_system_admin(org_id=uuid4()))
    payload = bulk_import.BulkImportDeckPayload(
        grade_no=11,
        chapter_no=None,
        subject="biology",
        flashcards=[bulk_import.BulkImportFlashcardItem(front="Q1", back="A1")],
        tags=["full_book"],
    )

    response = bulk_import.import_chapter_deck(payload, db=db)

    assert response.deck_name == "grade_11_biology_full"
    created_deck = next(
        item
        for item in db.added
        if getattr(item, "name", None) == "grade_11_biology_full"
    )
    assert {tag.name for tag in created_deck.tags} == {
        "grade_11",
        "biology",
        "full_book",
    }


def test_bulk_import_skips_duplicate_cards_on_rerun():
    existing_deck = SimpleNamespace(
        id=uuid4(),
        name="grade_6_science_chapter_3",
        normalized_name="grade6sciencechapter3",
        is_deleted=False,
        is_global=True,
        organization_id=None,
        description=None,
        tags=[],
    )
    existing_cards = [
        SimpleNamespace(
            front="Q1",
            back="A1",
            card_type="basic",
            source_label="anki-bulk-import",
            mcq_options=None,
            mcq_answer_index=None,
        ),
        SimpleNamespace(
            front="Which one?",
            back="Because",
            card_type="mcq",
            source_label="mcq-bulk-import",
            mcq_options=["A", "B", "C", "D"],
            mcq_answer_index=1,
        ),
    ]
    db = RecordingDB(
        system_admin=_system_admin(org_id=uuid4()),
        existing_deck=existing_deck,
        existing_cards=existing_cards,
    )
    payload = bulk_import.BulkImportDeckPayload(
        grade_no=6,
        chapter_no=3,
        flashcards=[bulk_import.BulkImportFlashcardItem(front="Q1", back="A1")],
        mcqs=[
            bulk_import.BulkImportMcqItem(
                question="Which one?",
                explanation="Because",
                options=["A", "B", "C", "D"],
                answer_index=1,
            )
        ],
    )

    response = bulk_import.import_chapter_deck(payload, db=db)

    assert response.created is False
    assert response.flashcards_imported == 0
    assert response.mcqs_imported == 0
    assert response.total_cards_imported == 0
    assert db.committed is True


def test_bulk_import_rejects_empty_options():
    db = RecordingDB(system_admin=_system_admin())
    payload = bulk_import.BulkImportDeckPayload(
        grade_no=6,
        chapter_no=3,
        mcqs=[
            bulk_import.BulkImportMcqItem(
                question="Which one?",
                explanation="Because",
                options=["A", "", "C", "D"],
                answer_index=1,
            )
        ],
    )

    with pytest.raises(HTTPException) as exc:
        bulk_import.import_chapter_deck(payload, db=db)

    assert exc.value.status_code == 400
    assert exc.value.detail == "MCQ options cannot be empty"


def test_bulk_import_batch_sums_results():
    db = RecordingDB(system_admin=_system_admin(org_id=uuid4()))
    payload = bulk_import.BulkImportBatchPayload(
        decks=[
            bulk_import.BulkImportDeckPayload(
                grade_no=6,
                chapter_no=1,
                flashcards=[bulk_import.BulkImportFlashcardItem(front="Q1", back="A1")],
            ),
            bulk_import.BulkImportDeckPayload(
                grade_no=6,
                chapter_no=2,
                mcqs=[
                    bulk_import.BulkImportMcqItem(
                        question="Which one?",
                        explanation="Because",
                        options=["A", "B", "C", "D"],
                        answer_index=1,
                    )
                ],
            ),
        ]
    )

    response = bulk_import.import_chapter_decks(payload, db=db)

    assert response.deck_count == 2
    assert response.total_flashcards_imported == 1
    assert response.total_mcqs_imported == 1
    assert response.total_cards_imported == 2
    assert len(response.imported_decks) == 2
