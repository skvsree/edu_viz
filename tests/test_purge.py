"""Tests for permanent (hard) deletion of soft-deleted records.

The purge helpers issue dependency-ordered DELETEs against a Session, so these
tests drive them with a recording session double: it answers the id lookups the
service makes and keeps every statement in order. That lets the tests assert the
properties that actually matter (children before parents, parent row last,
guards honoured, stored objects cleaned up) without needing a live database.
"""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import unquote_plus
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.routers import pages
from app.services import purge as purge_service
from app.services.purge import PurgeError
from app.services.access import ROLE_ADMIN, ROLE_SYSTEM_ADMIN
from tests.test_dashboard_routes import FakeDB, make_request, render_body
from tests.test_settings_routes import JobsSettingsDB


class _Result:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class RecordingSession:
    """Session double that records statements and answers purge's lookups."""

    def __init__(
        self,
        *,
        card_ids=(),
        test_ids=(),
        attempt_ids=(),
        bulk_ids=(),
        concept_map_ids=(),
        revision_keys=(),
        active_file_ids=(),
        active_job_ids=(),
        rowcount=1,
    ):
        self.card_ids = list(card_ids)
        self.test_ids = list(test_ids)
        self.attempt_ids = list(attempt_ids)
        self.bulk_ids = list(bulk_ids)
        self.concept_map_ids = list(concept_map_ids)
        self.revision_keys = list(revision_keys)
        self.active_file_ids = list(active_file_ids)
        self.active_job_ids = list(active_job_ids)
        self.rowcount = rowcount
        self.statements: list[str] = []
        self.committed = False

    # -- Session API -------------------------------------------------------
    def execute(self, stmt):
        text = " ".join(str(stmt).split())
        self.statements.append(text)
        if text.upper().startswith("DELETE"):
            return _Result(rowcount=self.rowcount)
        return _Result(self._rows_for(text))

    def commit(self):
        self.committed = True

    # -- helpers -----------------------------------------------------------
    def _rows_for(self, text: str):
        lowered = text.lower()
        if "revision_pdf_storage_key" in lowered:
            return self.revision_keys
        if "from jobs" in lowered:
            return self.active_job_ids
        if "from bulk_ai_upload_files" in lowered:
            return self.active_file_ids
        if "from test_attempts" in lowered:
            return self.attempt_ids
        if "from tests" in lowered:
            return self.test_ids
        if "from bulk_ai_uploads" in lowered:
            return self.bulk_ids
        if "from concept_maps" in lowered:
            return self.concept_map_ids
        if "from cards" in lowered:
            return self.card_ids
        return []

    def deletes(self) -> list[str]:
        return [s for s in self.statements if s.upper().startswith("DELETE")]

    def table_order(self) -> list[str]:
        return [statement.split()[2].strip('"') for statement in self.deletes()]


class FakeStorage:
    def __init__(self):
        self.prefixes: list[str] = []

    def delete_prefix(self, *, prefix):
        self.prefixes.append(prefix)
        return 2


@pytest.fixture()
def fake_storage(monkeypatch):
    storage = FakeStorage()
    monkeypatch.setattr(purge_service, "get_storage", lambda: storage)
    return storage


def _deck(*, is_deleted: bool = True):
    return SimpleNamespace(
        id=uuid4(), is_deleted=is_deleted, name="Biology", user_id=uuid4()
    )


def _system_admin():
    return SimpleNamespace(
        id=uuid4(),
        role=ROLE_SYSTEM_ADMIN,
        organization_id=None,
        email="root@example.com",
        identity_sub="root-sub",
    )


def _org_admin():
    return SimpleNamespace(
        id=uuid4(),
        role=ROLE_ADMIN,
        organization_id=uuid4(),
        email="admin@example.com",
        identity_sub="admin-sub",
    )


# --------------------------------------------------------------------------
# purge_deck
# --------------------------------------------------------------------------


def test_purge_deck_refuses_a_deck_that_is_not_deleted(fake_storage):
    session = RecordingSession()

    with pytest.raises(PurgeError):
        purge_service.purge_deck(session, _deck(is_deleted=False))

    assert session.statements == []
    assert session.committed is False
    assert fake_storage.prefixes == []


