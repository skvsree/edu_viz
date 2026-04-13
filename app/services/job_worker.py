"""
Job worker service for processing background jobs.
Run as: python -m app.services.job_worker
"""

import io
import os
import signal
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pypdf
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import Card, CardState, Job, JobStatus, BulkAIUpload, BulkAIUploadStatus, BulkAIUploadFile, BulkAIUploadFileStatus, Deck, Review, User
from app.services.access import normalize_deck_name
from app.services.ai_auth import get_env_ai_provider_name, get_scope_provider, resolve_ai_credential
from app.services.ai_generation import AIGenerationError, build_iterative_study_pack_prompt, get_study_pack_provider, merge_study_packs, normalize_generated_text
from app.services.storage import get_storage, StorageError


WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"
MAX_WORKERS = int(os.environ.get("JOB_WORKER_THREADS", "2"))
POLL_INTERVAL = int(os.environ.get("JOB_POLL_INTERVAL", "5"))
JOB_LEASE_SECONDS = int(os.environ.get("JOB_LEASE_SECONDS", "60"))
_shutdown = False
_active_jobs: set[uuid.UUID] = set()
_active_jobs_lock = threading.Lock()


def extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        text_parts = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                text_parts.append(text)
        return "\n".join(text_parts)
    except Exception:
        return ""


def extract_title_from_text(text: str, filename: str) -> tuple[str, str | None]:
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


def _split_text_for_ai_upload(text: str, *, max_chars: int = 6000, overlap_chars: int = 800) -> list[str]:
    source = (text or "").strip()
    if not source:
        return []

    paragraphs = [part.strip() for part in source.split("\n\n") if part.strip()]
    if not paragraphs:
        paragraphs = [source]

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= max_chars:
            current = paragraph
            continue
        start = 0
        while start < len(paragraph):
            end = min(len(paragraph), start + max_chars)
            piece = paragraph[start:end].strip()
            if piece:
                chunks.append(piece)
            if end >= len(paragraph):
                break
            start = max(0, end - overlap_chars)
        current = ""
    if current:
        chunks.append(current)
    return chunks



def _clear_deck_generated_content(db: Session, deck_id: uuid.UUID) -> None:
    card_ids = db.execute(select(Card.id).where(Card.deck_id == deck_id)).scalars().all()
    card_id_list = list(card_ids)
    if not card_id_list:
        return
    db.execute(delete(CardState).where(CardState.card_id.in_(card_id_list)))
    db.execute(delete(Review).where(Review.card_id.in_(card_id_list)))
    db.execute(delete(Card).where(Card.id.in_(card_id_list)))



