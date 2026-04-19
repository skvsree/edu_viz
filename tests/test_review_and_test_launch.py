from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from starlette.datastructures import FormData

from app.api.routers import content, pages
from tests.test_dashboard_routes import FakeDB, make_request, render_body


def test_review_page_starts_review_without_count_selection():
    org_id = uuid4()
    deck = SimpleNamespace(
        id=uuid4(),
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        user_id=uuid4(),
        name="Biology",
    )
    user = SimpleNamespace(id=uuid4(), role="admin", organization_id=org_id)

    response = pages.review_page(
        make_request(path="/review", query_string=f"deck_id={deck.id}".encode()),
        user=user,
        db=FakeDB({str(deck.id): deck, deck.id: deck}),
    )

    body = render_body(response)
    assert "Choose how many flashcards to study" not in body
    assert 'name="count"' not in body
    assert f"/review/next?deck_id={deck.id}" in body


def test_review_page_autoloads_session_when_count_is_selected():
    org_id = uuid4()
    deck = SimpleNamespace(
        id=uuid4(),
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        user_id=uuid4(),
        name="Biology",
    )
    user = SimpleNamespace(id=uuid4(), role="admin", organization_id=org_id)

    response = pages.review_page(
        make_request(
            path="/review", query_string=f"deck_id={deck.id}&count=25".encode()
        ),
        user=user,
        db=FakeDB({str(deck.id): deck, deck.id: deck}),
    )

    body = render_body(response)
    assert "/review/next?" in body
    assert f"deck_id={deck.id}" in body
    assert "remaining=25" in body


def test_take_test_page_renders_all_questions_directly():
    org_id = uuid4()
    deck = SimpleNamespace(
        id=uuid4(),
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        user_id=uuid4(),
    )
    questions = [
        SimpleNamespace(
            id=uuid4(),
            position=i,
            card=SimpleNamespace(front=f"Q{i}", mcq_options=["A", "B", "C", "D"]),
        )
        for i in range(1, 13)
    ]
    test = SimpleNamespace(
        id=uuid4(),
        title="Test taken @ 2026-03-17 18:30",
        description=None,
        deck=deck,
        questions=questions,
    )
    user = SimpleNamespace(
        id=uuid4(), role="admin", organization_id=org_id, is_test_enabled=True
    )
    db = FakeDB()

    class Result:
        def scalar_one_or_none(self):
            return test

    db.execute = lambda stmt: Result()
    response = content.take_test_page(
        str(test.id), make_request(path=f"/tests/{test.id}"), user=user, db=db
    )

    body = render_body(response)
    assert "Test in progress" in body
    assert body.count('name="question_ids"') == 12
    assert "1 of 12" in body


def test_create_test_auto_generates_title_and_redirects():
    org_id = uuid4()
    deck = SimpleNamespace(
        id=uuid4(),
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        user_id=uuid4(),
    )
    user = SimpleNamespace(
        id=uuid4(), role="admin", organization_id=org_id, is_test_enabled=True
    )
    db = FakeDB({str(deck.id): deck, deck.id: deck})

    fake_test = SimpleNamespace(id=uuid4())
    with patch.object(
        content, "create_test_from_deck", return_value=fake_test
    ) as create_mock:
        response = content.create_test(deck_id=str(deck.id), count=25, user=user, db=db)

    assert response.status_code == 303
    assert f"/tests/{fake_test.id}" in response.headers["location"]
    kwargs = create_mock.call_args.kwargs
    assert kwargs["question_count"] == 25
    assert "title" not in kwargs


def test_submit_test_ignores_question_ids_hidden_inputs_when_parsing_answers():
    org_id = uuid4()
    deck = SimpleNamespace(
        id=uuid4(),
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        user_id=uuid4(),
    )
    test = SimpleNamespace(id=uuid4(), deck=deck)
    user = SimpleNamespace(
        id=uuid4(), role="admin", organization_id=org_id, is_test_enabled=True
    )
    db = FakeDB()

    class Result:
        def scalar_one_or_none(self):
            return test

    db.execute = lambda stmt: Result()

    question_one = uuid4()
    question_two = uuid4()
    captured: dict[str, object] = {}

    class FakeRequest:
        async def form(self):
            return FormData(
                [
                    ("question_ids", str(question_one)),
                    (f"question_{question_one}", "2"),
                    ("question_ids", str(question_two)),
                    (f"question_{question_two}", "1"),
                ]
            )

    def fake_submit_attempt(db, *, test, user_id, answers, question_ids=None):
        captured["answers"] = answers
        captured["question_ids"] = question_ids
        return SimpleNamespace(id=uuid4())

    with patch.object(content, "submit_attempt", side_effect=fake_submit_attempt):
        response = asyncio.run(
            content.submit_test(str(test.id), FakeRequest(), user=user, db=db)
        )

    assert response.status_code == 303
    assert captured["answers"] == {str(question_one): 2, str(question_two): 1}
    assert captured["question_ids"] == [str(question_one), str(question_two)]
    assert db.committed is True
