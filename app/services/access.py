from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.core.config import settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from sqlalchemy.sql.elements import ColumnElement
    from app.models import Deck, User

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLE_SYSTEM_ADMIN = "system_admin"

# Deck access scopes
SCOPE_GLOBAL = "global"
SCOPE_ORG = "org"
SCOPE_USER = "user"

# Per-user access levels
ACCESS_NONE = "none"
ACCESS_READ = "read"
ACCESS_WRITE = "write"
ACCESS_DELETE = "delete"

ACCESS_LEVELS = [ACCESS_NONE, ACCESS_READ, ACCESS_WRITE, ACCESS_DELETE]

NAME_NORMALIZER_RE = re.compile(r"[^a-z0-9]+")


def normalize_deck_name(value: str) -> str:
    normalized = NAME_NORMALIZER_RE.sub("", value.strip().lower())
    return normalized


def is_system_admin(user: Any) -> bool:
    return user.role == ROLE_SYSTEM_ADMIN


def is_org_admin(user: Any) -> bool:
    return user.role == ROLE_ADMIN and user.organization_id is not None


def _has_explicit_access(user: Any, deck: Any, required: str) -> bool:
    """Check if user has explicit per-deck access >= required level."""
    # This is called from can_access_deck etc which receive loaded deck_accesses
    accesses = getattr(deck, "deck_accesses", None)
    if not accesses:
        return False
    # deck_accesses is a list of DeckAccess objects
    for acc in accesses:
        if str(acc.user_id) == str(user.id):
            return _access_level_rank(acc.access_level) >= _access_level_rank(required)
    return False


def _access_level_rank(level: str) -> int:
    """Rank access levels: none=0, read=1, write=2, delete=3"""
    ranks = {ACCESS_NONE: 0, ACCESS_READ: 1, ACCESS_WRITE: 2, ACCESS_DELETE: 3}
    return ranks.get(level, 0)


def can_access_deck(user: Any, deck: Any) -> bool:
    """Check if user can read/view a deck."""
    if deck.is_deleted:
        return False
    if is_system_admin(user):
        return True

    # Owner always has access
    if str(deck.user_id) == str(user.id):
        return True

    # Explicit grant can always expand visibility
    if _has_explicit_access(user, deck, ACCESS_READ):
        return True

    access_level = getattr(deck.access_level, "value", deck.access_level)

    # Global: anyone can read
    if access_level == SCOPE_GLOBAL:
        return True

    # Org: same organization can read
    if access_level == SCOPE_ORG:
        if user.organization_id and deck.organization_id:
            return str(user.organization_id) == str(deck.organization_id)
        return False

    # User scope: only owner unless explicitly shared
    return False


def can_write_deck(user: Any, deck: Any) -> bool:
    """Check if user can write/modify a deck."""
    if deck.is_deleted:
        return False
    if is_system_admin(user):
        return True

    # Owner always has write
    if str(deck.user_id) == str(user.id):
        return True

    access_level = getattr(deck.access_level, "value", deck.access_level)

    # Org admin can write org-level decks
    if access_level == SCOPE_ORG and is_org_admin(user):
        if user.organization_id and deck.organization_id:
            return str(user.organization_id) == str(deck.organization_id)

    # Explicit grant
    return _has_explicit_access(user, deck, ACCESS_WRITE)


def can_delete_deck(user: Any, deck: Any) -> bool:
    """Check if user can delete a deck."""
    if deck.is_deleted:
        return False
    if is_system_admin(user):
        return True

    # Owner can delete own decks
    if str(deck.user_id) == str(user.id):
        return True

    access_level = getattr(deck.access_level, "value", deck.access_level)

    # Org admin can delete org-level decks
    if access_level == SCOPE_ORG and is_org_admin(user):
        if user.organization_id and deck.organization_id:
            return str(user.organization_id) == str(deck.organization_id)

    # Explicit grant
    return _has_explicit_access(user, deck, ACCESS_DELETE)


def can_manage_deck(user: Any, deck: Any) -> bool:
    """Alias for can_write_deck (backward compat)."""
    return can_write_deck(user, deck)


def can_manage_decks(user: Any) -> bool:
    return user.role in {ROLE_ADMIN, ROLE_SYSTEM_ADMIN, ROLE_USER}


def can_set_deck_access(user: Any, deck: Any) -> bool:
    """Check if user can grant/revoke access on a deck."""
    if is_system_admin(user):
        return True
    # Owner can grant access on their decks
    if str(deck.user_id) == str(user.id):
        return True
    # Org admin can grant access on org-level decks
    if deck.access_level == SCOPE_ORG and is_org_admin(user):
        if user.organization_id and deck.organization_id:
            return str(user.organization_id) == str(deck.organization_id)
    return False


