"""A per-deck retry that arrives while the worker is already mid-run must not
be orphaned forever.

Symptom (prod, 2026-09-26)
--------------------------
Two decks of the same bulk were force-retried ~10 s apart. The worker had
already snapshotted its pending-file list (and ``job.total_items == 1``, since
each single-deck retry sets it from its own target list), so it processed only
the first deck and then marked the job COMPLETED. The second deck's row stayed
``pending`` with zero cards — and because the deck is wiped
(``_clear_deck_generated_content``) before regeneration, that deck was left
EMPTY. Only a second resume call (which re-flips the reused Job row back to
pending) got it going again.

Fix: after the per-file loop, if the bulk still has pending rows, leave the job
pending so the scheduler re-runs it instead of stamping it completed.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.models.bulk_ai_upload import BulkAIUploadStatus
from app.models.job import JobStatus
from app.services import job_worker


class _CountingDB:
    """Returns a fixed pending-row count for the helper's COUNT query."""

    def __init__(self, pending: int):
        self.pending = pending
        self.commits = 0
        self.executed: list = []

    def execute(self, stmt):
        self.executed.append(stmt)
        value = self.pending
        return SimpleNamespace(
            scalar_one=lambda: value,
            scalars=lambda: SimpleNamespace(all=lambda: [], first=lambda: None),
        )

    def commit(self):
        self.commits += 1


def _job(**over):
    job = SimpleNamespace(
        id=uuid4(),
        reference_id=uuid4(),
        status=JobStatus.RUNNING.value,
        total_items=1,
        processed_items=1,
        failed_items=0,
        worker_id="worker-1",
        locked_at="locked",
        completed_at="done",
    )
    for k, v in over.items():
        setattr(job, k, v)
    return job


def _bulk(**over):
    bulk = SimpleNamespace(
        id=uuid4(),
        status=BulkAIUploadStatus.PROCESSING.value,
        is_auto_stop=False,
        completed_at=None,
    )
    for k, v in over.items():
        setattr(bulk, k, v)
    return bulk


def test_late_retry_row_requeues_the_job_instead_of_completing_it():
    """pending rows remain → the job goes back to pending, not completed."""
    db = _CountingDB(pending=1)
    job = _job()
    bulk = _bulk()

    requeued = job_worker._requeue_job_for_late_files(db, job, bulk)

    assert requeued == 1
    assert job.status == JobStatus.PENDING.value, (
        "the job was left COMPLETED while one of its files is still pending — "
        "the scheduler only picks up pending/failed jobs, so that deck would "
        "stay empty forever"
    )
    assert job.completed_at is None
    assert job.worker_id is None
    assert bulk.status == BulkAIUploadStatus.PENDING.value
    assert bulk.completed_at is None
    assert db.commits >= 1


def test_no_pending_rows_leaves_the_job_alone():
    """Nothing left pending → the helper must not touch the job."""
    db = _CountingDB(pending=0)
    job = _job(status=JobStatus.COMPLETED.value, completed_at="done")
    bulk = _bulk(status=BulkAIUploadStatus.COMPLETED.value)

    requeued = job_worker._requeue_job_for_late_files(db, job, bulk)

    assert requeued == 0
    assert job.status == JobStatus.COMPLETED.value
    assert bulk.status == BulkAIUploadStatus.COMPLETED.value


def test_finalize_path_calls_the_requeue_helper():
    """The helper is only useful if the finalize block actually consults it."""
    import inspect

    source = inspect.getsource(job_worker.process_bulk_ai_upload)
    assert "_requeue_job_for_late_files(" in source, (
        "process_bulk_ai_upload never calls _requeue_job_for_late_files, so a "
        "retry row created mid-run is orphaned when the job completes"
    )
