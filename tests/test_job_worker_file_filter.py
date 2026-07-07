"""Regression test for the worker file-list filter bug.

Symptom
-------
``process_bulk_ai_upload`` in ``app/services/job_worker.py`` used to filter
its initial ``select(BulkAIUploadFile)`` query to ``status = 'pending'``
ONLY when ``job.total_items == 1``. For multi-file bulks (the common case),
it would pick up every row regardless of status, including completed and
stopped rows. The per-row loop only skipped ``stopped``, so completed rows
were silently overwritten back to ``processing`` and re-generated from
scratch, wasting AI tokens and potentially producing duplicate cards.

This test pins the behavior: the worker must only ever see PENDING rows in
its file list, regardless of total_items. We assert this by inspecting the
``where`` clauses on the SQLAlchemy statement that ``process_bulk_ai_upload``
hands to ``db.execute``.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import job_worker
from app.models.bulk_ai_upload import BulkAIUploadFileStatus


def _job(total_items: int, reference_id=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        reference_id=reference_id or uuid4(),
        total_items=total_items,
        processed_items=0,
        failed_items=0,
    )


def _bulk_in_db(bulk_id) -> SimpleNamespace:
    return SimpleNamespace(
        id=bulk_id,
        flashcards_generated=0,
        mcqs_generated=0,
        total_files=0,
    )


class _CapturingDB:
    """DB stand-in that captures the last executed statement and returns
    no rows so ``process_bulk_ai_upload`` falls into its early-exit branch
    after we've inspected the query."""

    def __init__(self, bulk):
        self.bulk = bulk
        self.captured_stmt = None
        self.commits = 0

    def get(self, model, key):
        # The worker calls db.get(BulkAIUpload, job.reference_id) at line 388
        # to load the bulk. Return it for any model that matches the bulk's id.
        if key == self.bulk.id:
            return self.bulk
        return None

    def execute(self, stmt):
        self.captured_stmt = stmt
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [], first=lambda: None),
        )

    def commit(self):
        self.commits += 1

    def refresh(self, *_a, **_k):
        pass

    def add(self, *_a, **_k):
        pass


def _where_clauses_include_pending(stmt) -> bool:
    """Return True if the statement's where clause tree contains a
    BulkAIUploadFile.status == 'pending' comparison."""
    # SQLAlchemy _StatementClause doesn't expose its where tree as a list,
    # so we look for the ._where_criterion attribute or serialize and check.
    where = getattr(stmt, "_where_criterion", None)
    if where is not None:
        # Walk the boolean tree looking for the pending literal.
        pending_literal = BulkAIUploadFileStatus.PENDING.value
        seen: list = [where]

        def _find(node) -> bool:
            for child in seen:
                if hasattr(child, "left") and hasattr(child, "right"):
                    # BinaryOp; recurse into both sides
                    if _find_binary(child):
                        return True
                # Direct value comparison: pull .value from the right side
                right = getattr(child, "right", None)
                left = getattr(child, "left", None)
                if right is not None and getattr(right, "value", None) == pending_literal:
                    # Look for a Column on the left whose key is 'status'
                    if left is not None and getattr(left, "key", None) == "status":
                        return True
            return False

        def _find_binary(node) -> bool:
            if _find([node]):
                return True
            for attr in ("left", "right"):
                child = getattr(node, attr, None)
                if child is not None and _find_binary(child):
                    return True
            return False

        return _find_binary(where)

    # Fallback: render to SQL and look for the literal value.
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    return BulkAIUploadFileStatus.PENDING.value in compiled


def _str_stmt(stmt) -> str:
    try:
        return str(stmt.compile(compile_kwargs={"literal_binds": True}))
    except Exception:
        return repr(stmt)


@pytest.mark.parametrize("total_items", [1, 2, 14, 29])
def test_process_bulk_ai_upload_filters_to_pending_regardless_of_total_items(total_items, monkeypatch):
    """The file-list query must always restrict to status='pending', even
    when total_items != 1. Regression for the duplicate-work bug where a
    completed row was re-flipped to processing and re-processed."""
    bulk_id = uuid4()
    bulk = _bulk_in_db(bulk_id)
    db = _CapturingDB(bulk)
    job = _job(total_items, reference_id=bulk_id)  # match bulk id

    # process_bulk_ai_upload does its file query, then falls into the
    # 'Missing queued upload files' early-exit because our mock returns [].
    # By that point the where-clause has been built and passed to
    # db.execute, which is exactly what we want to capture.
    job_worker.process_bulk_ai_upload(db, job)

    assert db.captured_stmt is not None, "process_bulk_ai_upload did not execute a file query"
    stmt_str = _str_stmt(db.captured_stmt)
    assert "status" in stmt_str.lower(), (
        f"file-list query has no status filter; full statement: {stmt_str!r}"
    )
    assert BulkAIUploadFileStatus.PENDING.value in stmt_str, (
        f"file-list query for total_items={total_items} does not restrict to "
        f"status='{BulkAIUploadFileStatus.PENDING.value}'; got: {stmt_str!r}"
    )
