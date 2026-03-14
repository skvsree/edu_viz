from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Card, Test, TestAttempt, TestAttemptAnswer, TestQuestion


@dataclass(slots=True)
class TestReportSummary:
    total_attempts: int
    best_score: int | None
    latest_score: int | None


def create_test_from_deck(
    db: Session,
    *,
    deck_id,
    created_by_user_id,
    title: str,
    description: str | None,
    question_count: int,
) -> Test:
    cards = (
        db.execute(
            select(Card)
            .where(Card.deck_id == deck_id, Card.card_type == "mcq")
            .order_by(Card.created_at.asc())
            .limit(question_count)
        )
        .scalars()
        .all()
    )
    if not cards:
        raise ValueError("This deck has no MCQs yet.")

    test = Test(
        deck_id=deck_id,
        created_by_user_id=created_by_user_id,
        title=title,
        description=description,
        question_count=len(cards),
        is_published=True,
    )
    db.add(test)
    db.flush()
    for pos, card in enumerate(cards, start=1):
        db.add(TestQuestion(test_id=test.id, card_id=card.id, position=pos))
    return test


def list_accessible_tests(db: Session, *, deck_id):
    return (
        db.execute(
            select(Test)
            .options(selectinload(Test.deck))
            .where(Test.deck_id == deck_id, Test.is_published.is_(True))
            .order_by(Test.created_at.desc())
        )
        .scalars()
        .all()
    )


def submit_attempt(db: Session, *, test: Test, user_id, answers: dict[str, int | None]) -> TestAttempt:
    questions = (
        db.execute(
            select(TestQuestion)
            .options(selectinload(TestQuestion.card))
            .where(TestQuestion.test_id == test.id)
            .order_by(TestQuestion.position.asc())
        )
        .scalars()
        .all()
    )
    attempt = TestAttempt(test_id=test.id, user_id=user_id, total_questions=len(questions), score=0)
    db.add(attempt)
    db.flush()

    score = 0
    for question in questions:
        selected = answers.get(str(question.id))
        is_correct = selected == question.card.mcq_answer_index
        if is_correct:
            score += 1
        db.add(
            TestAttemptAnswer(
                attempt_id=attempt.id,
                question_id=question.id,
                selected_option_index=selected,
                is_correct=is_correct,
                notes=question.card.back,
            )
        )
    attempt.score = score
    return attempt


def build_test_report(db: Session, *, attempt_id):
    attempt = db.execute(
        select(TestAttempt)
        .options(
            selectinload(TestAttempt.answers)
            .selectinload(TestAttemptAnswer.question)
            .selectinload(TestQuestion.card),
            selectinload(TestAttempt.test),
        )
        .where(TestAttempt.id == attempt_id)
    ).scalar_one()
    all_attempts = (
        db.execute(
            select(TestAttempt)
            .where(TestAttempt.test_id == attempt.test_id, TestAttempt.user_id == attempt.user_id)
            .order_by(TestAttempt.completed_at.desc())
        )
        .scalars()
        .all()
    )
    summary = TestReportSummary(
        total_attempts=len(all_attempts),
        best_score=max((item.score for item in all_attempts), default=None),
        latest_score=all_attempts[0].score if all_attempts else None,
    )
    return attempt, summary


def user_attempts_for_test(db: Session, *, test_id, user_id):
    return (
        db.execute(
            select(TestAttempt)
            .where(TestAttempt.test_id == test_id, TestAttempt.user_id == user_id)
            .order_by(TestAttempt.completed_at.desc())
        )
        .scalars()
        .all()
    )