def can_change_access_level(user: Any, deck: Any, new_level: str) -> bool:
    """Check if user can change a deck's access level."""
    if is_system_admin(user):
        return True
    # Only owner can change access level
    if str(deck.user_id) != str(user.id):
        return False
    # Users can't make decks global
    if new_level == SCOPE_GLOBAL and user.role == ROLE_USER:
        return False
    # Org admins can promote to org level
    if new_level == SCOPE_ORG and not is_org_admin(user):
        return False
    return True


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
    from app.models import Deck
    from app.models.deck import DeckAccessScope

    if is_system_admin(user):
        return Deck.is_deleted.is_(False)

    clauses = [Deck.is_deleted.is_(False)]

    visibility = [Deck.user_id == user.id]  # owner
    if user.organization_id:
        visibility.append(
            Deck.organization_id == user.organization_id
        )  # org member
    visibility.append(Deck.access_level == DeckAccessScope.GLOBAL)  # global

    clauses.append(or_(*visibility))
    return clauses[0] & clauses[1]


def browse_accessible_deck_clause(user: "User") -> "ColumnElement[bool]":
    """Browse-only visibility: include explicit per-user shared decks."""
    from app.models import Deck, DeckAccess, DeckAccessLevel
    from app.models.deck import DeckAccessScope

    if is_system_admin(user):
        return Deck.is_deleted.is_(False)

    visibility = [
        Deck.user_id == user.id,
        Deck.access_level == DeckAccessScope.GLOBAL,
        (DeckAccess.user_id == user.id) & (DeckAccess.access_level != DeckAccessLevel.NONE),
    ]
    if user.organization_id:
        visibility.append(
            (Deck.access_level == DeckAccessScope.ORG) & (Deck.organization_id == user.organization_id)
        )

    return Deck.is_deleted.is_(False) & or_(*visibility)


def load_browse_deck_query(user: "User"):
    """Base browse query with eager-loaded access grants for permission checks."""
    from app.models import Deck, DeckAccess

    return (
        select(Deck)
        .outerjoin(DeckAccess, DeckAccess.deck_id == Deck.id)
        .options(
            selectinload(Deck.organization),
            selectinload(Deck.tags),
            selectinload(Deck.deck_accesses),
        )
    )


# ── Browse tab helpers ────────────────────────────────────────────────────────

TAB_ALL = "all"
TAB_GLOBAL = "global"
TAB_ORG = "org"
TAB_USER = "user"

TAB_LABELS = {
    TAB_ALL: "All",
    TAB_GLOBAL: "Global",
    TAB_ORG: "Org",
    TAB_USER: "Mine",
}

TAB_ORDER = [TAB_ALL, TAB_GLOBAL, TAB_ORG, TAB_USER]


def get_browse_tabs(user: "User") -> list[dict]:
    """Return list of visible tabs for the given user's role."""
    if is_system_admin(user):
        return [
            {"key": TAB_ALL, "label": TAB_LABELS[TAB_ALL]},
            {"key": TAB_GLOBAL, "label": TAB_LABELS[TAB_GLOBAL]},
            {"key": TAB_ORG, "label": TAB_LABELS[TAB_ORG]},
            {"key": TAB_USER, "label": TAB_LABELS[TAB_USER]},
        ]
    if user.role == ROLE_ADMIN:
        return [
            {"key": TAB_GLOBAL, "label": TAB_LABELS[TAB_GLOBAL]},
            {"key": TAB_ORG, "label": TAB_LABELS[TAB_ORG]},
            {"key": TAB_USER, "label": TAB_LABELS[TAB_USER]},
        ]
    return [
        {"key": TAB_GLOBAL, "label": TAB_LABELS[TAB_GLOBAL]},
        {"key": TAB_USER, "label": TAB_LABELS[TAB_USER]},
    ]


def default_tab(user: "User") -> str:
    """Return the default active tab for a user."""
    tabs = get_browse_tabs(user)
    return tabs[0]["key"] if tabs else TAB_GLOBAL


def browse_filter_clause(user: "User", tab: str) -> "ColumnElement[bool]":
    """Return SQLAlchemy filter clause for the given browse tab."""
    from app.models import Deck
    from app.models.deck import DeckAccessScope

    base = accessible_deck_clause(user)

    if tab == TAB_ALL:
        # system_admin sees everything — no extra filter
        return base
    if tab == TAB_GLOBAL:
        return base & (Deck.access_level == DeckAccessScope.GLOBAL)
    if tab == TAB_ORG:
        if user.organization_id:
            return base & (Deck.access_level == DeckAccessScope.ORG) & (Deck.organization_id == user.organization_id)
        return base & (Deck.access_level == DeckAccessScope.ORG)
    if tab == TAB_USER:
        # Personal decks: access_level=user AND owner is current user
        return base & (Deck.access_level == DeckAccessScope.USER) & (Deck.user_id == user.id)
    return base


def can_write_tab(user: "User", tab: str) -> bool:
    """Check if user has write access to any deck in the given tab."""
    if is_system_admin(user):
        return True
    if tab == TAB_GLOBAL:
        return False  # global is read-only for non-system-admins
    if tab == TAB_ORG:
        return is_org_admin(user)
    if tab == TAB_USER:
        return True  # users can always write their own decks
    return False


# ── Test throttle ─────────────────────────────────────────────────────────────

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
