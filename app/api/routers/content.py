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
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload
from time import sleep
from datetime import datetime, timedelta
import threading

from app.api.deps import current_user
from app.api.routers.pages import templates
from app.core.db import get_db
from app.models import Card, CardState, Deck, Review as CardReview, Test, TestAttempt, TestAttemptAnswer, TestQuestion, User, AIUploadGeneration, AIUploadGenerationStatus
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
from app.services.ai_auth import get_env_ai_provider_name, get_scope_provider, resolve_ai_credential
from app.services.ai_generation import AIGenerationError, build_iterative_study_pack_prompt, build_study_pack_prompt, generate_study_pack, get_study_pack_provider, merge_study_packs, normalize_generated_text, _parse_study_pack_json
from app.services.content_extraction import ContentExtractionError, extract_text
from app.services.mcq_import import McqImportError, parse_mcq_json
from app.services.tests import build_test_report, create_test_from_deck, list_accessible_tests, submit_attempt, user_attempts_for_test

router = APIRouter(tags=["content"])
logger = logging.getLogger(__name__)


def _split_text_for_ai_upload(text: str, *, max_chars: int = 9000, overlap_chars: int = 1200) -> list[str]:
    cleaned = "\n".join(line.strip() for line in (text or "").splitlines() if line.strip())
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]

    chunks: list[str] = []
    start = 0
    step = max(1, max_chars - overlap_chars)
    while start < len(cleaned):
        end = min(len(cleaned), start + max_chars)
        chunk = cleaned[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(cleaned):
            break
        start += step
    return chunks


def _generate_iterative_ai_upload_pack(text: str, *, credential) -> tuple:
    provider_client = get_study_pack_provider(credential.provider)
    chunks = _split_text_for_ai_upload(text)
    if not chunks:
        raise AIGenerationError("No usable study text found in uploaded file.")

    aggregate = merge_study_packs()
    modes = ("core", "mechanisms", "traps")

    for chunk in chunks:
        chunk_pack = merge_study_packs()
        for mode in modes:
            prompt = build_iterative_study_pack_prompt(
                chunk,
                mode=mode,
                existing_flashcards=[item.front for item in merge_study_packs(aggregate, chunk_pack).flashcards],
                existing_mcqs=[item.question for item in merge_study_packs(aggregate, chunk_pack).mcqs],
                max_flashcards=18,
                max_mcqs=18,
            )
            pass_pack = provider_client.generate_from_prompt(prompt, credential)
            before_flash = len(chunk_pack.flashcards)
            before_mcq = len(chunk_pack.mcqs)
            chunk_pack = merge_study_packs(chunk_pack, pass_pack)
            added_flash = len(chunk_pack.flashcards) - before_flash
            added_mcq = len(chunk_pack.mcqs) - before_mcq
            if added_flash + added_mcq <= 2:
                continue
        aggregate = merge_study_packs(aggregate, chunk_pack)

    if not aggregate.flashcards and not aggregate.mcqs:
        raise AIGenerationError("AI provider did not return usable flashcards or MCQs.")
    return aggregate, len(chunks)


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


def _resolve_ai_provider_and_credential(db: Session, user: User):
    from app.models import Organization
    provider = get_scope_provider(db, "user", user.id) if user.id else None
    if not provider and user.organization_id:
        org = db.get(Organization, user.organization_id)
        if org and org.is_ai_enabled:
            provider = get_scope_provider(db, "organization", org.id)
    if not provider:
        provider = get_env_ai_provider_name() or "openai"
    resolution = resolve_ai_credential(db, user, provider)
    credential = resolution.credential
    if not credential:
        raise AIGenerationError(resolution.reason or "No AI credential configured for you or your organization.")
    return provider, credential


def _run_ai_upload_generation(*, generation_id: str, deck_id: str, user_id: str, filename: str, payload: bytes) -> None:
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        generation = db.get(AIUploadGeneration, generation_id)
        user = db.get(User, user_id)
        deck = db.get(Deck, deck_id)
        if not generation or not user or not deck:
            return

        generation.status = AIUploadGenerationStatus.IN_PROGRESS.value
        generation.started_at = datetime.utcnow()
        db.commit()

        text = extract_text(filename or "", payload)
        provider_name, credential = _resolve_ai_provider_and_credential(db, user)
        generation.provider = credential.provider or provider_name
        chunks = _split_text_for_ai_upload(text)
        if not chunks:
            raise AIGenerationError("No usable study text found in uploaded file.")
        generation.total_chunks = len(chunks)
        generation.processed_chunks = 0
        db.commit()

        provider_client = get_study_pack_provider(credential.provider)
        aggregate = merge_study_packs()
        modes = ("core", "mechanisms", "traps")

        existing_flashcards = {
            (normalize_generated_text(card.front or ""), normalize_generated_text(card.back or ""))
            for card in db.execute(select(Card).where(Card.deck_id == deck.id, Card.card_type == "basic")).scalars().all()
        }
        existing_mcqs = {
            normalize_generated_text(card.front or "")
            for card in db.execute(select(Card).where(Card.deck_id == deck.id, Card.card_type == "mcq")).scalars().all()
        }

        created_flashcards = 0
        created_mcqs = 0
        skipped_duplicates = 0

        for index, chunk in enumerate(chunks, start=1):
            chunk_pack = merge_study_packs()
            for mode in modes:
                prompt = build_iterative_study_pack_prompt(
                    chunk,
                    mode=mode,
                    existing_flashcards=[item.front for item in merge_study_packs(aggregate, chunk_pack).flashcards],
                    existing_mcqs=[item.question for item in merge_study_packs(aggregate, chunk_pack).mcqs],
                    max_flashcards=18,
                    max_mcqs=18,
                )
                pass_pack = provider_client.generate_from_prompt(prompt, credential)
                chunk_pack = merge_study_packs(chunk_pack, pass_pack)

            new_cards: list[Card] = []
            for item in chunk_pack.flashcards:
                key = (normalize_generated_text(item.front), normalize_generated_text(item.back))
                if key in existing_flashcards:
                    skipped_duplicates += 1
                    continue
                existing_flashcards.add(key)
                created_flashcards += 1
                new_cards.append(Card(deck_id=deck.id, front=item.front, back=item.back, card_type="basic", source_label="ai-upload"))
            for item in chunk_pack.mcqs:
                key = normalize_generated_text(item.question)
                if key in existing_mcqs:
                    skipped_duplicates += 1
                    continue
                existing_mcqs.add(key)
                created_mcqs += 1
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
            if new_cards:
                db.add_all(new_cards)
                db.flush()
                db.add_all([CardState(card_id=card.id) for card in new_cards])
                db.commit()
            else:
                db.commit()

            aggregate = merge_study_packs(aggregate, chunk_pack)
            generation = db.get(AIUploadGeneration, generation_id)
            if not generation:
                return
            generation.processed_chunks = index
            generation.flashcards_generated = created_flashcards
            generation.mcqs_generated = created_mcqs
            generation.duplicates_skipped = skipped_duplicates
            db.commit()

        generation = db.get(AIUploadGeneration, generation_id)
        if not generation:
            return
        if not created_flashcards and not created_mcqs:
            generation.status = AIUploadGenerationStatus.FAILED.value
            generation.error_message = "AI provider did not return usable flashcards or MCQs."
        else:
            generation.status = AIUploadGenerationStatus.COMPLETED.value
            generation.completed_at = datetime.utcnow()
        generation.flashcards_generated = created_flashcards
        generation.mcqs_generated = created_mcqs
        generation.duplicates_skipped = skipped_duplicates
        db.commit()
    except Exception as exc:
        logger.exception("AI upload generation failed for deck %s", deck_id)
        try:
            generation = db.get(AIUploadGeneration, generation_id)
            if generation:
                generation.status = AIUploadGenerationStatus.FAILED.value
                generation.error_message = str(exc)
                generation.completed_at = datetime.utcnow()
                db.commit()
        except Exception:
            logger.exception("Failed to persist AI upload generation failure state for deck %s", deck_id)
    finally:
        db.close()



def _require_system_admin(user: User) -> None:
    if user.role != ROLE_SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="Only system admins can access this page.")


