import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class MCQGenerationStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class MCQGeneration(Base):
    """Tracks MCQ generation status for each deck."""
    __tablename__ = "mcq_generations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deck_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("decks.id"), index=True, nullable=False)
    
    status: Mapped[str] = mapped_column(String(50), default=MCQGenerationStatus.NOT_STARTED.value, nullable=False)
    total_cards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed_cards: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mcqs_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    deck = relationship("Deck", back_populates="mcq_generations")

    @property
    def is_complete(self) -> bool:
        return self.status == MCQGenerationStatus.COMPLETED.value
