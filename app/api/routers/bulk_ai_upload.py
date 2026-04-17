import io
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import pypdf
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select, update

from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.db import get_db
from app.models import (
    BulkAIUpload,
    BulkAIUploadFile,
    BulkAIUploadFileStatus,
    BulkAIUploadStatus,
    Card,
    CardState,
    Deck,
    Job,
    JobStatus,
    Review,
    User,
)
from app.models.deck import DeckAccessScope
from app.services.access import normalize_deck_name
from app.services.storage import StorageError, get_storage, guess_content_type


router = APIRouter(prefix="/api/v1", tags=["bulk-ai-upload"])


def enqueue_ai_upload_job(
    db: Session,
    *,
    user: User,
    source_file: UploadFile,
    folder_id: str | None = None,
    existing_deck_id: str | None = None,
) -> tuple[BulkAIUpload, Job]:
    """Queue AI upload work using the shared bulk upload job path.

    When existing_deck_id is provided, the worker reuses that deck instead of creating a new one.
    """
    filename = source_file.filename or ""
    is_zip = filename.lower().endswith(".zip")

    if not is_zip and not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only ZIP or PDF files are accepted")

    try:
        target_folder_id = uuid.UUID(folder_id) if folder_id else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid folder id") from exc

    upload_bytes = source_file.file.read()
    if not upload_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    source_file.file.seek(0)

    files_to_queue: list[tuple[str, bytes]] = []
    if is_zip:
        try:
            with zipfile.ZipFile(io.BytesIO(upload_bytes)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    if not info.filename.lower().endswith(".pdf"):
                        continue
                    with archive.open(info) as file_obj:
                        files_to_queue.append((Path(info.filename).name, file_obj.read()))
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail="Invalid ZIP file") from exc
        if not files_to_queue:
            raise HTTPException(status_code=400, detail="ZIP file does not contain any PDFs")
        if existing_deck_id:
            raise HTTPException(status_code=400, detail="ZIP upload cannot target an existing deck")
    else:
        files_to_queue.append((filename, upload_bytes))

    bulk = BulkAIUpload(
        user_id=user.id,
        filename=filename,
        status=BulkAIUploadStatus.PENDING.value,
        total_files=len(files_to_queue),
        processed_files=0,
        flashcards_generated=0,
        mcqs_generated=0,
        failed_files=0,
        error_message=f"folder_id={target_folder_id}" if target_folder_id else None,
        is_auto_stop=False,
        deck_id=uuid.UUID(existing_deck_id) if existing_deck_id else None,
    )
    db.add(bulk)
    db.flush()

    storage = get_storage()
    for index, (queued_name, queued_bytes) in enumerate(files_to_queue):
        deck = _ensure_bulk_upload_deck(
            db,
            user,
            queued_name,
            target_folder_id,
            existing_deck_id=existing_deck_id if index == 0 else None,
        )
        storage_key = None
        try:
            object_key = f"bulk-ai-upload/{bulk.id}/{uuid.uuid4()}-{Path(queued_name).name}"
            stored = storage.save_bytes(
                key=object_key,
                data=queued_bytes,
                content_type=guess_content_type(queued_name),
            )
            storage_key = stored.key
        except StorageError:
            storage_key = None

        file_row = BulkAIUploadFile(
            bulk_upload_id=bulk.id,
            original_filename=queued_name,
            content_text=None if storage_key else queued_bytes.decode("latin1"),
            storage_key=storage_key,
            status=BulkAIUploadFileStatus.PENDING.value,
            created_deck_id=deck.id,
        )
        db.add(file_row)

    job = Job(
        job_type="bulk_ai_upload",
        status=JobStatus.PENDING.value,
        reference_id=bulk.id,
        total_items=len(files_to_queue),
        processed_items=0,
        failed_items=0,
    )
    db.add(job)
    db.commit()
    db.refresh(bulk)
    db.refresh(job)
    return bulk, job


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract text content from PDF bytes."""
    try:
        pdf_file = io.BytesIO(file_bytes)
        reader = pypdf.PdfReader(pdf_file)
        text_parts = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                text_parts.append(text)
        return "\n".join(text_parts)
    except Exception:
        return ""


def extract_title_from_text(text: str, filename: str) -> tuple[str, str | None]:
    """Extract title and description from text content."""
    lines = text.strip().split("\n") if text else []
    title = None
    description = None

    for line in lines[:20]:
        line = line.strip()
        if line and len(line) > 3 and not line.isdigit():
            title = line[:250]
            break

    if not title:
        title = Path(filename).stem[:250]

    if title:
        title_idx = text.find(title)
        if title_idx != -1:
            remaining = text[title_idx + len(title):].strip()
            if remaining:
                sentences = remaining.split(".")[:3]
                description = ".".join(sentences).strip()
                if description and not description.endswith("."):
                    description += "."

    return title, description


def _make_unique_deck_name(db: Session, user: User, base_title: str) -> str:
    base_name = (base_title or "Untitled").strip()[:255] or "Untitled"
    candidate = base_name
    suffix = 2
    while True:
        normalized = normalize_deck_name(candidate)
        exists = db.execute(
            select(Deck.id)
            .where(Deck.user_id == user.id)
            .where(Deck.normalized_name == normalized)
            .where(Deck.is_deleted.is_(False))
            .limit(1)
        ).scalar_one_or_none()
        if not exists:
            return candidate
        trimmed = base_name[: max(1, 255 - len(f"-{suffix}"))].rstrip()
        candidate = f"{trimmed}-{suffix}"
        suffix += 1


def _clear_deck_generated_content(db: Session, deck_id):
    card_ids = db.execute(select(Card.id).where(Card.deck_id == deck_id)).scalars().all()
    card_id_list = list(card_ids)
    if not card_id_list:
        return
    db.execute(delete(CardState).where(CardState.card_id.in_(card_id_list)))
    db.execute(delete(Review).where(Review.card_id.in_(card_id_list)))
    db.execute(delete(Card).where(Card.id.in_(card_id_list)))


def _ensure_bulk_upload_deck(
    db: Session,
    user: User,
    filename: str,
    folder_id: str | None,
    existing_deck_id=None,
) -> Deck:
    if existing_deck_id:
        existing = db.get(Deck, existing_deck_id)
        if existing and existing.user_id == user.id and not existing.is_deleted:
            if existing.folder_id != folder_id:
                existing.folder_id = folder_id
                db.flush()
            return existing

    deck_name = _make_unique_deck_name(db, user, Path(filename or "upload").stem)
    deck = Deck(
        user_id=user.id,
        organization_id=user.organization_id,
        folder_id=folder_id,
        name=deck_name,
        normalized_name=normalize_deck_name(deck_name),
        description=None,
        access_level=DeckAccessScope.USER,
        is_global=False,
    )
    db.add(deck)
    db.flush()
    return deck


@router.post("/bulk-ai-upload/start")
def start_bulk_ai_upload(
    source_file: UploadFile = File(...),
    folder_id: str | None = Form(default=None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Accept upload, create all file rows and decks up front, then queue worker processing."""
    print(
        (
            f"[bulk-ai-upload/start] user={user.id} "
            f"folder_id={folder_id!r} "
            f"filename={getattr(source_file, 'filename', None)!r}"
        ),
        flush=True,
    )
    bulk, job = enqueue_ai_upload_job(
        db,
        user=user,
        source_file=source_file,
        folder_id=folder_id,
    )

    file_rows = db.execute(
        select(BulkAIUploadFile)
        .where(BulkAIUploadFile.bulk_upload_id == bulk.id)
        .order_by(BulkAIUploadFile.created_at)
    ).scalars().all()
    created_deck_ids = [str(row.created_deck_id) for row in file_rows if row.created_deck_id]

    return {
        "id": str(bulk.id),
        "job_id": str(job.id),
        "status": bulk.status,
        "deck_id": (
            str(bulk.deck_id)
            if bulk.deck_id
            else (created_deck_ids[0] if created_deck_ids else None)
        ),
        "deck_ids": created_deck_ids,
        "total_files": bulk.total_files,
        "created_decks": len(created_deck_ids),
        "message": "Submitted. Redirecting to Jobs.",
    }


