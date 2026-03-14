from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.api.routers import pages
from app.services.access import ROLE_ADMIN, ROLE_USER


class FakeDB:
    def __init__(self, objects: dict[object, object] | None = None):
        self.objects = objects or {}
        self.added: list[object] = []
        self.committed = False
        self.rolled_back = False

    def get(self, model, key):
        return self.objects.get(key)

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def make_request(path: str = "/dashboard", query_string: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "query_string": query_string,
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
    )


def render_body(response) -> str:
    return response.body.decode("utf-8")


def test_dashboard_shows_deck_actions_for_manageable_deck():
    org_id = uuid4()
    user = SimpleNamespace(
        id=uuid4(),
        role=ROLE_ADMIN,
        organization_id=org_id,
        email="admin@example.com",
        identity_sub="admin-sub",
        organization=SimpleNamespace(name="Northwind Academy"),
    )
    deck = SimpleNamespace(
        id=uuid4(),
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        organization=SimpleNamespace(name="Northwind Academy"),
        user_id=uuid4(),
        name="Biology fundamentals",
        description="Core intro deck",
        tags=[],
    )
    deck_stats = [
        SimpleNamespace(
            deck=deck,
            cards_reviewed=12,
            cards_due=3,
            accuracy=0.8,
            last_reviewed=None,
        )
    ]
    with patch.object(pages, "list_accessible_deck_stats", lambda db, *, user: deck_stats):
        response = pages.dashboard(make_request(), user=user, db=FakeDB({deck.id: deck}))

    body = render_body(response)
    assert "Add deck" in body
    assert 'id="create-deck-modal"' in body
    assert "Review" in body
    assert f"/review?deck_id={deck.id}" in body
    assert "Open deck" in body
    assert "Edit" in body
    assert "Delete" in body
    assert f"/decks/{deck.id}/delete" in body


def test_create_deck_validation_error_reopens_create_modal():
    user = SimpleNamespace(
        id=uuid4(),
        role=ROLE_ADMIN,
        organization_id=uuid4(),
        email="admin@example.com",
        identity_sub="admin-sub",
        organization=SimpleNamespace(name="Northwind Academy"),
    )
    with patch.object(pages, "list_accessible_deck_stats", lambda db, *, user: []):
        response = pages.create_deck(
            make_request(),
            name="   ",
            description="Draft",
            is_global=False,
            user=user,
            db=FakeDB(),
        )

    body = render_body(response)
    assert response.status_code == 400
    assert "Deck name is required." in body
    assert 'id="create-deck-modal"' in body
    assert 'data-open-on-load="true"' in body
    assert 'Create a new study space' in body


def test_update_deck_validation_error_reopens_edit_modal():
    org_id = uuid4()
    user = SimpleNamespace(
        id=uuid4(),
        role=ROLE_ADMIN,
        organization_id=org_id,
        email="admin@example.com",
        identity_sub="admin-sub",
        organization=SimpleNamespace(name="Northwind Academy"),
    )
    deck = SimpleNamespace(
        id=uuid4(),
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        organization=SimpleNamespace(name="Northwind Academy"),
        user_id=uuid4(),
        name="Chemistry",
        description="Atoms and bonds",
        tags=[],
    )
    with patch.object(pages, "list_accessible_deck_stats", lambda db, *, user: []):
        response = pages.update_deck(
            make_request(),
            deck_id=str(deck.id),
            name="   ",
            description="Next draft",
            user=user,
            db=FakeDB({str(deck.id): deck, deck.id: deck}),
        )

    body = render_body(response)
    assert response.status_code == 400
    assert "Deck name cannot be empty." in body
    assert 'id="edit-deck-modal"' in body
    assert 'data-open-on-load="true"' in body
    assert "Chemistry" in body


def test_delete_deck_soft_deletes_and_redirects():
    org_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role=ROLE_ADMIN, organization_id=org_id)
    deck = SimpleNamespace(
        id=uuid4(),
        is_deleted=False,
        deleted_at=None,
        is_global=False,
        organization_id=org_id,
        user_id=uuid4(),
    )
    db = FakeDB({str(deck.id): deck, deck.id: deck})

    response = pages.delete_deck(deck_id=str(deck.id), user=user, db=db)

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard?success=Deck+deleted"
    assert deck.is_deleted is True
    assert deck.deleted_at is not None
    assert db.committed is True


def test_dashboard_hides_management_actions_for_regular_user():
    org_id = uuid4()
    user = SimpleNamespace(
        id=uuid4(),
        role=ROLE_USER,
        organization_id=org_id,
        email="student@example.com",
        identity_sub="student-sub",
        organization=SimpleNamespace(name="Northwind Academy"),
    )
    deck = SimpleNamespace(
        id=uuid4(),
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        organization=SimpleNamespace(name="Northwind Academy"),
        user_id=uuid4(),
        name="History",
        description=None,
        tags=[],
    )
    with patch.object(
        pages,
        "list_accessible_deck_stats",
        lambda db, *, user: [SimpleNamespace(deck=deck, cards_reviewed=0, cards_due=0, accuracy=None, last_reviewed=None)],
    ):
        response = pages.dashboard(make_request(), user=user, db=FakeDB())

    body = render_body(response)
    assert "Add deck" not in body
    assert "Delete" not in body
    assert "Edit" not in body
    assert "Review" in body
