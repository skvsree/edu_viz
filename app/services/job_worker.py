"""
Job worker service for processing background jobs.
Run as: python -m app.services.job_worker
"""

from __future__ import annotations

import io
import logging
import os
import re
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
from app.models import (
    BulkAIUpload,
    BulkAIUploadChildFile,
    BulkAIUploadFile,
    BulkAIUploadFileStatus,
    BulkAIUploadRevisionNote,
    BulkAIUploadStatus,
    BulkRevisionNoteStatus,
    Card,
    CardState,
    ConceptMap,
    ConceptMapStatus,
    Deck,
    Job,
    JobStatus,
    Review,
    User,
)
from app.services.access import normalize_deck_name
from app.services.ai_auth import get_env_ai_provider_name, get_scope_provider, resolve_ai_credential
from app.services.ai_generation import (
    AIGenerationError,
    build_iterative_study_pack_prompt,
    build_title_generation_prompt,
    get_study_pack_provider,
    merge_study_packs,
    normalize_generated_text,
    opencode_session_id,
    parse_title_generation_json,
)
from app.services.storage import get_storage, StorageError
from app.services.concept_map import generate_concept_map


WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"
MAX_WORKERS = int(os.environ.get("JOB_WORKER_THREADS", "1"))
POLL_INTERVAL = int(os.environ.get("JOB_POLL_INTERVAL", "5"))
JOB_LEASE_SECONDS = int(os.environ.get("JOB_LEASE_SECONDS", "60"))
MAX_529_RETRIES = int(os.environ.get("JOB_MAX_529_RETRIES", "5"))
MAX_AI_FORMAT_RETRIES = int(os.environ.get("JOB_MAX_AI_FORMAT_RETRIES", "3"))
MAX_TRANSIENT_RETRIES = 4
# A hung request costs its whole read timeout, so timeouts get fewer
# attempts than an instantly-rejected 503 (a 20-minute pass blocks the run).
MAX_TIMEOUT_RETRIES = 2

AI_FORMAT_RETRY_FAILURE_MESSAGE = (
    f"AI returned invalid structured output after {MAX_AI_FORMAT_RETRIES} attempts."
)
logger = logging.getLogger(__name__)
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


def _clean_title_line(line: str) -> str:
    return re.sub(r"\s+", " ", (line or "").strip())


def _looks_like_noise(line: str) -> bool:
    if not line:
        return True
    lowered = line.lower()
    if len(line) < 4:
        return True
    if line.isdigit():
        return True
    noise_prefixes = (
        "unit ",
        "page ",
        "www.",
        "http://",
        "https://",
    )
    if lowered.startswith(noise_prefixes):
        return True
    if re.fullmatch(r"[\d\s\-–—.:]+", line):
        return True
    return False


def _latest_bulk_attempt_rows(db: Session, bulk_upload_id: uuid.UUID) -> list[BulkAIUploadFile]:
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


_SECTION_LABELS = ("chapter", "lesson")