def test_purge_deck_refuses_while_a_job_is_running(fake_storage):
    session = RecordingSession(bulk_ids=[uuid4()], active_job_ids=[uuid4()])

    with pytest.raises(PurgeError):
        purge_service.purge_deck(session, _deck())

    assert session.deletes() == []
    assert session.committed is False


def test_purge_deck_deletes_children_before_parents_and_commits(fake_storage):
    deck = _deck()
    card_id, test_id, attempt_id, bulk_id, concept_map_id = (uuid4() for _ in range(5))
    session = RecordingSession(
        card_ids=[card_id],
        test_ids=[test_id],
        attempt_ids=[attempt_id],
        bulk_ids=[bulk_id],
        concept_map_ids=[concept_map_id],
    )

    counts = purge_service.purge_deck(session, deck)

    order = session.table_order()
    # Answers before attempts, attempts/questions before tests.
    assert order.index("test_attempt_answers") < order.index("test_attempts")
    assert order.index("test_attempts") < order.index("tests")
    # Cards' dependants before cards, and cards before the deck row.
    assert order.index("reviews") < order.index("cards")
    assert order.index("card_states") < order.index("cards")
    # Upload children before the upload rows; the deck goes last.
    assert order.index("bulk_ai_upload_files") < order.index("bulk_ai_uploads")
    assert order.index("bulk_ai_upload_child_files") < order.index("bulk_ai_uploads")
    assert order[-1] == "decks"

    # Every table that references a deck (or its cards/tests) is accounted for.
    for table in (
        "test_attempt_answers",
        "test_attempts",
        "test_questions",
        "tests",
        "reviews",
        "card_states",
        "deck_tags",
        "cards",
        "ai_upload_generations",
        "mcq_generations",
        "deck_mcq_generation_items",
        "concept_maps",
        "deck_accesses",
        "user_deck_favorites",
        "bulk_ai_upload_revision_notes",
        "bulk_ai_upload_files",
        "bulk_ai_upload_child_files",
        "bulk_ai_uploads",
        "jobs",
        "decks",
    ):
        assert table in order, f"{table} was never cleared"

    assert session.committed is True
    assert counts["decks"] == 1
    assert counts["cards"] == 1
    assert counts["tests"] == 1
    assert counts["concept_maps"] == 1
    assert counts["bulk_ai_uploads"] == 1
    assert counts["jobs"] == 1


def test_purge_deck_skips_models_whose_table_is_missing(monkeypatch, fake_storage):
    """mcq_generations has no migration and is absent on some deployments.

    A delete against a missing table would abort the transaction, so the purge
    must skip it and still remove everything else.
    """
    monkeypatch.setattr(
        purge_service,
        "_table_present",
        lambda db, table_name: table_name != "mcq_generations",
    )
    session = RecordingSession()

    counts = purge_service.purge_deck(session, _deck())

    order = session.table_order()
    assert "mcq_generations" not in order
    assert "mcq_generations" not in counts
    assert "decks" in order
    assert order[-1] == "decks"
    assert session.committed is True


def test_model_foreign_keys_cascade_for_deck_owned_tables():
    """The database must clean up a deck's children on its own (migration 0031).

    purge_deck deletes explicitly for accurate counts, but the schema is the
    safety net: removing a deck row must not leave orphans behind in cards,
    tests, attempts, reviews or MCQ rows.
    """
    from app.models import (
        Card,
        CardState,
        DeckMcqGenerationItem,
        MCQGeneration,
        Review,
        Test,
        TestAttempt,
        TestAttemptAnswer,
        TestQuestion,
        deck_tags,
    )

    scoped_columns = [
        Card.__table__.c.deck_id,
        CardState.__table__.c.card_id,
        Review.__table__.c.card_id,
        Test.__table__.c.deck_id,
        TestAttempt.__table__.c.test_id,
        TestAttemptAnswer.__table__.c.attempt_id,
        TestAttemptAnswer.__table__.c.question_id,
        TestQuestion.__table__.c.test_id,
        TestQuestion.__table__.c.card_id,
        MCQGeneration.__table__.c.deck_id,
        DeckMcqGenerationItem.__table__.c.deck_id,
    ]
    for column in scoped_columns:
        rules = {fk.ondelete for fk in column.foreign_keys}
        assert rules == {"CASCADE"}, f"{column.table.name}.{column.name} -> {rules}"

    for fk in deck_tags.foreign_keys:
        assert fk.ondelete == "CASCADE", f"deck_tags.{fk.parent.name} -> {fk.ondelete}"