def _require_ai_generation_access(user: User) -> None:
    if not can_use_ai_generation(user):
        raise HTTPException(status_code=403, detail="AI generation is not enabled for your account or organization.")


def _require_mcq_json_access(user: User) -> None:
    if not can_import_mcq_json(user):
        raise HTTPException(status_code=403, detail="You do not have permission to import MCQ JSON.")


@router.post("/decks/{deck_id}/ai-import/start")
def ai_import_deck_content_start(
    deck_id: str,
    source_file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_ai_generation_access(user)
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    if not source_file.filename:
        raise HTTPException(status_code=400, detail="Please choose a PDF or DOCX file.")

    try:
        provider_name, credential = _resolve_ai_provider_and_credential(db, user)
    except AIGenerationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    latest = db.execute(
        select(AIUploadGeneration)
        .where(AIUploadGeneration.deck_id == deck.id)
        .order_by(AIUploadGeneration.created_at.desc())
    ).scalars().first()
    if latest and latest.status == AIUploadGenerationStatus.IN_PROGRESS.value:
        return {
            "ok": True,
            "already_running": True,
            "generation_id": str(latest.id),
            "provider": latest.provider or credential.provider or provider_name,
            "total": latest.total_chunks or 0,
            "completed": latest.processed_chunks,
            "flashcards_generated": latest.flashcards_generated,
            "mcqs_generated": latest.mcqs_generated,
            "duplicates_skipped": latest.duplicates_skipped,
        }

    payload = source_file.file.read()
    source_file.file.close()
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    generation = AIUploadGeneration(
        deck_id=deck.id,
        status=AIUploadGenerationStatus.IN_PROGRESS.value,
        total_chunks=None,
        processed_chunks=0,
        flashcards_generated=0,
        mcqs_generated=0,
        duplicates_skipped=0,
        provider=credential.provider or provider_name,
        filename=(source_file.filename or "")[:255],
        started_at=datetime.utcnow(),
    )
    db.add(generation)
    db.commit()
    db.refresh(generation)

    worker = threading.Thread(
        target=_run_ai_upload_generation,
        kwargs={
            "generation_id": str(generation.id),
            "deck_id": str(deck.id),
            "user_id": str(user.id),
            "filename": source_file.filename or "",
            "payload": payload,
        },
        daemon=True,
    )
    worker.start()

    return {
        "ok": True,
        "already_running": False,
        "generation_id": str(generation.id),
        "provider": credential.provider or provider_name,
        "total": 0,
        "completed": 0,
        "flashcards_generated": 0,
        "mcqs_generated": 0,
        "duplicates_skipped": 0,
    }


@router.get("/decks/{deck_id}/ai-import/stream")
def ai_import_deck_content_stream(
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_ai_generation_access(user)
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    def event_stream():
        sent_state = None
        while True:
            db.expire_all()
            latest = db.execute(
                select(AIUploadGeneration)
                .where(AIUploadGeneration.deck_id == deck.id)
                .order_by(AIUploadGeneration.created_at.desc())
            ).scalars().first()
            if not latest:
                yield ": keep-alive\n\n"
                sleep(1)
                continue

            state = (
                latest.status,
                latest.total_chunks or 0,
                latest.processed_chunks,
                latest.flashcards_generated,
                latest.mcqs_generated,
                latest.duplicates_skipped,
                latest.error_message or "",
                latest.provider or "",
            )
            if state != sent_state:
                payload = {
                    "generation_id": str(latest.id),
                    "total": latest.total_chunks or 0,
                    "completed": latest.processed_chunks,
                    "flashcards_generated": latest.flashcards_generated,
                    "mcqs_generated": latest.mcqs_generated,
                    "duplicates_skipped": latest.duplicates_skipped,
                    "provider": latest.provider,
                    "filename": latest.filename,
                }
                if latest.status == AIUploadGenerationStatus.IN_PROGRESS.value:
                    event_name = "start" if sent_state is None else "progress"
                    yield f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"
                elif latest.status == AIUploadGenerationStatus.COMPLETED.value:
                    yield f"event: complete\ndata: {json.dumps(payload)}\n\n"
                    break
                elif latest.status == AIUploadGenerationStatus.FAILED.value:
                    payload["message"] = latest.error_message or "AI upload failed."
                    yield f"event: generation_error\ndata: {json.dumps(payload)}\n\n"
                    break
                sent_state = state
            else:
                yield ": keep-alive\n\n"
            sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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

    # Determine provider: user > org > global env
    from app.models import Organization
    provider = get_scope_provider(db, "user", user.id) if user.id else None
    if not provider and user.organization_id:
        org = db.get(Organization, user.organization_id)
        if org and org.is_ai_enabled:
            provider = get_scope_provider(db, "organization", org.id)
    if not provider:
        provider = get_env_ai_provider_name() or "openai"

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
    from app.models import DeckMcqGenerationItem, DeckMcqGenerationItemStatus
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    unique_ids = sorted({card_id for card_id in card_ids if card_id})
    if not unique_ids:
        return RedirectResponse(url=f"/decks/{deck.id}/mcqs?update_error={quote_plus('Select at least one MCQ to delete.')}", status_code=303)

    cards = db.execute(select(Card.id, Card.deck_id, Card.card_type, Card.source_label).where(Card.id.in_(unique_ids))).all()
    if len(cards) != len(unique_ids) or any(card.deck_id != deck.id or card.card_type != "mcq" for card in cards):
        return RedirectResponse(url=f"/decks/{deck.id}/mcqs?update_error={quote_plus('Some selected MCQs are invalid for this deck.')}", status_code=303)

    deleted_ai_generated = any((card.source_label or "").startswith("ai-mcq") for card in cards)

    _delete_cards_with_test_dependencies(db, card_ids=unique_ids)

    if deleted_ai_generated:
        generation_items = db.execute(
            select(DeckMcqGenerationItem).where(DeckMcqGenerationItem.deck_id == deck.id)
        ).scalars().all()
        for item in generation_items:
            item.status = DeckMcqGenerationItemStatus.NOT_STARTED.value
            item.completed_at = None

    db.commit()
    reset_suffix = " and reset generation state" if deleted_ai_generated else ""
    return RedirectResponse(url=f"/decks/{deck.id}/mcqs?update_success={quote_plus(f'Deleted {len(unique_ids)} MCQ(s){reset_suffix}')}", status_code=303)


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


def _resolve_generation_provider_and_credential(db: Session, user: User):
    from app.models import Organization

    provider = get_scope_provider(db, "user", user.id) if user.id else None
    if not provider and user.organization_id:
        org = db.get(Organization, user.organization_id)
        if org and org.is_ai_enabled:
            provider = get_scope_provider(db, "organization", org.id)
    if not provider:
        provider = get_env_ai_provider_name() or "openai"

    resolution = resolve_ai_credential(db, user, provider)
    return provider, resolution



def _generate_mcqs_background(deck_id: str, user_id: str, generation_id: str) -> None:
    from app.core.db import SessionLocal
    from app.models import MCQGeneration, MCQGenerationStatus, DeckMcqGenerationItem, DeckMcqGenerationItemStatus

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        deck = db.get(Deck, deck_id)
        mcq_gen = db.get(MCQGeneration, generation_id)
        if not user or not deck or not mcq_gen:
            return

        provider, resolution = _resolve_generation_provider_and_credential(db, user)
        credential = resolution.credential
        if not credential:
            mcq_gen.status = MCQGenerationStatus.FAILED.value
            mcq_gen.error_message = resolution.reason or "No AI credential configured."
            db.commit()
            return

        source_cards = db.execute(
            select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.asc())
        ).scalars().all()
        source_cards = [card for card in source_cards if card.card_type != "mcq"]
        existing_items = db.execute(
            select(DeckMcqGenerationItem).where(DeckMcqGenerationItem.deck_id == deck.id)
        ).scalars().all()
        item_by_card_id = {str(item.source_card_id): item for item in existing_items}

        pending_cards = [
            card for card in source_cards
            if item_by_card_id.get(str(card.id)) and item_by_card_id[str(card.id)].status != DeckMcqGenerationItemStatus.COMPLETED.value
        ]

        total_cards = len(source_cards)
        pending_total = len(pending_cards)
        completed_before = total_cards - pending_total
        mcq_gen.total_cards = total_cards
        mcq_gen.processed_cards = completed_before
        mcq_gen.mcqs_generated = 0
        mcq_gen.error_message = None
        db.commit()

        all_mcqs = []
        processed_this_run = 0
        failed_this_run = 0
        batch_size = 5

        def build_batch_text(batch_cards):
            return "\n\n".join(
                part.strip()
                for card in batch_cards
                for part in (card.front or "", card.back or "")
                if part and part.strip()
            )

        def request_pack(batch_cards, mcq_target=None):
            batch_text = build_batch_text(batch_cards)
            if not batch_text.strip():
                return None
            num_mcqs_local = mcq_target if mcq_target is not None else min(6, max(3, len(batch_cards)))
            prompt = build_study_pack_prompt(batch_text, num_flashcards=0, num_mcqs=num_mcqs_local)
            provider_client = get_study_pack_provider(credential.provider)
            if not hasattr(provider_client, "generate"):
                raise AIGenerationError(f"Unsupported AI study pack provider: {credential.provider}")

            if credential.provider == "openai":
                from openai import OpenAI
                client = OpenAI(api_key=credential.secret)
                response = client.responses.create(model="gpt-4.1-mini", input=prompt)
                return _parse_study_pack_json(response.output_text)
            elif credential.provider == "claude":
                import requests
                response = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": credential.secret,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 8192,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=120,
                )
                if response.status_code != 200:
                    raise AIGenerationError(f"Claude API error: {response.status_code} - {response.text[:200]}")
                data = response.json()
                content = data.get("content", [{}])[0].get("text", "")
                if not content:
                    raise AIGenerationError("Claude returned empty response.")
                return _parse_study_pack_json(content)
            elif credential.provider in {"minimax", "opencode"}:
                import requests
                response = requests.post(
                    "https://api.minimax.io/v1/text/chatcompletion_v2",
                    headers={"Authorization": f"Bearer {credential.secret}", "Content-Type": "application/json"},
                    json={
                        "model": "MiniMax-M2",
                        "messages": [
                            {"role": "system", "content": "Return compact strict JSON only. No markdown, no code fences, no commentary, no prose, no trailing text."},
                            {"role": "user", "content": prompt},
                        ],
                        "max_completion_tokens": 2048,
                        "temperature": 0.2,
                    },
                    timeout=60,
                )
                if response.status_code != 200:
                    raise AIGenerationError(f"Minimax API error: {response.status_code} - {response.text[:200]}")
                data = response.json()
                base_resp = data.get("base_resp") or {}
                if base_resp.get("status_code") not in {None, 0}:
                    raise AIGenerationError(base_resp.get("status_msg") or "Minimax request failed.")
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if not content:
                    raise AIGenerationError(f"Minimax returned empty response. Raw: {response.text[:300]}")
                return _parse_study_pack_json(content)
            else:
                raise AIGenerationError(f"Unsupported AI study pack provider: {credential.provider}")

        def save_batch_success(batch_cards, pack):
            nonlocal processed_this_run
            all_mcqs.extend(pack.mcqs)
            if pack.mcqs:
                mcq_cards = [
                    Card(
                        deck_id=deck.id,
                        front=item.question,
                        back=item.explanation,
                        card_type="mcq",
                        mcq_options=item.options,
                        mcq_answer_index=item.answer_index,
                        source_label=f"ai-mcq:gen:{generation_id}",
                    )
                    for item in pack.mcqs
                ]
                db.add_all(mcq_cards)
                db.flush()
                db.add_all([CardState(card_id=card.id) for card in mcq_cards])
            now = datetime.utcnow()
            for card in batch_cards:
                item_by_card_id[str(card.id)].status = DeckMcqGenerationItemStatus.COMPLETED.value
                item_by_card_id[str(card.id)].completed_at = now
            processed_this_run += len(batch_cards)
            mcq_gen.processed_cards = completed_before + processed_this_run
            mcq_gen.mcqs_generated = len(all_mcqs)
            db.commit()

        def mark_batch_failed(batch_cards):
            nonlocal processed_this_run, failed_this_run
            now = datetime.utcnow()
            for card in batch_cards:
                item_by_card_id[str(card.id)].status = DeckMcqGenerationItemStatus.FAILED.value
                item_by_card_id[str(card.id)].completed_at = now
            failed_this_run += len(batch_cards)
            processed_this_run += len(batch_cards)
            mcq_gen.processed_cards = completed_before + processed_this_run
            mcq_gen.mcqs_generated = len(all_mcqs)
            db.commit()

        for i in range(0, pending_total, batch_size):
            batch = pending_cards[i:i + batch_size]
            batch_text = build_batch_text(batch)
            if not batch_text.strip():
                now = datetime.utcnow()
                for card in batch:
                    item_by_card_id[str(card.id)].status = DeckMcqGenerationItemStatus.COMPLETED.value
                    item_by_card_id[str(card.id)].completed_at = now
                processed_this_run += len(batch)
                mcq_gen.processed_cards = completed_before + processed_this_run
                db.commit()
                continue

            try:
                pack = request_pack(batch)
                if pack is None:
                    now = datetime.utcnow()
                    for card in batch:
                        item_by_card_id[str(card.id)].status = DeckMcqGenerationItemStatus.COMPLETED.value
                        item_by_card_id[str(card.id)].completed_at = now
                    processed_this_run += len(batch)
                    mcq_gen.processed_cards = completed_before + processed_this_run
                    db.commit()
                    continue
                save_batch_success(batch, pack)
            except Exception as batch_exc:
                logger.warning("mcq batch failed, retrying smaller generation_id=%s deck_id=%s batch_start=%s error=%s", generation_id, deck_id, i, batch_exc)
                recovered = False
                for card in batch:
                    try:
                        single_pack = request_pack([card], mcq_target=1)
                        if single_pack is None:
                            now = datetime.utcnow()
                            item_by_card_id[str(card.id)].status = DeckMcqGenerationItemStatus.COMPLETED.value
                            item_by_card_id[str(card.id)].completed_at = now
                            processed_this_run += 1
                            mcq_gen.processed_cards = completed_before + processed_this_run
                            db.commit()
                            continue
                        save_batch_success([card], single_pack)
                        recovered = True
                    except Exception as single_exc:
                        logger.warning("mcq single-card fallback failed generation_id=%s deck_id=%s card_id=%s error=%s", generation_id, deck_id, card.id, single_exc)
                        mark_batch_failed([card])
                if not recovered and failed_this_run:
                    mcq_gen.error_message = f"Some source cards failed due to invalid AI output. Skipped {failed_this_run} item(s)."
                    db.commit()

        mcq_gen.status = MCQGenerationStatus.COMPLETED.value
        mcq_gen.completed_at = datetime.utcnow()
        db.commit()

        mcq_gen.status = MCQGenerationStatus.COMPLETED.value
        mcq_gen.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.exception("mcq background generation failed generation_id=%s deck_id=%s", generation_id, deck_id)
        try:
            mcq_gen = db.get(MCQGeneration, generation_id)
            if mcq_gen is not None:
                mcq_gen.status = MCQGenerationStatus.FAILED.value
                mcq_gen.error_message = str(exc)
                db.commit()
        except Exception:
            logger.exception("mcq background failure state update failed generation_id=%s", generation_id)
    finally:
        db.close()


