from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Card, CardState, Review, User
from app.fsrs.scheduler import FsrsCard, FsrsScheduler
from app.services.access import accessible_deck_clause


class ReviewService:
    def __init__(self, scheduler: FsrsScheduler | None = None):
        self.scheduler = scheduler or FsrsScheduler()

    def ensure_state(self, db: Session, card_id: uuid.UUID) -> CardState:
        state = db.get(CardState, card_id)
        if state is None:
            state = CardState(card_id=card_id)
            db.add(state)
            db.flush()
        return state

    def next_due_card(self, db: Session, *, user: User, deck_id: uuid.UUID | None = None) -> Card | None:
        # Pick the earliest due card from the decks visible to this user.
        now = datetime.now(timezone.utc)
        stmt = (
            select(Card)
            .join(Card.deck)
            .join(Card.state, isouter=True)
            .where(accessible_deck_clause(user))
        )
        if deck_id is not None:
            stmt = stmt.where(Card.deck_id == deck_id)
        stmt = stmt.order_by(CardState.due.asc().nullsfirst(), Card.created_at.asc()).limit(1)
        card = db.execute(stmt).scalars().first()
        if card is None:
            return None

        # If state exists and due is in future, allow it only if there are no due cards?
        # For MVP, we still show the earliest card even if not due.
        _ = now
        return card

    def rate(self, db: Session, *, card_id: uuid.UUID, rating: int) -> Review:
        now = datetime.now(timezone.utc)

        card = db.get(Card, card_id)
        if card is None:
            raise ValueError("card not found")

        state = self.ensure_state(db, card_id)

        elapsed_days = 0
        if state.last_review is not None:
            elapsed = now - state.last_review.replace(tzinfo=timezone.utc)
            elapsed_days = max(0, int(elapsed.total_seconds() // 86400))

        res = self.scheduler.rate(
            now=now,
            rating=rating,
            card=FsrsCard(
                stability=state.stability,
                difficulty=state.difficulty,
                reps=state.reps,
                lapses=state.lapses,
                state=state.state,
                last_review=state.last_review,
            ),
        )

        state.stability = res.stability
        state.difficulty = res.difficulty
        state.due = res.due
        state.reps = state.reps + 1
        if rating == 1:
            state.lapses = state.lapses + 1
        state.state = res.state
        state.last_review = now

        review = Review(
            card_id=card_id,
            rating=rating,
            review_time=now,
            scheduled_days=res.scheduled_days,
            elapsed_days=elapsed_days,
        )
        db.add(review)
        db.flush()
        return review
