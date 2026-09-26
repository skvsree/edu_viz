"""``POST /bulk-ai-upload/{id}/retitle`` renames decks without regenerating.

Why this exists
---------------
Retrying a deck to fix its name would *replace* its cards (the retry wipes the
deck first), so a naming fix must not go through the retry path. This endpoint
reads the stored PDF, recomputes the name from the document's navigation block,
and writes only deck metadata — the test asserts the destructive helpers are
never reached.

It also restores folder placement: a per-deck force retry used to detach the
deck from its folder (see ``test_retry_preserves_deck_folder``), so the four
Class I decks that were retried had to be put back, and this is the only route
available to a caller holding the bulk-import API key (deck move is
session-only).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.api.routers import bulk_ai_upload as router_module
from app.models.bulk_ai_upload import BulkAIUploadFile, BulkAIUploadFileStatus
from app.services.access import normalize_deck_name

MRIDANG_101 = """Two little hands
go clap, clap, clap.
Unit 1
My Family and Me
Chapter 1
Two Little Hands
"""

MRIDANG_102 = """Let us read
When I meet someone in
the afternoon, I say ‘Good
afternoon’.
Chapter 2
Greetings
"""

MRIDANG_103 = """Let us speak
Unit 2
Life Around Us
Chapter 1
Picture Time
"""

MRIDANG_104 = """Once there was a man who
sold caps.
Let us read
Chapter 2
The Cap-seller and the Monkeys
"""

MATHS_NO_NAV = """Finding the
Furry Cat!1
Let us sing
Chapter 1.indd 1 1/17/2025 12:09:01 PM
"""


def _deck(name: str, *, folder_id=None):
    deck = SimpleNamespace(
        id=uuid4(),
        user_id=None,
        organization_id=None,
        is_deleted=False,
        folder_id=folder_id,
        name=name,
        normalized_name=normalize_deck_name(name),
        description="keep me",
    )
    return deck


class _RetitleDB:
    def __init__(self, *, bulk, children, decks, texts):
        self.bulk = bulk
        self.children = children
        self.decks = decks
        self.texts = texts
        self.commits = 0

    def _result(self, values):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: list(values)),
        )

    def execute(self, stmt):
        text = str(stmt)
        if "bulk_ai_upload_child_files" in text:
            return self._result(self.children)
        if "bulk_ai_upload_files" in text:
            return self._result([c.latest_attempt for c in self.children])
        if "FROM decks" in text:
            return self._result(self.decks)
        return self._result([])

    def get(self, model, key):
        for deck in self.decks:
            if deck.id == key:
                return deck
        for child in self.children:
            if child.id == key:
                return child
        if getattr(self.bulk, "id", None) == key:
            return self.bulk
        return None

    def commit(self):
        self.commits += 1


class _FakeStorage:
    """Serves the PDF text per storage key."""

    def __init__(self, texts):
        self.texts = texts

    def open_bytes(self, key):
        if key not in self.texts:
            raise FileNotFoundError(key)
        return (self.texts[key].encode(), "application/pdf")


def _build(monkeypatch, *, rows, folder_id=None):
    """rows: list of (filename, storage_key, source_text, current_deck_name)."""
    user = SimpleNamespace(id=uuid4(), organization_id=None)
    bulk = SimpleNamespace(id=uuid4(), user_id=user.id, status="completed")

    children, decks, texts = [], [], {}
    base = datetime(2026, 9, 25, 12, 0, 0)
    for index, (filename, key, text, deck_name) in enumerate(rows):
        deck = _deck(deck_name)
        deck.user_id = user.id
        file_row = BulkAIUploadFile(
            bulk_upload_id=bulk.id,
            child_file_id=uuid4(),
            created_deck_id=deck.id,
            original_filename=filename,
            extracted_title=deck_name,
            content_text=None,
            storage_key=key,
            status=BulkAIUploadFileStatus.COMPLETED.value,
            flashcards_generated=10,
            mcqs_generated=10,
            duplicate_count=0,
            error_message=None,
            file_size=1,
        )
        file_row.id = uuid4()
        file_row.created_at = base + timedelta(minutes=index)
        child = SimpleNamespace(
            id=file_row.child_file_id,
            bulk_upload_id=bulk.id,
            original_filename=filename,
            latest_attempt=file_row,
            latest_attempt_id=file_row.id,
            display_title=None,
            created_at=base + timedelta(minutes=index),
        )
        children.append(child)
        decks.append(deck)
        texts[key] = text

    db = _RetitleDB(bulk=bulk, children=children, decks=decks, texts=texts)
    monkeypatch.setattr("app.api.routers.bulk_ai_upload.get_storage", lambda: _FakeStorage(texts))
    monkeypatch.setattr(
        "app.services.job_worker.extract_text_from_pdf",
        lambda pdf_bytes: pdf_bytes.decode() if isinstance(pdf_bytes, bytes) else str(pdf_bytes),
    )
    # Anything that would wipe or rewrite cards must explode if reached.

    def _boom(*_a, **_k):
        raise AssertionError("retitle must never touch generated cards")
    monkeypatch.setattr("app.api.routers.bulk_ai_upload._clear_deck_generated_content", _boom)
    return user, bulk, db, decks


def test_retitle_names_decks_from_the_navigation_block(monkeypatch):
    user, bulk, db, decks = _build(
        monkeypatch,
        rows=[
            ("aemr101.pdf", "key/101", MRIDANG_101, "Chapter 1 - Two Little Hands"),
            ("aemr102.pdf", "key/102", MRIDANG_102, "Chapter 02 - Greetings"),
            ("aemr103.pdf", "key/103", MRIDANG_103, "Chapter 02 - Life Around Us"),
            ("aemr104.pdf", "key/104", MRIDANG_104, "Chapter 02 - The Cap-seller and the Monkeys"),
        ],
    )

    result = router_module.retitle_bulk_ai_upload(bulk.id, folder_id=None, user=user, db=db)

    assert result["updated_count"] == 4
    names = [d.name for d in decks]
    assert names == [
        "Unit 1 · Ch 1 · Two Little Hands",
        "Unit 1 · Ch 2 · Greetings",  # aemr102 prints no unit line
        "Unit 2 · Ch 1 · Picture Time",  # the old name said "Chapter 02 - Life Around Us"
        "Unit 2 · Ch 2 · The Cap-seller and the Monkeys",  # carried from aemr103
    ]
    assert len(set(names)) == 4, "derived names must be unique"
    for deck in decks:
        assert deck.normalized_name == normalize_deck_name(deck.name)


def test_retitle_applies_the_target_folder(monkeypatch):
    folder_id = uuid4()
    user, bulk, db, decks = _build(
        monkeypatch,
        rows=[("aemr101.pdf", "key/101", MRIDANG_101, "Chapter 1 - Two Little Hands")],
    )

    result = router_module.retitle_bulk_ai_upload(bulk.id, folder_id=folder_id, user=user, db=db)

    assert result["folder_id"] == str(folder_id)
    assert all(deck.folder_id == folder_id for deck in decks), (
        "retitle with folder_id must re-place detached decks"
    )


def test_files_without_a_navigation_block_keep_their_name(monkeypatch):
    """Joyful Mathematics has no printed nav block; its unique AI names stay."""
    folder_id = uuid4()
    user, bulk, db, decks = _build(
        monkeypatch,
        rows=[("aejm101.pdf", "key/m1", MATHS_NO_NAV, "Chapter 01 - Finding the Furry Cat!")],
    )

    result = router_module.retitle_bulk_ai_upload(bulk.id, folder_id=folder_id, user=user, db=db)

    assert result["updated_count"] == 0
    assert result["skipped_count"] == 1
    assert decks[0].name == "Chapter 01 - Finding the Furry Cat!"
    # Still re-placed, because placement is independent of the title.
    assert decks[0].folder_id == folder_id


def test_same_derived_name_twice_gets_a_suffix(monkeypatch):
    """Two uploads of the same chapter must not end up sharing one deck name."""
    user, bulk, db, decks = _build(
        monkeypatch,
        rows=[
            ("aemr101.pdf", "key/a", MRIDANG_101, "Chapter 1 - Two Little Hands"),
            ("aemr101-copy.pdf", "key/b", MRIDANG_101, "Chapter 1 - Two Little Hands copy"),
        ],
    )

    router_module.retitle_bulk_ai_upload(bulk.id, folder_id=None, user=user, db=db)

    assert decks[0].name == "Unit 1 · Ch 1 · Two Little Hands"
    assert decks[1].name == "Unit 1 · Ch 1 · Two Little Hands (2)"
    assert decks[0].normalized_name != decks[1].normalized_name
