from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Card, CardState, Deck, Review, User
from app.services.access import DashboardDeckStats, accessible_deck_clause


def list_accessible_deck_stats(db: Session, *, user: User) -> list[DashboardDeckStats]:
    now = datetime.now(timezone.utc)
    decks = (
        db.execute(
            select(Deck)
            .options(selectinload(Deck.organization), selectinload(Deck.tags))
            .where(accessible_deck_clause(user))
            .order_by(Deck.is_global.desc(), Deck.created_at.desc())
        )
        .scalars()
        .all()
    )
    if not decks:
        return []

    deck_ids = [deck.id for deck in decks]

    review_stats = {
        row.deck_id: row
        for row in db.execute(
            select(
                Card.deck_id.label("deck_id"),
                func.count(func.distinct(Review.card_id)).label("cards_reviewed"),
                func.avg(case((Review.rating >= 3, 1.0), else_=0.0)).label("accuracy"),
                func.max(Review.review_time).label("last_reviewed"),
            )
            .join(Review, Review.card_id == Card.id)
            .where(Card.deck_id.in_(deck_ids))
            .group_by(Card.deck_id)
        )
    }

    due_stats = {
        row.deck_id: row.cards_due
        for row in db.execute(
            select(Card.deck_id.label("deck_id"), func.count(Card.id).label("cards_due"))
            .join(CardState, CardState.card_id == Card.id)
            .where(Card.deck_id.in_(deck_ids), CardState.due <= now)
            .group_by(Card.deck_id)
        )
    }

    results: list[DashboardDeckStats] = []
    for deck in decks:
        review_row = review_stats.get(deck.id)
        results.append(
            DashboardDeckStats(
                deck=deck,
                cards_reviewed=int(review_row.cards_reviewed) if review_row else 0,
                cards_due=int(due_stats.get(deck.id, 0)),
                accuracy=float(review_row.accuracy) if review_row and review_row.accuracy is not None else None,
                last_reviewed=review_row.last_reviewed if review_row else None,
            )
        )
    return results
