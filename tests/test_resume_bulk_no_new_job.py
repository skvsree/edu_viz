"""Test that single-file retries do NOT create a new Job row.

When a user force-retries a single file in an existing bulk upload,
the /settings/jobs page should NOT show a new "job card for the full
zip" — it should still show one card per bulk, with the file-level
retry reflected inside that card.

The bug: resume_bulk_ai_upload() with file_id or deck_id set was
unconditionally creating a new Job row (db.add(job)) for every
retry. With 10 retries of a single file, you got 10 jobs in the
Active tab, all pointing at the same bulk — confusingly appearing
as "10 different uploads".

The fix: single-file / single-deck retries should REUSE the most
recent existing Job for the bulk. Only a full-bulk retry (no
file_id, no deck_id) should create a fresh Job row.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

from app.api.routers import bulk_ai_upload as router_module
from app.models.bulk_ai_upload import (
    BulkAIUploadChildFile,
    BulkAIUploadFile,
    BulkAIUploadFileStatus,
    BulkAIUploadStatus,
)
from app.models.job import Job, JobStatus


# ---------------------------------------------------------------------------
# DB stub that routes queries to the right in-memory list
# ---------------------------------------------------------------------------


class _MemoryDB:
    """Stub DB. Routes each .execute() to the right list based on
    the SQLAlchemy statement's table name. Mirrors the pattern used
    by test_bulk_import.py::RecordingDB."""

    def __init__(self, *, bulk=None):
        self.commits = 0
        self.bulk = bulk
        self.jobs: list = []
        self.file_rows: list = []
        self.child_rows: list = []
        self.added: list = []

    def _result(self, values):
        class _R:
            def __init__(self, vs):
                self.vs = vs

            def scalars(self):
                return self

            def all(self):
                return list(self.vs)

            def first(self):
                return self.vs[0] if self.vs else None

        return _R(values)

    def execute(self, stmt):
        text = str(stmt)
        if "FROM bulk_ai_uploads" in text:
            return self._result([self.bulk] if self.bulk else [])
        if "FROM bulk_ai_upload_child_files" in text:
            return self._result(self.child_rows)
        if "FROM bulk_ai_upload_files" in text:
            return self._result(self.file_rows)
        if "FROM jobs" in text:
            return self._result(self.jobs)
        return self._result([])

    def get(self, model, key):
        if model is router_module.BulkAIUpload and self.bulk and str(self.bulk.id) == str(key):
            return self.bulk
        if model is Job:
            for j in self.jobs:
                if j.id == key:
                    return j
        if model is BulkAIUploadChildFile:
            for c in self.child_rows:
                if c.id == key:
                    return c
        return None

    def add(self, value):
        self.added.append(value)
        if isinstance(value, Job):
            self.jobs.append(value)
        elif isinstance(value, BulkAIUploadFile):
            self.file_rows.append(value)
        elif isinstance(value, BulkAIUploadChildFile):
            self.child_rows.append(value)

    def add_all(self, items):
        for it in items:
            self.add(it)

    def commit(self):
        self.commits += 1

    def flush(self):
        for j in self.jobs:
            if getattr(j, "id", None) is None:
                j.id = uuid4()
        for f in self.file_rows:
            if getattr(f, "id", None) is None:
                f.id = uuid4()
        for c in self.child_rows:
            if getattr(c, "id", None) is None:
                c.id = uuid4()

    def refresh(self, obj):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user():
    return SimpleNamespace(id=uuid4(), organization_id=None)


def _make_bulk(*, status=BulkAIUploadStatus.STOPPED.value):
    return SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        status=status,
        filename="gees_zipped.pdf",
        is_auto_stop=True,
        error_message=None,
        processed_files=7,
        failed_files=0,
        skipped_files=0,
        flashcards_generated=1500,
        mcqs_generated=1400,
        completed_at=None,
    )


def _make_child_row(*, latest_attempt=None, deck_id=None):
    return SimpleNamespace(
        id=uuid4(),
        bulk_upload_id=None,
        display_title="Climates of India",
        original_filename="gees103.pdf",
        latest_attempt_id=latest_attempt.id if latest_attempt else None,
        latest_attempt=latest_attempt,
        created_at=datetime.utcnow(),
    )


def _test_setup_for_single_file(monkeypatch, bulk):
    """Common test setup: create user, bulk, child, source_row, db, job."""
    _patch_router_dependencies(monkeypatch)
    user = _make_user()
    source_row = _make_file_row(deck_id=uuid4(), bulk_id=bulk.id)
    child = _make_child_row(latest_attempt=source_row)
    source_row.child_file_id = child.id  # ensure linkage

    db = _MemoryDB(bulk=bulk)
    db.file_rows = [source_row]
    db.child_rows = [child]
    return user, child, source_row, db


def _make_file_row(
    *,
    child_file=None,
    status=BulkAIUploadFileStatus.STOPPED.value,
    deck_id=None,
    flashcards=0,
    mcqs=0,
    original_filename="gees103.pdf",
    bulk_id=None,
):
    row = BulkAIUploadFile(
        bulk_upload_id=bulk_id,
        child_file_id=child_file.id if child_file else None,
        created_deck_id=deck_id,
        original_filename=original_filename,
        extracted_title="Climates of India",
        extracted_description="...",
        content_text=None,
        storage_key=f"bulk/{original_filename}",
        status=status,
        flashcards_generated=flashcards,
        mcqs_generated=mcqs,
        duplicate_count=0,
        error_message=None,
        file_size=12345,
    )
    row.id = uuid4()
    row.created_at = datetime.utcnow()
    row.started_at = datetime.utcnow()
    row.completed_at = datetime.utcnow()
    return row


def _patch_router_dependencies(monkeypatch):
    """Disable the side-effect helpers in resume_bulk_ai_upload so the
    test focuses on Job creation and counter preservation."""

    class _FakeStorage:
        def open_bytes(self, key):
            return (b"fake-pdf-bytes", "application/pdf")

    monkeypatch.setattr(
        "app.api.routers.bulk_ai_upload.get_storage",
        lambda: _FakeStorage(),
    )
    monkeypatch.setattr(
        "app.api.routers.bulk_ai_upload._clear_deck_generated_content",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.api.routers.bulk_ai_upload._recompute_child_latest_attempt",
        lambda *a, **k: None,
    )

    def fake_prepare_retry(db, bulk, user, source_file, child_file=None):
        return _make_file_row(
            child_file=child_file,
            deck_id=source_file.created_deck_id,
            status=BulkAIUploadFileStatus.PENDING.value,
            bulk_id=bulk.id,
        )

    monkeypatch.setattr(
        "app.api.routers.bulk_ai_upload._prepare_fresh_retry_attempt",
        fake_prepare_retry,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_single_file_retry_does_not_create_new_job(monkeypatch):
    """When resume_bulk_ai_upload is called with file_id set, it
    must NOT create a new Job row. The bulk's existing latest Job
    is reused. Otherwise the /settings/jobs page shows a new 'card'
    for the same bulk on every file-level retry.
    """
    bulk = _make_bulk()
    user, child, source_row, db = _test_setup_for_single_file(monkeypatch, bulk)

    # Pre-existing job for this bulk (the worker is mid-process)
    existing_job = Job(
        job_type="bulk_ai_upload",
        reference_id=bulk.id,
        status=JobStatus.RUNNING.value,
        total_items=14,
    )
    existing_job.id = uuid4()
    existing_job.created_at = datetime.utcnow()
    db.jobs.append(existing_job)

    router_module.resume_bulk_ai_upload(
        str(bulk.id),
        file_id=str(child.id),
        force=True,
        user=user,
        db=db,
    )

    # No new Job should have been created.
    assert len(db.jobs) == 1, (
        f"single-file retry created {len(db.jobs) - 1} new Job row(s); "
        f"expected to reuse the existing one. "
        f"This causes the /settings/jobs page to show a new 'card' "
        f"for the same bulk on every file-level retry."
    )
    assert db.jobs[0].id == existing_job.id


def test_full_bulk_retry_creates_new_job(monkeypatch):
    """When resume_bulk_ai_upload is called WITHOUT file_id or deck_id,
    it is a full-bulk retry and SHOULD create a new Job row. This
    is the only retry path that warrants a fresh Job.
    """
    _patch_router_dependencies(monkeypatch)
    user = _make_user()
    bulk = _make_bulk()

    child = _make_child_row()
    source_row = _make_file_row(child_file=child, deck_id=uuid4(), bulk_id=bulk.id)

    db = _MemoryDB(bulk=bulk)
    db.file_rows = [source_row]
    db.child_rows = [child]

    existing_job = Job(
        job_type="bulk_ai_upload",
        reference_id=bulk.id,
        status=JobStatus.COMPLETED.value,
        total_items=14,
    )
    existing_job.id = uuid4()
    existing_job.created_at = datetime.utcnow()
    db.jobs.append(existing_job)

    router_module.resume_bulk_ai_upload(
        str(bulk.id),
        file_id=None,
        deck_id=None,
        user=user,
        db=db,
    )

    # A new Job should be created for full-bulk retry.
    assert len(db.jobs) == 2, (
        f"full-bulk retry should create exactly 1 new Job; got "
        f"{len(db.jobs) - 1} new jobs (expected 1)."
    )
    new_job = db.jobs[-1]
    assert new_job.id != existing_job.id
    assert new_job.reference_id == bulk.id


def test_single_file_retry_preserves_bulk_counters(monkeypatch):
    """Single-file retry must NOT reset the bulk's running
    flashcards_generated / mcqs_generated / processed_files
    counters. Only a full-bulk retry should zero them.
    """
    bulk = _make_bulk()
    # These are the values from a partial run we want to preserve
    bulk.processed_files = 7
    bulk.flashcards_generated = 1500
    bulk.mcqs_generated = 1400

    user, child, _source_row, db = _test_setup_for_single_file(monkeypatch, bulk)

    router_module.resume_bulk_ai_upload(
        str(bulk.id),
        file_id=str(child.id),
        force=True,
        user=user,
        db=db,
    )

    # Counters must be preserved across a single-file retry.
    assert bulk.processed_files == 7, (
        f"single-file retry reset processed_files from 7 to "
        f"{bulk.processed_files}. The new file's attempt should "
        f"add to the existing count, not zero it."
    )
    assert bulk.flashcards_generated == 1500, (
        f"single-file retry reset flashcards_generated from 1500 to "
        f"{bulk.flashcards_generated}. The new file's cards should "
        f"add to the existing total."
    )
    assert bulk.mcqs_generated == 1400, (
        f"single-file retry reset mcqs_generated from 1400 to "
        f"{bulk.mcqs_generated}."
    )
