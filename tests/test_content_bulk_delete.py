from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.api.routers import content
from app.services.access import ROLE_ADMIN


class RecordingDB:
    def __init__(self, deck, execute_results):
        self.deck = deck
        self.execute_results = list(execute_results)
        self.statements = []
        self.committed = False

    def get(self, model, key):
        if str(key) == str(self.deck.id):
            return self.deck
        return None

    def execute(self, stmt):
        self.statements.append(str(stmt))
        items = self.execute_results.pop(0) if self.execute_results else []

        class _Result:
            def __init__(self, values):
                self.values = values

            def all(self):
                return self.values

            def scalars(self):
                return self

        return _Result(items)

    def commit(self):
        self.committed = True


def _make_user(org_id):
    return SimpleNamespace(
        id=uuid4(),
        role=ROLE_ADMIN,
        organization_id=org_id,
        email="admin@example.com",
        identity_sub="admin-sub",
        organization=SimpleNamespace(name="Northwind Academy"),
    )


def test_bulk_delete_mcqs_removes_linked_test_attempt_data_before_cards():
    org_id = uuid4()
    deck = SimpleNamespace(id=uuid4(), organization_id=org_id, is_global=False, is_deleted=False)
    card_id = str(uuid4())
    test_id = uuid4()
    db = RecordingDB(
        deck,
        execute_results=[
            [SimpleNamespace(id=card_id, deck_id=deck.id, card_type="mcq")],
            [test_id],
            [],
            [],
            [],
            [],
        ],
    )

    response = content.bulk_delete_mcqs(
        str(deck.id),
        card_ids=[card_id],
        user=_make_user(org_id),
        db=db,
    )

    assert response.headers["location"].startswith(f"/decks/{deck.id}/mcqs?update_success=")
    assert db.committed is True
    assert "FROM test_questions" in db.statements[1]
    assert "DELETE FROM test_attempt_answers" in db.statements[2]
    assert "DELETE FROM test_attempts" in db.statements[3]
    assert "DELETE FROM test_questions" in db.statements[4]
    assert "DELETE FROM tests" in db.statements[5]
    assert "DELETE FROM cards" in db.statements[6]


def test_bulk_delete_flashcards_skips_test_cleanup_when_no_tests_reference_cards():
    org_id = uuid4()
    deck = SimpleNamespace(id=uuid4(), organization_id=org_id, is_global=False, is_deleted=False)
    card_id = str(uuid4())
    db = RecordingDB(
        deck,
        execute_results=[
            [SimpleNamespace(id=card_id, deck_id=deck.id, card_type="basic")],
            [],
            [],
        ],
    )

    response = content.bulk_delete_flashcards(
        str(deck.id),
        card_ids=[card_id],
        user=_make_user(org_id),
        db=db,
    )

    assert response.headers["location"].startswith(f"/decks/{deck.id}/flashcards?update_success=")
    assert db.committed is True
    assert len(db.statements) == 3
    assert "FROM test_questions" in db.statements[1]
    assert "DELETE FROM cards" in db.statements[2]