@router.post("/decks/{deck_id}/ai-import/start")
def start_single_deck_ai_upload(
    deck_id: str,
    request: Request,
    source_file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or deck.is_deleted:
        raise HTTPException(status_code=404)

    from app.services.access import can_manage_deck, can_use_ai_generation

    if not can_use_ai_generation(user) or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    existing_pending_job = db.execute(
        select(Job)
        .join(BulkAIUpload, BulkAIUpload.id == Job.reference_id)
        .where(Job.job_type == "bulk_ai_upload")
        .where(Job.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]))
        .where(BulkAIUpload.deck_id == deck.id)
        .order_by(Job.created_at.desc())
        .limit(1)
    ).scalars().first()
    wants_json = "application/json" in (request.headers.get("accept") or "") or request.headers.get("x-requested-with") == "fetch"

    if existing_pending_job:
        payload = {
            "job_id": str(existing_pending_job.id),
            "status": existing_pending_job.status,
            "already_running": True,
        }
        if wants_json:
            return JSONResponse(payload)
        return RedirectResponse(
            url=f"/settings/jobs?submitted=bulk-ai-upload&job={existing_pending_job.id}&notice=Bulk+upload+already+running",
            status_code=303,
        )

    bulk, job = enqueue_ai_upload_job(
        db,
        user=user,
        source_file=source_file,
        existing_deck_id=deck_id,
    )
    payload = {
        "job_id": str(job.id),
        "bulk_upload_id": str(bulk.id),
        "status": bulk.status,
        "total_files": bulk.total_files,
        "already_running": False,
        "message": "Submitted. Redirecting to Jobs.",
    }
    if wants_json:
        return JSONResponse(payload)
    return RedirectResponse(
        url=f"/settings/jobs?submitted=bulk-ai-upload&job={job.id}&notice=Bulk+upload+submitted",
        status_code=303,
    )


