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
