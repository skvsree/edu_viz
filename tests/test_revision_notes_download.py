"""Tests for one-click revision notes (generate on demand, then download).

The deck page button hits ``GET /decks/{id}/revision-notes.pdf`` directly: a
ready PDF is reused, anything else is generated inline. Before this, the deck
button led to a separate concept map page and generation was queued to a job
handler that called a function which did not exist, so the download always
failed.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.routers import pages
from app.services import concept_map as cm
from app.services.access import ROLE_ADMIN
from tests.test_dashboard_routes import FakeDB, make_request


def _deck():
    return SimpleNamespace(
        id=uuid4(),
        name="Biology",
        description="Cells and tissues",
        user_id=uuid4(),
        is_deleted=False,
        is_global=False,
        organization_id=None,
    )


def _concept_map(deck, *, status=None, key=None):
    return SimpleNamespace(
        id=uuid4(),
        deck_id=deck.id,
        source_file_id=None,
        status="ready",
        title="Cells",
        revision_pdf_status=status,
        revision_pdf_storage_key=key,
        error_message=None,
        completed_at=None,
    )


def _user():
    return SimpleNamespace(
        id=uuid4(),
        role=ROLE_ADMIN,
        organization_id=None,
        email="admin@example.com",
        identity_sub="admin-sub",
    )


class _Query:
    """Chainable stand-in for the legacy ``db.query(...)`` builder."""

    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, count):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


def _db_with_cards(cards):
    return SimpleNamespace(
        query=lambda *a, **k: _Query(cards),
        get=lambda model, key: None,
        commit=lambda: None,
        add=lambda value: None,
        refresh=lambda value: None,
    )


# --------------------------------------------------------------------------
# source text resolution
# --------------------------------------------------------------------------


def test_revision_source_prefers_the_uploaded_chapter(monkeypatch):
    monkeypatch.setattr(cm, "_extracted_payload", lambda row: "U" * 400)
    concept_map = SimpleNamespace(source_file_id=uuid4(), deck_id=uuid4())
    db = SimpleNamespace(get=lambda model, key: SimpleNamespace())

    text = cm._concept_map_source_text(db, concept_map, max_chars=1000)

    assert text == "U" * 400


def test_revision_source_falls_back_to_the_deck_cards(monkeypatch):
    cards = [
        SimpleNamespace(front="Photosynthesis needs light", back="chlorophyll"),
        SimpleNamespace(front="Respiration releases energy", back="mitochondria"),
    ]
    concept_map = SimpleNamespace(source_file_id=None, deck_id=uuid4())

    text = cm._concept_map_source_text(
        _db_with_cards(cards), concept_map, max_chars=1000
    )

    assert "Photosynthesis needs light" in text
    assert "mitochondria" in text


def test_revision_source_respects_the_char_cap():
    cards = [SimpleNamespace(front="x" * 500, back="y" * 500)]
    concept_map = SimpleNamespace(source_file_id=None, deck_id=uuid4())

    text = cm._concept_map_source_text(
        _db_with_cards(cards), concept_map, max_chars=120
    )

    assert len(text) == 120


# --------------------------------------------------------------------------
# generation contract
# --------------------------------------------------------------------------


def test_generation_records_failure_instead_of_raising():
    """A thin source must end as 'failed' with a reason, never an exception."""
    deck = _deck()
    concept_map = _concept_map(deck)
    db = FakeDB(
        objects={concept_map.id: concept_map, deck.id: deck},
        query_results=[],
    )

    result = cm.generate_concept_map_revision_pdf(
        db, concept_map_id=concept_map.id, credential_provider_name="opencode",
        credential=None,
    )

    assert result is concept_map
    assert concept_map.revision_pdf_status == "failed"
    assert "Not enough content" in concept_map.error_message
    assert db.committed is True


def test_generation_stores_the_pdf_and_marks_it_ready(monkeypatch):
    deck = _deck()
    concept_map = _concept_map(deck)
    topic = SimpleNamespace(title="Cells", sections=[("Cells", "bullets")])
    saved = {}

    class Storage:
        def save_bytes(self, *, key, data, content_type=None):
            saved["key"] = key
            saved["bytes"] = data
            return SimpleNamespace(key=key)

    monkeypatch.setattr(cm, "heuristic_topics", lambda text, max_topics=9: [topic])
    monkeypatch.setattr(
        cm, "_concept_map_source_text",
        lambda db, concept_map, max_chars: "Cell theory " * 60,
    )
    from app.services import revision_notes as RN

    monkeypatch.setattr(RN, "_attach_payloads", lambda text, topics: topics)
    monkeypatch.setattr(
        RN, "_build_revision_doc", lambda **kwargs: SimpleNamespace(**kwargs)
    )
    monkeypatch.setattr(
        RN, "render_revision_pdf", lambda doc, name: (b"%PDF-1.4 fake", 3)
    )
    import app.services.storage as storage_module

    monkeypatch.setattr(storage_module, "get_storage", lambda: Storage())

    db = FakeDB(objects={concept_map.id: concept_map, deck.id: deck})

    result = cm.generate_concept_map_revision_pdf(
        db, concept_map_id=concept_map.id, credential=None
    )

    assert result.revision_pdf_status == "ready"
    assert concept_map.revision_pdf_storage_key == cm.revision_pdf_key(concept_map.id)
    assert saved["bytes"] == b"%PDF-1.4 fake"
    assert saved["key"].startswith("concept_maps/")


# --------------------------------------------------------------------------
# one-click download
# --------------------------------------------------------------------------


def test_ensure_reuses_a_ready_pdf_without_regenerating(monkeypatch):
    deck = _deck()
    concept_map = _concept_map(deck, status="ready", key="concept_maps/x/y.pdf")
    db = FakeDB(objects={deck.id: deck}, query_results=[concept_map])

    def explode(*args, **kwargs):
        raise AssertionError("must not regenerate a ready PDF")

    monkeypatch.setattr("app.services.concept_map.generate_concept_map_revision_pdf", explode)

    result = pages.ensure_deck_revision_pdf(db, deck=deck, user=_user())

    assert result is concept_map


def test_ensure_generates_when_not_ready(monkeypatch):
    deck = _deck()
    concept_map = _concept_map(deck)
    db = FakeDB(objects={deck.id: deck}, query_results=[concept_map])
    calls = {}

    def fake_generate(db, *, concept_map_id, credential_provider_name=None, credential=None):
        calls["concept_map_id"] = concept_map_id
        calls["provider"] = credential_provider_name
        concept_map.revision_pdf_status = "ready"
        concept_map.revision_pdf_storage_key = "concept_maps/x/y.pdf"
        return concept_map

    monkeypatch.setattr(
        "app.services.concept_map.generate_concept_map_revision_pdf", fake_generate
    )
    monkeypatch.setattr(pages, "_deck_ai_credential", lambda db, user: ("opencode", None))

    result = pages.ensure_deck_revision_pdf(db, deck=deck, user=_user())

    assert calls["concept_map_id"] == concept_map.id
    assert calls["provider"] == "opencode"
    assert result.revision_pdf_status == "ready"


def test_download_route_streams_the_stored_pdf(monkeypatch):
    deck = _deck()
    user = _user()
    deck.user_id = user.id  # the deck owner is the one downloading
    concept_map = _concept_map(deck, status="ready", key="concept_maps/x/y.pdf")

    monkeypatch.setattr(
        pages, "ensure_deck_revision_pdf", lambda db, *, deck, user: concept_map
    )

    class Storage:
        def open_bytes(self, *, key):
            return b"%PDF-1.4 stored", "application/pdf"

    import app.services.storage as storage_module

    monkeypatch.setattr(storage_module, "get_storage", lambda: Storage())

    response = pages.deck_revision_notes_download(
        make_request(path=f"/decks/{deck.id}/revision-notes.pdf"),
        deck_id=str(deck.id),
        user=user,
        db=FakeDB({str(deck.id): deck, deck.id: deck}),
    )

    assert response.status_code == 200
    assert response.body == b"%PDF-1.4 stored"
    assert "attachment" in response.headers["content-disposition"]
    assert "Revision Notes.pdf" in response.headers["content-disposition"]


def test_download_route_explains_a_generation_failure(monkeypatch):
    deck = _deck()
    user = _user()
    deck.user_id = user.id
    concept_map = _concept_map(deck, status="failed")
    concept_map.error_message = "Revision notes generation failed: no source text"

    monkeypatch.setattr(
        pages, "ensure_deck_revision_pdf", lambda db, *, deck, user: concept_map
    )

    response = pages.deck_revision_notes_download(
        make_request(path=f"/decks/{deck.id}/revision-notes.pdf"),
        deck_id=str(deck.id),
        user=user,
        db=FakeDB({str(deck.id): deck, deck.id: deck}),
    )

    body = response.body.decode()
    assert response.status_code == 502
    assert "could not be generated" in body
    assert "no source text" in body
    assert f"/decks/{deck.id}" in body  # link back to the deck


def test_download_route_requires_access():
    deck = _deck()
    deck.organization_id = uuid4()

    with pytest.raises(Exception) as excinfo:
        pages.deck_revision_notes_download(
            make_request(path=f"/decks/{deck.id}/revision-notes.pdf"),
            deck_id=str(deck.id),
            user=_user(),  # no org access to this deck
            db=FakeDB({str(deck.id): deck, deck.id: deck}),
        )

    assert getattr(excinfo.value, "status_code", None) == 404
