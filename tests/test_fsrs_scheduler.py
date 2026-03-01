from datetime import datetime, timezone

from app.fsrs.scheduler import FsrsCard, FsrsScheduler


def test_fsrs_rate_increases_stability_on_good():
    s = FsrsScheduler()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    card = FsrsCard(stability=0.5, difficulty=5.0, reps=0, lapses=0, state="NEW", last_review=None)

    res = s.rate(now=now, rating=3, card=card)

    assert res.stability > card.stability
    assert 1.0 <= res.difficulty <= 10.0
    assert res.scheduled_days >= 0


def test_fsrs_again_schedules_today():
    s = FsrsScheduler()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    card = FsrsCard(stability=3.0, difficulty=5.0, reps=10, lapses=1, state="REVIEW", last_review=now)

    res = s.rate(now=now, rating=1, card=card)

    assert res.scheduled_days == 0
    assert res.state in {"LEARNING", "RELEARNING"}
