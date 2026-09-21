"""The retry popup must state what a regeneration wipe will delete.

A retry runs _clear_deck_generated_content(), which removes every card, card
state and review in the deck, so the confirmation popup asks the server for the
counts first. These tests drive the helper and the route with a session double.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.routers import bulk_ai_upload
from app.services.purge import deck_wipe_preview


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _PreviewSession:
    """Answers the lookups deck_wipe_preview and the route make."""

    def __init__(self, *, deck=None, file_record=None, card_ids=(), reviews=0, card_states=0):
        self.deck = deck
        self.file_record = file_record
        self.card_ids = list(card_ids)
        self.reviews = reviews
        self.card_states = card_states
        self.statements: list[str] = []

    def get(self, model, ident):
        name = getattr(model, "__name__", "")
        if name == "Deck":
            return self.deck
        if name == "BulkAIUploadFile":
            return self.file_record
        return None

    def execute(self, statement):
        text = str(statement).lower()
        self.statements.append(text)
        if "count(" in text:
            if "reviews" in text:
                return _Scalar(self.reviews)
            if "card_states" in text:
                return _Scalar(self.card_states)
            return _Scalar(len(self.card_ids))
        return _Rows(self.card_ids)


def _deck(name="Chapter 05"):
    return SimpleNamespace(id=uuid4(), name=name)


def test_preview_reports_cards_reviews_and_states():
    deck = _deck()
    session = _PreviewSession(
        deck=deck,
        card_ids=[uuid4() for _ in range(5)],
        reviews=7,
        card_states=5,
    )

    preview = deck_wipe_preview(session, deck.id)

    assert preview["has_deck"] is True
    assert preview["deck_name"] == "Chapter 05"
    assert preview["cards"] == 5
    assert preview["reviews"] == 7
    assert preview["card_states"] == 5


def test_preview_without_a_deck_says_nothing_will_be_deleted():
    session = _PreviewSession(deck=None)

    assert deck_wipe_preview(session, None)["has_deck"] is False
    assert deck_wipe_preview(session, uuid4())["has_deck"] is False
    # No card lookup should be attempted when there is no deck.
    assert all("cards" not in statement for statement in session.statements)


def test_preview_skips_count_queries_for_an_empty_deck():
    deck = _deck()
    session = _PreviewSession(deck=deck, card_ids=[])

    preview = deck_wipe_preview(session, deck.id)

    assert preview["cards"] == 0 and preview["reviews"] == 0
    assert not any("count(" in statement for statement in session.statements)


def test_route_returns_counts_for_the_file_deck():
    deck = _deck("Measurement of Length and Motion")
    file_id = uuid4()
    session = _PreviewSession(
        deck=deck,
        file_record=SimpleNamespace(id=file_id, created_deck_id=deck.id),
        card_ids=[uuid4(), uuid4()],
        reviews=3,
        card_states=2,
    )

    preview = bulk_ai_upload.regeneration_preview(
        file_id=file_id,
        user=SimpleNamespace(role="system_admin"),
        db=session,
    )

    assert preview["file_id"] == str(file_id)
    assert preview["deck_name"] == "Measurement of Length and Motion"
    assert preview["cards"] == 2 and preview["reviews"] == 3


def test_route_rejects_non_admins():
    session = _PreviewSession()

    with pytest.raises(HTTPException) as excinfo:
        bulk_ai_upload.regeneration_preview(
            file_id=uuid4(),
            user=SimpleNamespace(role="user"),
            db=session,
        )

    assert excinfo.value.status_code == 403


def test_route_404s_for_an_unknown_file():
    session = _PreviewSession(file_record=None)

    with pytest.raises(HTTPException) as excinfo:
        bulk_ai_upload.regeneration_preview(
            file_id=uuid4(),
            user=SimpleNamespace(role="system_admin"),
            db=session,
        )

    assert excinfo.value.status_code == 404
