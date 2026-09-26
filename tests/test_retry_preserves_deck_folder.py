"""Force-retrying a single deck must NOT strip the deck's folder.

Symptom (prod, 2026-09-26)
--------------------------
Force-retrying one chapter deck of an already-completed bulk upload (the
per-deck retry button, or ``POST /api/v1/bulk-ai-upload/{id}/resume
?deck_id=…&force=true``) moved that deck out of its folder to the root.

Cause: ``_prepare_fresh_retry_attempt`` called ``_ensure_bulk_upload_deck``
with ``folder_id=None`` for the existing deck, and that helper assigns the
value unconditionally when it differs::

    if existing.folder_id != folder_id:
        existing.folder_id = folder_id

so a retry silently detached the deck. The bulk row can no longer repair the
placement either, because the retry path clears ``bulk.error_message`` (where
``folder_id=…`` was stashed for the worker) before the worker runs.

Four Class I NCERT decks lost their ``India > ncert > Class_I > {English,
Mathematics}`` placement this way.

Fix: carry the deck's current folder into the retry row's deck.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.api.routers import bulk_ai_upload as router_module
from app.models.bulk_ai_upload import BulkAIUploadFileStatus


class _FakeDeck:
    def __init__(self, *, user_id, folder_id):
        self.id = uuid4()
        self.user_id = user_id
        self.folder_id = folder_id
        self.is_deleted = False
        self.name = "Chapter 02 - Greetings"
        self.normalized_name = "chapter 02 greetings"
        self.description = "old description"


class _FakeDB:
    def __init__(self, deck):
        self.deck = deck
        self.added: list = []
        self.commits = 0

    def get(self, model, key):
        if model is router_module.Deck and str(key) == str(self.deck.id):
            return self.deck
        return None

    def add(self, value):
        self.added.append(value)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1


def _prepare(monkeypatch, *, folder_id):
    user = SimpleNamespace(id=uuid4(), organization_id=None)
    deck = _FakeDeck(user_id=user.id, folder_id=folder_id)
    db = _FakeDB(deck)

    monkeypatch.setattr(
        "app.api.routers.bulk_ai_upload._clear_deck_generated_content",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.api.routers.bulk_ai_upload._recompute_child_latest_attempt",
        lambda *a, **k: None,
    )

    bulk = SimpleNamespace(id=uuid4(), filename="aemr1.zip")
    source_file = SimpleNamespace(
        id=uuid4(),
        created_deck_id=deck.id,
        original_filename="aemr102.pdf",
        storage_key="bulk/aemr102.pdf",
        file_size=1234,
        extracted_title="Chapter 02 - Greetings",
        extracted_description="old description",
    )
    child_file = SimpleNamespace(id=uuid4(), latest_attempt_id=None, display_title=None)

    retry_row = router_module._prepare_fresh_retry_attempt(
        db,
        bulk=bulk,
        user=user,
        source_file=source_file,
        child_file=child_file,
    )
    return deck, retry_row


def test_retry_keeps_the_deck_in_its_folder(monkeypatch):
    """A deck that lived in a folder must still live in that folder after a retry."""
    folder_id = uuid4()
    deck, retry_row = _prepare(monkeypatch, folder_id=folder_id)

    assert deck.folder_id == folder_id, (
        "force-retrying a single deck detached it from its folder "
        f"(folder_id became {deck.folder_id!r} instead of {folder_id!r}). "
        "The retry must preserve the deck's existing folder placement."
    )
    assert str(retry_row.created_deck_id) == str(deck.id)
    assert retry_row.status == BulkAIUploadFileStatus.PENDING.value


def test_retry_of_a_folderless_deck_stays_at_root(monkeypatch):
    """A deck with no folder (already at root) must not gain a folder."""
    deck, _retry_row = _prepare(monkeypatch, folder_id=None)
    assert deck.folder_id is None