@router.post("/decks/{deck_id}/generate-mcqs/start")
def start_generate_mcqs_for_deck(
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.models import MCQGeneration, MCQGenerationStatus, DeckMcqGenerationItem, DeckMcqGenerationItemStatus

    _require_ai_generation_access(user)
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    provider, resolution = _resolve_generation_provider_and_credential(db, user)
    credential = resolution.credential
    if not credential:
        raise HTTPException(status_code=400, detail=resolution.reason or "No AI credential configured.")

    source_cards = db.execute(select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.asc())).scalars().all()
    source_cards = [card for card in source_cards if card.card_type != "mcq"]
    if not source_cards:
        raise HTTPException(status_code=400, detail="No non-MCQ source cards found")

    existing_running = db.execute(
        select(MCQGeneration).where(MCQGeneration.deck_id == deck.id, MCQGeneration.status == MCQGenerationStatus.IN_PROGRESS.value)
    ).scalars().first()
    if existing_running:
        started_at = existing_running.started_at or existing_running.created_at
        processed = existing_running.processed_cards or 0
        now = datetime.now(started_at.tzinfo) if started_at and started_at.tzinfo else datetime.utcnow()
        if started_at and now - started_at > timedelta(minutes=10) and processed < (existing_running.total_cards or 0):
            existing_running.status = MCQGenerationStatus.FAILED.value
            existing_running.error_message = "Generation stalled without completing."
            db.commit()
        else:
            return {"ok": True, "generation_id": str(existing_running.id), "provider": provider, "already_running": True}

    existing_items = db.execute(
        select(DeckMcqGenerationItem).where(DeckMcqGenerationItem.deck_id == deck.id)
    ).scalars().all()
    item_by_card_id = {str(item.source_card_id): item for item in existing_items}
    changed = False
    for card in source_cards:
        key = str(card.id)
        if key not in item_by_card_id:
            db.add(DeckMcqGenerationItem(deck_id=deck.id, source_card_id=card.id, status=DeckMcqGenerationItemStatus.NOT_STARTED.value))
            changed = True
    if changed:
        db.commit()

    total_cards = len(source_cards)
    completed_before = db.execute(
        select(DeckMcqGenerationItem).where(
            DeckMcqGenerationItem.deck_id == deck.id,
            DeckMcqGenerationItem.status == DeckMcqGenerationItemStatus.COMPLETED.value,
        )
    ).scalars().all()

    mcq_gen = MCQGeneration(
        deck_id=deck.id,
        status=MCQGenerationStatus.IN_PROGRESS.value,
        total_cards=total_cards,
        processed_cards=len(completed_before),
        mcqs_generated=0,
        started_at=datetime.utcnow(),
        error_message=None,
    )
    db.add(mcq_gen)
    db.commit()
    db.refresh(mcq_gen)

    threading.Thread(
        target=_generate_mcqs_background,
        args=(str(deck.id), str(user.id), str(mcq_gen.id)),
        daemon=True,
    ).start()
    return {
        "ok": True,
        "generation_id": str(mcq_gen.id),
        "provider": provider,
        "already_running": False,
        "completed": len(completed_before),
        "total": total_cards,
    }


