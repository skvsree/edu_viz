from __future__ import annotations

import io
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import deps
from app.api.routers import bulk_ai_upload, bulk_import
from app.services.storage import StorageError
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


def test_should_queue_archive_member_skips_macos_noise():
    assert bulk_ai_upload._should_queue_archive_member("chapter1.pdf") is True
    assert bulk_ai_upload._should_queue_archive_member("nested/chapter2.pdf") is True
    assert bulk_ai_upload._should_queue_archive_member("__MACOSX/chapter1.pdf") is False
    assert bulk_ai_upload._should_queue_archive_member("nested/._chapter1.pdf") is False
    assert bulk_ai_upload._should_queue_archive_member("._chapter1.pdf") is False
    assert bulk_ai_upload._should_queue_archive_member("notes.txt") is False


def test_enqueue_ai_upload_job_requires_storage_success():
    from uuid import uuid4
    from unittest.mock import patch

    from fastapi import HTTPException

    user = SimpleNamespace(id=uuid4(), organization_id=None)

    class EnqueueDB:
        def __init__(self):
            self.added = []
            self.flushes = 0

        def add(self, obj):
            self.added.append(obj)

        def flush(self):
            self.flushes += 1
            for obj in self.added:
                if getattr(obj, "id", None) is None:
                    obj.id = uuid4()

        def commit(self):
            assert False, "commit should not be reached"

        def refresh(self, obj):
            return None

    db = EnqueueDB()
    deck = SimpleNamespace(id=uuid4())
    storage = SimpleNamespace(
        save_stream=lambda **kwargs: (_ for _ in ()).throw(StorageError("boom"))
    )

    with patch.object(bulk_ai_upload, "get_storage", return_value=storage), patch.object(
        bulk_ai_upload, "_ensure_bulk_upload_deck", return_value=deck
    ):
        try:
            bulk_ai_upload.enqueue_ai_upload_job(
                db=db,
                user=user,
                source_file=SimpleNamespace(
                    filename="chapter.pdf",
                    file=io.BytesIO(b"%PDF-1.4 sample"),
                ),
            )
            assert False, "expected HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 503
            assert "Failed to store upload file" in exc.detail


def test_resume_bulk_ai_upload_rejects_missing_storage():
    from uuid import uuid4
    from unittest.mock import patch

    from fastapi import HTTPException

    from app.models import BulkAIUploadStatus

    user = SimpleNamespace(id=uuid4(), organization_id=None)
    bulk_id = uuid4()
    bulk = SimpleNamespace(
        id=bulk_id,
        filename="batch.zip",
        total_files=1,
        status=BulkAIUploadStatus.FAILED.value,
        deck_id=None,
    )
    file_row = SimpleNamespace(
        bulk_upload_id=bulk_id,
        original_filename="a.pdf",
        storage_key="bulk-ai-upload/missing/a.pdf",
        status="failed",
    )

    class ResumeDB:
        def __init__(self):
            self.added = []
            self.commits = 0

        def get(self, model, value):
            return bulk if value == str(bulk_id) or value == bulk_id else None

        def execute(self, stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [file_row]))

        def add(self, obj):
            self.added.append(obj)

        def commit(self):
            self.commits += 1

    db = ResumeDB()
    storage = SimpleNamespace(open_bytes=lambda key: (_ for _ in ()).throw(FileNotFoundError(key)))

    with patch.object(bulk_ai_upload, "get_storage", return_value=storage):
        try:
            bulk_ai_upload.resume_bulk_ai_upload(str(bulk_id), user=user, db=db)
            assert False, "expected HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 409
            assert "original upload files are missing" in exc.detail

    assert db.added == []


def test_title_prompt_warns_when_archive_and_pdf_filenames_match():
    from app.services.ai_generation import build_title_generation_prompt

    prompt = build_title_generation_prompt(
        "Real chapter heading\nHuman Reproduction\nDetailed source content.",
        "biology.pdf",
        archive_filename="biology.zip",
    )

    assert "Archive filename: biology.zip" in prompt
    assert "PDF filename: biology.pdf" in prompt
    assert "do not copy that repeated filename as the title" in prompt
    assert "Human Reproduction" in prompt


