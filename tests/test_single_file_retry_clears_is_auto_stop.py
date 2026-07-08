"""Regression test for the single-file retry "is_auto_stop" bug.

Bug: in resume_bulk_ai_upload, when a user force-retries a single
file in a bulk, the bulk's is_auto_stop flag was being recomputed
from ALL file rows in the bulk, including superseded retry rows
(status=stopped) and any other file that was status=stopped or
status=processing. The result: is_auto_stop was always True for
any bulk that had a stopped supersede row (which is every bulk
that's been retried at least once).

The job worker checks is_auto_stop at the top of
process_bulk_ai_upload and bails out immediately if True:

    if bulk and bulk.is_auto_stop:
        bulk.status = BulkAIUploadStatus.STOPPED.value
        ...
        job.status = JobStatus.FAILED.value
        job.error_message = 'Job stopped by user'
        return

This caused single-file retries to complete in ~30ms with
"Job stopped by user" — no AI calls were made, the new file row
stayed in 'processing' state forever, and the user saw no
progress in the UI.

Real-world repro: bulk 94cba926-... had 3 file rows for
gees109.pdf (two superseded STOPPED rows + one PROCESSING
attempt). Every retry created a new PENDING attempt and a Job
that immediately failed with "Job stopped by user". The
processing row never advanced.

Fix: for single-file / single-deck retries, is_auto_stop must be
False. is_auto_stop is a USER-INTENT flag — it should only be
True when the user explicitly hit Stop or Cancel. A retry is the
opposite of a stop: the user is starting a new attempt.
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
from app.models.job import Job


# ---------------------------------------------------------------------------
# DB stub
# ---------------------------------------------------------------------------


class _MemoryDB:
    """Routes queries to the right in-memory list. Same pattern as
    test_resume_bulk_no_new_job._MemoryDB."""

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


def _make_bulk(*, status=BulkAIUploadStatus.STOPPED.value, is_auto_stop=True):
    """A stopped bulk that the user wants to retry a single file on.
    is_auto_stop defaults to True because the user hit Stop earlier —
    that flag was the whole problem."""
    return SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        status=status,
        filename="gees_zipped.pdf",
        is_auto_stop=is_auto_stop,
        error_message=None,
        processed_files=10,
        failed_files=0,
        skipped_files=0,
        flashcards_generated=2000,
        mcqs_generated=1800,
        completed_at=None,
    )


def _make_file_row(
    *,
    child_file=None,
    status=BulkAIUploadFileStatus.STOPPED.value,
    deck_id=None,
    original_filename="gees109.pdf",
    bulk_id=None,
    started_at=None,
    completed_at=None,
):
    row = BulkAIUploadFile(
        bulk_upload_id=bulk_id,
        child_file_id=child_file.id if child_file else None,
        created_deck_id=deck_id,
        original_filename=original_filename,
        extracted_title="From the Rulers to the Ruled",
        extracted_description="...",
        content_text=None,
        storage_key=f"bulk/{original_filename}",
        status=status,
        flashcards_generated=0,
        mcqs_generated=0,
        duplicate_count=0,
        error_message="Superseded by retry" if status == BulkAIUploadFileStatus.STOPPED.value else None,
        file_size=12345,
    )
    row.id = uuid4()
    row.created_at = datetime.utcnow()
    row.started_at = started_at or datetime.utcnow()
    row.completed_at = completed_at or datetime.utcnow()
    return row


def _make_child_row(*, latest_attempt=None):
    return SimpleNamespace(
        id=uuid4(),
        bulk_upload_id=None,
        display_title="From the Rulers to the Ruled",
        original_filename="gees109.pdf",
        latest_attempt_id=latest_attempt.id if latest_attempt else None,
        latest_attempt=latest_attempt,
        created_at=datetime.utcnow(),
    )


def _patch_router_dependencies(monkeypatch):
    """Disable side-effects so the test focuses on is_auto_stop handling."""

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


def test_single_file_retry_clears_is_auto_stop_when_superseded_rows_exist(monkeypatch):
    """A bulk with multiple superseded STOPPED rows for the same file
    (i.e. previous retries) must have is_auto_stop set to False after a
    single-file retry, so the worker actually processes the new attempt.

    Reproduces the gees109.pdf bug: bulk 94cba926-... had 2 superseded
    rows + 1 processing row, so the old code recomputed is_auto_stop
    from all_statuses and got True. Worker bailed in 30ms with
    "Job stopped by user". File never moved off processing.
    """
    _patch_router_dependencies(monkeypatch)
    bulk = _make_bulk(is_auto_stop=True)
    user = _make_user()

    # The actual scenario: 2 superseded STOPPED rows + 1 stuck PROCESSING
    # row for the same file, all in the same bulk.
    deck_id = uuid4()
    superseded_1 = _make_file_row(
        status=BulkAIUploadFileStatus.STOPPED.value,
        deck_id=deck_id,
        bulk_id=bulk.id,
    )
    superseded_2 = _make_file_row(
        status=BulkAIUploadFileStatus.STOPPED.value,
        deck_id=deck_id,
        bulk_id=bulk.id,
    )
    stuck_processing = _make_file_row(
        status=BulkAIUploadFileStatus.PROCESSING.value,
        deck_id=deck_id,
        bulk_id=bulk.id,
        completed_at=None,
    )
    # The child row's latest_attempt points to the most recent (processing) row.
    child = _make_child_row(latest_attempt=stuck_processing)
    stuck_processing.child_file_id = child.id

    db = _MemoryDB(bulk=bulk)
    db.file_rows = [superseded_1, superseded_2, stuck_processing]
    db.child_rows = [child]

    # Sanity: pre-condition matches the real bug — bulk is auto-stopped
    # and the file has both stopped (supersede) and processing (stuck) rows.
    assert bulk.is_auto_stop is True
    statuses = [(f.status or '').lower() for f in db.file_rows]
    assert "stopped" in statuses
    assert "processing" in statuses

    # Force-retry the single file.
    router_module.resume_bulk_ai_upload(
        str(bulk.id),
        file_id=str(child.id),
        force=True,
        user=user,
        db=db,
    )

    # The fix: a single-file retry must clear is_auto_stop. The user
    # is starting a new attempt, not stopping the bulk.
    assert bulk.is_auto_stop is False, (
        "Single-file retry left bulk.is_auto_stop=True. The job worker "
        "checks this flag at the top of process_bulk_ai_upload and "
        "bails out immediately if True, so the new attempt would never "
        "be processed. The user would see the file stuck in 'processing' "
        "and the Job fail in 30ms with 'Job stopped by user'."
    )


def test_single_deck_retry_clears_is_auto_stop(monkeypatch):
    """Same as above but for deck_id-based retries."""
    _patch_router_dependencies(monkeypatch)
    bulk = _make_bulk(is_auto_stop=True)
    user = _make_user()

    deck_id = uuid4()
    superseded = _make_file_row(
        status=BulkAIUploadFileStatus.STOPPED.value,
        deck_id=deck_id,
        bulk_id=bulk.id,
    )
    stuck = _make_file_row(
        status=BulkAIUploadFileStatus.PROCESSING.value,
        deck_id=deck_id,
        bulk_id=bulk.id,
        completed_at=None,
    )
    child = _make_child_row(latest_attempt=stuck)
    stuck.child_file_id = child.id

    db = _MemoryDB(bulk=bulk)
    db.file_rows = [superseded, stuck]
    db.child_rows = [child]

    router_module.resume_bulk_ai_upload(
        str(bulk.id),
        deck_id=str(deck_id),
        force=True,
        user=user,
        db=db,
    )

    assert bulk.is_auto_stop is False, (
        "Single-deck retry left bulk.is_auto_stop=True. Same root cause "
        "as the file_id bug: superseded STOPPED rows in the bulk make "
        "is_auto_stop=True and the worker bails."
    )


def test_full_bulk_retry_keeps_is_auto_stop_false(monkeypatch):
    """A full-bulk retry (no file_id, no deck_id) must also leave
    is_auto_stop=False. The user just hit Retry, not Stop."""
    _patch_router_dependencies(monkeypatch)
    user = _make_user()
    bulk = _make_bulk(is_auto_stop=True)

    child = _make_child_row()
    source_row = _make_file_row(child_file=child, deck_id=uuid4(), bulk_id=bulk.id)

    db = _MemoryDB(bulk=bulk)
    db.file_rows = [source_row]
    db.child_rows = [child]

    router_module.resume_bulk_ai_upload(
        str(bulk.id),
        file_id=None,
        deck_id=None,
        user=user,
        db=db,
    )

    assert bulk.is_auto_stop is False
