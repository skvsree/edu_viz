"""The per-file job log endpoint that backs the jobs-page log panel."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.routers import pages


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _EventsSession:
    def __init__(self, *, file_record=None, events=()):
        self.file_record = file_record
        self.events = list(events)
        self.statements: list = []

    def get(self, model, ident):
        return self.file_record

    def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.events)


def _file_row(**overrides):
    base = {
        "id": uuid4(),
        "status": "processing",
        "current_stage": "chunk 3/6 round 2 · core",
        "chunks_total": 6,
        "chunks_completed": 2,
        "passes_total": 54,
        "passes_completed": 12,
        "passes_failed": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _event(level, message, when=None):
    return SimpleNamespace(
        level=level,
        message=message,
        created_at=when or datetime(2026, 9, 21, 3, 5, 0),
    )


def test_events_endpoint_reports_progress_and_log_oldest_first():
    file_row = _file_row()
    session = _EventsSession(
        file_record=file_row,
        events=[_event("warn", "newer"), _event("info", "older")],
    )

    payload = pages.job_events_for_file(
        file_id=str(file_row.id),
        limit=200,
        user=SimpleNamespace(role="system_admin"),
        db=session,
    )

    assert payload["chunks"] == {"completed": 2, "total": 6}
    assert payload["passes"] == {"completed": 12, "failed": 1, "total": 54}
    assert payload["stage"] == "chunk 3/6 round 2 · core"
    # The query returns newest first; the panel reads oldest first.
    assert [item["message"] for item in payload["events"]] == ["older", "newer"]
    assert payload["events"][0]["level"] == "info"


def test_events_endpoint_requires_system_admin():
    with pytest.raises(HTTPException) as excinfo:
        pages.job_events_for_file(
            file_id=str(uuid4()),
            limit=200,
            user=SimpleNamespace(role="user"),
            db=_EventsSession(),
        )

    assert excinfo.value.status_code == 403


def test_events_endpoint_404s_for_an_unknown_file():
    with pytest.raises(HTTPException) as excinfo:
        pages.job_events_for_file(
            file_id=str(uuid4()),
            limit=200,
            user=SimpleNamespace(role="system_admin"),
            db=_EventsSession(file_record=None),
        )

    assert excinfo.value.status_code == 404


def test_events_endpoint_404s_for_a_malformed_id():
    with pytest.raises(HTTPException) as excinfo:
        pages.job_events_for_file(
            file_id="not-a-uuid",
            limit=200,
            user=SimpleNamespace(role="system_admin"),
            db=_EventsSession(),
        )

    assert excinfo.value.status_code == 404


def test_events_endpoint_with_no_events_returns_empty_list():
    file_row = _file_row(current_stage=None, chunks_total=0, chunks_completed=0)
    session = _EventsSession(file_record=file_row, events=[])

    payload = pages.job_events_for_file(
        file_id=str(file_row.id),
        limit=200,
        user=SimpleNamespace(role="system_admin"),
        db=session,
    )

    assert payload["events"] == []
    assert payload["chunks"]["total"] == 0
