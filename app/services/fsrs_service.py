from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class FsrsResult:
    stability: float
    difficulty: float
    state: str
    scheduled_days: int
    due: datetime


class FsrsScheduler:
    """Minimal FSRS-like adapter.

    This is intentionally small and server-authoritative.
    Replace internals with a full FSRS implementation later.

    Ratings: 1=Again, 2=Hard, 3=Good, 4=Easy
    """

    def rate(
        self,
        *,
        now: datetime,
        rating: int,
        stability: float,
        difficulty: float,
        reps: int,
        lapses: int,
        state: str,
    ) -> FsrsResult:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        rating = int(rating)
        if rating < 1 or rating > 4:
            raise ValueError("rating must be 1..4")

        # Very small heuristic schedule (placeholder for true FSRS):
        # - NEW/LEARNING uses short steps
        # - REVIEW grows with stability; lapses reduce it
        base = max(stability, 0.1)

        if state in {"NEW", "LEARNING", "RELEARNING"}:
            steps = {1: 0, 2: 0, 3: 1, 4: 3}
            scheduled_days = steps[rating]
            next_state = "LEARNING" if rating in {2, 3} else ("REVIEW" if rating == 4 else "RELEARNING")
        else:
            mult = {1: 0.0, 2: 0.6, 3: 1.2, 4: 1.8}[rating]
            penalty = 1.0 / (1.0 + lapses)
            scheduled_days = int(round(base * mult * 2.5 * penalty))
            if rating == 1:
                scheduled_days = 0
            scheduled_days = max(scheduled_days, 0)
            next_state = "REVIEW" if rating != 1 else "RELEARNING"

        # Update difficulty/stability heuristics
        difficulty = float(difficulty)
        stability = float(stability)
        if rating == 1:
            difficulty = min(10.0, difficulty + 0.6)
            stability = max(0.1, stability * 0.5)
        elif rating == 2:
            difficulty = min(10.0, difficulty + 0.2)
            stability = stability + 0.3
        elif rating == 3:
            difficulty = max(1.0, difficulty - 0.1)
            stability = stability + 0.8
        else:
            difficulty = max(1.0, difficulty - 0.3)
            stability = stability + 1.5

        due = now + timedelta(days=scheduled_days)

        return FsrsResult(
            stability=stability,
            difficulty=difficulty,
            state=next_state,
            scheduled_days=scheduled_days,
            due=due,
        )