def test_retry_reuses_existing_deck_id_instead_of_name_lookup(monkeypatch):
    from app.api.routers.bulk_ai_upload import _prepare_fresh_retry_attempt
    from app.models.bulk_ai_upload import (
        BulkAIUploadChildFile,
        BulkAIUploadFileStatus,
    )

    user_id = uuid4()
    deck_id = uuid4()
    child_file_id = uuid4()
    bulk = SimpleNamespace(id=uuid4(), filename="same.zip")
    user = SimpleNamespace(id=user_id, organization_id=None)
    source_file = SimpleNamespace(
        created_deck_id=deck_id,
        original_filename="same.pdf",
        child_file_id=child_file_id,
        extracted_title="Real source title",
        extracted_description="Source summary",
        storage_key="bulk/source.pdf",
        file_size=123,
    )
    existing_deck = SimpleNamespace(
        id=deck_id,
        user_id=user_id,
        is_deleted=False,
        folder_id=uuid4(),
    )
    existing_child = SimpleNamespace(
        id=child_file_id,
        bulk_upload_id=bulk.id,
        latest_attempt_id=None,
        display_title=None,
    )

    class RetryDB:
        def __init__(self):
            self.added = []
            self.flushed = False

        def get(self, model, key):
            if model is BulkAIUploadChildFile:
                return existing_child if key == child_file_id else None
            return existing_deck if key == deck_id else None

        def add(self, value):
            self.added.append(value)

        def flush(self):
            self.flushed = True

    db = RetryDB()
    monkeypatch.setattr(
        "app.api.routers.bulk_ai_upload._clear_deck_generated_content",
        lambda session, target_deck_id: None,
    )

    retry_row = _prepare_fresh_retry_attempt(
        db,
        bulk=bulk,
        user=user,
        source_file=source_file,
    )

    assert retry_row.created_deck_id == deck_id
    assert retry_row.status == BulkAIUploadFileStatus.PENDING.value
    assert existing_deck.folder_id is None
    assert db.added == [retry_row]
    assert db.flushed is True
    # Retry row links to the source's existing child_file, preserving identity.
    assert retry_row.child_file_id == child_file_id
    assert existing_child.latest_attempt_id == retry_row.id


def test_retry_backfills_child_file_id_when_source_has_none(monkeypatch):
    """Legacy source rows have child_file_id=None; the retry must promote
    them to a real BulkAIUploadChildFile so future retries dedupe by
    child_file_id instead of falling back to original_filename."""
    from app.api.routers.bulk_ai_upload import _prepare_fresh_retry_attempt
    from app.models.bulk_ai_upload import (
        BulkAIUploadChildFile,
        BulkAIUploadFileStatus,
    )

    user_id = uuid4()
    deck_id = uuid4()
    bulk = SimpleNamespace(id=uuid4(), filename="same.zip")
    user = SimpleNamespace(id=user_id, organization_id=None)
    source_file = SimpleNamespace(
        created_deck_id=deck_id,
        original_filename="same.pdf",
        child_file_id=None,                # ← legacy row
        extracted_title="Source title",
        extracted_description="Source desc",
        storage_key="bulk/source.pdf",
        file_size=123,
    )
    existing_deck = SimpleNamespace(
        id=deck_id, user_id=user_id, is_deleted=False, folder_id=uuid4(),
    )

    created_child = []

    class RetryDB:
        def __init__(self):
            self.added = []
            self.flushed = False

        def get(self, model, key):
            return existing_deck if key == deck_id else None

        def execute(self, stmt):  # noqa: ARG002 - unused in this test
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(
                    first=lambda: None,
                    all=lambda: [],
                ),
                scalar_one_or_none=lambda: None,
            )

        def add(self, value):
            self.added.append(value)
            if isinstance(value, BulkAIUploadChildFile):
                created_child.append(value)

        def flush(self):
            self.flushed = True
            for child in created_child:
                if getattr(child, "id", None) is None:
                    child.id = uuid4()

    db = RetryDB()
    monkeypatch.setattr(
        "app.api.routers.bulk_ai_upload._clear_deck_generated_content",
        lambda session, target_deck_id: None,
    )
    # Avoid hitting the real deck-name uniqueness check
    monkeypatch.setattr(
        "app.api.routers.bulk_ai_upload._ensure_bulk_upload_deck",
        lambda session, u, fn, fid, existing_deck_id=None: SimpleNamespace(
            id=existing_deck_id or uuid4(),
            user_id=u.id,
            is_deleted=False,
            folder_id=None,
        ),
    )

    retry_row = _prepare_fresh_retry_attempt(
        db,
        bulk=bulk,
        user=user,
        source_file=source_file,
    )

    # 1. A child file was created
    assert len(created_child) == 1, (
        f"Expected 1 child file to be created, got {len(created_child)}"
    )
    new_child = created_child[0]
    assert new_child.bulk_upload_id == bulk.id
    assert new_child.original_filename == "same.pdf"
    assert new_child.storage_key == "bulk/source.pdf"

    # 2. The retry row links to the new child
    assert retry_row.child_file_id == new_child.id

    # 3. The new child has its latest_attempt_id pointing at the retry row
    assert new_child.latest_attempt_id == retry_row.id

    # 4. The retry row is pending and ready for processing
    assert retry_row.status == BulkAIUploadFileStatus.PENDING.value
    assert retry_row.flashcards_generated == 0
    assert retry_row.mcqs_generated == 0

    # 5. db.add was called for both the child file and the retry row, in that order
    child_idx = db.added.index(new_child)
    retry_idx = db.added.index(retry_row)
    assert child_idx < retry_idx


