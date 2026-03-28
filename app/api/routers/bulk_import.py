from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import require_bulk_import_api_key
from app.core.db import get_db
from app.models import Card, CardState, Deck, Tag, User
from app.services.access import ROLE_SYSTEM_ADMIN, normalize_deck_name

router = APIRouter(prefix="/api/v1/import", tags=["bulk-import"])


class BulkImportFlashcardItem(BaseModel):
    front: str = Field(min_length=1)
    back: str = Field(min_length=1)
    source_label: str | None = None


class BulkImportMcqItem(BaseModel):
    question: str = Field(min_length=1)
    explanation: str = ""
    options: list[str] = Field(min_length=4, max_length=4)
    answer_index: int = Field(ge=0, le=3)
    source_label: str | None = None


class BulkImportDeckPayload(BaseModel):
    grade_no: int = Field(ge=1)
    chapter_no: int | None = Field(default=None, ge=1)
    subject: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    flashcards: list[BulkImportFlashcardItem] = Field(default_factory=list)
    mcqs: list[BulkImportMcqItem] = Field(default_factory=list)


class BulkImportDeckResponse(BaseModel):
    deck_id: str
    deck_name: str
    created: bool
    flashcards_imported: int
    mcqs_imported: int
    total_cards_imported: int


class BulkImportBatchPayload(BaseModel):
    decks: list[BulkImportDeckPayload] = Field(min_length=1)


class BulkImportBatchResponse(BaseModel):
    imported_decks: list[BulkImportDeckResponse]
    deck_count: int
    total_flashcards_imported: int
    total_mcqs_imported: int
    total_cards_imported: int



def _deck_name(grade_no: int, chapter_no: int | None, subject: str | None = None) -> str:
    if chapter_no is None:
        if subject:
            return f"grade_{grade_no}_{normalize_deck_name(subject)}_full"
        return f"grade_{grade_no}_science_full"
    return f"grade_{grade_no}_science_chapter_{chapter_no}"



