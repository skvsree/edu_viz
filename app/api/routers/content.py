from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import current_user
from app.api.routers.pages import templates
from app.core.db import get_db
from app.models import Card, CardState, Deck, Review as CardReview, Test, TestAttempt, TestAttemptAnswer, TestQuestion, User
from app.services.access import (
    ROLE_SYSTEM_ADMIN,
    can_access_deck,
    can_access_tests,
    can_import_mcq_json,
    can_manage_deck,
    can_manage_tests,
    can_open_test_center,
    can_use_ai_generation,
)
from app.services.ai_auth import get_env_ai_provider_name, resolve_ai_credential
from app.services.ai_generation import AIGenerationError, generate_study_pack
from app.services.content_extraction import ContentExtractionError, extract_text
from app.services.mcq_import import McqImportError, parse_mcq_json
from app.services.tests import build_test_report, create_test_from_deck, list_accessible_tests, submit_attempt, user_attempts_for_test

router = APIRouter(tags=["content"])


def _delete_cards_with_test_dependencies(db: Session, *, card_ids: list[str]) -> None:
    affected_test_ids = db.execute(
        select(TestQuestion.test_id).where(TestQuestion.card_id.in_(card_ids)).distinct()
    ).scalars().all()
    if affected_test_ids:
        db.execute(
            delete(TestAttemptAnswer).where(
                TestAttemptAnswer.attempt_id.in_(
                    select(TestAttempt.id).where(TestAttempt.test_id.in_(affected_test_ids))
                )
            )
        )
        db.execute(delete(TestAttempt).where(TestAttempt.test_id.in_(affected_test_ids)))
        db.execute(delete(TestQuestion).where(TestQuestion.test_id.in_(affected_test_ids)))
        db.execute(delete(Test).where(Test.id.in_(affected_test_ids)))

    db.execute(delete(CardState).where(CardState.card_id.in_(card_ids)))
    db.execute(delete(CardReview).where(CardReview.card_id.in_(card_ids)))
    db.execute(delete(Card).where(Card.id.in_(card_ids)))


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
        # Determine provider: user > org > global
        provider = "openai"
        if user.id:
            from app.services.ai_auth import get_scope_provider
            user_provider = get_scope_provider(db, "user", user.id)
            if user_provider:
                provider = user_provider
            elif user.organization_id:
                from app.models import Organization
                org = db.get(Organization, user.organization_id)
                if org and org.is_ai_enabled:
                    org_provider = get_scope_provider(db, "organization", org.id)
                    if org_provider:
                        provider = org_provider
        resolution = resolve_ai_credential(db, user, provider)
        credential = resolution.credential
        if not credential:
            raise AIGenerationError(resolution.reason or "No AI credential configured for you or your organization.")
        pack = generate_study_pack(text, provider_name=credential.provider, credential=credential)
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


@router.post("/decks/{deck_id}/generate-mcqs")
def generate_mcqs_for_deck(
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_ai_generation_access(user)
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    # Try user-level provider first, then org, then global
    provider = "openai"
    if user.id:
        from app.services.ai_auth import get_scope_provider
        user_provider = get_scope_provider(db, "user", user.id)
        if user_provider:
            provider = user_provider
        elif user.organization_id:
            from app.models import Organization
            org = db.get(Organization, user.organization_id)
            if org and org.is_ai_enabled:
                org_provider = get_scope_provider(db, "organization", org.id)
                if org_provider:
                    provider = org_provider

    resolution = resolve_ai_credential(db, user, provider)
    credential = resolution.credential
    if not credential:
        return RedirectResponse(url=f"/decks/{deck.id}?import_error={quote_plus(resolution.reason or 'No AI credential configured for you or your organization.')}", status_code=303)

    source_cards = db.execute(select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.asc())).scalars().all()
    source_text = "\n\n".join(
        part.strip()
        for card in source_cards
        for part in (card.front or "", card.back or "")
        if part and part.strip()
    )
    if not source_text:
        return RedirectResponse(url=f"/decks/{deck.id}?import_error={quote_plus('No study text found to generate MCQs.')}", status_code=303)

    try:
        pack = generate_study_pack(source_text, provider_name=credential.provider, credential=credential)
    except AIGenerationError as exc:
        return RedirectResponse(url=f"/decks/{deck.id}?import_error={quote_plus(str(exc))}", status_code=303)

    mcq_cards = [
        Card(
            deck_id=deck.id,
            front=item.question,
            back=item.explanation,
            card_type="mcq",
            mcq_options=item.options,
            mcq_answer_index=item.answer_index,
            source_label="ai-mcq",
        )
        for item in pack.mcqs
    ]
    if mcq_cards:
        db.add_all(mcq_cards)
        db.flush()
        db.add_all([CardState(card_id=card.id) for card in mcq_cards])
        db.commit()
    return RedirectResponse(url=f"/decks/{deck.id}?import_success={quote_plus(f'Generated {len(mcq_cards)} MCQs')}", status_code=303)


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


