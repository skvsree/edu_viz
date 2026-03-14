from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.api.routers import pages
from app.services.access import ROLE_ADMIN, ROLE_SYSTEM_ADMIN, ROLE_USER


class FakeDB:
    def __init__(self, objects: dict[object, object] | None = None, execute_results: list[object] | None = None):
        self.objects = objects or {}
        self.execute_results = execute_results or []
        self.added: list[object] = []
        self.committed = False
        self.rolled_back = False

    def get(self, model, key):
        return self.objects.get(key)

    def add(self, value):
        self.added.append(value)

    def execute(self, stmt):
        class _ScalarResult:
            def __init__(self, items):
                self.items = items

            def scalars(self):
                return self

            def all(self):
                return self.items

        return _ScalarResult(self.execute_results)

    def flush(self):
        pass

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


def test_deck_overview_links_to_split_management_pages():
    org_id = uuid4()
    deck = SimpleNamespace(
        id=uuid4(),
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        user_id=uuid4(),
        name="Biology",
        description="Cells and tissues",
    )
    user = SimpleNamespace(id=uuid4(), role=ROLE_ADMIN, organization_id=org_id, organization=SimpleNamespace(is_ai_enabled=True), is_test_enabled=False)
    cards = [
        SimpleNamespace(card_type="basic"),
        SimpleNamespace(card_type="basic"),
        SimpleNamespace(card_type="mcq"),
    ]

    response = pages.deck_overview(
        make_request(path=f"/decks/{deck.id}"),
        deck_id=str(deck.id),
        user=user,
        db=FakeDB({str(deck.id): deck, deck.id: deck}, execute_results=cards),
    )

    body = render_body(response)
    assert "Open deck" in body
    assert f"/decks/{deck.id}/flashcards" in body
    assert f"/decks/{deck.id}/mcqs" in body
    assert f"/decks/{deck.id}/ai-upload" in body
    assert "2 ready for review" in body
    assert "1 in question bank" in body


def test_flashcards_page_keeps_flashcard_management_together():
    org_id = uuid4()
    deck = SimpleNamespace(
        id=uuid4(),
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        user_id=uuid4(),
        name="Biology",
        description=None,
    )
    flashcard = SimpleNamespace(id=uuid4(), card_type="basic", front="Front", back="Back")
    mcq = SimpleNamespace(id=uuid4(), card_type="mcq", front="Question", back="Answer", mcq_options=["A", "B", "C", "D"], mcq_answer_index=1)
    user = SimpleNamespace(id=uuid4(), role=ROLE_ADMIN, organization_id=org_id)

    response = pages.deck_flashcards(
        make_request(path=f"/decks/{deck.id}/flashcards"),
        deck_id=str(deck.id),
        user=user,
        db=FakeDB({str(deck.id): deck, deck.id: deck}, execute_results=[flashcard, mcq]),
    )

    body = render_body(response)
    assert "Flashcard management" in body
    assert "Add flashcard" in body
    assert "Import flashcards CSV" in body
    assert "Edit flashcard" in body
    assert "Edit MCQ" not in body


def test_mcqs_page_keeps_admin_features_and_mcq_list():
    org_id = uuid4()
    deck = SimpleNamespace(
        id=uuid4(),
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        user_id=uuid4(),
        name="Biology",
        description=None,
    )
    flashcard = SimpleNamespace(id=uuid4(), card_type="basic", front="Front", back="Back")
    mcq = SimpleNamespace(id=uuid4(), card_type="mcq", front="Question", back="Answer", mcq_options=["A", "B", "C", "D"], mcq_answer_index=1)
    user = SimpleNamespace(id=uuid4(), role=ROLE_ADMIN, organization_id=org_id, organization=SimpleNamespace(is_ai_enabled=True), is_test_enabled=True)

    response = pages.deck_mcqs(
        make_request(path=f"/decks/{deck.id}/mcqs"),
        deck_id=str(deck.id),
        user=user,
        db=FakeDB({str(deck.id): deck, deck.id: deck}, execute_results=[flashcard, mcq]),
    )

    body = render_body(response)
    assert "MCQ management" in body
    assert "MCQ JSON import" in body
    assert "AI study generation" not in body
    assert f"/decks/{deck.id}/ai-upload" in body
    assert "Edit MCQ" in body
    assert "Edit flashcard" not in body


def test_create_card_redirects_to_flashcards_page():
    org_id = uuid4()
    deck = SimpleNamespace(id=uuid4(), is_deleted=False, is_global=False, organization_id=org_id, user_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role=ROLE_ADMIN, organization_id=org_id)
    db = FakeDB({str(deck.id): deck, deck.id: deck})

    response = pages.create_card(deck_id=str(deck.id), front="Question", back="Answer", user=user, db=db)

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == f"/decks/{deck.id}/flashcards?import_success=Flashcard+added"
    assert db.committed is True