def test_migration_cascade_list_covers_every_scoped_table():
    """Migration 0031 must keep rewriting the rule for every scoped FK."""
    from pathlib import Path

    migration = Path("alembic/versions/0031_mcq_fk_cascade.py").read_text()
    for table, column, parent in (
        ("cards", "deck_id", "decks"),
        ("tests", "deck_id", "decks"),
        ("test_questions", "test_id", "tests"),
        ("test_questions", "card_id", "cards"),
        ("test_attempts", "test_id", "tests"),
        ("test_attempt_answers", "attempt_id", "test_attempts"),
        ("test_attempt_answers", "question_id", "test_questions"),
        ("reviews", "card_id", "cards"),
        ("card_states", "card_id", "cards"),
        ("deck_tags", "deck_id", "decks"),
        ("deck_tags", "tag_id", "tags"),
        ("mcq_generations", "deck_id", "decks"),
    ):
        assert f"('{table}', '{column}', '{parent}')" in migration


def test_purge_deck_clears_media_and_revision_pdf_objects(fake_storage):
    deck = _deck()
    session = RecordingSession(revision_keys=["reports/deck/revision.pdf"])

    counts = purge_service.purge_deck(session, deck)

    assert f"{deck.id}/" in fake_storage.prefixes
    assert "reports/deck/revision.pdf" in fake_storage.prefixes
    assert counts["storage_objects"] == 4  # two prefixes x 2 objects


# --------------------------------------------------------------------------
# purge_bulk_upload
# --------------------------------------------------------------------------


def test_purge_bulk_upload_refuses_while_files_are_processing(fake_storage):
    session = RecordingSession(active_file_ids=[uuid4()])

    with pytest.raises(PurgeError):
        purge_service.purge_bulk_upload(
            session, SimpleNamespace(id=uuid4(), status="processing")
        )

    assert session.deletes() == []
    assert session.committed is False


def test_purge_bulk_upload_deletes_rows_in_dependency_order(fake_storage):
    bulk_id = uuid4()
    session = RecordingSession()

    counts = purge_service.purge_bulk_upload(
        session, SimpleNamespace(id=bulk_id, status="failed")
    )

    assert session.table_order() == [
        "bulk_ai_upload_revision_notes",
        "bulk_ai_upload_files",
        "bulk_ai_upload_child_files",
        "jobs",
        "bulk_ai_uploads",
    ]
    assert session.committed is True
    assert counts["bulk_ai_uploads"] == 1
    assert f"bulk-ai-upload/{bulk_id}/" in fake_storage.prefixes
    assert f"bulk_uploads/{bulk_id}/" in fake_storage.prefixes


# --------------------------------------------------------------------------
# admin routes
# --------------------------------------------------------------------------


def test_deleted_decks_page_requires_system_admin(monkeypatch):
    monkeypatch.setattr(pages, "deleted_decks", lambda db: [])

    with pytest.raises(HTTPException) as excinfo:
        pages._deleted_decks_response(
            make_request(path="/settings/deleted-decks"),
            user=_org_admin(),
            db=FakeDB(),
        )

    assert excinfo.value.status_code == 403


