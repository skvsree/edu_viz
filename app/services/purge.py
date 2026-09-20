"""Permanent (hard) deletion of records that are only soft-deleted.

Deleting a deck only flips ``is_deleted`` so it can still be recovered. Once
an admin decides a record is never coming back, the helpers here remove it and
everything that references it for good, plus the stored objects it owns.

Ordering matters: several tables reference decks, cards, tests and attempts
*without* ``ON DELETE CASCADE`` (cards, tests, test_questions, test_attempts,
test_attempt_answers, reviews, card_states, deck_tags), so children are cleared
before their parents and the parent row always goes last. Tables that do
cascade (concept maps, deck accesses, favorites, MCQ/AI generations, bulk
uploads) are still deleted explicitly so the returned counts are complete and
the behaviour does not depend on the schema's cascade rules.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, inspect, select
from sqlalchemy.orm import Session

from app.models import (
    AIUploadGeneration,
    BulkAIUpload,
    BulkAIUploadChildFile,
    BulkAIUploadFile,
    BulkAIUploadFileStatus,
    BulkAIUploadRevisionNote,
    BulkAIUploadStatus,
    Card,
    CardState,
    ConceptMap,
    Deck,
    DeckAccess,
    DeckMcqGenerationItem,
    Job,
    JobStatus,
    MCQGeneration,
    Review,
    Test,
    TestAttempt,
    TestAttemptAnswer,
    TestQuestion,
    UserDeckFavorite,
    deck_tags,
)
from app.services.storage import deck_media_prefix, get_storage

logger = logging.getLogger(__name__)

# Object prefixes the purge owns. Uploaded source files live under
# ``bulk-ai-upload/<bulk id>/`` and generated revision PDFs under
# ``bulk_uploads/<bulk id>/``.
BULK_UPLOAD_OBJECT_PREFIX = "bulk-ai-upload/{bulk_id}/"
BULK_REVISION_NOTES_OBJECT_PREFIX = "bulk_uploads/{bulk_id}/"

ACTIVE_BULK_STATUSES = {
    BulkAIUploadStatus.PENDING.value,
    BulkAIUploadStatus.PROCESSING.value,
}
ACTIVE_FILE_STATUSES = {
    BulkAIUploadFileStatus.PENDING.value,
    BulkAIUploadFileStatus.PROCESSING.value,
}
ACTIVE_JOB_STATUSES = {JobStatus.PENDING.value, JobStatus.RUNNING.value}


class PurgeError(Exception):
    """Raised when a record may not (or cannot) be permanently deleted."""


def _deleted_rows(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def _table_present(db: Session, table_name: str) -> bool:
    """Whether ``table_name`` exists in this environment's schema.

    The schema is not identical everywhere: ``mcq_generations`` is defined as a
    model and used by the MCQ routes but no migration creates it, so it is
    missing on some deployments. Deleting from a missing table would abort the
    purge transaction, so the purge checks first and skips what is not there.
    """
    try:
        return bool(inspect(db.get_bind()).has_table(table_name))
    except Exception:  # noqa: BLE001 - no bind available: just attempt the delete
        return True


def _delete_storage_prefix(prefix: str) -> int:
    if not prefix:
        return 0
    try:
        return get_storage().delete_prefix(prefix=prefix)
    except Exception as exc:  # noqa: BLE001 - best effort cleanup
        logger.warning("purge: storage cleanup failed for prefix %s: %s", prefix, exc)
        return 0


def _active_job_ids(db: Session, reference_ids: list) -> list:
    if not reference_ids:
        return []
    return list(
        db.execute(
            select(Job.id)
            .where(Job.reference_id.in_(reference_ids))
            .where(Job.status.in_(list(ACTIVE_JOB_STATUSES)))
        )
        .scalars()
        .all()
    )


def purge_bulk_upload(db: Session, bulk: BulkAIUpload) -> dict[str, int]:
    """Permanently delete a bulk AI upload with its files and note rows.

    The decks the upload produced are left alone — they are real study
    content; only the upload machinery and its bookkeeping go away.
    """
    active_files = db.execute(
        select(BulkAIUploadFile.id)
        .where(BulkAIUploadFile.bulk_upload_id == bulk.id)
        .where(BulkAIUploadFile.status.in_(list(ACTIVE_FILE_STATUSES)))
    ).scalars().all()
    if active_files:
        raise PurgeError(
            "This upload still has files being processed. Stop it and wait for the "
            "current file to finish before deleting it."
        )
    if _active_job_ids(db, [bulk.id]):
        raise PurgeError("This upload has a running job. Wait for it to finish first.")

    bulk_id = bulk.id
    counts: dict[str, int] = {}

    counts["bulk_ai_upload_revision_notes"] = _deleted_rows(
        db.execute(
            delete(BulkAIUploadRevisionNote).where(
                BulkAIUploadRevisionNote.bulk_upload_id == bulk_id
            )
        )
    )
    counts["bulk_ai_upload_files"] = _deleted_rows(
        db.execute(
            delete(BulkAIUploadFile).where(BulkAIUploadFile.bulk_upload_id == bulk_id)
        )
    )
    counts["bulk_ai_upload_child_files"] = _deleted_rows(
        db.execute(
            delete(BulkAIUploadChildFile).where(
                BulkAIUploadChildFile.bulk_upload_id == bulk_id
            )
        )
    )
    counts["jobs"] = _deleted_rows(
        db.execute(delete(Job).where(Job.reference_id == bulk_id))
    )
    counts["bulk_ai_uploads"] = _deleted_rows(
        db.execute(delete(BulkAIUpload).where(BulkAIUpload.id == bulk_id))
    )
    db.commit()

    counts["storage_objects"] = _delete_storage_prefix(
        BULK_UPLOAD_OBJECT_PREFIX.format(bulk_id=bulk_id)
    ) + _delete_storage_prefix(
        BULK_REVISION_NOTES_OBJECT_PREFIX.format(bulk_id=bulk_id)
    )
    logger.info("purge: bulk upload %s permanently deleted: %s", bulk_id, counts)
    return counts


def purge_deck(db: Session, deck: Deck) -> dict[str, int]:
    """Permanently delete a soft-deleted deck and everything that belongs to it."""
    if not getattr(deck, "is_deleted", False):
        raise PurgeError(
            "Only decks that were already deleted can be removed permanently."
        )

    deck_id = deck.id
    counts: dict[str, int] = {}

    bulk_ids = list(
        db.execute(
            select(BulkAIUpload.id).where(BulkAIUpload.deck_id == deck_id)
        ).scalars().all()
    )
    concept_map_ids = list(
        db.execute(
            select(ConceptMap.id).where(ConceptMap.deck_id == deck_id)
        ).scalars().all()
    )
    active_jobs = _active_job_ids(db, list(bulk_ids) + list(concept_map_ids))
    if active_jobs:
        raise PurgeError(
            "This deck still has a running job (concept map or bulk upload). "
            "Wait for it to finish before deleting the deck permanently."
        )

    revision_pdf_keys = [
        key
        for key in db.execute(
            select(ConceptMap.revision_pdf_storage_key).where(
                ConceptMap.deck_id == deck_id,
                ConceptMap.revision_pdf_storage_key.is_not(None),
            )
        ).scalars().all()
        if key
    ]

    # Tests -> attempts -> answers (no cascades anywhere on this path).
    test_ids = list(
        db.execute(select(Test.id).where(Test.deck_id == deck_id)).scalars().all()
    )
    if test_ids:
        attempt_ids = list(
            db.execute(
                select(TestAttempt.id).where(TestAttempt.test_id.in_(test_ids))
            ).scalars().all()
        )
        if attempt_ids:
            counts["test_attempt_answers"] = _deleted_rows(
                db.execute(
                    delete(TestAttemptAnswer).where(
                        TestAttemptAnswer.attempt_id.in_(attempt_ids)
                    )
                )
            )
        counts["test_attempts"] = _deleted_rows(
            db.execute(delete(TestAttempt).where(TestAttempt.test_id.in_(test_ids)))
        )
        counts["test_questions"] = _deleted_rows(
            db.execute(delete(TestQuestion).where(TestQuestion.test_id.in_(test_ids)))
        )
        counts["tests"] = _deleted_rows(
            db.execute(delete(Test).where(Test.id.in_(test_ids)))
        )

    # Reviews and card states before the cards they point at.
    card_ids = list(
        db.execute(select(Card.id).where(Card.deck_id == deck_id)).scalars().all()
    )
    if card_ids:
        counts["reviews"] = _deleted_rows(
            db.execute(delete(Review).where(Review.card_id.in_(card_ids)))
        )
        counts["card_states"] = _deleted_rows(
            db.execute(delete(CardState).where(CardState.card_id.in_(card_ids)))
        )
    counts["deck_tags"] = _deleted_rows(
        db.execute(delete(deck_tags).where(deck_tags.c.deck_id == deck_id))
    )
    counts["cards"] = _deleted_rows(
        db.execute(delete(Card).where(Card.deck_id == deck_id))
    )

    # Uploads that targeted this deck: notes -> attempts -> child files.
    if bulk_ids:
        for label, model in (
            ("bulk_ai_upload_revision_notes", BulkAIUploadRevisionNote),
            ("bulk_ai_upload_files", BulkAIUploadFile),
            ("bulk_ai_upload_child_files", BulkAIUploadChildFile),
        ):
            counts[label] = _deleted_rows(
                db.execute(
                    delete(model).where(model.bulk_upload_id.in_(bulk_ids))
                )
            )

    counts["ai_upload_generations"] = _deleted_rows(
        db.execute(
            delete(AIUploadGeneration).where(AIUploadGeneration.deck_id == deck_id)
        )
    )
    if _table_present(db, "mcq_generations"):
        counts["mcq_generations"] = _deleted_rows(
            db.execute(delete(MCQGeneration).where(MCQGeneration.deck_id == deck_id))
        )
    else:
        logger.info(
            "purge: mcq_generations table absent in this environment; skipped"
        )
    counts["deck_mcq_generation_items"] = _deleted_rows(
        db.execute(
            delete(DeckMcqGenerationItem).where(
                DeckMcqGenerationItem.deck_id == deck_id
            )
        )
    )
    counts["concept_maps"] = _deleted_rows(
        db.execute(delete(ConceptMap).where(ConceptMap.deck_id == deck_id))
    )
    counts["deck_accesses"] = _deleted_rows(
        db.execute(delete(DeckAccess).where(DeckAccess.deck_id == deck_id))
    )
    counts["user_deck_favorites"] = _deleted_rows(
        db.execute(
            delete(UserDeckFavorite).where(UserDeckFavorite.deck_id == deck_id)
        )
    )
    if bulk_ids:
        counts["bulk_ai_uploads"] = _deleted_rows(
            db.execute(delete(BulkAIUpload).where(BulkAIUpload.id.in_(bulk_ids)))
        )

    # Jobs only hold a loose reference_id, so clean them up by hand. Jobs for
    # the deck's cards/tests do not exist — every job type is deck-scoped.
    reference_ids = list(bulk_ids) + list(concept_map_ids)
    if reference_ids:
        counts["jobs"] = _deleted_rows(
            db.execute(delete(Job).where(Job.reference_id.in_(reference_ids)))
        )

    # Anything else that referenced this deck (e.g. bulk_ai_upload_files rows of
    # *other* uploads whose created_deck_id pointed here) is handled by the
    # schema's ON DELETE SET NULL.
    counts["decks"] = _deleted_rows(
        db.execute(delete(Deck).where(Deck.id == deck_id))
    )
    db.commit()

    storage_objects = _delete_storage_prefix(deck_media_prefix(str(deck_id)))
    for key in revision_pdf_keys:
        storage_objects += _delete_storage_prefix(key)
    counts["storage_objects"] = storage_objects

    logger.info("purge: deck %s permanently deleted: %s", deck_id, counts)
    return counts


def deleted_decks(db: Session) -> list[Deck]:
    """Soft-deleted decks, newest deletion first."""
    return list(
        db.execute(
            select(Deck)
            .where(Deck.is_deleted.is_(True))
            .order_by(Deck.deleted_at.desc().nullslast())
        )
        .scalars()
        .all()
    )
