from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import current_user
from app.api.routers.pages import templates
from app.core.db import get_db
from app.models import Card, CardState, Deck, Test, TestQuestion, User
from app.services.access import (
    ROLE_SYSTEM_ADMIN,
    can_access_deck,
    can_access_tests,
    can_import_mcq_json,
    can_manage_deck,
    can_manage_tests,
    can_use_ai_generation,
)
from app.services.ai_generation import AIGenerationError, generate_study_pack
from app.services.content_extraction import ContentExtractionError, extract_text
from app.services.mcq_import import McqImportError, parse_mcq_json
from app.services.tests import build_test_report, create_test_from_deck, list_accessible_tests, submit_attempt, user_attempts_for_test

router = APIRouter(tags=["content"])


def _require_system_admin(user: User) -> None:
    if user.role != ROLE_SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="Only system admins can access this page.")


def _require_ai_generation_access(user: User) -> None:
    if not can_use_ai_generation(user):
        raise HTTPException(status_code=403, detail="AI generation is not enabled for your account or organization.")


def _require_mcq_json_access(user: User) -> None:
    if not can_import_mcq_json(user):
        raise HTTPException(status_code=403, detail="You do not have permission to import MCQ JSON.")


@router.post("/decks/{deck_id}/ai-import")
def ai_import_deck_content(
    deck_id: str,
    source_file: UploadFile = File(...),
    flashcard_count: int = Form(default=12),
    mcq_count: int = Form(default=10),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_ai_generation_access(user)
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    payload = source_file.file.read()
    source_file.file.close()
    try:
        text = extract_text(source_file.filename or "", payload)
        pack = generate_study_pack(text, flashcard_count=flashcard_count, mcq_count=mcq_count)
    except (ContentExtractionError, AIGenerationError) as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(url=f"/decks/{deck.id}/ai-upload?import_error={message}", status_code=303)

    new_cards: list[Card] = []
    for item in pack.flashcards:
        new_cards.append(Card(deck_id=deck.id, front=item.front, back=item.back, card_type="basic", source_label="ai-upload"))
    for item in pack.mcqs:
        new_cards.append(
            Card(
                deck_id=deck.id,
                front=item.question,
                back=item.explanation,
                card_type="mcq",
                mcq_options=item.options,
                mcq_answer_index=item.answer_index,
                source_label="ai-upload",
            )
        )
    db.add_all(new_cards)
    db.flush()
    db.add_all([CardState(card_id=card.id) for card in new_cards])
    db.commit()
    return RedirectResponse(url=f"/decks/{deck.id}/ai-upload?import_success={quote_plus(f'Generated {len(pack.flashcards)} flashcards and {len(pack.mcqs)} MCQs')}", status_code=303)


@router.post("/decks/{deck_id}/mcqs/import-json")
def import_mcqs_json(
    deck_id: str,
    mcq_file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_mcq_json_access(user)
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    try:
        mcqs = parse_mcq_json(mcq_file.file.read())
    except McqImportError as exc:
        return RedirectResponse(url=f"/decks/{deck.id}/mcqs?import_error={quote_plus(str(exc))}", status_code=303)
    finally:
        mcq_file.file.close()

    cards = [
        Card(
            deck_id=deck.id,
            front=item.question,
            back=item.explanation,
            card_type="mcq",
            mcq_options=item.options,
            mcq_answer_index=item.answer_index,
            source_label="json-import",
        )
        for item in mcqs
    ]
    db.add_all(cards)
    db.flush()
    db.add_all([CardState(card_id=card.id) for card in cards])
    db.commit()
    return RedirectResponse(url=f"/decks/{deck.id}/mcqs?import_success={quote_plus(f'Imported {len(cards)} MCQs from JSON')}", status_code=303)


@router.get("/sample-mcqs.json")
def sample_mcq_json_download():
    sample = {
        "mcqs": [
            {
                "question": "Which planet is known as the Red Planet?",
                "options": ["Earth", "Mars", "Venus", "Jupiter"],
                "answer_index": 1,
                "explanation": "Mars appears reddish because of iron oxide on its surface.",
            },
            {
                "question": "What is the capital of Tamil Nadu?",
                "options": ["Coimbatore", "Madurai", "Chennai", "Pollachi"],
                "answer_index": 2,
                "explanation": "Chennai is the capital city of Tamil Nadu.",
            },
        ]
    }
    return Response(
        content=json.dumps(sample, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="sample-mcqs.json"'},
    )


@router.get("/decks/{deck_id}/anki-export.csv")
def export_anki_csv(deck_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)

    cards = db.execute(select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.asc())).scalars().all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["front", "back"])
    for card in cards:
        writer.writerow([card.front, card.back])
    filename = f"{deck.name.lower().replace(' ', '_')}_anki.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/decks/{deck_id}/flashcards/{card_id}/edit", response_class=HTMLResponse)
