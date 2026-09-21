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
