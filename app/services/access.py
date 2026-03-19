from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement
    from app.models import Deck, User

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLE_SYSTEM_ADMIN = "system_admin"

NAME_NORMALIZER_RE = re.compile(r"[^a-z0-9]+")


def normalize_deck_name(value: str) -> str:
    normalized = NAME_NORMALIZER_RE.sub("", value.strip().lower())
    return normalized


def is_system_admin(user: Any) -> bool:
    return user.role == ROLE_SYSTEM_ADMIN


def can_manage_decks(user: Any) -> bool:
    return user.role in {ROLE_ADMIN, ROLE_SYSTEM_ADMIN}


def can_manage_deck(user: Any, deck: Any) -> bool:
    if deck.is_deleted:
        return False
    if is_system_admin(user):
        return True
    if user.role != ROLE_ADMIN:
        return False
    if deck.is_global:
        return False
    if user.organization_id and deck.organization_id == user.organization_id:
        return True
    return deck.organization_id is None and deck.user_id == user.id


def can_access_deck(user: Any, deck: Any) -> bool:
    if deck.is_deleted:
        return False
    if is_system_admin(user):
        return True
    if deck.is_global:
        return True
    if user.organization_id and deck.organization_id == user.organization_id:
        return True
    return deck.organization_id is None and deck.user_id == user.id


def can_use_ai_generation(user: Any) -> bool:
    if is_system_admin(user):
        return True
    if user.role != ROLE_ADMIN:
        return False
    organization = getattr(user, "organization", None)
    return bool(user.organization_id and organization and getattr(organization, "is_ai_enabled", False))


def can_import_mcq_json(user: Any) -> bool:
    return user.role in {ROLE_ADMIN, ROLE_SYSTEM_ADMIN}


def can_manage_tests(user: Any, deck: Any) -> bool:
    return can_manage_deck(user, deck)


def can_access_tests(user: Any, deck: Any) -> bool:
    return bool(getattr(user, "is_test_enabled", False)) and can_access_deck(user, deck)


def can_open_test_center(user: Any, deck: Any, *, has_test_content: bool = False, has_published_tests: bool = False) -> bool:
    if can_manage_tests(user, deck):
        return can_access_deck(user, deck)
    return can_access_tests(user, deck) and (has_test_content or has_published_tests)


def deck_has_test_content(cards: list[Any]) -> bool:
    return any(
        getattr(card, "card_type", None) == "mcq"
        and getattr(card, "mcq_options", None)
        and getattr(card, "mcq_answer_index", None) in {0, 1, 2, 3}
        for card in cards
    )


def accessible_deck_clause(user: "User") -> "ColumnElement[bool]":
    from sqlalchemy import or_

    from app.models import Deck

    if is_system_admin(user):
        return Deck.is_deleted.is_(False)
    clauses = [Deck.is_deleted.is_(False)]
    visibility_clauses = [Deck.is_global.is_(True), Deck.user_id == user.id]
    if user.organization_id:
        visibility_clauses.append(Deck.organization_id == user.organization_id)
    clauses.append(or_(*visibility_clauses))
    return clauses[0] & clauses[1]


@dataclass(slots=True)
class DashboardDeckStats:
    deck: Deck
    cards_reviewed: int = 0
    cards_due: int = 0
    accuracy: float | None = None
    last_reviewed: datetime | None = None