def edit_flashcard_page(deck_id: str, card_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    deck = db.get(Deck, deck_id)
    card = db.get(Card, card_id)
    if not deck or not card or card.deck_id != deck.id or not can_manage_deck(user, deck) or card.card_type != "basic":
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("cards/edit_flashcard.html", {"request": request, "user": user, "deck": deck, "card": card, "title": f"Edit flashcard | {deck.name}"})


@router.post("/decks/{deck_id}/flashcards/{card_id}/edit")
def edit_flashcard(deck_id: str, card_id: str, front: str = Form(...), back: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    deck = db.get(Deck, deck_id)
    card = db.get(Card, card_id)
    if not deck or not card or card.deck_id != deck.id or not can_manage_deck(user, deck) or card.card_type != "basic":
        raise HTTPException(status_code=404)
    card.front = front.strip()
    card.back = back.strip()
    db.commit()
    return RedirectResponse(url=f"/decks/{deck.id}/flashcards?update_success={quote_plus('Flashcard updated')}", status_code=303)


@router.get("/decks/{deck_id}/mcqs/{card_id}/edit", response_class=HTMLResponse)
def edit_mcq_page(deck_id: str, card_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    deck = db.get(Deck, deck_id)
    card = db.get(Card, card_id)
    if not deck or not card or card.deck_id != deck.id or not can_manage_deck(user, deck) or card.card_type != "mcq":
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("cards/edit_mcq.html", {"request": request, "user": user, "deck": deck, "card": card, "title": f"Edit MCQ | {deck.name}"})


@router.post("/decks/{deck_id}/mcqs/{card_id}/edit")
def edit_mcq(
    deck_id: str,
    card_id: str,
    question: str = Form(...),
    explanation: str = Form(default=""),
    option_0: str = Form(...),
    option_1: str = Form(...),
    option_2: str = Form(...),
    option_3: str = Form(...),
    answer_index: int = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    card = db.get(Card, card_id)
    if not deck or not card or card.deck_id != deck.id or not can_manage_deck(user, deck) or card.card_type != "mcq":
        raise HTTPException(status_code=404)
    card.front = question.strip()
    card.back = explanation.strip()
    card.mcq_options = [option_0.strip(), option_1.strip(), option_2.strip(), option_3.strip()]
    card.mcq_answer_index = answer_index
    db.commit()
    return RedirectResponse(url=f"/decks/{deck.id}/mcqs?update_success={quote_plus('MCQ updated')}", status_code=303)


@router.get("/decks/{deck_id}/tests", response_class=HTMLResponse)
def deck_tests_page(deck_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)
    if not can_access_tests(user, deck):
        raise HTTPException(status_code=403, detail="Tests are not enabled for your account.")
    tests = list_accessible_tests(db, deck_id=deck.id)
    attempts_by_test = {str(test.id): user_attempts_for_test(db, test_id=test.id, user_id=user.id) for test in tests}
    return templates.TemplateResponse(
        "tests/list.html",
        {
            "request": request,
            "user": user,
            "deck": deck,
            "tests": tests,
            "attempts_by_test": attempts_by_test,
            "can_edit": can_manage_tests(user, deck),
            "title": f"Tests | {deck.name}",
        },
    )


@router.post("/decks/{deck_id}/tests")
def create_test(deck_id: str, title: str = Form(...), description: str = Form(default=""), question_count: int = Form(default=10), user: User = Depends(current_user), db: Session = Depends(get_db)):
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_tests(user, deck):
        raise HTTPException(status_code=404)
    try:
        create_test_from_deck(db, deck_id=deck.id, created_by_user_id=user.id, title=title.strip(), description=description.strip() or None, question_count=max(1, min(question_count, 100)))
    except ValueError as exc:
        return RedirectResponse(url=f"/decks/{deck.id}/tests?error={quote_plus(str(exc))}", status_code=303)
    db.commit()
    return RedirectResponse(url=f"/decks/{deck.id}/tests", status_code=303)


@router.get("/tests/{test_id}", response_class=HTMLResponse)
def take_test_page(test_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    test = db.execute(select(Test).options(selectinload(Test.questions).selectinload(TestQuestion.card), selectinload(Test.deck)).where(Test.id == test_id)).scalar_one_or_none()
    if not test or not can_access_tests(user, test.deck):
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("tests/take.html", {"request": request, "user": user, "test": test, "title": test.title})


@router.post("/tests/{test_id}/submit")
async def submit_test(test_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    test = db.execute(select(Test).options(selectinload(Test.deck)).where(Test.id == test_id)).scalar_one_or_none()
    if not test or not can_access_tests(user, test.deck):
        raise HTTPException(status_code=404)
    answers: dict[str, int | None] = {}
    form = await request.form()
    for key, value in form.items():
        if key.startswith("question_"):
            answers[key.removeprefix("question_")] = int(value) if value != "" else None
    attempt = submit_attempt(db, test=test, user_id=user.id, answers=answers)
    db.commit()
    return RedirectResponse(url=f"/attempts/{attempt.id}", status_code=303)


@router.get("/attempts/{attempt_id}", response_class=HTMLResponse)
def attempt_report_page(attempt_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    attempt, summary = build_test_report(db, attempt_id=attempt_id)
    if attempt.user_id != user.id and user.role != ROLE_SYSTEM_ADMIN:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("tests/report.html", {"request": request, "user": user, "attempt": attempt, "summary": summary, "title": f"Report | {attempt.test.title}"})
