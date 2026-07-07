"""Test that bulk-AI job counter updates happen per chunk, not only at file end.

This is the behavior the user noticed: while a 7-chunk file is being
processed, the bulk row's flashcards_generated/mcqs_generated stayed at
0 even after 50+ cards had been generated and saved to the cards table.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.services import job_worker


class _CountingDB:
    """Minimal DB stand-in: tracks which attributes get assigned to bulk/file."""

    def __init__(self):
        self.commits = 0
        self.added = []

    def add_all(self, items):
        self.added.extend(items)

    def commit(self):
        self.commits += 1

    def flush(self):
        pass

    def get(self, *_a, **_k):
        return None

    def refresh(self, *_a, **_k):
        pass

    def execute(self, *_a, **_k):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))


def _bulk(initial_fc=0, initial_mcq=0):
    b = SimpleNamespace(
        id=uuid4(),
        flashcards_generated=initial_fc,
        mcqs_generated=initial_mcq,
    )
    return b


def _file_row(initial_fc=0, initial_mcq=0):
    f = SimpleNamespace(
        id=uuid4(),
        flashcards_generated=initial_fc,
        mcqs_generated=initial_mcq,
        duplicate_count=0,
    )
    return f


def test_record_chunk_progress_updates_both_file_and_bulk_counters():
    """After processing one chunk, file row and bulk row should both
    reflect the new flashcards/mcqs totals, and a commit should fire so
    the live counter is observable by SSE/polling consumers.
    """
    bulk = _bulk(initial_fc=10, initial_mcq=20)  # bulk already has other files
    file_row = _file_row(initial_fc=0, initial_mcq=0)
    db = _CountingDB()

    job_worker.record_chunk_progress(
        db=db,
        file_record=file_row,
        bulk=bulk,
        chunk_flashcards=18,
        chunk_mcqs=18,
    )

    # Bulk must reflect the delta: prior + new chunk.
    assert bulk.flashcards_generated == 28
    assert bulk.mcqs_generated == 38
    # File row must reflect its own running total.
    assert file_row.flashcards_generated == 18
    assert file_row.mcqs_generated == 18
    # And the change must be committed so live readers see it.
    assert db.commits == 1


def test_record_chunk_progress_is_idempotent_when_no_new_cards():
    db = _CountingDB()
    bulk = _bulk(initial_fc=10, initial_mcq=20)
    file_row = _file_row(initial_fc=0, initial_mcq=0)

    job_worker.record_chunk_progress(
        db=db, file_record=file_row, bulk=bulk,
        chunk_flashcards=0, chunk_mcqs=0,
    )

    # No-op must not change counts and must still commit (so a heartbeat
    # is observable).
    assert bulk.flashcards_generated == 10
    assert bulk.mcqs_generated == 20
    assert file_row.flashcards_generated == 0
    assert file_row.mcqs_generated == 0
    assert db.commits == 1


def test_record_chunk_progress_accumulates_across_chunks():
    """Three chunks of (10, 5) each should land as 30/15 on both rows."""
    db = _CountingDB()
    bulk = _bulk()
    file_row = _file_row()

    for _ in range(3):
        job_worker.record_chunk_progress(
            db=db, file_record=file_row, bulk=bulk,
            chunk_flashcards=10, chunk_mcqs=5,
        )

    assert bulk.flashcards_generated == 30
    assert bulk.mcqs_generated == 15
    assert file_row.flashcards_generated == 30
    assert file_row.mcqs_generated == 15
    assert db.commits == 3


def test_record_chunk_progress_handles_none_initial_values():
    """Existing rows in the DB may have NULL counts (legacy data or
    freshly-created rows before the column default was added)."""
    db = _CountingDB()
    bulk = SimpleNamespace(flashcards_generated=None, mcqs_generated=None)
    file_row = SimpleNamespace(
        flashcards_generated=None, mcqs_generated=None, duplicate_count=0,
    )

    job_worker.record_chunk_progress(
        db=db, file_record=file_row, bulk=bulk,
        chunk_flashcards=5, chunk_mcqs=3,
    )

    assert bulk.flashcards_generated == 5
    assert bulk.mcqs_generated == 3
    assert file_row.flashcards_generated == 5
    assert file_row.mcqs_generated == 3
