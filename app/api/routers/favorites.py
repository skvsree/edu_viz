from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.db import get_db
from app.models import Deck, User
from app.models.user_deck_favorite import UserDeckFavorite
from app.services.access import can_access_deck

router = APIRouter(prefix="/api/v1/favorites", tags=["favorites"])


def _require_access_or_404(user: User, deck: Deck) -> None:
    if not can_access_deck(user, deck):
        raise HTTPException(status_code=404)


@router.post("/decks/{deck_id}/toggle")
def toggle_favorite_deck(
    request: Request,
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status_code=404)
    _require_access_or_404(user, deck)

    existing = db.execute(
        select(UserDeckFavorite)
        .where(UserDeckFavorite.user_id == user.id, UserDeckFavorite.deck_id == deck.id)
    ).scalars().first()

    if existing:
        db.execute(
            delete(UserDeckFavorite).where(
                UserDeckFavorite.user_id == user.id,
                UserDeckFavorite.deck_id == deck.id,
            )
        )
        db.commit()
        return JSONResponse({"favorite": False})

    db.add(UserDeckFavorite(user_id=user.id, deck_id=deck.id))
    db.commit()
    return JSONResponse({"favorite": True})


@router.get("/decks")
def list_favorite_decks(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    favorite_decks = (
        db.execute(
            select(Deck)
            .join(UserDeckFavorite, UserDeckFavorite.deck_id == Deck.id)
            .where(
                UserDeckFavorite.user_id == user.id,
                Deck.is_deleted.is_(False),
            )
        )
        .scalars()
        .all()
    )

    return {
        "decks": [
            {
                "id": str(d.id),
                "name": d.name,
                "description": d.description,
                "is_global": d.is_global,
            }
            for d in favorite_decks
        ]
    }