def test_deleted_decks_page_lists_deleted_decks(monkeypatch):
    deck = _deck()
    monkeypatch.setattr(pages, "deleted_decks", lambda db: [deck])

    class PageDB(FakeDB):
        """Answers the page's card/owner lookups by table name."""

        def execute(self, stmt):
            text = str(stmt).lower()
            if "from cards" in text:
                return _Result([(deck.id, 3)])
            if "from users" in text:
                return _Result(
                    [SimpleNamespace(id=deck.user_id, email="owner@example.com")]
                )
            return _Result([])

    response = pages._deleted_decks_response(
        make_request(path="/settings/deleted-decks"),
        user=_system_admin(),
        db=PageDB(),
    )

    body = render_body(response)
    assert response.status_code == 200
    assert "Biology" in body
    assert "owner@example.com" in body
    assert "3 cards" in body  # card count shown in the row meta
    assert f'data-purge-deck="{deck.id}"' in body
    assert "Delete permanently" in body
    assert "purge-deck-modal" in body  # the confirmation popup


def test_purge_deck_route_404s_for_a_deck_that_is_not_deleted():
    deck = _deck(is_deleted=False)

    with pytest.raises(HTTPException) as excinfo:
        pages.purge_deck_permanently(
            deck_id=str(deck.id),
            user=_system_admin(),
            db=FakeDB({str(deck.id): deck, deck.id: deck}),
        )

    assert excinfo.value.status_code == 404


def test_purge_deck_route_redirects_with_a_success_message(monkeypatch):
    deck = _deck()
    captured = {}

    def fake_purge(db, target):
        captured["deck"] = target
        return {"decks": 1, "cards": 4, "storage_objects": 2}

    monkeypatch.setattr(pages, "purge_deck", fake_purge)

    response = pages.purge_deck_permanently(
        deck_id=str(deck.id),
        user=_system_admin(),
        db=FakeDB({str(deck.id): deck, deck.id: deck}),
    )

    message = unquote_plus(response.headers["location"])
    assert response.status_code == 303
    assert captured["deck"] is deck
    assert "/settings/deleted-decks?success=" in message
    assert "Biology permanently deleted" in message
    assert "5 records" in message
    assert "2 stored objects" in message


def test_purge_deck_route_reports_a_guard_failure(monkeypatch):
    deck = _deck()

    def failing_purge(db, target):
        raise PurgeError("This deck still has a running job.")

    monkeypatch.setattr(pages, "purge_deck", failing_purge)

    response = pages.purge_deck_permanently(
        deck_id=str(deck.id),
        user=_system_admin(),
        db=FakeDB({str(deck.id): deck, deck.id: deck}),
    )

    location = unquote_plus(response.headers["location"])
    assert response.status_code == 303
    assert "/settings/deleted-decks?error=" in location
    assert "running job" in location


# --------------------------------------------------------------------------
# jobs page: delete action wiring
# --------------------------------------------------------------------------


def _terminal_bulk_job(status: str):
    bulk_id = uuid4()
    job = SimpleNamespace(
        id=uuid4(),
        job_type="bulk_ai_upload",
        status=status,
        processed_items=1,
        total_items=2,
        failed_items=0,
        created_at=None,
        completed_at=None,
        reference_id=bulk_id,
    )
    bulk = SimpleNamespace(
        id=bulk_id,
        filename="batch.zip",
        total_files=2,
        status=status,
        deck_id=None,
    )
    return job, bulk


def test_jobs_page_offers_permanent_delete_for_a_failed_upload():
    job, bulk = _terminal_bulk_job("failed")
    db = JobsSettingsDB(jobs=[job], bulks=[bulk])

    response = pages.jobs_page(
        make_request(path="/settings/jobs", query_string=b"tab=history"),
        user=_system_admin(),
        db=db,
    )
    body = render_body(response)

    assert f"/api/v1/bulk-ai-upload/{bulk.id}/purge" in body
    assert "Delete permanently" in body
    assert "cannot be undone" in body


def test_jobs_page_hides_permanent_delete_while_an_upload_runs():
    job, bulk = _terminal_bulk_job("processing")
    db = JobsSettingsDB(jobs=[job], bulks=[bulk])

    response = pages.jobs_page(
        make_request(path="/settings/jobs"),
        user=_system_admin(),
        db=db,
    )
    body = render_body(response)

    # The card renders (cancel is available) but no permanent delete is offered.
    assert f"/api/v1/bulk-ai-upload/{bulk.id}/cancel" in body
    assert f"/api/v1/bulk-ai-upload/{bulk.id}/purge" not in body


