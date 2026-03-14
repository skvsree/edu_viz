from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from starlette.responses import RedirectResponse

from app.api.routers import content, pages
from app.services.access import can_access_tests, can_import_mcq_json, can_use_ai_generation, deck_has_test_content
from tests.test_dashboard_routes import FakeDB, make_request, render_body


def test_access_helpers_cover_test_and_ai_flags():
    org_id = uuid4()
    admin = SimpleNamespace(id=uuid4(), role="admin", organization_id=org_id, organization=SimpleNamespace(is_ai_enabled=True), is_test_enabled=True)
    learner = SimpleNamespace(id=uuid4(), role="user", organization_id=org_id, organization=SimpleNamespace(is_ai_enabled=True), is_test_enabled=False)
    sysadmin = SimpleNamespace(id=uuid4(), role="system_admin", organization_id=None, organization=None, is_test_enabled=True)
    deck = SimpleNamespace(is_deleted=False, is_global=False, organization_id=org_id, user_id=uuid4())

    assert can_use_ai_generation(admin) is True
    assert can_use_ai_generation(learner) is False
    assert can_use_ai_generation(sysadmin) is True
    assert can_import_mcq_json(admin) is True
    assert can_access_tests(admin, deck) is True
    assert can_access_tests(learner, deck) is False
    assert deck_has_test_content([SimpleNamespace(card_type="mcq", mcq_options=["A", "B", "C", "D"], mcq_answer_index=2)]) is True


def test_deck_overview_shows_test_action_only_for_enabled_users_with_mcqs():
    org_id = uuid4()
    deck = SimpleNamespace(id=uuid4(), is_deleted=False, is_global=False, organization_id=org_id, user_id=uuid4(), name="Biology", description="Cells")
    enabled_user = SimpleNamespace(id=uuid4(), role="admin", organization_id=org_id, is_test_enabled=True, organization=SimpleNamespace(is_ai_enabled=True))
    disabled_user = SimpleNamespace(id=uuid4(), role="admin", organization_id=org_id, is_test_enabled=False, organization=SimpleNamespace(is_ai_enabled=True))
    cards = [SimpleNamespace(card_type="mcq", mcq_options=["A", "B", "C", "D"], mcq_answer_index=0)]

    enabled = pages.deck_overview(make_request(path=f"/decks/{deck.id}"), deck_id=str(deck.id), user=enabled_user, db=FakeDB({str(deck.id): deck, deck.id: deck}, execute_results=cards))
    disabled = pages.deck_overview(make_request(path=f"/decks/{deck.id}"), deck_id=str(deck.id), user=disabled_user, db=FakeDB({str(deck.id): deck, deck.id: deck}, execute_results=cards))

    assert ">Test<" in render_body(enabled)
    assert ">Test<" not in render_body(disabled)


def test_mcq_page_shows_admin_json_import_and_sample_download_without_ai_form_when_org_ai_disabled():
    org_id = uuid4()
    deck = SimpleNamespace(id=uuid4(), is_deleted=False, is_global=False, organization_id=org_id, user_id=uuid4(), name="Biology", description=None)
    user = SimpleNamespace(id=uuid4(), role="admin", organization_id=org_id, organization=SimpleNamespace(is_ai_enabled=False), is_test_enabled=True)
    mcq = SimpleNamespace(id=uuid4(), card_type="mcq", front="Question", back="Answer", mcq_options=["A", "B", "C", "D"], mcq_answer_index=1)

    response = pages.deck_mcqs(make_request(path=f"/decks/{deck.id}/mcqs"), deck_id=str(deck.id), user=user, db=FakeDB({str(deck.id): deck, deck.id: deck}, execute_results=[mcq]))
    body = render_body(response)
    assert "MCQ JSON import" in body
    assert "Download sample JSON" in body
    assert "AI study generation" not in body


def test_create_test_allows_admin_manager():
    org_id = uuid4()
    deck = SimpleNamespace(id=uuid4(), is_deleted=False, is_global=False, organization_id=org_id, user_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="admin", organization_id=org_id, is_test_enabled=True)
    db = FakeDB({str(deck.id): deck, deck.id: deck})

    with patch.object(content, "create_test_from_deck", lambda *args, **kwargs: None):
        response = content.create_test(deck_id=str(deck.id), title="Quiz", description="", question_count=5, user=user, db=db)

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303


def test_sample_mcq_json_download_is_available():
    response = content.sample_mcq_json_download()
    assert response.media_type == "application/json"
    assert "sample-mcqs.json" in response.headers["content-disposition"]