def test_double_force_retry_does_not_create_duplicate_file_rows(monkeypatch):
    """Calling resume_bulk_ai_upload with force=True twice must NOT add a
    third row for the same logical file. Each (original_filename, storage_key)
    pair should map to exactly one pending row at any time."""
    from app.api.routers import bulk_ai_upload as router_module
    from app.models.bulk_ai_upload import (
        BulkAIUploadChildFile,
        BulkAIUploadFile,
        BulkAIUploadFileStatus,
    )

    user_id = uuid4()
    deck_id = uuid4()
    bulk_id = uuid4()
    user = SimpleNamespace(id=user_id, organization_id=None)

    # Initial state: bulk with 1 legacy file row (child_file_id=None)
    source_row = BulkAIUploadFile(
        bulk_upload_id=bulk_id,
        child_file_id=None,
        created_deck_id=deck_id,
        original_filename="gees103.pdf",
        extracted_title="Climates of India",
        extracted_description="...",
        content_text=None,
        storage_key="bulk/source.pdf",
        status=BulkAIUploadFileStatus.STOPPED.value,
        flashcards_generated=36,
        mcqs_generated=35,
        duplicate_count=0,
        error_message="Superseded by retry",
        file_size=12345,
    )
    source_row.id = uuid4()
    source_row.created_at = datetime.utcnow()

    bulk = SimpleNamespace(
        id=bulk_id,
        user_id=user_id,
        status=router_module.BulkAIUploadStatus.STOPPED.value,
        filename="gees103.pdf",
        is_auto_stop=True,
        error_message=None,
        processed_files=0,
        failed_files=0,
        skipped_files=0,
        flashcards_generated=0,
        mcqs_generated=0,
        completed_at=None,
        child_files=[],
        files=[source_row],
    )

    # An in-memory DB that tracks added rows
    in_memory_tables = {"child_files": [], "files": []}

    class MemoryDB:
        def __init__(self):
            self.commits = 0

        def get(self, model, key):
            if model is BulkAIUploadChildFile:
                for c in in_memory_tables["child_files"]:
                    if c.id == key:
                        return c
                return None
            return None

        def execute(self, stmt):
            # Crude heuristic: pull everything from in-memory files.
            # The router iterates these in order created_at asc.
            class _R:
                def __init__(self, vals):
                    self.vals = vals

                def scalars(self):
                    return self

                def all(self):
                    return self.vals

                def first(self):
                    return self.vals[0] if self.vals else None

            return _R(list(in_memory_tables["files"]))

        def add(self, value):
            if isinstance(value, BulkAIUploadChildFile):
                in_memory_tables["child_files"].append(value)
            elif isinstance(value, BulkAIUploadFile):
                in_memory_tables["files"].append(value)

        def flush(self):
            for c in in_memory_tables["child_files"]:
                if getattr(c, "id", None) is None:
                    c.id = uuid4()
            for f in in_memory_tables["files"]:
                if getattr(f, "id", None) is None:
                    f.id = uuid4()
                if getattr(f, "created_at", None) is None:
                    f.created_at = datetime.utcnow()

        def commit(self):
            self.commits += 1

        def refresh(self, obj):
            return None

    db = MemoryDB()
    in_memory_tables["files"].append(source_row)

    monkeypatch.setattr(
        "app.api.routers.bulk_ai_upload._clear_deck_generated_content",
        lambda session, target_deck_id: None,
    )

    # Patch _ensure_bulk_upload_deck to return a deterministic mock deck
    monkeypatch.setattr(
        "app.api.routers.bulk_ai_upload._ensure_bulk_upload_deck",
        lambda session, u, fn, fid, existing_deck_id=None: SimpleNamespace(
            id=existing_deck_id or uuid4(), user_id=u.id, is_deleted=False, folder_id=None,
        ),
    )

    # First retry
    router_module._prepare_fresh_retry_attempt(
        db, bulk=bulk, user=user, source_file=source_row,
    )

    # Second retry - source becomes the row created by the first retry
    first_retry = in_memory_tables["files"][-1]
    assert first_retry is not source_row
    router_module._prepare_fresh_retry_attempt(
        db, bulk=bulk, user=user, source_file=first_retry,
    )

    # Now count: 1 source + 1 first retry + 1 second retry = 3 rows total.
    # But the second retry should have created a *new* child_file rather than
    # inheriting the first retry's (since first retry now has child_file_id
    # populated, it should reuse that child file and create a new attempt row).
    #
    # So we expect:
    #   child_files: 2  (one promoted for source, one for the new source which
    #                    actually should be 1 because the second retry should
    #                    have inherited the first retry's child file)
    # The actual bug is that without deduping, we get 3 file rows all with
    # the same original_filename, and _latest_bulk_attempt_rows returns all
    # three (because the child-file latest_attempt_id logic only filters when
    # child_file_id is set).
    #
    # This test asserts that after the fix, each (original_filename,
    # storage_key) pair maps to at most ONE pending row in _latest_bulk_attempt_rows.
    latest_rows = router_module._latest_bulk_attempt_rows(db, bulk_id)
    pending_count = sum(
        1 for r in latest_rows
        if r.status == BulkAIUploadFileStatus.PENDING.value
        and r.original_filename == "gees103.pdf"
    )
    assert pending_count <= 1, (
        f"After two retries, expected at most 1 pending row for gees103.pdf, "
        f"got {pending_count}: {[r.id for r in latest_rows]}"
    )
