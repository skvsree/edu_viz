from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from fastapi import HTTPException

from app.api.routers import deck_accesses


def test_delete_deck_api_marks_deck_deleted_for_manager():
    deck_id = uuid4()
    deck = SimpleNamespace(id=deck_id, is_deleted=False)
    user = SimpleNamespace(id=uuid4())

    class DeleteDB:
        def __init__(self):
            self.commits = 0

        def get(self, model, value):
            return deck if value == deck_id else None

        def execute(self, stmt):
            return SimpleNamespace(scalars=lambda: [])

        def commit(self):
            self.commits += 1

    db = DeleteDB()

    with patch.object(deck_accesses, "can_manage_deck", return_value=True):
        response = deck_accesses.delete_deck_api(deck_id, user=user, db=db)

    assert response is None
    assert deck.is_deleted is True
    assert db.commits == 1


def test_delete_deck_api_404_for_unknown_deck():
    user = SimpleNamespace(id=uuid4())

    class DeleteDB:
        def get(self, model, value):
            return None

    db = DeleteDB()

    try:
        deck_accesses.delete_deck_api(uuid4(), user=user, db=db)
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_delete_deck_api_404_when_user_cannot_manage():
    deck_id = uuid4()
    deck = SimpleNamespace(id=deck_id, is_deleted=False)
    user = SimpleNamespace(id=uuid4())

    class DeleteDB:
        def get(self, model, value):
            return deck if value == deck_id else None

    db = DeleteDB()

    with patch.object(deck_accesses, "can_manage_deck", return_value=False):
        try:
            deck_accesses.delete_deck_api(deck_id, user=user, db=db)
            assert False, "expected HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 404