@router.get("/bulk-ai-upload/{bulk_id}")
def get_bulk_ai_upload(
    bulk_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Get status of a bulk AI upload."""
    bulk = db.get(BulkAIUpload, bulk_id)
    if not bulk:
        raise HTTPException(status_code=404, detail="Bulk upload not found")

    # Get associated job
    job = db.execute(
        select(Job)
        .where(Job.reference_id == bulk.id)
        .where(Job.job_type == "bulk_ai_upload")
        .order_by(Job.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    files = db.execute(
        select(BulkAIUploadFile)
        .where(BulkAIUploadFile.bulk_upload_id == bulk.id)
        .order_by(BulkAIUploadFile.created_at)
    ).scalars().all()

    return {
        "id": str(bulk.id),
        "job_id": str(job.id) if job else None,
        "job_status": job.status if job else None,
        "status": bulk.status,
        "deck_id": str(bulk.deck_id) if bulk.deck_id else None,
        "total_files": bulk.total_files,
        "processed_files": bulk.processed_files,
        "flashcards_generated": bulk.flashcards_generated,
        "mcqs_generated": bulk.mcqs_generated,
        "skipped_files": bulk.skipped_files,
        "failed_files": bulk.failed_files,
        "error_message": bulk.error_message,
        "started_at": bulk.started_at.isoformat() if bulk.started_at else None,
        "completed_at": bulk.completed_at.isoformat() if bulk.completed_at else None,
        "created_at": bulk.created_at.isoformat(),
        "files": [
            {
                "id": str(f.id),
                "original_filename": f.original_filename,
                "created_deck_id": str(f.created_deck_id) if f.created_deck_id else None,
                "extracted_title": f.extracted_title,
                "extracted_description": f.extracted_description,
                "status": f.status,
                "flashcards_generated": f.flashcards_generated,
                "mcqs_generated": f.mcqs_generated,
                "duplicate_count": f.duplicate_count,
                "error_message": f.error_message,
            }
            for f in files
        ],
    }


@router.post("/bulk-ai-upload/{bulk_id}/stop")
def stop_bulk_ai_upload(
    bulk_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Stop a running bulk AI upload."""
    bulk = db.get(BulkAIUpload, bulk_id)
    if not bulk:
        raise HTTPException(status_code=404, detail="Bulk upload not found")

    if bulk.status not in {BulkAIUploadStatus.PENDING.value, BulkAIUploadStatus.PROCESSING.value}:
        raise HTTPException(status_code=400, detail="Bulk upload is not running")

    bulk.is_auto_stop = True
    bulk.status = BulkAIUploadStatus.STOPPED.value
    db.commit()

    # Also stop the job
    job = db.execute(
        select(Job)
        .where(Job.reference_id == bulk.id)
        .where(Job.job_type == "bulk_ai_upload")
        .where(Job.status == JobStatus.RUNNING.value)
        .limit(1)
    ).scalar_one_or_none()

    if job:
        job.status = JobStatus.FAILED.value
        job.error_message = "Stopped by user"
        job.completed_at = datetime.utcnow()
        db.commit()

    return {"id": str(bulk.id), "status": bulk.status}


@router.post("/bulk-ai-upload/{bulk_id}/resume")
def resume_bulk_ai_upload(
    bulk_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Resume a stopped bulk AI upload."""
    bulk = db.get(BulkAIUpload, bulk_id)
    if not bulk:
        raise HTTPException(status_code=404, detail="Bulk upload not found")

    if bulk.status not in {BulkAIUploadStatus.STOPPED.value, BulkAIUploadStatus.FAILED.value}:
        raise HTTPException(status_code=400, detail="Bulk upload cannot be resumed")

    deck = _ensure_bulk_upload_deck(db, user, bulk.filename, None, existing_deck_id=bulk.deck_id)
    bulk.deck_id = deck.id
    _clear_deck_generated_content(db, deck.id)

    # Reset all file records for full regeneration into the same deck
    db.execute(
        update(BulkAIUploadFile)
        .where(BulkAIUploadFile.bulk_upload_id == bulk.id)
        .values(
            status=BulkAIUploadFileStatus.PENDING.value,
            created_deck_id=deck.id,
            flashcards_generated=0,
            mcqs_generated=0,
            duplicate_count=0,
            error_message=None,
            started_at=None,
            completed_at=None,
        )
    )

    bulk.status = BulkAIUploadStatus.PENDING.value
    bulk.is_auto_stop = False
    bulk.error_message = None
    bulk.processed_files = 0
    bulk.failed_files = 0
    bulk.skipped_files = 0
    bulk.flashcards_generated = 0
    bulk.mcqs_generated = 0
    db.commit()

    # Create new job
    job = Job(
        job_type="bulk_ai_upload",
        reference_id=bulk.id,
        status=JobStatus.PENDING.value,
        total_items=bulk.total_files,
    )
    db.add(job)
    db.commit()

    return {"id": str(bulk.id), "job_id": str(job.id), "status": bulk.status}


@router.get("/jobs")
def list_jobs(
    user: User = Depends(current_user),
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """List all jobs."""
    # Only system admin can list all jobs
    if user.role != "system_admin":
        raise HTTPException(status_code=403, detail="Admin only")

    jobs = db.execute(
        select(Job)
        .order_by(Job.created_at.desc())
        .limit(limit)
    ).scalars().all()

    return {
        "jobs": [
            {
                "id": str(j.id),
                "job_type": j.job_type,
                "status": j.status,
                "reference_id": str(j.reference_id) if j.reference_id else None,
                "total_items": j.total_items,
                "processed_items": j.processed_items,
                "failed_items": j.failed_items,
                "error_message": j.error_message,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None,
                "created_at": j.created_at.isoformat(),
            }
            for j in jobs
        ]
    }