# --------------------------------------------------------------------------
# multi-select bulk purge
# --------------------------------------------------------------------------


def _bulk_request(deck_ids):
    request = make_request(path="/settings/deleted-decks")
    request._deck_ids = deck_ids  # kept for readability of the call below
    return request


def test_bulk_purge_removes_every_selected_deck(monkeypatch):
    first, second = _deck(), _deck()
    purged = []

    def fake_purge(db, deck):
        purged.append(deck)
        return {"decks": 1, "cards": 4, "storage_objects": 2}

    monkeypatch.setattr(pages, "purge_deck", fake_purge)

    response = pages.purge_selected_decks(
        deck_ids=[str(first.id), str(second.id)],
        user=_system_admin(),
        db=FakeDB({str(first.id): first, first.id: first, str(second.id): second, second.id: second}),
    )

    location = unquote_plus(response.headers["location"])
    assert response.status_code == 303
    assert [deck for deck in purged] == [first, second]
    assert "2 deck(s) permanently deleted" in location
    assert "10 records" in location
    assert "4 stored objects" in location


def test_bulk_purge_skips_a_deck_that_was_restored(monkeypatch):
    deleted = _deck()
    restored = _deck(is_deleted=False)
    purged = []

    monkeypatch.setattr(
        pages, "purge_deck", lambda db, deck: purged.append(deck) or {"decks": 1}
    )

    response = pages.purge_selected_decks(
        deck_ids=[str(deleted.id), str(restored.id)],
        user=_system_admin(),
        db=FakeDB(
            {
                str(deleted.id): deleted,
                deleted.id: deleted,
                str(restored.id): restored,
                restored.id: restored,
            }
        ),
    )

    location = unquote_plus(response.headers["location"])
    assert purged == [deleted]
    assert "restored since the page loaded" in location
    assert "warning=" in location


def test_bulk_purge_reports_a_refused_deck(monkeypatch):
    deck = _deck()

    def refusing_purge(db, target):
        raise PurgeError("This deck still has a running job.")

    monkeypatch.setattr(pages, "purge_deck", refusing_purge)

    response = pages.purge_selected_decks(
        deck_ids=[str(deck.id)],
        user=_system_admin(),
        db=FakeDB({str(deck.id): deck, deck.id: deck}),
    )

    location = unquote_plus(response.headers["location"])
    assert "No decks were deleted" in location
    assert "still has a running job" in location


def test_bulk_purge_without_a_selection_reports_it(monkeypatch):
    monkeypatch.setattr(
        pages, "purge_deck", lambda db, deck: pytest.fail("nothing should be purged")
    )

    response = pages.purge_selected_decks(
        deck_ids=[], user=_system_admin(), db=FakeDB()
    )

    location = unquote_plus(response.headers["location"])
    assert response.status_code == 303
    assert "No decks were selected" in location


def test_deleted_decks_page_offers_multi_select(monkeypatch):
    monitor, other = _deck(), _deck()
    monkeypatch.setattr(pages, "deleted_decks", lambda db: [monitor, other])

    class PageDB(FakeDB):
        def execute(self, stmt):
            text = str(stmt).lower()
            if "from cards" in text:
                return _Result([(monitor.id, 3), (other.id, 1)])
            return _Result([])

    response = pages._deleted_decks_response(
        make_request(path="/settings/deleted-decks"),
        user=_system_admin(),
        db=PageDB(),
    )
    body = render_body(response)

    assert 'action="/settings/decks/purge-selected"' in body
    # One selectable checkbox per deck, named for the bulk form.
    assert body.count('name="deck_ids"') == 2
    assert f'value="{monitor.id}"' in body
    assert f'value="{other.id}"' in body
    assert body.count("data-purge-select") >= 2
    assert 'id="purge-select-all"' in body
    assert 'id="purge-selected-btn"' in body
    # Responsive layout: the page must not use the jobs table classes, which
    # are scoped to jobs.html's own stylesheet and left this page unstyled.
    assert "jobs-files-table" not in body
    assert "purge-item" in body
