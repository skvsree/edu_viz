import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.db import get_db, SessionLocal
from app.models import (
    BulkAIUpload,
    BulkAIUploadFile,
    Card,
    Deck,
    Job,
    User,
)
from app.models.user_deck_favorite import UserDeckFavorite
from app.services.access import accessible_deck_clause, can_access_deck

router = APIRouter(prefix="/api/v1/live", tags=["deck-live"])


def _deck_counts(db: Session, deck_ids: list[UUID]) -> dict[str, dict[str, int]]:
    if not deck_ids:
        return {}
    rows = db.execute(
        select(
            Card.deck_id.label("deck_id"),
            func.count(Card.id).label("total_count"),
            func.sum(case((Card.card_type == "basic", 1), else_=0)).label("flashcard_count"),
            func.sum(case((Card.card_type == "mcq", 1), else_=0)).label("mcq_count"),
        )
        .where(Card.deck_id.in_(deck_ids))
        .group_by(Card.deck_id)
    ).all()
    result = {
        str(deck_id): {
            "flashcard_count": 0,
            "mcq_count": 0,
            "total_count": 0,
        }
        for deck_id in deck_ids
    }
    for row in rows:
        result[str(row.deck_id)] = {
            "flashcard_count": int(row.flashcard_count or 0),
            "mcq_count": int(row.mcq_count or 0),
            "total_count": int(row.total_count or 0),
        }
    return result


def _deck_import_status(db: Session, deck_ids: list[UUID]) -> dict[str, dict[str, str | bool]]:
    if not deck_ids:
        return {}
    result = {
        str(deck_id): {"import_in_progress": False, "badge_text": ""}
        for deck_id in deck_ids
    }
    rows = db.execute(
        select(Job.id, Job.status, BulkAIUpload.deck_id, BulkAIUploadFile.created_deck_id)
        .join(BulkAIUpload, BulkAIUpload.id == Job.reference_id)
        .outerjoin(BulkAIUploadFile, BulkAIUploadFile.bulk_upload_id == BulkAIUpload.id)
        .where(Job.job_type == "bulk_ai_upload")
        .where(Job.status.in_(["pending", "running"]))
        .where(
            or_(
                BulkAIUpload.deck_id.in_(deck_ids),
                BulkAIUploadFile.created_deck_id.in_(deck_ids),
            )
        )
    ).all()
    for _job_id, _status, bulk_deck_id, created_deck_id in rows:
        for deck_id in (bulk_deck_id, created_deck_id):
            if deck_id is None:
                continue
            key = str(deck_id)
            if key in result:
                result[key] = {
                    "import_in_progress": True,
                    "badge_text": "Import in progress",
                }
    return result