@router.get("/decks/{deck_id}/generate-mcqs/stream")
def generate_mcqs_stream(
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.models import MCQGeneration, MCQGenerationStatus

    _require_ai_generation_access(user)
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    provider, _ = _resolve_generation_provider_and_credential(db, user)

    def event_stream():
        sent_state = None
        while True:
            local_db = next(get_db())
            try:
                latest = local_db.execute(
                    select(MCQGeneration).where(MCQGeneration.deck_id == deck.id).order_by(MCQGeneration.created_at.desc())
                ).scalars().first()
                if latest is None:
                    payload = {"message": "No generation found."}
                    yield f"event: generation_error\ndata: {json.dumps(payload)}\n\n"
                    return

                payload = {
                    "generation_id": str(latest.id),
                    "status": latest.status,
                    "processed": latest.processed_cards or 0,
                    "completed": latest.processed_cards or 0,
                    "total": latest.total_cards or 0,
                    "total_mcqs": latest.mcqs_generated or 0,
                    "mcqs_created": latest.mcqs_generated or 0,
                    "provider": provider,
                    "error": latest.error_message,
                    "deck_url": f"/decks/{deck.id}",
                }
                state_key = json.dumps(payload, sort_keys=True)
                if state_key != sent_state:
                    if latest.status == MCQGenerationStatus.IN_PROGRESS.value:
                        event_name = "start" if sent_state is None else "progress"
                    elif latest.status == MCQGenerationStatus.COMPLETED.value:
                        event_name = "complete"
                    elif latest.status == MCQGenerationStatus.FAILED.value:
                        event_name = "generation_error"
                    else:
                        event_name = "progress"
                    yield f": keep-alive\n\n"
                    yield f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"
                    sent_state = state_key
                    if latest.status in {MCQGenerationStatus.COMPLETED.value, MCQGenerationStatus.FAILED.value}:
                        return
            finally:
                local_db.close()
            sleep(2)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/decks/{deck_id}/mcq-generation-status")
def get_mcq_generation_status(
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Get current MCQ generation status for a deck."""
    from app.models import MCQGeneration, DeckMcqGenerationItem, DeckMcqGenerationItemStatus
    
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)
    
    generations = db.execute(
        select(MCQGeneration).where(MCQGeneration.deck_id == deck.id).order_by(MCQGeneration.created_at.desc())
    ).scalars().all()
    
    # Get latest generation
    latest = generations[0] if generations else None
    
    # Count existing MCQs for this deck
    mcq_count = db.execute(
        select(Card.id).where(Card.deck_id == deck.id, Card.card_type == "mcq")
    ).scalars().all()
    generation_items = db.execute(
        select(DeckMcqGenerationItem).where(DeckMcqGenerationItem.deck_id == deck.id)
    ).scalars().all()
    completed_items = [item for item in generation_items if item.status == DeckMcqGenerationItemStatus.COMPLETED.value]

    return {
        "total_generations": len(generations),
        "latest": {
            "id": str(latest.id) if latest else None,
            "status": latest.status if latest else "not_started",
            "total_cards": latest.total_cards if latest else 0,
            "processed_cards": latest.processed_cards if latest else 0,
            "mcqs_generated": latest.mcqs_generated if latest else 0,
            "error_message": latest.error_message if latest else None,
            "started_at": latest.started_at.isoformat() if latest and latest.started_at else None,
            "completed_at": latest.completed_at.isoformat() if latest and latest.completed_at else None,
        } if latest else None,
        "total_mcqs_in_deck": len(mcq_count),
        "source_card_status": {
            "total": len(generation_items),
            "completed": len(completed_items),
            "pending": max(len(generation_items) - len(completed_items), 0),
        },
    }
