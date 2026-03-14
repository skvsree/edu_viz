from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.services.access import (
    ROLE_ADMIN,
    ROLE_SYSTEM_ADMIN,
    ROLE_USER,
    can_access_deck,
    can_manage_deck,
    can_manage_decks,
    normalize_deck_name,
)


def test_normalize_deck_name_ignores_spaces_and_symbols():
    assert normalize_deck_name(" Biology: Fundamentals! ") == "biologyfundamentals"


def test_normalize_deck_name_rejects_symbol_only_input():
    assert normalize_deck_name("!!!") == ""


def test_regular_user_can_access_org_and_global_decks_but_not_manage():
    org_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role=ROLE_USER, organization_id=org_id)
    org_deck = SimpleNamespace(
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        user_id=uuid4(),
    )
    global_deck = SimpleNamespace(
        is_deleted=False,
        is_global=True,
        organization_id=None,
        user_id=uuid4(),
    )

    assert can_access_deck(user, org_deck)
    assert can_access_deck(user, global_deck)
    assert not can_manage_decks(user)
    assert not can_manage_deck(user, org_deck)


def test_admin_can_manage_only_their_org_decks():
    org_id = uuid4()
    other_org_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role=ROLE_ADMIN, organization_id=org_id)
    own_deck = SimpleNamespace(
        is_deleted=False,
        is_global=False,
        organization_id=org_id,
        user_id=uuid4(),
    )
    other_deck = SimpleNamespace(
        is_deleted=False,
        is_global=False,
        organization_id=other_org_id,
        user_id=uuid4(),
    )

    assert can_manage_decks(user)
    assert can_manage_deck(user, own_deck)
    assert not can_manage_deck(user, other_deck)


def test_system_admin_can_manage_global_decks():
    user = SimpleNamespace(id=uuid4(), role=ROLE_SYSTEM_ADMIN, organization_id=None)
    deck = SimpleNamespace(
        is_deleted=False,
        is_global=True,
        organization_id=None,
        user_id=uuid4(),
    )

    assert can_manage_deck(user, deck)