@router.get("/decks/counts")
def deck_counts(
    deck_ids: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    parsed_ids: list[UUID] = []
    for raw in deck_ids.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed_ids.append(UUID(raw))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid deck id: {raw}") from exc

    decks = db.execute(
        select(Deck)
        .where(Deck.id.in_(parsed_ids))
        .where(accessible_deck_clause(user))
    ).scalars().all()
    visible_ids = [deck.id for deck in decks]
    return {
        "counts": _deck_counts(db, visible_ids),
        "imports": _deck_import_status(db, visible_ids),
    }


@router.get("/decks/{deck_id}/counts")
def single_deck_counts(
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)
    counts = _deck_counts(db, [deck.id]).get(
        str(deck.id),
        {"flashcard_count": 0, "mcq_count": 0, "total_count": 0},
    )
    counts.update(
        _deck_import_status(db, [deck.id]).get(
            str(deck.id),
            {"import_in_progress": False, "badge_text": ""},
        )
    )
    return counts


@router.get("/decks/counts/stream")
async def deck_counts_stream(
    deck_ids: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    parsed_ids: list[UUID] = []
    for raw in deck_ids.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed_ids.append(UUID(raw))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid deck id: {raw}") from exc

    decks = db.execute(
        select(Deck)
        .where(Deck.id.in_(parsed_ids))
        .where(accessible_deck_clause(user))
    ).scalars().all()
    visible_ids = [deck.id for deck in decks]

    async def event_generator():
        while True:
            live_db = SessionLocal()
            try:
                payload = {
                    "counts": _deck_counts(live_db, visible_ids),
                    "imports": _deck_import_status(live_db, visible_ids),
                }
                yield f"data: {json.dumps(payload)}\n\n"
            finally:
                live_db.close()
            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs/{job_id}/stream")
async def job_stream(
    job_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404)

    bulk = None
    if job.reference_id and job.job_type == "bulk_ai_upload":
        bulk = db.get(BulkAIUpload, job.reference_id)
        if not bulk:
            raise HTTPException(status_code=404)
        if user.role != "system_admin" and bulk.user_id != user.id:
            raise HTTPException(status_code=403)
    elif user.role != "system_admin":
        raise HTTPException(status_code=403)

    async def event_generator():
        while True:
            live_db = SessionLocal()
            try:
                live_job = live_db.get(Job, UUID(job_id))
                if not live_job:
                    yield 'data: {"error":"Job not found"}\n\n'
                    break

                payload = {
                    "job": {
                        "id": str(live_job.id),
                        "job_type": live_job.job_type,
                        "status": live_job.status,
                        "reference_id": (
                            str(live_job.reference_id)
                            if live_job.reference_id
                            else None
                        ),
                        "total_items": int(live_job.total_items or 0),
                        "processed_items": int(live_job.processed_items or 0),
                        "failed_items": int(live_job.failed_items or 0),
                        "error_message": live_job.error_message,
                    }
                }

                if live_job.reference_id and live_job.job_type == "bulk_ai_upload":
                    live_bulk = live_db.get(BulkAIUpload, live_job.reference_id)
                    files = live_db.execute(
                        select(BulkAIUploadFile)
                        .where(BulkAIUploadFile.bulk_upload_id == live_job.reference_id)
                        .order_by(BulkAIUploadFile.created_at)
                    ).scalars().all()
                    created_deck_ids = [f.created_deck_id for f in files if f.created_deck_id]
                    created_decks = []
                    counts = _deck_counts(live_db, created_deck_ids)
                    if created_deck_ids:
                        deck_rows = live_db.execute(
                            select(Deck).where(Deck.id.in_(created_deck_ids))
                        ).scalars().all()
                        deck_map = {deck.id: deck for deck in deck_rows}
                        for f in files:
                            if not f.created_deck_id or f.created_deck_id not in deck_map:
                                continue
                            deck = deck_map[f.created_deck_id]
                            deck_counts_payload = counts.get(
                                str(deck.id),
                                {
                                    "flashcard_count": 0,
                                    "mcq_count": 0,
                                    "total_count": 0,
                                },
                            )
                            created_decks.append(
                                {
                                    "id": str(deck.id),
                                    "name": deck.name,
                                    "flashcard_count": deck_counts_payload["flashcard_count"],
                                    "mcq_count": deck_counts_payload["mcq_count"],
                                    "file_status": f.status,
                                    "original_filename": f.original_filename,
                                }
                            )
                    file_details = [
                        {
                            "id": str(f.id),
                            "original_filename": f.original_filename,
                            "status": f.status,
                            "error_message": f.error_message,
                        }
                        for f in files
                        if f.status in {"processing", "completed", "failed"}
                    ]
                    payload["bulk"] = {
                        "id": str(live_bulk.id) if live_bulk else None,
                        "status": live_bulk.status if live_bulk else None,
                        "total_files": int(live_bulk.total_files or 0) if live_bulk else 0,
                        "processed_files": int(live_bulk.processed_files or 0) if live_bulk else 0,
                        "flashcards_generated": int(live_bulk.flashcards_generated or 0) if live_bulk else 0,
                        "mcqs_generated": int(live_bulk.mcqs_generated or 0) if live_bulk else 0,
                        "failed_files": int(live_bulk.failed_files or 0) if live_bulk else 0,
                        "created_decks": created_decks,
                        "file_details": file_details,
                    }

                yield f"data: {json.dumps(payload)}\n\n"

                if live_job.status in {"completed", "failed"}:
                    break
            finally:
                live_db.close()
            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/dashboard/favorites/meta")
def dashboard_favorites_meta(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    favorite_ids = db.execute(
        select(UserDeckFavorite.deck_id).where(UserDeckFavorite.user_id == user.id)
    ).scalars().all()
    visible_decks = db.execute(
        select(Deck)
        .where(Deck.id.in_(favorite_ids))
        .where(accessible_deck_clause(user))
    ).scalars().all()
    visible_ids = [deck.id for deck in visible_decks]
    counts = _deck_counts(db, visible_ids)
    imports = _deck_import_status(db, visible_ids)
    return JSONResponse({"counts": counts, "imports": imports})
