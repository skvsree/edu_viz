from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class FsrsCard:
    stability: float
    difficulty: float
    reps: int
    lapses: int
    state: str  # NEW|LEARNING|REVIEW|RELEARNING
    last_review: datetime | None


@dataclass
class FsrsResult:
    stability: float
    difficulty: float
    state: str
    scheduled_days: int
    due: datetime


class FsrsScheduler:
    """FSRS-style scheduler (non-placeholder, but still small).

    This is a clean-room implementation inspired by FSRS concepts:
    - stability/difficulty
    - forgetting curve: R = exp(-t/S)
    - ratings: 1 Again, 2 Hard, 3 Good, 4 Easy

    It is not a byte-for-byte port of upstream FSRS.
    """

    def __init__(self):
        # Tunable weights (reasonable defaults). Keep it simple for MVP.
        self.w = {
            "init_stability": 0.4,
            "init_difficulty": 5.0,
            "diff_gain_again": 0.6,
            "diff_gain_hard": 0.25,
            "diff_gain_good": -0.05,
            "diff_gain_easy": -0.2,
            "stab_gain_hard": 0.3,
            "stab_gain_good": 1.0,
            "stab_gain_easy": 1.7,
            "stab_decay_again": 0.55,
        }

    def _clamp(self, x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))

    def _retrievability(self, *, elapsed_days: float, stability: float) -> float:
        stability = max(stability, 0.1)
        return math.exp(-elapsed_days / stability)

    def rate(self, *, now: datetime, rating: int, card: FsrsCard) -> FsrsResult:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        rating = int(rating)
        if rating < 1 or rating > 4:
            raise ValueError("rating must be 1..4")

        stability = float(card.stability or self.w["init_stability"])
        difficulty = float(card.difficulty or self.w["init_difficulty"])

        elapsed_days = 0.0
        if card.last_review is not None:
            last = card.last_review
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            elapsed_days = max(0.0, (now - last).total_seconds() / 86400.0)

        R = self._retrievability(elapsed_days=elapsed_days, stability=stability)

        # Difficulty update
        diff_delta = {
            1: self.w["diff_gain_again"],
            2: self.w["diff_gain_hard"],
            3: self.w["diff_gain_good"],
            4: self.w["diff_gain_easy"],
        }[rating]
        # If card was barely remembered (low R), increase difficulty slightly even for good.
        difficulty = difficulty + diff_delta + (0.15 * (1.0 - R))
        difficulty = self._clamp(difficulty, 1.0, 10.0)

        # Stability update
        if rating == 1:
            stability = max(0.1, stability * self.w["stab_decay_again"])
            next_state = "RELEARNING" if card.reps > 0 else "LEARNING"
            scheduled_days = 0
        else:
            gain = {2: self.w["stab_gain_hard"], 3: self.w["stab_gain_good"], 4: self.w["stab_gain_easy"]}[rating]
            # higher difficulty => lower stability growth; lower R => lower growth
            growth = gain * (1.0 - 0.07 * (difficulty - 5.0)) * (0.6 + 0.4 * R)
            stability = stability + max(0.05, growth)
            next_state = "REVIEW"

            # Interval selection: target retention ~0.9 for good, higher for hard, lower for easy
            target_R = {2: 0.93, 3: 0.9, 4: 0.85}[rating]
            target_R = self._clamp(target_R, 0.75, 0.97)
            scheduled_days = int(round(-stability * math.log(target_R)))
            scheduled_days = max(1, scheduled_days)

        # Lapse penalty for many lapses
        if card.lapses > 0:
            scheduled_days = int(round(scheduled_days / (1.0 + 0.35 * card.lapses)))
            scheduled_days = max(0 if rating == 1 else 1, scheduled_days)

        due = now + timedelta(days=scheduled_days)

        return FsrsResult(
            stability=stability,
            difficulty=difficulty,
            state=next_state,
            scheduled_days=scheduled_days,
            due=due,
        )