def _format_section_number(raw: str) -> str:
    """Return the section number padded for natural sort.

    Roman numerals are uppercased ("II" stays "II"). Pure digits are
    zero-padded to at least two digits so `Lesson 02` sorts before
    `Lesson 10` in listings. If the number can't be safely parsed as a
    positive integer (e.g. mixed text), the original string is returned
    unchanged.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return candidate
    if re.fullmatch(r"[ivxlcdm]+", candidate, re.IGNORECASE):
        return candidate.upper()
    if candidate.isdigit():
        return candidate.zfill(2)
    try:
        value = int(candidate)
    except ValueError:
        return candidate
    return str(value).zfill(2)


def _match_section_line(line: str) -> tuple[str, str, str] | None:
    """Detect "Chapter N - Title" / "Lesson N - Title" style lines.

    Returns ``(label, formatted_number, rest)`` or ``None`` when the line
    is not a recognisable section header. `label` is lowercased,
    `formatted_number` is zero-padded for natural sort.
    """
    for label in _SECTION_LABELS:
        pattern = (
            rf"^{label}\s+([0-9]+|[ivxlcdm]+)\b"
            rf"(?:\s*[:\-–—.]\s*|\s+)(.+)$"
        )
        match = re.match(pattern, line, re.IGNORECASE)
        if match:
            return label, _format_section_number(match.group(1)), _clean_title_line(match.group(2))
        pattern_bare = rf"^{label}\s+([0-9]+|[ivxlcdm]+)\b$"
        match = re.match(pattern_bare, line, re.IGNORECASE)
        if match:
            return label, _format_section_number(match.group(1)), ""
    return None


def _derive_best_title(lines: list[str], filename: str) -> str:
    cleaned = [_clean_title_line(line) for line in lines[:40]]
    candidates = [line for line in cleaned if not _looks_like_noise(line)]
    if not candidates:
        return Path(filename).stem[:250]

    matched_label = None
    matched_number = None
    title_part = None
    matched_line = None
    # Try structured "Chapter N - Title" / "Lesson N - Title" first so
    # we keep the section number for natural sort.
    for line in candidates[:20]:
        match = _match_section_line(line)
        if match:
            label, number, _title_part_candidate = match
            matched_label = label
            matched_number = number
            matched_line = line
            if _title_part_candidate:
                title_part = _title_part_candidate
                break
            continue
        if matched_label and matched_number and not title_part and line.lower() != matched_line.lower():
            title_part = line
            break

    book_title = None
    for line in candidates[:12]:
        lowered = line.lower()
        if any(lowered.startswith(f"{label} ") for label in _SECTION_LABELS):
            continue
        book_title = line
        break

    if matched_label and matched_number:
        full_title = title_part or book_title or Path(filename).stem
        return f"{matched_label.capitalize()} {matched_number} - {full_title}"[:250]

    return (book_title or Path(filename).stem)[:250]


# Backwards-compatible alias used by older tests/callers.
def _derive_best_title_legacy_chapter_check(lines, filename):  # pragma: no cover - legacy shim
    return _derive_best_title(lines, filename)


def extract_title_from_text(text: str, filename: str) -> tuple[str, str | None]:
    lines = text.strip().split("\n") if text else []
    title = _derive_best_title(lines, filename)
    description = None
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


def _is_retryable_529_error(exc: Exception) -> bool:
    message = str(exc or "")
    return bool(re.search(r"\b529\b", message) and "overloaded" in message.lower())


def _is_retryable_ai_format_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    return any(
        phrase in message
        for phrase in (
            "invalid json",
            "empty response",
            "empty content",
            "returned no choices",
            "invalid choice payload",
            "did not return usable flashcards or mcqs",
        )
    )


def _ai_format_retry_delay(attempt: int) -> int:
    return min(30, 2 ** attempt)


_pass_semaphore = None
_pass_semaphore_lock = threading.Lock()


def _get_pass_semaphore():
    """Process-wide cap on AI passes in flight.

    Three extraction modes per chunk times two job threads meant up to six long
    generations at once, and the endpoint answered 503 / read-timeouts for all
    of them while a single request of the same size succeeded in ~30s.
    """
    global _pass_semaphore
    if _pass_semaphore is None:
        with _pass_semaphore_lock:
            if _pass_semaphore is None:
                from app.core.config import settings

                limit = max(1, int(getattr(settings, "ai_pass_concurrency", 2) or 1))
                _pass_semaphore = threading.BoundedSemaphore(limit)
                print(
                    f"[job-worker] AI pass concurrency limit = {limit}",
                    flush=True,
                )
    return _pass_semaphore


def _is_timeout_error(exc: Exception) -> bool:
    """True for read/connect timeouts, the slowest class of failure."""
    lowered = str(exc or "").lower()
    return "timed out" in lowered or "timeout" in lowered


def _is_retryable_transient_error(exc: Exception) -> bool:
    """True for provider hiccups that a retry usually clears.

    Observed in production: OpenCode returning HTTP 503 under load, and
    requests read-timeouts at 180s while three extraction modes run per chunk.
    Neither was retried, so the affected mode contributed nothing and a file
    finished "completed" with a fraction of its cards.
    """
    message = str(exc or "")
    lowered = message.lower()
    for status in ("429", "500", "502", "503", "504"):
        if f"error ({status})" in message:
            return True
    for phrase in (
        "read timed out",
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
        "connection error",
        "temporarily unavailable",
        "service unavailable",
        "too many requests",
        "rate limit",
    ):
        if phrase in lowered:
            return True
    return False


def _transient_retry_delay(attempt: int) -> int:
    return min(45, 5 * attempt)


def _classify_pass_failure(exc: Exception) -> str:
    """Bucket a failed AI pass so the run summary names the real cause."""
    message = str(exc or "")
    lowered = message.lower()
    if "read timed out" in lowered or "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    for status in ("429", "500", "502", "503", "504"):
        if f"error ({status})" in message:
            return f"HTTP {status}"
    if _is_retryable_529_error(exc):
        return "HTTP 529"
    if _is_retryable_ai_format_error(exc):
        return "unparseable response"
    return "other error"


def _describe_pass_failures(reasons) -> str:
    """Short human summary of why AI passes failed, most common first."""
    if not reasons:
        return "unknown errors"
    ordered = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{reason} x{count}" for reason, count in ordered[:3])


def _generate_text_with_retry(
    provider_client, prompt: str, credential, *, log_prefix: str
) -> str:
    attempt = 0
    while True:
        attempt += 1
        try:
            with _get_pass_semaphore():
                return provider_client.generate_text(prompt, credential)
        except Exception as exc:
            retry_limit = (
                MAX_TIMEOUT_RETRIES if _is_timeout_error(exc) else MAX_TRANSIENT_RETRIES
            )
            if _is_retryable_transient_error(exc) and attempt < retry_limit:
                sleep_seconds = _transient_retry_delay(attempt)
                print(
                    f"{log_prefix} retryable_transient attempt={attempt} "
                    f"sleep={sleep_seconds}s err={str(exc)[:200]}",
                    flush=True,
                )
                time.sleep(sleep_seconds)
                continue
            if _is_retryable_transient_error(exc):
                raise AIGenerationError(
                    f"AI provider unavailable after {attempt} attempts: {str(exc)[:200]}"
                ) from exc
            if _is_retryable_529_error(exc) and attempt < MAX_529_RETRIES:
                sleep_seconds = min(60, 5 * attempt)
                print(
                    f"{log_prefix} retryable_529 attempt={attempt} sleep={sleep_seconds}s err={str(exc)[:200]}",
                    flush=True,
                )
                time.sleep(sleep_seconds)
                continue
            if _is_retryable_529_error(exc):
                raise AIGenerationError(
                    f"AI provider overloaded after {attempt} attempts; please retry later."
                ) from exc
            if _is_retryable_ai_format_error(exc) and attempt < MAX_AI_FORMAT_RETRIES:
                sleep_seconds = _ai_format_retry_delay(attempt)
                print(
                    f"{log_prefix} retryable_ai_format attempt={attempt} sleep={sleep_seconds}s err={str(exc)[:200]}",
                    flush=True,
                )
                time.sleep(sleep_seconds)
                continue
            if _is_retryable_ai_format_error(exc):
                raise AIGenerationError(AI_FORMAT_RETRY_FAILURE_MESSAGE) from exc
            raise


def _generate_pack_with_retry(
    provider_client, prompt: str, credential, *, log_prefix: str
):
    attempt = 0
    while True:
        attempt += 1
        try:
            with _get_pass_semaphore():
                return provider_client.generate_from_prompt(prompt, credential)
        except Exception as exc:
            retry_limit = (
                MAX_TIMEOUT_RETRIES if _is_timeout_error(exc) else MAX_TRANSIENT_RETRIES
            )
            if _is_retryable_transient_error(exc) and attempt < retry_limit:
                sleep_seconds = _transient_retry_delay(attempt)
                print(
                    f"{log_prefix} retryable_transient attempt={attempt} "
                    f"sleep={sleep_seconds}s err={str(exc)[:200]}",
                    flush=True,
                )
                time.sleep(sleep_seconds)
                continue
            if _is_retryable_transient_error(exc):
                raise AIGenerationError(
                    f"AI provider unavailable after {attempt} attempts: {str(exc)[:200]}"
                ) from exc
            if _is_retryable_529_error(exc) and attempt < MAX_529_RETRIES:
                sleep_seconds = min(60, 5 * attempt)
                print(
                    f"{log_prefix} retryable_529 attempt={attempt} sleep={sleep_seconds}s err={str(exc)[:200]}",
                    flush=True,
                )
                time.sleep(sleep_seconds)
                continue
            if _is_retryable_529_error(exc):
                raise AIGenerationError(
                    f"AI provider overloaded after {attempt} attempts; please retry later."
                ) from exc
            if _is_retryable_ai_format_error(exc) and attempt < MAX_AI_FORMAT_RETRIES:
                sleep_seconds = _ai_format_retry_delay(attempt)
                print(
                    f"{log_prefix} retryable_ai_format attempt={attempt} sleep={sleep_seconds}s err={str(exc)[:200]}",
                    flush=True,
                )
                time.sleep(sleep_seconds)
                continue
            if _is_retryable_ai_format_error(exc):
                raise AIGenerationError(AI_FORMAT_RETRY_FAILURE_MESSAGE) from exc
            raise


def _resolve_ai_provider_and_credential(
    db: Session, user: User
):
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


def _run_chunk_modes_parallel(
    *,
    provider,
    credential,
    chunk_text: str,
    aggregate,
    modes: tuple[str, ...],
    max_flashcards: int,
    max_mcqs: int,
    log_prefix: str,
    executor: ThreadPoolExecutor,
    failure_log: dict | None = None,
) -> tuple:
    """Run the 3 extraction modes (core/mechanisms/traps) for a single
    chunk in parallel via the supplied executor.

    The 3 modes are independent — they ask the model the same source
    text with different "lens" instructions — so they can run
    concurrently. Wall time per chunk drops from ~3x single-call
    latency to ~1x.

    Returns (merged_chunk_pack, failed_modes_set). The caller is
    responsible for de-duplicating merged_chunk_pack against
    `aggregate` and earlier chunks via the existing dedup loop.

    The "already covered" list in each prompt contains only
    `aggregate` items, NOT in-flight results from sibling modes.
    Cross-mode duplicates are caught by the caller's dedup pass
    (existing_flashcards / existing_mcqs sets).
    """

    def _run_mode(mode: str):
        """Worker for a single mode submission. Returns (mode, pack_or_None, err)."""
        try:
            print(
                f"{log_prefix} ai pass start mode={mode}",
                flush=True,
            )
            prompt = build_iterative_study_pack_prompt(
                chunk_text,
                mode=mode,
                # Trim to 15 most-recent items to keep the prompt
                # small. The prompt builder slices to 15 anyway, so
                # we avoid materialising full lists in Python first.
                existing_flashcards=[
                    item.front for item in aggregate.flashcards[-15:]
                ],
                existing_mcqs=[
                    item.question for item in aggregate.mcqs[-15:]
                ],
                max_flashcards=max_flashcards,
                max_mcqs=max_mcqs,
            )
            pass_pack = _generate_pack_with_retry(
                provider,
                prompt,
                credential,
                log_prefix=f"{log_prefix} mode={mode}",
            )
            print(
                f"{log_prefix} ai pass ok mode={mode} "
                f"flashcards={len(pass_pack.flashcards)} mcqs={len(pass_pack.mcqs)}",
                flush=True,
            )
            return (mode, pass_pack, None)
        except AIGenerationError as e:
            print(
                f"{log_prefix} ai pass failed mode={mode} err={str(e)[:200]}",
                flush=True,
            )
            if failure_log is not None:
                failure_log[mode] = _classify_pass_failure(e)
            return (mode, None, e)
        except Exception as e:
            # Catch-all: a single broken mode must not abort the whole chunk.
            print(
                f"{log_prefix} ai pass crashed mode={mode} err={str(e)[:200]}",
                flush=True,
            )
            if failure_log is not None:
                failure_log[mode] = _classify_pass_failure(e)
            return (mode, None, e)

    failed_modes: set[str] = set()
    packs_to_merge: list = []
    futures = [executor.submit(_run_mode, mode) for mode in modes]
    for fut in futures:
        mode, pack, err = fut.result()
        if err is not None:
            failed_modes.add(mode)
            continue
        if pack is not None:
            packs_to_merge.append(pack)

    chunk_pack = merge_study_packs(*packs_to_merge) if packs_to_merge else merge_study_packs()
    return chunk_pack, failed_modes


def record_chunk_progress(
    *,
    db: Session,
    file_record: BulkAIUploadFile,
    bulk: BulkAIUpload,
    chunk_flashcards: int,
    chunk_mcqs: int,
) -> None:
    """Update file + bulk card counters after a chunk of AI generation.

    Called once per chunk so live UI consumers (SSE / polling) see
    running totals instead of waiting for the whole file to finish.
    The bulk row uses a delta (new - previous) so repeated calls for
    the same file accumulate correctly even after a mid-file crash.
    """
    new_file_flashcards = (file_record.flashcards_generated or 0) + chunk_flashcards
    new_file_mcqs = (file_record.mcqs_generated or 0) + chunk_mcqs
    file_record.flashcards_generated = new_file_flashcards
    file_record.mcqs_generated = new_file_mcqs
    bulk.flashcards_generated = max(0, (bulk.flashcards_generated or 0) + chunk_flashcards)
    bulk.mcqs_generated = max(0, (bulk.mcqs_generated or 0) + chunk_mcqs)
    db.commit()


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

    file_query = (
        select(BulkAIUploadFile)
        .where(BulkAIUploadFile.bulk_upload_id == bulk.id)
        # Always restrict to pending rows regardless of job.total_items.
        # Previously the .where() below was only applied when total_items
        # was 1, which meant multi-file bulks would pick up rows in any
        # status (including completed/stopped) and re-process them,
        # wasting AI tokens. See test_job_worker_file_filter.py for
        # the regression test.
        .where(BulkAIUploadFile.status == BulkAIUploadFileStatus.PENDING.value)
        .order_by(BulkAIUploadFile.created_at)
    )
    file_records = db.execute(file_query).scalars().all()
    if not file_records:
        bulk.status = BulkAIUploadStatus.FAILED.value
        bulk.error_message = "Missing queued upload files"
        job.status = JobStatus.FAILED.value
        job.error_message = "Missing queued upload files"
        db.commit()
        return

    owner = (
        db.get(User, bulk.user_id)
        if bulk.user_id
        else (db.get(User, bulk.deck.user_id) if getattr(bulk, "deck", None) else None)
    )
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
    provider_client = get_study_pack_provider(
        credential.provider,
        session_id=opencode_session_id(f"job-{job.id}"),
    )
    bulk.provider = credential.provider or provider_name
    db.commit()

    storage = get_storage()
    bulk.total_files = len(file_records)
    job.total_items = len(file_records)
    db.commit()

    hard_fail_job = False
    hard_fail_message = None

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

        db.refresh(file_record)
        if file_record.child_file_id:
            child_file = db.get(BulkAIUploadChildFile, file_record.child_file_id)
            if child_file and child_file.latest_attempt_id and child_file.latest_attempt_id != file_record.id:
                continue
        if (file_record.status or '').lower() == BulkAIUploadFileStatus.STOPPED.value:
            job.failed_items += 1
            db.commit()
            continue

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
            upload_bytes = file_record.content_text.encode('utf-8')

        if upload_bytes is None:
            file_record.status = BulkAIUploadFileStatus.FAILED.value
            file_record.error_message = 'Queued upload file missing'
            file_record.completed_at = datetime.utcnow()
            job.failed_items += 1
            db.commit()
            continue

        pdf_name = file_record.original_filename or bulk.filename or 'upload.pdf'
        pdf_data = upload_bytes
        mode_executor = None  # assigned inside the try block

        try:
            print(f"[job-worker] start file job={job.id} file={pdf_name}", flush=True)
            text = extract_text_from_pdf(pdf_data)
            title = None
            description = None
            try:
                archive_filename = bulk.filename if bulk else None
                title_prompt = build_title_generation_prompt(
                    text,
                    pdf_name,
                    archive_filename=archive_filename,
                )
                raw_title_response = _generate_text_with_retry(
                    provider_client,
                    title_prompt,
                    credential,
                    log_prefix=f"[job-worker] ai title job={job.id} file={pdf_name}",
                )
                ai_title, ai_description = parse_title_generation_json(raw_title_response)
                if ai_title:
                    title = ai_title[:250]
                if ai_description:
                    description = ai_description[:5000]
                print(
                    f"[job-worker] ai title ok job={job.id} file={pdf_name} title={title!r}",
                    flush=True,
                )
            except Exception as title_exc:
                print(
                    f"[job-worker] ai title failed job={job.id} file={pdf_name} err={str(title_exc)[:200]}",
                    flush=True,
                )

            if not title:
                fallback_title, fallback_description = extract_title_from_text(text, pdf_name)
                title = fallback_title[:250] if fallback_title else None
                if fallback_description and not description:
                    description = fallback_description[:5000]
                print(
                    f"[job-worker] title fallback job={job.id} file={pdf_name} title={title!r}",
                    flush=True,
                )

            if not title:
                raise AIGenerationError('Unable to derive a usable title from document content.')
            print(
                f"[job-worker] extracted title job={job.id} file={pdf_name} "
                f"title={title!r} text_len={len(text or '')}",
                flush=True,
            )

            deck = None
            if file_record.created_deck_id:
                deck = db.get(Deck, file_record.created_deck_id)
            if deck is None:
                raise AIGenerationError('Missing per-file deck for uploaded file.')
            if title:
                candidate_title = title[:255]
                candidate_normalized = normalize_deck_name(candidate_title)
                existing_deck = db.execute(
                    select(Deck.id)
                    .where(Deck.user_id == owner.id)
                    .where(Deck.normalized_name == candidate_normalized)
                    .where(Deck.is_deleted.is_(False))
                    .where(Deck.id != deck.id)
                    .limit(1)
                ).scalar_one_or_none()
                if existing_deck is None:
                    deck.name = candidate_title
                    deck.normalized_name = candidate_normalized
            if folder_id is not None:
                deck.folder_id = folder_id
            if description:
                deck.description = (description or '')[:5000] or None

            _clear_deck_generated_content(db, deck.id)

            resolved_title = (title or deck.name or file_record.original_filename or '').strip()[:255] or None
            file_record.created_deck_id = deck.id
            file_record.extracted_title = resolved_title
            file_record.extracted_description = description
            file_record.content_text = text[:50000] if text else None
            if file_record.child_file and resolved_title:
                file_record.child_file.display_title = resolved_title
            db.commit()

            flashcards_generated = 0
            mcqs_generated = 0
            duplicate_count = 0

            chunks = _split_text_for_ai_upload(text)
            if not chunks:
                raise AIGenerationError('No usable study text found in uploaded file.')

            aggregate = merge_study_packs()
            modes = ('core', 'mechanisms', 'traps')
            existing_flashcards: set[tuple[str, str]] = set()
            existing_mcqs: set[str] = set()

            # Per-file mode executor: 3 workers so the 3 extraction
            # modes within each chunk can run concurrently. Created
            # per-file (not per-chunk) so the worker pool overhead is
            # amortized across all chunks in a file. Closed at the
            # end of the per-file block via the with-statement below.
            mode_executor = ThreadPoolExecutor(
                max_workers=len(modes),
                thread_name_prefix=f"modes-{pdf_name[:20]}",
            )

            print(
                f"[job-worker] generation start job={job.id} file={pdf_name} "
                f"chunks={len(chunks)} provider={credential.provider}",
                flush=True,
            )
            failed_pass_total = 0
            failed_pass_reasons: dict[str, int] = {}
            for chunk_index, chunk in enumerate(chunks, start=1):
                db.refresh(file_record)
                bulk = db.get(BulkAIUpload, job.reference_id)
                if bulk and bulk.is_auto_stop:
                    raise AIGenerationError('Job stopped by user')
                if (file_record.status or '').lower() == BulkAIUploadFileStatus.STOPPED.value:
                    raise AIGenerationError('File canceled by user')
                print(
                    f"[job-worker] chunk start job={job.id} file={pdf_name} "
                    f"chunk={chunk_index}/{len(chunks)} chunk_len={len(chunk)}",
                    flush=True,
                )
                chunk_failure_log: dict = {}
                chunk_pack, failed_modes = _run_chunk_modes_parallel(
                    provider=provider_client,
                    credential=credential,
                    chunk_text=chunk,
                    aggregate=aggregate,
                    modes=modes,
                    max_flashcards=18,
                    max_mcqs=18,
                    log_prefix=(
                        f"[job-worker] ai pass job={job.id} file={pdf_name} "
                        f"chunk={chunk_index}/{len(chunks)}"
                    ),
                    executor=mode_executor,
                    failure_log=chunk_failure_log,
                )

                if failed_modes:
                    failed_pass_total += len(failed_modes)
                    for reason in chunk_failure_log.values():
                        failed_pass_reasons[reason] = (
                            failed_pass_reasons.get(reason, 0) + 1
                        )
                    print(
                        f"[job-worker] chunk job={job.id} file={pdf_name} "
                        f"chunk={chunk_index}/{len(chunks)} partial_modes_failed="
                        f"{','.join(sorted(failed_modes))} (continuing with successful modes)",
                        flush=True,
                    )

                new_cards: list[Card] = []
                chunk_flashcard_count = 0
                chunk_mcq_count = 0
                for item in chunk_pack.flashcards:
                    key = (normalize_generated_text(item.front), normalize_generated_text(item.back))
                    if key in existing_flashcards:
                        duplicate_count += 1
                        continue
                    existing_flashcards.add(key)
                    flashcards_generated += 1
                    chunk_flashcard_count += 1
                    new_cards.append(
                        Card(
                            deck_id=deck.id,
                            front=item.front,
                            back=item.back,
                            card_type="basic",
                            source_label="bulk-ai-upload",
                        )
                    )
                for item in chunk_pack.mcqs:
                    key = normalize_generated_text(item.question)
                    if key in existing_mcqs:
                        duplicate_count += 1
                        continue
                    existing_mcqs.add(key)
                    mcqs_generated += 1
                    chunk_mcq_count += 1
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
                # Persist per-chunk progress so live UI consumers (SSE /
                # polling) see running totals instead of waiting for the
                # whole file to finish. record_chunk_progress commits the
                # counter update to the DB. The bulk row may have been
                # deleted by a concurrent admin action; in that case we
                # only update the file row so progress is not lost.
                if bulk is not None:
                    record_chunk_progress(
                        db=db,
                        file_record=file_record,
                        bulk=bulk,
                        chunk_flashcards=chunk_flashcard_count,
                        chunk_mcqs=chunk_mcq_count,
                    )
                else:
                    file_record.flashcards_generated = (
                        (file_record.flashcards_generated or 0) + chunk_flashcard_count
                    )
                    file_record.mcqs_generated = (
                        (file_record.mcqs_generated or 0) + chunk_mcq_count
                    )
                    db.commit()
                aggregate = merge_study_packs(aggregate, chunk_pack)

            if not flashcards_generated and not mcqs_generated:
                raise AIGenerationError('AI provider did not return usable flashcards or MCQs.')

            # mode_executor is shut down in the outer finally block
            # below so the per-file worker threads are joined whether
            # the file succeeded, failed, or raised mid-chunk.

            print(
                f"[job-worker] file completed job={job.id} file={pdf_name} "
                f"flashcards={flashcards_generated} mcqs={mcqs_generated} "
                f"duplicates={duplicate_count}",
                flush=True,
            )
            # bulk + file counters are already correct from per-chunk
            # A file whose every AI pass failed must not be reported as a
            # clean success - that is what hid a run that produced 10% of its
            # cards behind a "completed" status.
            total_passes = len(chunks) * len(modes)
            if failed_pass_total and not flashcards_generated and not mcqs_generated:
                raise AIGenerationError(
                    f"All {failed_pass_total} AI passes failed "
                    f"({_describe_pass_failures(failed_pass_reasons)}); "
                    f"no cards were generated. Retry this file."
                )

            # record_chunk_progress calls; just stamp final status and
            # the duplicate count.
            file_record.status = BulkAIUploadFileStatus.COMPLETED.value
            file_record.flashcards_generated = flashcards_generated
            file_record.mcqs_generated = mcqs_generated
            file_record.duplicate_count = duplicate_count
            file_record.completed_at = datetime.utcnow()
            if failed_pass_total:
                file_record.error_message = (
                    f"Partial generation: {failed_pass_total} of {total_passes} "
                    f"AI passes failed ({_describe_pass_failures(failed_pass_reasons)}). "
                    f"Retry this file to fill the gaps."
                )[:500]
                print(
                    f"[job-worker] file partial job={job.id} file={pdf_name} "
                    f"failed_passes={failed_pass_total}/{total_passes} "
                    f"reasons={_describe_pass_failures(failed_pass_reasons)}",
                    flush=True,
                )
            # Enqueue revision-notes PDF generation for this child file.
            # The note_row is a long-lived tracker; the revision-notes
            # job is a separate Job that produces the actual bytes.
            if file_record.child_file_id:
                _enqueue_revision_notes_job(
                    db,
                    bulk=bulk,
                    file_record=file_record,
                )
                # Generate a concept map for the deck this file belongs to.
                # The generator is fast (heuristic-only, no AI calls) and
                # runs inline. If a map already exists for this deck, the
                # generator marks it stale and re-creates it.
                if file_record.created_deck_id:
                    try:
                        generate_concept_map(
                            db,
                            deck_id=file_record.created_deck_id,
                            source_file_id=file_record.id,
                            title=file_record.extracted_title or file_record.original_filename.rsplit(".", 1)[0],
                        )
                    except Exception:
                        logger.exception(
                            "concept_map: inline generation failed for deck %s file %s",
                            file_record.created_deck_id,
                            file_record.id,
                        )
            if bulk is not None:
                # Safety net: if the per-chunk calls did not run (e.g. the
                # file had zero chunks because text was empty), make sure
                # the bulk reflects the final totals instead of zero.
                if bulk.flashcards_generated is None or bulk.flashcards_generated < flashcards_generated:
                    bulk.flashcards_generated = flashcards_generated
                if bulk.mcqs_generated is None or bulk.mcqs_generated < mcqs_generated:
                    bulk.mcqs_generated = mcqs_generated
            job.processed_items += 1
        except Exception as e:
            print(f"[job-worker] file failed job={job.id} file={pdf_name} err={str(e)[:300]}", flush=True)
            if str(e) == 'File canceled by user':
                file_record.status = BulkAIUploadFileStatus.STOPPED.value
            elif str(e) == 'Job stopped by user':
                file_record.status = BulkAIUploadFileStatus.STOPPED.value
            else:
                file_record.status = BulkAIUploadFileStatus.FAILED.value
            file_record.error_message = str(e)[:500]
            file_record.completed_at = datetime.utcnow()
            job.failed_items += 1
            if str(e) == AI_FORMAT_RETRY_FAILURE_MESSAGE:
                hard_fail_job = True
                hard_fail_message = (
                    f"AI returned invalid JSON after {MAX_AI_FORMAT_RETRIES} attempts. "
                    f"Stopped on file: {pdf_name}"
                )
        finally:
            # Always shut down the per-file mode executor so its 3
            # worker threads are joined — whether the file succeeded,
            # failed, or raised mid-chunk. Without this, a failed file
            # would leak 3 threads per failed file.
            if mode_executor is not None:
                mode_executor.shutdown(wait=True)
        db.commit()
        if hard_fail_job:
            break

    bulk = db.get(BulkAIUpload, job.reference_id)
    if bulk:
        all_rows = db.execute(
            select(BulkAIUploadFile)
            .where(BulkAIUploadFile.bulk_upload_id == bulk.id)
            .order_by(BulkAIUploadFile.created_at.asc())
        ).scalars().all()
        latest_rows: list[BulkAIUploadFile] = []
        for row in all_rows:
            if not row.child_file_id:
                latest_rows.append(row)
                continue
            child_row = db.get(BulkAIUploadChildFile, row.child_file_id)
            if not child_row or not child_row.latest_attempt_id:
                continue
            if child_row.latest_attempt_id == row.id:
                latest_rows.append(row)

        latest_statuses = [(row.status or '').lower() for row in latest_rows]
        bulk.processed_files = sum(
            1 for status in latest_statuses if status == BulkAIUploadFileStatus.COMPLETED.value
        )
        bulk.failed_files = sum(
            1
            for status in latest_statuses
            if status in {
                BulkAIUploadFileStatus.FAILED.value,
                BulkAIUploadFileStatus.STOPPED.value,
            }
        )
        if bulk.is_auto_stop:
            bulk.status = BulkAIUploadStatus.STOPPED.value
            bulk.error_message = 'Job stopped by user'
        elif hard_fail_job:
            bulk.status = BulkAIUploadStatus.FAILED.value
            bulk.error_message = hard_fail_message
        elif any(status == BulkAIUploadFileStatus.PROCESSING.value for status in latest_statuses):
            bulk.status = BulkAIUploadStatus.PROCESSING.value
            bulk.error_message = None
        elif any(status == BulkAIUploadFileStatus.PENDING.value for status in latest_statuses):
            bulk.status = BulkAIUploadStatus.PENDING.value
            bulk.error_message = None
        elif any(status == BulkAIUploadFileStatus.FAILED.value for status in latest_statuses):
            bulk.status = BulkAIUploadStatus.FAILED.value
            bulk.error_message = None
        elif any(status == BulkAIUploadFileStatus.STOPPED.value for status in latest_statuses):
            bulk.status = BulkAIUploadStatus.STOPPED.value
            bulk.error_message = 'Job stopped by user'
        else:
            bulk.status = BulkAIUploadStatus.COMPLETED.value
            bulk.error_message = None
        bulk.completed_at = datetime.utcnow()

    print(
        f"[job-worker] job finalize job={job.id} processed={job.processed_items} "
        f"failed={job.failed_items} hard_fail={hard_fail_job}",
        flush=True,
    )
    if bulk and bulk.is_auto_stop:
        job.status = JobStatus.FAILED.value
        job.error_message = 'Job stopped by user'
    elif hard_fail_job:
        job.status = JobStatus.FAILED.value
        job.error_message = hard_fail_message
    else:
        job.status = (
            JobStatus.FAILED.value
            if job.failed_items > 0 and job.processed_items == 0
            else JobStatus.COMPLETED.value
        )
    job.completed_at = datetime.utcnow()
    db.commit()

    # Keep original uploaded source files in object storage so failed jobs can be retried.


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
        elif job.job_type == "revision_notes":
            process_revision_notes(db, job)
        elif job.job_type == "concept_map":
            process_concept_map_gen(db, job)
        elif job.job_type == "concept_map_revision":
            process_concept_map_revision(db, job)
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
                if job.job_type == "bulk_ai_upload" and job.reference_id:
                    bulk = db.get(BulkAIUpload, job.reference_id)
                    if bulk:
                        bulk.status = BulkAIUploadStatus.FAILED.value
                        bulk.error_message = str(e)[:500]
                        bulk.completed_at = datetime.utcnow()
                        file_records = [
                            file_record
                            for file_record in _latest_bulk_attempt_rows(db, bulk.id)
                            if file_record.status == BulkAIUploadFileStatus.PROCESSING.value
                        ]
                        for file_record in file_records:
                            file_record.status = BulkAIUploadFileStatus.FAILED.value
                            file_record.error_message = str(e)[:500]
                            file_record.completed_at = datetime.utcnow()
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
        .where(
            (Job.worker_id == WORKER_ID)
            | (Job.worker_id.is_(None))
            | (Job.locked_at.is_(None))
            | (Job.locked_at < stale_before)
        )
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
            .where((Job.worker_id.is_(None)) | (Job.locked_at.is_(None)) | (Job.locked_at < stale_before))
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


# ---------------------------------------------------------------------------
# Revision-notes pipeline
# ---------------------------------------------------------------------------
def _enqueue_revision_notes_job(
    db: Session,
    *,
    bulk: BulkAIUpload,
    file_record: BulkAIUploadFile,
    section_titles: list[str] | None = None,
) -> None:
    """After a file successfully produces cards, queue a revision-notes PDF
    build for the underlying child file.

    Idempotent: at most one ready note per child. If a previous failed
    attempt exists, mark it stale and create a fresh row so the worker
    can safely rerun.

    ``section_titles`` is passed through to the worker so the rendered
    PDF uses the book's real sub-section titles instead of the chunked
    ``Section N`` fallback. Stored on the new ``BulkAIUploadRevisionNote``
    row so the worker can retrieve it independently.
    """
    if not file_record.child_file_id or not bulk.deck_id:
        return
    child_id = file_record.child_file_id

    existing = (
        db.query(BulkAIUploadRevisionNote)
        .filter(BulkAIUploadRevisionNote.child_file_id == child_id)
        .order_by(BulkAIUploadRevisionNote.created_at.desc())
        .first()
    )
    if existing and existing.status == BulkRevisionNoteStatus.READY.value:
        # Already have a usable PDF for this child - nothing to do.
        return
    if existing:
        # Mark stale so we don't accidentally overwrite a fresh run
        existing.status = BulkRevisionNoteStatus.FAILED.value
        existing.error_message = (
            (existing.error_message or "") + " | superseded by new run"
        )[:1000]
        db.flush()

    note = BulkAIUploadRevisionNote(
        bulk_upload_id=bulk.id,
        child_file_id=child_id,
        source_file_id=file_record.id,
        deck_id=bulk.deck_id,
        status=BulkRevisionNoteStatus.PENDING.value,
    )
    db.add(note)
    db.flush()

    # Persist section_titles on the note via the JSONB column added in
    # alembic 0028. Older rows that pre-date the column will simply get
    # None and fall back to the chunked heuristic for the chapter.
    if section_titles:
        # Set attribute even if column was added later - SQLAlchemy
        # will write it on flush. We set it before commit.
        try:
            setattr(note, "section_titles", section_titles)
        except Exception:  # pragma: no cover - column may not exist yet
            pass

    job = Job(
        job_type="revision_notes",
        reference_id=note.id,
        status=JobStatus.PENDING.value,
        total_items=1,
        processed_items=0,
    )
    db.add(job)
    db.commit()


def process_revision_notes(db: Session, job: Job) -> None:
    """Render a revision-notes PDF for one note row, persist + mark ready."""
    note = db.get(BulkAIUploadRevisionNote, job.reference_id)
    if not note:
        job.status = JobStatus.FAILED.value
        job.error_message = "Revision note not found"
        db.commit()
        return
    from app.services.revision_notes import (
        generate_revision_notes_for_child,
    )

    # Pull section_titles (set by callers when the deck has a known TOC)
    # so the renderer can label topics with real sub-section names
    # instead of the chunked-fallback "Section N" placeholders.
    section_titles = getattr(note, "section_titles", None)

    # Resolve AI credentials for DeepSeek-driven revision notes.
    # We always use the "deepseek" provider (deepseek-v4-pro) for
    # revision notes — it's explicitly configured for this task.
    # The API key from any resolved credential works with the
    # opencode endpoint that deepseek-v4-pro uses.
    provider_name: str | None = None
    credential = None
    try:
        from app.models import User
        deck = db.get(Deck, note.deck_id) if note.deck_id else None
        if deck and deck.user_id:
            owner = db.get(User, deck.user_id)
            if owner:
                _resolved_name, credential = _resolve_ai_provider_and_credential(
                    db, owner
                )
                # Force deepseek provider for revision notes
                provider_name = "deepseek"
    except Exception as exc:
        logger.warning(
            "process_revision_notes: credential resolution failed for note %s: %s",
            note.id,
            exc,
        )

    try:
        updated = generate_revision_notes_for_child(
            db,
            child_file_id=note.child_file_id,
            note=note,
            credential_provider_name=provider_name,
            credential=credential,
            section_titles=section_titles,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "process_revision_notes: failed note=%s", note.id
        )
        job.status = JobStatus.FAILED.value
        job.error_message = f"Render failed: {exc}"[:500]
        job.completed_at = datetime.utcnow()
        db.commit()
        return

    if updated.status == BulkRevisionNoteStatus.READY.value:
        job.processed_items = 1
        job.status = JobStatus.COMPLETED.value
        job.completed_at = datetime.utcnow()
    else:
        job.status = JobStatus.FAILED.value
        job.error_message = (updated.error_message or "render failed")[:500]
        job.completed_at = datetime.utcnow()
    db.commit()


def process_concept_map_gen(db: Session, job: Job) -> None:
    """Background job: generate/regenerate a concept map.

    Hard-regenerate: always overwrites any existing map (never skips).
    The job reference_id is the ConceptMap id (pre-created with
    status=pending by the web endpoint).
    """
    from app.services.concept_map import generate_concept_map as _gen

    concept_map_id = job.reference_id
    if not concept_map_id:
        job.status = JobStatus.FAILED.value
        job.error_message = "Missing concept map reference"
        db.commit()
        return

    concept_map = db.get(ConceptMap, concept_map_id)
    if not concept_map:
        job.status = JobStatus.FAILED.value
        job.error_message = "Concept map not found"
        db.commit()
        return

    # Find the latest completed source file for this deck
    source_file = (
        db.query(BulkAIUploadFile)
        .filter(
            BulkAIUploadFile.created_deck_id == concept_map.deck_id,
            BulkAIUploadFile.status == BulkAIUploadFileStatus.COMPLETED.value,
            BulkAIUploadFile.content_text.isnot(None),
            BulkAIUploadFile.content_text != "",
        )
        .order_by(BulkAIUploadFile.completed_at.desc())
        .first()
    )

    if not source_file:
        concept_map.status = ConceptMapStatus.FAILED.value
        concept_map.error_message = "No source text available"
        job.status = JobStatus.FAILED.value
        job.error_message = "No source text available for this deck"
        job.completed_at = datetime.utcnow()
        db.commit()
        return

    # Resolve AI credentials for key-point selection (same pattern as
    # process_concept_map_revision). Falls back to heuristic-only if
    # credential resolution fails.
    provider_name: str | None = None
    credential = None
    try:
        owner = None
        if concept_map.deck:
            owner = db.get(User, concept_map.deck.user_id) if concept_map.deck.user_id else None
        if owner:
            provider_name, credential = _resolve_ai_provider_and_credential(db, owner)
    except Exception:
        logger.warning(
            "concept_map: credential resolution failed for deck %s, using heuristic-only",
            concept_map.deck_id,
        )

    try:
        # _gen updates the existing row in-place (our UUID survives)
        result = _gen(
            db,
            deck_id=concept_map.deck_id,
            source_file_id=source_file.id,
            title=source_file.extracted_title or "",
            credential_provider_name=provider_name,
            credential=credential,
        )
        # _gen already committed and set the status; refresh to confirm
        db.refresh(result)
        if result.status == ConceptMapStatus.READY.value:
            job.processed_items = 1
            job.status = JobStatus.COMPLETED.value
        else:
            job.status = JobStatus.FAILED.value
            job.error_message = (result.error_message or "Generation failed")[:500]
    except Exception as exc:  # noqa: BLE001
        logger.exception("concept_map: generation failed for deck %s", concept_map.deck_id)
        concept_map.status = ConceptMapStatus.FAILED.value
        concept_map.error_message = f"Generation failed: {exc}"
        job.status = JobStatus.FAILED.value
        job.error_message = str(exc)[:500]
    job.completed_at = datetime.utcnow()
    db.commit()


def process_concept_map_revision(db: Session, job: Job) -> None:
    """Background job: AI-select keypoints + render concept map revision PDF.

    The job reference_id is the ConceptMap id.
    """
    from app.services.concept_map import generate_concept_map_revision_pdf
    from app.services.job_worker import _resolve_ai_provider_and_credential

    concept_map_id = job.reference_id
    if not concept_map_id:
        job.status = JobStatus.FAILED.value
        job.error_message = "Missing concept map reference"
        db.commit()
        return

    concept_map = db.get(ConceptMap, concept_map_id)
    if not concept_map:
        job.status = JobStatus.FAILED.value
        job.error_message = "Concept map not found"
        db.commit()
        return

    # Resolve AI credentials (use the deck owner)
    owner = None
    if concept_map.deck:
        owner = db.get(User, concept_map.deck.user_id) if concept_map.deck.user_id else None
    if not owner:
        job.status = JobStatus.FAILED.value
        job.error_message = "No deck owner found for concept map"
        db.commit()
        return

    try:
        provider_name, credential = _resolve_ai_provider_and_credential(db, owner)
    except Exception as exc:
        job.status = JobStatus.FAILED.value
        job.error_message = f"Failed to resolve AI credential: {exc}"[:500]
        db.commit()
        return

    try:
        generate_concept_map_revision_pdf(
            db,
            concept_map_id=concept_map_id,
            credential_provider_name=credential.provider,
            credential=credential,
        )
    except Exception as exc:
        job.status = JobStatus.FAILED.value
        job.error_message = f"Revision PDF generation failed: {exc}"[:500]
        job.completed_at = datetime.utcnow()
        db.commit()
        return

    # Refresh to check final status
    db.refresh(concept_map)
    if concept_map.revision_pdf_status == "ready":
        job.processed_items = 1
        job.status = JobStatus.COMPLETED.value
    else:
        job.status = JobStatus.FAILED.value
        job.error_message = concept_map.error_message or "Unknown error"[:500]
    job.completed_at = datetime.utcnow()
    db.commit()


if __name__ == "__main__":
    # Module-level logger (avoid NameError when this file is imported but
    # process_revision_notes is never called).
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    run_worker_loop()