def _build_anki_mcq_apkg(deck: Deck, cards: list[Card]) -> Path:
    output_dir = Path(__file__).resolve().parent.parent.parent / "static" / "anki"
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^a-z0-9_-]+', '_', deck.name.lower()).strip('_') or "deck"
    apkg_path = output_dir / f"{safe_name}_mcqs.apkg"

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE col (id integer primary key, crt integer, mod integer, scm integer, ver integer, dty integer, usn integer, ls integer, conf text, models text, decks text, dconf text, tags text)")
        conn.execute("CREATE TABLE notes (id integer primary key, guid text, mid integer, mod integer, usn integer, tags text, flds text, sfld integer, csum integer, flags integer, data text)")
        model_id = 1001
        model = {
            str(model_id): {
                "id": model_id,
                "name": "EduViz MCQ",
                "flds": [{"name": "Question"}, {"name": "Options"}, {"name": "Answer"}, {"name": "Explanation"}],
            }
        }
        deck_map = {str(1): {"id": 1, "name": deck.name, "mod": 0, "usn": 0, "collapsed": False, "browserCollapsed": False, "extendNew": 0, "extendRev": 0, "conf": 1, "desc": ""}}
        conn.execute("INSERT INTO col VALUES (1, 0, 0, 0, 11, 0, 0, 0, '{}', ?, ?, '{}', '{}')", (json.dumps(model), json.dumps(deck_map)))
        note_id = 1
        for card in cards:
            if card.card_type != "mcq":
                continue
            options = card.mcq_options or []
            answer = options[card.mcq_answer_index] if card.mcq_answer_index is not None and 0 <= card.mcq_answer_index < len(options) else ""
            flds = "\x1f".join([
                card.front or "",
                "<br>".join(options),
                answer,
                card.back or "",
            ])
            conn.execute(
                "INSERT INTO notes VALUES (?, ?, ?, 0, 0, '', ?, 0, 0, 0, '')",
                (note_id, f"guid{note_id}", model_id, flds),
            )
            note_id += 1
        conn.commit()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdb = Path(tmpdir) / "collection.anki21"
            backup = sqlite3.connect(tmpdb)
            conn.backup(backup)
            backup.close()
            with zipfile.ZipFile(apkg_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(tmpdb, arcname="collection.anki21")
                zf.writestr("media", json.dumps({}))
    finally:
        conn.close()
    return apkg_path


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


@router.get("/decks/{deck_id}/anki-mcqs.apkg")
def export_anki_mcqs(deck_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)

    cards = db.execute(select(Card).where(Card.deck_id == deck.id, Card.card_type == "mcq").order_by(Card.created_at.asc())).scalars().all()
    apkg_path = _build_anki_mcq_apkg(deck, cards)
    return Response(
        content=apkg_path.read_bytes(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{apkg_path.name}"'},
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


@router.post("/decks/{deck_id}/flashcards/bulk-delete")
def bulk_delete_flashcards(
    deck_id: str,
    card_ids: list[str] = Form(default=[]),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    unique_ids = sorted({card_id for card_id in card_ids if card_id})
    if not unique_ids:
        return RedirectResponse(url=f"/decks/{deck.id}/flashcards?update_error={quote_plus('Select at least one flashcard to delete.')}", status_code=303)

    cards = db.execute(select(Card.id, Card.deck_id, Card.card_type).where(Card.id.in_(unique_ids))).all()
    if len(cards) != len(unique_ids) or any(card.deck_id != deck.id or card.card_type != "basic" for card in cards):
        return RedirectResponse(url=f"/decks/{deck.id}/flashcards?update_error={quote_plus('Some selected flashcards are invalid for this deck.')}", status_code=303)

    _delete_cards_with_test_dependencies(db, card_ids=unique_ids)
    db.commit()
    return RedirectResponse(url=f"/decks/{deck.id}/flashcards?update_success={quote_plus(f'Deleted {len(unique_ids)} flashcard(s)')}", status_code=303)


@router.post("/decks/{deck_id}/mcqs/bulk-delete")
def bulk_delete_mcqs(
    deck_id: str,
    card_ids: list[str] = Form(default=[]),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    unique_ids = sorted({card_id for card_id in card_ids if card_id})
    if not unique_ids:
        return RedirectResponse(url=f"/decks/{deck.id}/mcqs?update_error={quote_plus('Select at least one MCQ to delete.')}", status_code=303)

    cards = db.execute(select(Card.id, Card.deck_id, Card.card_type).where(Card.id.in_(unique_ids))).all()
    if len(cards) != len(unique_ids) or any(card.deck_id != deck.id or card.card_type != "mcq" for card in cards):
        return RedirectResponse(url=f"/decks/{deck.id}/mcqs?update_error={quote_plus('Some selected MCQs are invalid for this deck.')}", status_code=303)

    _delete_cards_with_test_dependencies(db, card_ids=unique_ids)
    db.commit()
    return RedirectResponse(url=f"/decks/{deck.id}/mcqs?update_success={quote_plus(f'Deleted {len(unique_ids)} MCQ(s)')}", status_code=303)


@router.get("/decks/{deck_id}/tests", response_class=HTMLResponse)
def deck_tests_page(deck_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)
    tests = list_accessible_tests(db, deck_id=deck.id)
    has_test_content = bool(db.execute(select(Card.id).where(Card.deck_id == deck.id, Card.card_type == "mcq").limit(1)).scalars().all())
    if not can_open_test_center(user, deck, has_test_content=has_test_content, has_published_tests=bool(tests)):
        raise HTTPException(status_code=403, detail="Tests are not enabled for your account.")
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
            "has_test_content": has_test_content,
            "title": f"Tests | {deck.name}",
        },
    )


@router.post("/decks/{deck_id}/tests")
def create_test(deck_id: str, count: int = Form(default=0), user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.services.access import check_test_throttle

    deck = db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status_code=404)

    # Users who have test-taking enabled should be able to start tests.
    # "Manage" permission is not required for creating/starting a test attempt.
    tests = list_accessible_tests(db, deck_id=deck.id)
    has_test_content = bool(
        db.execute(
            select(Card.id).where(Card.deck_id == deck.id, Card.card_type == "mcq").limit(1)
        ).scalars().all()
    )
    if not can_open_test_center(
        user,
        deck,
        has_test_content=has_test_content,
        has_published_tests=bool(tests),
    ):
        # Hide existence when user can't access tests.
        raise HTTPException(status_code=404)

    # Check throttling limits
    allowed, error_msg = check_test_throttle(user, deck, db)
    if not allowed:
        return RedirectResponse(url=f"/decks/{deck.id}/tests?error={quote_plus(error_msg)}", status_code=303)

    try:
        test = create_test_from_deck(db, deck_id=deck.id, created_by_user_id=user.id, question_count=count or None)
    except ValueError as exc:
        return RedirectResponse(url=f"/decks/{deck.id}/tests?error={quote_plus(str(exc))}", status_code=303)
    db.commit()
    return RedirectResponse(url=f"/tests/{test.id}", status_code=303)


@router.get("/tests/{test_id}", response_class=HTMLResponse)
def take_test_page(test_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    test = db.execute(select(Test).options(selectinload(Test.questions).selectinload(TestQuestion.card), selectinload(Test.deck)).where(Test.id == test_id)).scalar_one_or_none()
    if not test or not can_access_tests(user, test.deck):
        raise HTTPException(status_code=404)

    questions = sorted(test.questions, key=lambda item: item.position)
    import random
    random.shuffle(questions)

    return templates.TemplateResponse(
        "tests/take.html",
        {
            "request": request,
            "user": user,
            "test": test,
            "questions": questions,
            "title": test.title,
        },
    )


@router.post("/tests/{test_id}/submit")
async def submit_test(test_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    test = db.execute(select(Test).options(selectinload(Test.deck)).where(Test.id == test_id)).scalar_one_or_none()
    if not test or not can_access_tests(user, test.deck):
        raise HTTPException(status_code=404)
    answers: dict[str, int | None] = {}
    form = await request.form()
    question_ids = [value for key, value in form.multi_items() if key == "question_ids" and value]
    for key, value in form.items():
        if key.startswith("question_") and key != "question_ids":
            answers[key.removeprefix("question_")] = int(value) if value != "" else None
    attempt = submit_attempt(db, test=test, user_id=user.id, answers=answers, question_ids=question_ids or None)
    db.commit()
    return RedirectResponse(url=f"/attempts/{attempt.id}", status_code=303)


@router.get("/attempts/{attempt_id}", response_class=HTMLResponse)
def attempt_report_page(attempt_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    attempt, summary = build_test_report(db, attempt_id=attempt_id)
    if attempt.user_id != user.id and user.role != ROLE_SYSTEM_ADMIN:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("tests/report.html", {"request": request, "user": user, "attempt": attempt, "summary": summary, "title": f"Report | {attempt.test.title}"})
