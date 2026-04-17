import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class DeckMcqGenerationItemStatus(str, Enum):
    NOT_STARTED = "not_started"
    FAILED = "failed"
    COMPLETED = "completed"


class DeckMcqGenerationItem(Base):
    __tablename__ = "deck_mcq_generation_items"
    __table_args__ = (
        UniqueConstraint("deck_id", "source_card_id", name="uq_deck_mcq_generation_items_deck_source_card"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deck_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("decks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    source_card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cards.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default=DeckMcqGenerationItemStatus.NOT_STARTED.value,
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    deck = relationship("Deck", back_populates="mcq_generation_items")
    source_card = relationship("Card")