def process_bulk_ai_upload(db: Session, job: Job) -> None:

    """Process queued upload bytes and create one deck per PDF with AI-generated cards."""
    bulk = db.get(BulkAIUpload, job.reference_id)
    if not bulk:
        job.status = JobStatus.FAILED.value
        job.error_message = "Bulk upload not found"
        return

    bulk.status = BulkAIUploadStatus.PROCESSING.value
    bulk.started_at = datetime.utcnow()
    db.commit()

    file_records = db.execute(
        select(BulkAIUploadFile)
        .where(BulkAIUploadFile.bulk_upload_id == bulk.id)
        .order_by(BulkAIUploadFile.created_at)
    ).scalars().all()
    if not file_records:
        bulk.status = BulkAIUploadStatus.FAILED.value
        bulk.error_message = "Missing queued upload files"
        job.status = JobStatus.FAILED.value
        job.error_message = "Missing queued upload files"
        db.commit()
        return

    owner = db.get(User, bulk.user_id) if bulk.user_id else (db.get(User, bulk.deck.user_id) if getattr(bulk, 'deck', None) else None)
    if not owner:
        bulk.status = BulkAIUploadStatus.FAILED.value
        bulk.error_message = "Missing deck owner"
        job.status = JobStatus.FAILED.value
        job.error_message = "Missing deck owner"
        db.commit()
        return

    folder_id = None
    if bulk.error_message and bulk.error_message.startswith('folder_id='):
        raw = bulk.error_message.split('=', 1)[1].strip()
        if raw:
            folder_id = uuid.UUID(raw)

    provider_name, credential = _resolve_ai_provider_and_credential(db, owner)
    bulk.provider = credential.provider or provider_name
    db.commit()

    storage = get_storage()
    bulk.total_files = len(file_records)
    job.total_items = len(file_records)
    db.commit()

    for file_record in file_records:
        bulk = db.get(BulkAIUpload, job.reference_id)
        if bulk and bulk.is_auto_stop:
            bulk.status = BulkAIUploadStatus.STOPPED.value
            db.commit()
            job.status = JobStatus.FAILED.value
            job.error_message = 'Job stopped by user'
            job.completed_at = datetime.utcnow()
            db.commit()
            return

        file_started_at = datetime.utcnow()
        file_record.status = BulkAIUploadFileStatus.PROCESSING.value
        file_record.started_at = file_started_at
        file_record.completed_at = None
        file_record.error_message = None
        db.commit()

        upload_bytes = None
        if file_record.storage_key:
            try:
                upload_bytes, _content_type = storage.open_bytes(key=file_record.storage_key)
            except FileNotFoundError:
                upload_bytes = None
            except StorageError:
                upload_bytes = None
        if upload_bytes is None and file_record.content_text is not None:
            upload_bytes = file_record.content_text.encode('latin1')

        if upload_bytes is None:
            file_record.status = BulkAIUploadFileStatus.FAILED.value
            file_record.error_message = 'Queued upload file missing'
            file_record.completed_at = datetime.utcnow()
            job.failed_items += 1
            db.commit()
            continue

        pdf_name = file_record.original_filename or bulk.filename or 'upload.pdf'
        pdf_data = upload_bytes

        try:
            print(f"[job-worker] start file job={job.id} file={pdf_name}", flush=True)
            text = extract_text_from_pdf(pdf_data)
            title, description = extract_title_from_text(text, pdf_name)
            print(f"[job-worker] extracted title job={job.id} file={pdf_name} title={title!r} text_len={len(text or '')}", flush=True)

            deck = None
            if file_record.created_deck_id:
                deck = db.get(Deck, file_record.created_deck_id)
            if deck is None and bulk.deck_id:
                deck = db.get(Deck, bulk.deck_id)
            if deck is None:
                raise AIGenerationError('Missing pre-created deck for uploaded file.')
            if title:
                deck.name = title[:255]
                deck.normalized_name = normalize_deck_name(deck.name)
            if folder_id is not None:
                deck.folder_id = folder_id
            if description:
                deck.description = (description or '')[:5000] or None

            _clear_deck_generated_content(db, deck.id)

            file_record.created_deck_id = deck.id
            file_record.extracted_title = title or deck.name
            file_record.extracted_description = description
            file_record.content_text = text[:50000] if text else None
            db.commit()

            flashcards_generated = 0
            mcqs_generated = 0
            duplicate_count = 0

            chunks = _split_text_for_ai_upload(text)
            if not chunks:
                raise AIGenerationError('No usable study text found in uploaded file.')

            provider_client = get_study_pack_provider(credential.provider)
            aggregate = merge_study_packs()
            modes = ('core', 'mechanisms', 'traps')
            existing_flashcards: set[tuple[str, str]] = set()
            existing_mcqs: set[str] = set()

            print(f"[job-worker] generation start job={job.id} file={pdf_name} chunks={len(chunks)} provider={credential.provider}", flush=True)
            for chunk_index, chunk in enumerate(chunks, start=1):
                print(f"[job-worker] chunk start job={job.id} file={pdf_name} chunk={chunk_index}/{len(chunks)} chunk_len={len(chunk)}", flush=True)
                chunk_pack = merge_study_packs()
                for mode in modes:
                    print(f"[job-worker] ai pass start job={job.id} file={pdf_name} chunk={chunk_index}/{len(chunks)} mode={mode}", flush=True)
                    prompt = build_iterative_study_pack_prompt(
                        chunk,
                        mode=mode,
                        existing_flashcards=[item.front for item in merge_study_packs(aggregate, chunk_pack).flashcards],
                        existing_mcqs=[item.question for item in merge_study_packs(aggregate, chunk_pack).mcqs],
                        max_flashcards=18,
                        max_mcqs=18,
                    )
                    try:
                        pass_pack = provider_client.generate_from_prompt(prompt, credential)
                        print(f"[job-worker] ai pass ok job={job.id} file={pdf_name} chunk={chunk_index}/{len(chunks)} mode={mode} flashcards={len(pass_pack.flashcards)} mcqs={len(pass_pack.mcqs)}", flush=True)
                    except AIGenerationError as e:
                        print(f"[job-worker] ai pass failed job={job.id} file={pdf_name} chunk={chunk_index}/{len(chunks)} mode={mode} err={str(e)[:200]}", flush=True)
                        continue
                    chunk_pack = merge_study_packs(chunk_pack, pass_pack)

                new_cards: list[Card] = []
                for item in chunk_pack.flashcards:
                    key = (normalize_generated_text(item.front), normalize_generated_text(item.back))
                    if key in existing_flashcards:
                        duplicate_count += 1
                        continue
                    existing_flashcards.add(key)
                    flashcards_generated += 1
                    new_cards.append(Card(deck_id=deck.id, front=item.front, back=item.back, card_type='basic', source_label='bulk-ai-upload'))
                for item in chunk_pack.mcqs:
                    key = normalize_generated_text(item.question)
                    if key in existing_mcqs:
                        duplicate_count += 1
                        continue
                    existing_mcqs.add(key)
                    mcqs_generated += 1
                    new_cards.append(
                        Card(
                            deck_id=deck.id,
                            front=item.question,
                            back=item.explanation,
                            card_type='mcq',
                            mcq_options=item.options,
                            mcq_answer_index=item.answer_index,
                            source_label='bulk-ai-upload',
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

            if not flashcards_generated and not mcqs_generated:
                raise AIGenerationError('AI provider did not return usable flashcards or MCQs.')

            print(f"[job-worker] file completed job={job.id} file={pdf_name} flashcards={flashcards_generated} mcqs={mcqs_generated} duplicates={duplicate_count}", flush=True)
            file_record.status = BulkAIUploadFileStatus.COMPLETED.value
            file_record.flashcards_generated = flashcards_generated
            file_record.mcqs_generated = mcqs_generated
            file_record.duplicate_count = duplicate_count
            file_record.completed_at = datetime.utcnow()
            bulk.flashcards_generated += flashcards_generated
            bulk.mcqs_generated += mcqs_generated
            job.processed_items += 1
        except Exception as e:
            print(f"[job-worker] file failed job={job.id} file={pdf_name} err={str(e)[:300]}", flush=True)
            file_record.status = BulkAIUploadFileStatus.FAILED.value
            file_record.error_message = str(e)[:500]
            file_record.completed_at = datetime.utcnow()
            job.failed_items += 1
        db.commit()

    bulk = db.get(BulkAIUpload, job.reference_id)
    if bulk:
        bulk.processed_files = job.processed_items
        bulk.failed_files = job.failed_items
        bulk.status = BulkAIUploadStatus.FAILED.value if job.failed_items > 0 and job.processed_items == 0 else BulkAIUploadStatus.COMPLETED.value
        bulk.completed_at = datetime.utcnow()

    print(f"[job-worker] job finalize job={job.id} processed={job.processed_items} failed={job.failed_items}", flush=True)
    job.status = JobStatus.FAILED.value if job.failed_items > 0 and job.processed_items == 0 else JobStatus.COMPLETED.value
    job.completed_at = datetime.utcnow()
    db.commit()

    for file_record in file_records:
        if file_record.storage_key and storage is not None:
            try:
                storage.delete_prefix(prefix=file_record.storage_key)
            except Exception:
                pass


def _mark_job_active(job_id: uuid.UUID) -> bool:
    with _active_jobs_lock:
        if job_id in _active_jobs:
            return False
        _active_jobs.add(job_id)
        return True



def _mark_job_inactive(job_id: uuid.UUID) -> None:
    with _active_jobs_lock:
        _active_jobs.discard(job_id)



def process_job(job_id: uuid.UUID) -> None:
    """Process a single job."""
    if not _mark_job_active(job_id):
        return

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if not job:
            return

        if job.status == JobStatus.RUNNING.value:
            if job.worker_id != WORKER_ID:
                return
            job.locked_at = datetime.utcnow()
            if not job.started_at:
                job.started_at = datetime.utcnow()
            db.commit()
        elif job.status in {JobStatus.PENDING.value, JobStatus.FAILED.value}:
            job.worker_id = WORKER_ID
            job.locked_at = datetime.utcnow()
            job.status = JobStatus.RUNNING.value
            job.started_at = datetime.utcnow()
            db.commit()
        else:
            return

        if job.job_type == "bulk_ai_upload":
            process_bulk_ai_upload(db, job)
        else:
            job.status = JobStatus.FAILED.value
            job.error_message = f"Unknown job type: {job.job_type}"
            job.completed_at = datetime.utcnow()
            db.commit()

    except Exception as e:
        print(f"Error processing job {job_id}: {e}", file=sys.stderr)
        try:
            job = db.get(Job, job_id)
            if job:
                job.status = JobStatus.FAILED.value
                job.error_message = str(e)[:500]
                job.completed_at = datetime.utcnow()
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
        _mark_job_inactive(job_id)


def claim_available_jobs(db: Session, capacity: int) -> list[Job]:
    """Claim pending jobs and recover abandoned running jobs whose lease expired."""
    if capacity <= 0:
        return []

    now = datetime.utcnow()
    stale_before = now - timedelta(seconds=JOB_LEASE_SECONDS)

    running_jobs = db.execute(
        select(Job)
        .where(Job.status == JobStatus.RUNNING.value)
        .where((Job.worker_id == WORKER_ID) | (Job.worker_id == None) | (Job.locked_at == None) | (Job.locked_at < stale_before))
        .order_by(Job.created_at.asc())
        .limit(capacity)
    ).scalars().all()

    claimed: list[Job] = []
    claimed_ids: set[uuid.UUID] = set()
    for job in running_jobs:
        job.worker_id = WORKER_ID
        job.locked_at = now
        if not job.started_at:
            job.started_at = now
        claimed.append(job)
        claimed_ids.add(job.id)

    remaining = max(0, capacity - len(claimed))
    if remaining > 0:
        pending_jobs = db.execute(
            select(Job)
            .where(Job.status == JobStatus.PENDING.value)
            .where((Job.worker_id == None) | (Job.locked_at == None) | (Job.locked_at < stale_before))
            .order_by(Job.created_at.asc())
            .limit(remaining)
        ).scalars().all()
        for job in pending_jobs:
            if job.id in claimed_ids:
                continue
            job.worker_id = WORKER_ID
            job.locked_at = now
            job.status = JobStatus.RUNNING.value
            if not job.started_at:
                job.started_at = now
            claimed.append(job)
            claimed_ids.add(job.id)

    db.commit()
    return claimed


def run_worker_loop():
    """Main worker loop."""
    print(f"Job worker {WORKER_ID} starting with {MAX_WORKERS} threads...")

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    while not _shutdown:
        db = SessionLocal()
        try:
            with _active_jobs_lock:
                active_count = len(_active_jobs)
            capacity = max(0, MAX_WORKERS - active_count)
            available_jobs = claim_available_jobs(db, capacity)
            if available_jobs:
                print(f"Claimed {len(available_jobs)} jobs", flush=True)
                for job in available_jobs:
                    if not _shutdown:
                        executor.submit(process_job, job.id)
        except Exception as e:
            print(f"Error claiming jobs: {e}", file=sys.stderr)
        finally:
            db.close()

        if not _shutdown:
            time.sleep(POLL_INTERVAL)

    executor.shutdown(wait=True)
    print(f"Worker {WORKER_ID} stopped")


def signal_handler(signum, frame):
    global _shutdown
    print(f"Received signal {signum}, shutting down...")
    _shutdown = True


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    run_worker_loop()