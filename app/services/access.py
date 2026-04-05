from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.core.config import settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
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
    """Check if user can access tests.

    Hierarchy (highest to lowest precedence):
    1. User-level flag (is_test_enabled on User) - explicit per-user control
    2. Organization-level flag (is_test_enabled on Organization) - org override

    Note: The env default (test_enabled_default) is used only at user creation time
    to set the initial is_test_enabled value. It is NOT used as a runtime fallback.

    System admins bypass all restrictions.
    """
    if is_system_admin(user):
        return can_access_deck(user, deck)

    user_flag = getattr(user, "is_test_enabled", None)
    if user_flag is not None:
        # User has explicit flag set
        if user_flag:
            return can_access_deck(user, deck)
        # User explicitly disabled, but org can still override
        organization = getattr(user, "organization", None)
        if user.organization_id and organization:
            if getattr(organization, "is_test_enabled", False):
                return can_access_deck(user, deck)
        return False

    # No user-level flag set, check org-level
    organization = getattr(user, "organization", None)
    if user.organization_id and organization:
        if getattr(organization, "is_test_enabled", False):
            return can_access_deck(user, deck)

    return False


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


def check_test_throttle(user: Any, deck: Any, db: "Session") -> tuple[bool, str]:
    """Check if user is within test throttling limits.

    Returns:
        (allowed, error_message) - if allowed=True, user can take test.
        If allowed=False, error_message explains why.
    """
    # System admins bypass throttling
    if is_system_admin(user):
        return True, ""

    user_id = getattr(user, "id", None)
    if not user_id:
        return True, ""

    # Check cooldown
    if settings.test_cooldown_seconds > 0:
        from app.models import TestAttempt, Test

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.test_cooldown_seconds)
        last_attempt = db.execute(
            select(TestAttempt)
            .join(Test)
            .where(TestAttempt.user_id == user_id, Test.deck_id == deck.id, TestAttempt.started_at >= cutoff)
            .order_by(TestAttempt.started_at.desc())
            .limit(1)
        ).scalars().first()

        if last_attempt:
            remaining = settings.test_cooldown_seconds - int((datetime.now(timezone.utc) - last_attempt.started_at).total_seconds())
            if remaining > 0:
                return False, f"Please wait {remaining} seconds before taking another test on this deck."

    # Check daily limit (org-specific, capped by env max)
    daily_limit = settings.test_daily_limit
    if daily_limit > 0:
        # Allow org to override, but not exceed env max
        org = getattr(user, "organization", None)
        if org and getattr(org, "test_daily_limit", 0) > 0:
            daily_limit = min(org.test_daily_limit, daily_limit)

        if daily_limit > 0:
            from app.models import TestAttempt

            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            count = db.execute(
                select(func.count(TestAttempt.id))
                .where(TestAttempt.user_id == user_id, TestAttempt.started_at >= today_start)
            ).scalar() or 0

            if count >= daily_limit:
                return False, f"Daily test limit reached ({daily_limit} per day). Try again tomorrow."

    return True, ""
