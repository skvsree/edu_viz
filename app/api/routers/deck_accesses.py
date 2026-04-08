"""Deck access management API."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.db import get_db
from app.models import Deck, DeckAccess, User
from app.services.access import ACCESS_LEVELS, ACCESS_NONE, can_set_deck_access

router = APIRouter(prefix="/api/v1/decks", tags=["deck-access"])


class DeckAccessGrant(BaseModel):
    user_id: uuid.UUID
    access_level: str


class DeckAccessUpdate(BaseModel):
    access_level: str


class DeckAccessResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    deck_id: uuid.UUID
    access_level: str
    user_email: str | None = None


class DeckAccessScopeUpdate(BaseModel):
    access_level: str


# ── Per-deck access CRUD ──────────────────────────────────────────────────────


@router.get("/{deck_id}/access", response_model=list[DeckAccessResponse])
def list_deck_access(
    deck_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """List all access grants for a deck."""
    deck = db.get(Deck, deck_id)
    if not deck or deck.is_deleted:
        raise HTTPException(status_code=404, detail="Deck not found")

    if not can_set_deck_access(user, deck):
        raise HTTPException(status_code=403, detail="Cannot manage access for this deck")

    rows = db.execute(
        select(DeckAccess, User.email)
        .join(User, User.id == DeckAccess.user_id)
        .where(DeckAccess.deck_id == deck_id)
        .order_by(User.email.asc())
    ).all()
    return [
        DeckAccessResponse(
            id=access.id,
            user_id=access.user_id,
            deck_id=access.deck_id,
            access_level=getattr(access.access_level, "value", access.access_level),
            user_email=email,
        )
        for access, email in rows
    ]


@router.post("/{deck_id}/access", response_model=DeckAccessResponse, status_code=201)
def grant_deck_access(
    deck_id: uuid.UUID,
    grant: DeckAccessGrant,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Grant a user access to a deck."""
    deck = db.get(Deck, deck_id)
    if not deck or deck.is_deleted:
        raise HTTPException(status_code=404, detail="Deck not found")

    if not can_set_deck_access(user, deck):
        raise HTTPException(status_code=403, detail="Cannot manage access for this deck")

    # Validate access level
    if grant.access_level not in ACCESS_LEVELS or grant.access_level == ACCESS_NONE:
        raise HTTPException(status_code=400, detail="Invalid access_level. Use one of: read, write, delete")

    # Validate target user exists
    target_user = db.get(User, grant.user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="Target user not found")
    if str(target_user.id) == str(deck.user_id):
        raise HTTPException(status_code=400, detail="Owner already has full access")

    # Check if grant already exists
    existing = db.execute(
        select(DeckAccess).where(
            DeckAccess.deck_id == deck_id,
            DeckAccess.user_id == grant.user_id,
        )
    ).scalars().first()

    if existing:
        existing.access_level = grant.access_level
        db.commit()
        db.refresh(existing)
        return DeckAccessResponse(
            id=existing.id,
            user_id=existing.user_id,
            deck_id=existing.deck_id,
            access_level=getattr(existing.access_level, "value", existing.access_level),
            user_email=target_user.email,
        )

    access = DeckAccess(
        deck_id=deck_id,
        user_id=grant.user_id,
        access_level=grant.access_level,
    )
    db.add(access)
    db.commit()
    db.refresh(access)
    return DeckAccessResponse(
        id=access.id,
        user_id=access.user_id,
        deck_id=access.deck_id,
        access_level=getattr(access.access_level, "value", access.access_level),
        user_email=target_user.email,
    )


@router.put("/{deck_id}/access/{access_id}", response_model=DeckAccessResponse | None)
def update_deck_access(
    deck_id: uuid.UUID,
    access_id: uuid.UUID,
    update: DeckAccessUpdate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Update a user's access level on a deck."""
    deck = db.get(Deck, deck_id)
    if not deck or deck.is_deleted:
        raise HTTPException(status_code=404, detail="Deck not found")

    if not can_set_deck_access(user, deck):
        raise HTTPException(status_code=403, detail="Cannot manage access for this deck")

    if update.access_level not in ACCESS_LEVELS:
        raise HTTPException(status_code=400, detail=f"Invalid access_level. Must be one of: {ACCESS_LEVELS}")

    access = db.get(DeckAccess, access_id)
    if not access or str(access.deck_id) != str(deck_id):
        raise HTTPException(status_code=404, detail="Access grant not found")

    if update.access_level == ACCESS_NONE:
        db.delete(access)
        db.commit()
        return Response(status_code=204)

    access.access_level = update.access_level
    db.commit()
    db.refresh(access)
    target_user = db.get(User, access.user_id)
    return DeckAccessResponse(
        id=access.id,
        user_id=access.user_id,
        deck_id=access.deck_id,
        access_level=getattr(access.access_level, "value", access.access_level),
        user_email=target_user.email if target_user else None,
    )


@router.delete("/{deck_id}/access/{access_id}", status_code=204)
def revoke_deck_access(
    deck_id: uuid.UUID,
    access_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Revoke a user's access to a deck."""
    deck = db.get(Deck, deck_id)
    if not deck or deck.is_deleted:
        raise HTTPException(status_code=404, detail="Deck not found")

    if not can_set_deck_access(user, deck):
        raise HTTPException(status_code=403, detail="Cannot manage access for this deck")

    access = db.get(DeckAccess, access_id)
    if not access or str(access.deck_id) != str(deck_id):
        raise HTTPException(status_code=404, detail="Access grant not found")

    db.delete(access)
    db.commit()


# ── Deck access level (global/org/user scope) ────────────────────────────────


class DeckAccessLevelUpdate(BaseModel):
    access_level: str


@router.put("/{deck_id}/access-level", response_model=dict)
def update_deck_access_level(
    deck_id: uuid.UUID,
    update: DeckAccessLevelUpdate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Update a deck's access level (scope)."""
    deck = db.get(Deck, deck_id)
    if not deck or deck.is_deleted:
        raise HTTPException(status_code=404, detail="Deck not found")

    from app.services.access import can_change_access_level

    if not can_change_access_level(user, deck, update.access_level):
        raise HTTPException(status_code=403, detail="Cannot change access level for this deck")

    valid_levels = {"global", "org", "user"}
    if update.access_level not in valid_levels:
        raise HTTPException(status_code=400, detail=f"Invalid access_level. Must be one of: {sorted(valid_levels)}")

    deck.access_level = update.access_level
    # Sync is_global for backward compat
    deck.is_global = update.access_level == "global"
    db.commit()
    return {"id": str(deck.id), "access_level": getattr(deck.access_level, "value", deck.access_level)}
