"""A reclaimed bulk job must pick up files a dead worker left PROCESSING.

Restarting the app while a file was generating used to end with the reclaimed
job failing 'Missing queued upload files' and the upload stranded mid-file.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.services import job_worker


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _ResumeSession:
    """Answers the PROCESSING lookup and records the commit."""

    def __init__(self, processing_rows):
        self.processing_rows = list(processing_rows)
        self.commits = 0
        self.statements: list[str] = []

    def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.processing_rows)

    def commit(self):
        self.commits += 1


def test_orphaned_processing_files_are_returned_to_pending():
    rows = [
        SimpleNamespace(
            id=uuid4(),
            original_filename="fecu106.pdf",
            status="processing",
            error_message="stale",
        )
    ]
    session = _ResumeSession(rows)

    reclaimed = job_worker._reclaim_orphaned_processing_files(session, uuid4())

    assert len(reclaimed) == 1
    assert rows[0].status == "pending"
    assert rows[0].error_message is None
    assert session.commits == 1, "the reset must be committed before re-querying"


def test_nothing_is_touched_when_no_file_is_processing():
    session = _ResumeSession([])

    assert job_worker._reclaim_orphaned_processing_files(session, uuid4()) == []
    assert session.commits == 0


def test_reclaim_only_looks_at_processing_rows():
    session = _ResumeSession([])

    job_worker._reclaim_orphaned_processing_files(session, uuid4())

    statement = session.statements[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "bulk_ai_upload_files" in compiled
    assert "processing" in compiled, "the probe must target PROCESSING rows"


class _CounterSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def test_resumed_attempt_resets_the_file_and_bulk_counters():
    """A resumed file regenerates from scratch, so its totals must restart.

    Otherwise a restarted upload reports the previous attempt's cards on top of
    the new ones, and the bulk accumulates both.
    """
    file_record = SimpleNamespace(
        flashcards_generated=102,
        mcqs_generated=101,
        duplicate_count=7,
    )
    bulk = SimpleNamespace(flashcards_generated=102, mcqs_generated=101)

    job_worker._reset_file_attempt_counters(_CounterSession(), file_record, bulk)

    assert file_record.flashcards_generated == 0
    assert file_record.mcqs_generated == 0
    assert file_record.duplicate_count == 0
    assert bulk.flashcards_generated == 0
    assert bulk.mcqs_generated == 0


def test_counter_reset_never_goes_negative():
    file_record = SimpleNamespace(
        flashcards_generated=5, mcqs_generated=4, duplicate_count=1
    )
    bulk = SimpleNamespace(flashcards_generated=3, mcqs_generated=2)

    job_worker._reset_file_attempt_counters(_CounterSession(), file_record, bulk)

    assert bulk.flashcards_generated == 0
    assert bulk.mcqs_generated == 0


def test_counter_reset_is_a_noop_for_a_first_attempt():
    file_record = SimpleNamespace(
        flashcards_generated=0, mcqs_generated=0, duplicate_count=0
    )
    bulk = SimpleNamespace(flashcards_generated=0, mcqs_generated=0)

    job_worker._reset_file_attempt_counters(_CounterSession(), file_record, bulk)

    assert (file_record.flashcards_generated, bulk.flashcards_generated) == (0, 0)
