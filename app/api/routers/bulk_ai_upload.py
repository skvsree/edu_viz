import io
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import pypdf
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select

from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.db import get_db
from app.models import (
    BulkAIUpload,
    BulkAIUploadChildFile,
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


IGNORED_ZIP_PREFIXES = ("__MACOSX/",)
IGNORED_ZIP_NAME_PREFIXES = ("._",)


def _should_queue_archive_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    basename = Path(normalized).name
    if not basename:
        return False
    if any(normalized.startswith(prefix) for prefix in IGNORED_ZIP_PREFIXES):
        return False
    if any(basename.startswith(prefix) for prefix in IGNORED_ZIP_NAME_PREFIXES):
        return False
    return normalized.lower().endswith(".pdf")


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
                    if not _should_queue_archive_member(info.filename):
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
        except StorageError as exc:
            raise HTTPException(status_code=503, detail=f"Failed to store upload file: {queued_name}") from exc

        child_file = BulkAIUploadChildFile(
            bulk_upload_id=bulk.id,
            child_key=f"{index}:{queued_name}",
            original_filename=queued_name,
            display_title=Path(queued_name).stem,
            storage_key=storage_key,
            file_size=len(queued_bytes),
        )
        db.add(child_file)
        db.flush()

        file_row = BulkAIUploadFile(
            bulk_upload_id=bulk.id,
            child_file_id=child_file.id,
            original_filename=queued_name,
            content_text=None,
            storage_key=storage_key,
            status=BulkAIUploadFileStatus.PENDING.value,
            created_deck_id=deck.id,
            file_size=len(queued_bytes),
        )
        db.add(file_row)
        db.flush()
        child_file.latest_attempt_id = file_row.id

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


def _latest_bulk_attempt_rows(db: Session, bulk_upload_id) -> list[BulkAIUploadFile]:
    rows = db.execute(
        select(BulkAIUploadFile)
        .where(BulkAIUploadFile.bulk_upload_id == bulk_upload_id)
        .order_by(BulkAIUploadFile.created_at.asc())
    ).scalars().all()
    if not rows:
        return []

    child_ids = [row.child_file_id for row in rows if row.child_file_id]
    child_map: dict[uuid.UUID, BulkAIUploadChildFile] = {}
    if child_ids:
        child_rows = db.execute(
            select(BulkAIUploadChildFile).where(BulkAIUploadChildFile.id.in_(child_ids))
        ).scalars().all()
        child_map = {child.id: child for child in child_rows}

    latest_rows: list[BulkAIUploadFile] = []
    for row in rows:
        if not row.child_file_id:
            latest_rows.append(row)
            continue
        child_row = child_map.get(row.child_file_id)
        if child_row and child_row.latest_attempt_id == row.id:
            latest_rows.append(row)
    return latest_rows


def _recompute_child_latest_attempt(
    db: Session,
    child_file: BulkAIUploadChildFile | None,
) -> BulkAIUploadFile | None:
    if child_file is None:
        return None

    latest_attempt = db.execute(
        select(BulkAIUploadFile)
        .where(BulkAIUploadFile.child_file_id == child_file.id)
        .order_by(BulkAIUploadFile.created_at.desc())
        .limit(1)
    ).scalars().first()
    child_file.latest_attempt_id = latest_attempt.id if latest_attempt else None
    return latest_attempt


def _prepare_fresh_retry_attempt(
    db: Session,
    *,
    bulk: BulkAIUpload,
    user: User,
    source_file: BulkAIUploadFile,
    child_file: BulkAIUploadChildFile | None = None,
) -> BulkAIUploadFile:
    old_deck_id = source_file.created_deck_id
    if old_deck_id:
        _clear_deck_generated_content(db, old_deck_id)

    replacement_deck = _ensure_bulk_upload_deck(
        db,
        user,
        source_file.original_filename or bulk.filename,
        None,
    )

    retry_row = BulkAIUploadFile(
        bulk_upload_id=bulk.id,
        child_file_id=child_file.id if child_file else source_file.child_file_id,
        original_filename=source_file.original_filename,
        extracted_title=source_file.extracted_title,
        extracted_description=source_file.extracted_description,
        content_text=None,
        storage_key=source_file.storage_key,
        status=BulkAIUploadFileStatus.PENDING.value,
        flashcards_generated=0,
        mcqs_generated=0,
        duplicate_count=0,
        error_message=None,
        file_size=source_file.file_size,
        started_at=None,
        completed_at=None,
        created_deck_id=replacement_deck.id,
    )
    db.add(retry_row)
    db.flush()
    if child_file:
        child_file.latest_attempt_id = retry_row.id
        if source_file.extracted_title:
            child_file.display_title = source_file.extracted_title[:255]
    return retry_row


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
        "deck_id": (created_deck_ids[0] if created_deck_ids else None),
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
    wants_json = (
        "application/json" in (request.headers.get("accept") or "")
        or request.headers.get("x-requested-with") == "fetch"
    )

    if existing_pending_job:
        payload = {
            "job_id": str(existing_pending_job.id),
            "status": existing_pending_job.status,
            "already_running": True,
        }
        if wants_json:
            return JSONResponse(payload)
        return RedirectResponse(
            url=(
                f"/settings/jobs?submitted=bulk-ai-upload&job={existing_pending_job.id}"
                "&notice=Bulk+upload+already+running"
            ),
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

    files = _latest_bulk_attempt_rows(db, bulk.id)

    return {
        "id": str(bulk.id),
        "job_id": str(job.id) if job else None,
        "job_status": job.status if job else None,
        "status": bulk.status,
        "deck_id": str(files[0].created_deck_id) if files and files[0].created_deck_id else None,
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
    file_id: str | None = None,
    deck_id: str | None = None,
    force: bool = False,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Resume a stopped or failed bulk AI upload, optionally for one file or one deck."""
    bulk = db.get(BulkAIUpload, bulk_id)
    if not bulk:
        raise HTTPException(status_code=404, detail="Bulk upload not found")
    if file_id and deck_id:
        raise HTTPException(status_code=400, detail="Use either file_id or deck_id, not both")

    resumable_statuses = {
        BulkAIUploadStatus.STOPPED.value,
        BulkAIUploadStatus.FAILED.value,
        BulkAIUploadStatus.COMPLETED.value,
    }
    if file_id or deck_id:
        resumable_statuses.add(BulkAIUploadStatus.PENDING.value)
        resumable_statuses.add(BulkAIUploadStatus.PROCESSING.value)
    if bulk.status not in resumable_statuses:
        raise HTTPException(status_code=400, detail="Bulk upload cannot be resumed")

    child_files = db.execute(
        select(BulkAIUploadChildFile)
        .where(BulkAIUploadChildFile.bulk_upload_id == bulk.id)
        .order_by(BulkAIUploadChildFile.created_at.asc())
    ).scalars().all()
    file_records = db.execute(
        select(BulkAIUploadFile)
        .where(BulkAIUploadFile.bulk_upload_id == bulk.id)
        .order_by(BulkAIUploadFile.created_at.asc())
    ).scalars().all()
    if not file_records:
        raise HTTPException(status_code=404, detail="Bulk upload files not found")

    deck_to_file_ids: dict[str, set[str]] = {}
    for file_record in file_records:
        deck_key = str(getattr(file_record, "created_deck_id", None) or "")
        if not deck_key:
            continue
        file_identity = str(
            getattr(file_record, "child_file_id", None)
            or getattr(file_record, "id", None)
            or getattr(file_record, "storage_key", None)
            or getattr(file_record, "original_filename", None)
            or ""
        )
        deck_to_file_ids.setdefault(deck_key, set()).add(file_identity)
    shared_deck_ids = [deck for deck, ids in deck_to_file_ids.items() if len(ids) > 1]
    if shared_deck_ids and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                "This bulk upload has corrupted shared-deck history from an older retry flow. "
                "Use force retry to create a fresh attempt, or start a new upload."
            ),
        )

    child_files_by_id = {
        str(getattr(child, "id", "")): child
        for child in child_files
        if getattr(child, "id", None)
    }
    target_file_records = file_records
    target_child_files: list[BulkAIUploadChildFile] = []
    if deck_id:
        deck_matches = [
            f for f in file_records if str(getattr(f, "created_deck_id", None) or "") == deck_id
        ]
        if not deck_matches:
            raise HTTPException(status_code=404, detail="Bulk upload deck not found")
        latest_match = max(
            deck_matches,
            key=lambda item: (
                item.created_at or datetime.min,
                item.started_at or datetime.min,
                item.completed_at or datetime.min,
            ),
        )
        target_file_records = [latest_match]
        target_child_files = [
            child for child in child_files
            if str(getattr(child, "id", None) or "") == str(getattr(latest_match, "child_file_id", None) or "")
        ]
        retryable_statuses = {
            BulkAIUploadFileStatus.FAILED.value,
            BulkAIUploadFileStatus.PROCESSING.value,
            BulkAIUploadFileStatus.STOPPED.value,
        }
        if force:
            retryable_statuses.add(BulkAIUploadFileStatus.PENDING.value)
            retryable_statuses.add(BulkAIUploadFileStatus.COMPLETED.value)
        invalid_records = [
            f for f in target_file_records if (f.status or "").lower() not in retryable_statuses
        ]
        if invalid_records:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Only failed or stuck deck files can be retried individually "
                    "unless force retry is enabled"
                ),
            )
        if not getattr(latest_match, "child_file_id", None) and not force:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This deck belongs to a legacy bulk row with no child file record. "
                    "Use whole-bulk retry, or force retry if you intentionally want to retry this legacy deck."
                ),
            )
    elif file_id:
        target_child_files = [child for child in child_files if str(child.id) == file_id]
        legacy_attempt_id_used = False
        if target_child_files:
            target_file_records = [target_child_files[0].latest_attempt] if target_child_files[0].latest_attempt else []
        else:
            target_file_records = [
                f for f in file_records if str(getattr(f, "id", "")) == file_id
            ]
            if target_file_records:
                legacy_attempt_id_used = True
                child_match = child_files_by_id.get(
                    str(getattr(target_file_records[0], "child_file_id", None) or "")
                )
                target_child_files = [child_match] if child_match else []
        if not target_file_records:
            legacy_group = [
                f for f in file_records
                if not getattr(f, "child_file_id", None)
                and str(
                    getattr(f, "created_deck_id", None)
                    or getattr(f, "id", None)
                    or ""
                ) == file_id
            ]
            if legacy_group:
                legacy_attempt_id_used = True
                target_file_records = [
                    max(
                        legacy_group,
                        key=lambda item: (
                            item.created_at or datetime.min,
                            item.started_at or datetime.min,
                            item.completed_at or datetime.min,
                        ),
                    )
                ]
        if not target_file_records:
            raise HTTPException(status_code=404, detail="Bulk upload file not found")
        target_file = target_file_records[0]
        if legacy_attempt_id_used and getattr(target_file, "child_file_id", None):
            raise HTTPException(
                status_code=409,
                detail="This retry target now requires child_file_id. Refresh the Jobs page and retry again.",
            )
        retryable_statuses = {
            BulkAIUploadFileStatus.FAILED.value,
            BulkAIUploadFileStatus.PROCESSING.value,
            BulkAIUploadFileStatus.STOPPED.value,
        }
        if force:
            retryable_statuses.add(BulkAIUploadFileStatus.PENDING.value)
            retryable_statuses.add(BulkAIUploadFileStatus.COMPLETED.value)
        if getattr(target_file, "status", None) not in retryable_statuses:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Only failed or stuck files can be retried individually "
                    "unless force retry is enabled"
                ),
            )
        if legacy_attempt_id_used and not getattr(target_file, "child_file_id", None) and not force:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This legacy upload row has no child file record. "
                    "Use whole-bulk retry, or force retry if you intentionally want to retry this legacy row."
                ),
            )

    missing_storage_keys = []
    storage = get_storage()
    for file_record in target_file_records:
        if not getattr(file_record, "storage_key", None):
            missing_storage_keys.append(getattr(file_record, "original_filename", "Unknown file"))
            continue
        try:
            storage.open_bytes(key=file_record.storage_key)
        except Exception:
            missing_storage_keys.append(file_record.original_filename)

    if missing_storage_keys:
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot retry because original upload files are missing from storage: "
                + ", ".join(missing_storage_keys[:5])
                + ("..." if len(missing_storage_keys) > 5 else "")
            ),
        )

    def _mark_previous_attempt_superseded(target_file: BulkAIUploadFile) -> None:
        previous_status = (target_file.status or "").lower()
        if previous_status == BulkAIUploadFileStatus.FAILED.value:
            bulk.failed_files = max((bulk.failed_files or 0) - 1, 0)
        elif previous_status == BulkAIUploadFileStatus.COMPLETED.value:
            bulk.processed_files = max((bulk.processed_files or 0) - 1, 0)
        target_file.status = BulkAIUploadFileStatus.STOPPED.value
        target_file.error_message = "Superseded by retry"
        if not target_file.completed_at:
            target_file.completed_at = datetime.utcnow()

    if file_id or deck_id:
        if not target_child_files and target_file_records:
            for target_file in target_file_records:
                child_match = child_files_by_id.get(str(target_file.child_file_id or ""))
                if child_match:
                    target_child_files.append(child_match)
        for index, target_file in enumerate(target_file_records):
            _mark_previous_attempt_superseded(target_file)
            child_file = target_child_files[index] if index < len(target_child_files) else None
            retry_row = _prepare_fresh_retry_attempt(
                db,
                bulk=bulk,
                user=user,
                source_file=target_file,
                child_file=child_file,
            )
            file_records.append(retry_row)
        all_statuses = [(f.status or '').lower() for f in file_records]
        if any(status == BulkAIUploadFileStatus.PROCESSING.value for status in all_statuses):
            bulk.status = BulkAIUploadStatus.PROCESSING.value
        elif any(status == BulkAIUploadFileStatus.PENDING.value for status in all_statuses):
            bulk.status = BulkAIUploadStatus.PENDING.value
        elif any(status == BulkAIUploadFileStatus.FAILED.value for status in all_statuses):
            bulk.status = BulkAIUploadStatus.FAILED.value
        elif any(status == BulkAIUploadFileStatus.STOPPED.value for status in all_statuses):
            bulk.status = BulkAIUploadStatus.STOPPED.value
        else:
            bulk.status = BulkAIUploadStatus.COMPLETED.value
        bulk.is_auto_stop = any(status in {
            BulkAIUploadFileStatus.PENDING.value,
            BulkAIUploadFileStatus.PROCESSING.value,
            BulkAIUploadFileStatus.STOPPED.value,
        } for status in all_statuses)
        if bulk.status != BulkAIUploadStatus.FAILED.value:
            bulk.error_message = None
        if bulk.status != BulkAIUploadStatus.COMPLETED.value:
            bulk.completed_at = None
        total_items = len(target_file_records)
    else:
        if child_files:
            for child_file in child_files:
                latest_attempt = child_file.latest_attempt
                if latest_attempt is None:
                    continue
                _mark_previous_attempt_superseded(latest_attempt)
                retry_row = _prepare_fresh_retry_attempt(
                    db,
                    bulk=bulk,
                    user=user,
                    source_file=latest_attempt,
                    child_file=child_file,
                )
                file_records.append(retry_row)
        else:
            latest_legacy_attempts: dict[str, BulkAIUploadFile] = {}
            for file_record in file_records:
                legacy_key = str(file_record.created_deck_id or file_record.id)
                previous = latest_legacy_attempts.get(legacy_key)
                if previous is None:
                    latest_legacy_attempts[legacy_key] = file_record
                    continue
                previous_rank = (
                    previous.created_at or datetime.min,
                    previous.started_at or datetime.min,
                    previous.completed_at or datetime.min,
                )
                current_rank = (
                    file_record.created_at or datetime.min,
                    file_record.started_at or datetime.min,
                    file_record.completed_at or datetime.min,
                )
                if current_rank > previous_rank:
                    latest_legacy_attempts[legacy_key] = file_record
            for latest_attempt in latest_legacy_attempts.values():
                _mark_previous_attempt_superseded(latest_attempt)
                retry_row = _prepare_fresh_retry_attempt(
                    db,
                    bulk=bulk,
                    user=user,
                    source_file=latest_attempt,
                    child_file=None,
                )
                file_records.append(retry_row)
        bulk.status = BulkAIUploadStatus.PENDING.value
        bulk.is_auto_stop = False
        bulk.error_message = None
        bulk.processed_files = 0
        bulk.failed_files = 0
        bulk.skipped_files = 0
        bulk.flashcards_generated = 0
        bulk.mcqs_generated = 0
        bulk.completed_at = None
        total_items = len(target_file_records)
    db.commit()

    job = Job(
        job_type="bulk_ai_upload",
        reference_id=bulk.id,
        status=JobStatus.PENDING.value,
        total_items=total_items,
    )
    db.add(job)
    db.commit()

    return {
        "id": str(bulk.id),
        "job_id": str(job.id),
        "status": bulk.status,
        "retried_file_id": file_id,
        "retried_deck_id": deck_id,
    }


@router.post("/bulk-ai-upload/{bulk_id}/cancel")
def cancel_bulk_ai_upload(
    bulk_id: uuid.UUID,
    file_id: uuid.UUID | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    bulk = db.get(BulkAIUpload, bulk_id)
    if not bulk or bulk.user_id != user.id:
        raise HTTPException(status_code=404, detail="Bulk upload not found")

    if file_id:
        child_file = db.get(BulkAIUploadChildFile, file_id)
        file_record = None
        if child_file and child_file.bulk_upload_id == bulk.id:
            candidate_rows = db.execute(
                select(BulkAIUploadFile)
                .where(BulkAIUploadFile.child_file_id == child_file.id)
                .where(
                    BulkAIUploadFile.status.in_(
                        [
                            BulkAIUploadFileStatus.PENDING.value,
                            BulkAIUploadFileStatus.PROCESSING.value,
                        ]
                    )
                )
                .order_by(BulkAIUploadFile.created_at.desc())
            ).scalars().all()
            file_record = candidate_rows[0] if candidate_rows else child_file.latest_attempt
        else:
            file_record = db.get(BulkAIUploadFile, file_id)
            if file_record and file_record.bulk_upload_id == bulk.id:
                child_file = (
                    db.get(BulkAIUploadChildFile, file_record.child_file_id)
                    if file_record.child_file_id
                    else None
                )
        if not file_record or file_record.bulk_upload_id != bulk.id:
            raise HTTPException(status_code=404, detail="Bulk upload file not found")
        if file_record.status not in {
            BulkAIUploadFileStatus.PENDING.value,
            BulkAIUploadFileStatus.PROCESSING.value,
        }:
            raise HTTPException(status_code=400, detail="Only pending or running files can be canceled")
        file_record.status = BulkAIUploadFileStatus.STOPPED.value
        file_record.error_message = "File canceled by user"
        file_record.completed_at = datetime.utcnow()
        latest_active_statuses = {
            BulkAIUploadFileStatus.PENDING.value,
            BulkAIUploadFileStatus.PROCESSING.value,
            BulkAIUploadFileStatus.STOPPED.value,
        }
        sibling_rows = _latest_bulk_attempt_rows(db, bulk.id)
        sibling_statuses = [(row.status or "").lower() for row in sibling_rows]
        if any(status == BulkAIUploadFileStatus.PROCESSING.value for status in sibling_statuses):
            bulk.status = BulkAIUploadStatus.PROCESSING.value
        elif any(status == BulkAIUploadFileStatus.PENDING.value for status in sibling_statuses):
            bulk.status = BulkAIUploadStatus.PENDING.value
        elif any(status == BulkAIUploadFileStatus.FAILED.value for status in sibling_statuses):
            bulk.status = BulkAIUploadStatus.FAILED.value
        elif any(status == BulkAIUploadFileStatus.STOPPED.value for status in sibling_statuses):
            bulk.status = BulkAIUploadStatus.STOPPED.value
        else:
            bulk.status = BulkAIUploadStatus.COMPLETED.value
        bulk.is_auto_stop = any(status in latest_active_statuses for status in sibling_statuses)
        if bulk.status != BulkAIUploadStatus.FAILED.value:
            bulk.error_message = None
        if bulk.status != BulkAIUploadStatus.COMPLETED.value:
            bulk.completed_at = None
        latest_child_attempt = _recompute_child_latest_attempt(db, child_file)
        db.commit()
        return {
            "id": str(bulk.id),
            "status": bulk.status,
            "canceled_file_id": str(child_file.id if child_file else file_id),
            "latest_attempt_id": str(latest_child_attempt.id) if latest_child_attempt else None,
        }

    bulk.is_auto_stop = True
    bulk.error_message = "Job stop requested by user"
    active_files = [
        file_record
        for file_record in _latest_bulk_attempt_rows(db, bulk.id)
        if file_record.status in {
            BulkAIUploadFileStatus.PENDING.value,
            BulkAIUploadFileStatus.PROCESSING.value,
        }
    ]
    stopped_at = datetime.utcnow()
    for file_record in active_files:
        file_record.status = BulkAIUploadFileStatus.STOPPED.value
        file_record.error_message = "Job canceled by user"
        file_record.completed_at = stopped_at
    all_rows = _latest_bulk_attempt_rows(db, bulk.id)
    all_statuses = [(row.status or "").lower() for row in all_rows]
    if any(status == BulkAIUploadFileStatus.PROCESSING.value for status in all_statuses):
        bulk.status = BulkAIUploadStatus.PROCESSING.value
    elif any(status == BulkAIUploadFileStatus.PENDING.value for status in all_statuses):
        bulk.status = BulkAIUploadStatus.PENDING.value
    elif any(status == BulkAIUploadFileStatus.FAILED.value for status in all_statuses):
        bulk.status = BulkAIUploadStatus.FAILED.value
    elif any(status == BulkAIUploadFileStatus.STOPPED.value for status in all_statuses):
        bulk.status = BulkAIUploadStatus.STOPPED.value
    else:
        bulk.status = BulkAIUploadStatus.COMPLETED.value
    bulk.is_auto_stop = any(
        status in {
            BulkAIUploadFileStatus.PENDING.value,
            BulkAIUploadFileStatus.PROCESSING.value,
            BulkAIUploadFileStatus.STOPPED.value,
        }
        for status in all_statuses
    )
    if bulk.status != BulkAIUploadStatus.COMPLETED.value:
        bulk.completed_at = None
    db.commit()
    return {
        "id": str(bulk.id),
        "status": bulk.status,
        "cancel_requested": True,
        "canceled_files": len(active_files),
    }


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