def _clean_tag_names(grade_no: int, chapter_no: int | None, extra_tags: list[str], subject: str | None = None) -> list[str]:
    raw_names = [f"grade_{grade_no}", subject or "science"]
    if chapter_no is not None:
        raw_names.append(f"chapter_{chapter_no}")
    raw_names.extend(extra_tags)

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_names:
        name = raw_name.strip()
        if not name:
            continue
        normalized = normalize_deck_name(name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(name)
    return cleaned



def _assign_deck_tags(db: Session, deck: Deck, owner: User, tag_names: list[str]) -> None:
    if owner.organization_id is None:
        return
    existing_tags = {
        tag.normalized_name: tag
        for tag in db.execute(select(Tag).where(Tag.organization_id == owner.organization_id)).scalars().all()
    }
    updated_tags: list[Tag] = []
    for name in tag_names:
        normalized = normalize_deck_name(name)
        tag = existing_tags.get(normalized)
        if tag is None:
            tag = Tag(
                organization_id=owner.organization_id,
                name=name,
                normalized_name=normalized,
            )
            db.add(tag)
            db.flush()
            existing_tags[normalized] = tag
        updated_tags.append(tag)
    deck.tags = updated_tags



def _system_admin_user(db: Session) -> User:
    user = db.execute(select(User).where(User.role == ROLE_SYSTEM_ADMIN).order_by(User.created_at.asc())).scalars().first()
    if user is None:
        raise HTTPException(status_code=503, detail="No system admin is available for bulk import")
    return user


def _import_chapter_deck(payload: BulkImportDeckPayload, db: Session) -> BulkImportDeckResponse:
    if not payload.flashcards and not payload.mcqs:
        raise HTTPException(status_code=400, detail="At least one flashcard or MCQ is required")

    deck_name = _deck_name(payload.grade_no, payload.chapter_no, payload.subject)
    normalized_name = normalize_deck_name(deck_name)
    owner = _system_admin_user(db)
    tag_names = _clean_tag_names(payload.grade_no, payload.chapter_no, payload.tags, payload.subject)

    query = select(Deck).where(
        Deck.normalized_name == normalized_name,
        Deck.is_deleted.is_(False),
        Deck.is_global.is_(True),
    )

    deck = db.execute(query).scalars().first()
    created = False
    if deck is None:
        deck = Deck(
            user_id=owner.id,
            organization_id=None,
            name=deck_name,
            normalized_name=normalized_name,
            description=(payload.description or "").strip() or None,
            is_global=True,
        )
        db.add(deck)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="Deck with this name already exists") from exc
        created = True
    elif (payload.description or "").strip():
        deck.description = payload.description.strip()

    existing_cards = db.execute(select(Card).where(Card.deck_id == deck.id)).scalars().all()
    existing_basic_keys = {
        (card.front.strip(), card.back.strip(), card.card_type, card.source_label or "")
        for card in existing_cards
        if card.card_type == "basic"
    }
    existing_mcq_keys = {
        (
            card.front.strip(),
            card.back.strip(),
            tuple(card.mcq_options or []),
            card.mcq_answer_index,
            card.card_type,
            card.source_label or "",
        )
        for card in existing_cards
        if card.card_type == "mcq"
    }

    new_cards: list[Card] = []
    flashcards_imported = 0
    mcqs_imported = 0

    for item in payload.flashcards:
        front = item.front.strip()
        back = item.back.strip()
        source_label = (item.source_label or "anki-bulk-import").strip() or "anki-bulk-import"
        key = (front, back, "basic", source_label)
        if key in existing_basic_keys:
            continue
        existing_basic_keys.add(key)
        new_cards.append(
            Card(
                deck_id=deck.id,
                front=front,
                back=back,
                card_type="basic",
                source_label=source_label,
            )
        )
        flashcards_imported += 1
    for item in payload.mcqs:
        question = item.question.strip()
        explanation = item.explanation.strip()
        options = [option.strip() for option in item.options]
        if any(not option for option in options):
            raise HTTPException(status_code=400, detail="MCQ options cannot be empty")
        source_label = (item.source_label or "mcq-bulk-import").strip() or "mcq-bulk-import"
        key = (question, explanation, tuple(options), item.answer_index, "mcq", source_label)
        if key in existing_mcq_keys:
            continue
        existing_mcq_keys.add(key)
        new_cards.append(
            Card(
                deck_id=deck.id,
                front=question,
                back=explanation,
                card_type="mcq",
                mcq_options=options,
                mcq_answer_index=item.answer_index,
                source_label=source_label,
            )
        )
        mcqs_imported += 1

    _assign_deck_tags(db, deck, owner, tag_names)
    db.add_all(new_cards)
    db.flush()
    db.add_all([CardState(card_id=card.id) for card in new_cards])
    db.commit()

    return BulkImportDeckResponse(
        deck_id=str(deck.id),
        deck_name=deck.name,
        created=created,
        flashcards_imported=flashcards_imported,
        mcqs_imported=mcqs_imported,
        total_cards_imported=len(new_cards),
    )


@router.post("/deck", response_model=BulkImportDeckResponse, dependencies=[Depends(require_bulk_import_api_key)])
def import_chapter_deck(payload: BulkImportDeckPayload, db: Session = Depends(get_db)):
    return _import_chapter_deck(payload, db)


@router.post("/decks", response_model=BulkImportBatchResponse, dependencies=[Depends(require_bulk_import_api_key)])
def import_chapter_decks(payload: BulkImportBatchPayload, db: Session = Depends(get_db)):
    imported = [_import_chapter_deck(item, db) for item in payload.decks]
    return BulkImportBatchResponse(
        imported_decks=imported,
        deck_count=len(imported),
        total_flashcards_imported=sum(item.flashcards_imported for item in imported),
        total_mcqs_imported=sum(item.mcqs_imported for item in imported),
        total_cards_imported=sum(item.total_cards_imported for item in imported),
    )
