from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from app.api.routers import content, pages
from tests.test_dashboard_routes import FakeDB, make_request, render_body


def test_review_page_shows_quantity_picker_before_session_starts():
    org_id = uuid4()
    deck = SimpleNamespace(id=uuid4(), is_deleted=False, is_global=False, organization_id=org_id, user_id=uuid4(), name="Biology")
    user = SimpleNamespace(id=uuid4(), role="admin", organization_id=org_id)

    response = pages.review_page(make_request(path="/review", query_string=f"deck_id={deck.id}".encode()), user=user, db=FakeDB({str(deck.id): deck, deck.id: deck}))

    body = render_body(response)
    assert "Choose how many flashcards to study" in body
    assert 'name="count"' in body
    assert "Start review" in body


def test_review_page_autoloads_session_when_count_is_selected():
    org_id = uuid4()
    deck = SimpleNamespace(id=uuid4(), is_deleted=False, is_global=False, organization_id=org_id, user_id=uuid4(), name="Biology")
    user = SimpleNamespace(id=uuid4(), role="admin", organization_id=org_id)

    response = pages.review_page(make_request(path="/review", query_string=f"deck_id={deck.id}&count=25".encode()), user=user, db=FakeDB({str(deck.id): deck, deck.id: deck}))

    body = render_body(response)
    assert f"/review/next?remaining=25&deck_id={deck.id}" in body


def test_take_test_page_shows_launch_picker_before_questions():
    org_id = uuid4()
    deck = SimpleNamespace(id=uuid4(), is_deleted=False, is_global=False, organization_id=org_id, user_id=uuid4())
    questions = [
        SimpleNamespace(id=uuid4(), position=i, card=SimpleNamespace(front=f"Q{i}", mcq_options=["A", "B", "C", "D"]))
        for i in range(1, 13)
    ]
    test = SimpleNamespace(id=uuid4(), title="Quiz", description="", deck=deck, questions=questions)
    user = SimpleNamespace(id=uuid4(), role="admin", organization_id=org_id, is_test_enabled=True)
    db = FakeDB()

    class Result:
        def scalar_one_or_none(self):
            return test

    db.execute = lambda stmt: Result()
    response = content.take_test_page(str(test.id), make_request(path=f"/tests/{test.id}"), user=user, db=db)

    body = render_body(response)
    assert "Choose how many questions to take now" in body
    assert 'name="count"' in body
    assert "12 questions available" in body


def test_take_test_page_renders_selected_number_of_questions():
    org_id = uuid4()
    deck = SimpleNamespace(id=uuid4(), is_deleted=False, is_global=False, organization_id=org_id, user_id=uuid4())
    questions = [
        SimpleNamespace(id=uuid4(), position=i, card=SimpleNamespace(front=f"Q{i}", mcq_options=["A", "B", "C", "D"]))
        for i in range(1, 13)
    ]
    test = SimpleNamespace(id=uuid4(), title="Quiz", description="", deck=deck, questions=questions)
    user = SimpleNamespace(id=uuid4(), role="admin", organization_id=org_id, is_test_enabled=True)
    db = FakeDB()

    class Result:
        def scalar_one_or_none(self):
            return test

    db.execute = lambda stmt: Result()
    response = content.take_test_page(str(test.id), make_request(path=f"/tests/{test.id}", query_string=b"count=10"), user=user, db=db)

    body = render_body(response)
    assert "Showing 10 of 12 available questions" in body
    assert body.count('name="question_ids"') == 10
    assert "Q10. Q10" in body
    assert "Q11. Q11" not in body


def test_create_test_builds_full_bank_without_question_count():
    org_id = uuid4()
    deck = SimpleNamespace(id=uuid4(), is_deleted=False, is_global=False, organization_id=org_id, user_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="admin", organization_id=org_id, is_test_enabled=True)
    db = FakeDB({str(deck.id): deck, deck.id: deck})

    with patch.object(content, "create_test_from_deck") as create_mock:
        response = content.create_test(deck_id=str(deck.id), title="Quiz", description="Full bank", user=user, db=db)

    assert response.status_code == 303
    kwargs = create_mock.call_args.kwargs
    assert kwargs["title"] == "Quiz"
    assert kwargs["description"] == "Full bank"
    assert "question_count" not in kwargs
