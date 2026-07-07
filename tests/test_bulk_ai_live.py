"""Test the change-detection logic for bulk-AI live updates.

The full SSE endpoint is hard to unit-test (async generator, ASGI
client, real DB), so the logic that decides *whether* to emit an event
is extracted into a pure function. The SSE endpoint becomes a thin
wrapper that polls the DB and calls this function.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.api.routers import deck_live


def _bulk(
    *,
    status="processing",
    processed_files=0,
    total_files=14,
    flashcards_generated=0,
    mcqs_generated=0,
    error_message=None,
):
    return SimpleNamespace(
        id=uuid4(),
        status=status,
        processed_files=processed_files,
        total_files=total_files,
        flashcards_generated=flashcards_generated,
        mcqs_generated=mcqs_generated,
        error_message=error_message,
    )


def test_bulk_state_dict_returns_expected_fields():
    bulk = _bulk(flashcards_generated=42, mcqs_generated=21, processed_files=2)
    state = deck_live.bulk_state_dict(bulk)
    assert state["id"] == str(bulk.id)
    assert state["status"] == "processing"
    assert state["processed_files"] == 2
    assert state["total_files"] == 14
    assert state["flashcards_generated"] == 42
    assert state["mcqs_generated"] == 21


def test_bulk_state_dict_normalises_none_counts():
    bulk = _bulk(flashcards_generated=None, mcqs_generated=None)
    state = deck_live.bulk_state_dict(bulk)
    assert state["flashcards_generated"] == 0
    assert state["mcqs_generated"] == 0


def test_bulk_state_changed_returns_true_on_card_count_change():
    """The whole point of the SSE stream: emit a heartbeat whenever
    running counts change so the UI can update without polling."""
    before = deck_live.bulk_state_dict(
        _bulk(flashcards_generated=10, mcqs_generated=5)
    )
    after = deck_live.bulk_state_dict(
        _bulk(flashcards_generated=15, mcqs_generated=8)
    )
    assert deck_live.bulk_state_changed(before, after) is True


def test_bulk_state_changed_returns_true_on_status_change():
    before = deck_live.bulk_state_dict(_bulk(status="processing"))
    after = deck_live.bulk_state_dict(_bulk(status="completed"))
    assert deck_live.bulk_state_changed(before, after) is True


def test_bulk_state_changed_returns_true_on_processed_files_change():
    before = deck_live.bulk_state_dict(_bulk(processed_files=1))
    after = deck_live.bulk_state_dict(_bulk(processed_files=2))
    assert deck_live.bulk_state_changed(before, after) is True


def test_bulk_state_changed_returns_false_when_unchanged():
    state = deck_live.bulk_state_dict(_bulk())
    # Same state twice -> no change.
    assert deck_live.bulk_state_changed(state, state) is False


def test_bulk_state_changed_ignores_error_message_changes():
    """error_message is not displayed on the live badge so we don't need
    to fire events when only that field changes (would just spam the
    browser)."""
    before = deck_live.bulk_state_dict(_bulk(error_message="first"))
    after = deck_live.bulk_state_dict(_bulk(error_message="second"))
    assert deck_live.bulk_state_changed(before, after) is False


def test_bulk_state_dict_omits_id_field_for_comparison_correctness():
    """Two different bulks with the same numeric state should compare
    equal (the change detector is per-bulk, but defensive: don't let
    the id field cause false positives)."""
    a = deck_live.bulk_state_dict(_bulk())
    b = deck_live.bulk_state_dict(_bulk())
    a["id"] = "id-1"
    b["id"] = "id-2"
    # bulk_state_changed should not care about id.
    assert deck_live.bulk_state_changed(a, b) is False
