import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deck_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("decks.id"), index=True, nullable=False)

    front: Mapped[str] = mapped_column(Text, nullable=False)
    back: Mapped[str] = mapped_column(Text, nullable=False)
    card_type: Mapped[str] = mapped_column(String(50), default="basic", nullable=False)
    mcq_options: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    mcq_answer_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    # Anki import fields
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True)  # Full HTML with cloze/image markup
    media_files: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # ["file1.jpg", "file2.png"]
    cloze_number: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Cloze index (1, 2, 3...) or NULL

    deck = relationship("Deck", back_populates="cards")
    state = relationship("CardState", back_populates="card", uselist=False, cascade="all, delete-orphan")
    reviews = relationship("Review", back_populates="card", cascade="all, delete-orphan")
    test_questions = relationship("TestQuestion", back_populates="card")

    @property
    def is_cloze(self) -> bool:
        """Check if this is a cloze card."""
        return self.card_type == "cloze" and self.content_html is not None

    @property
    def has_media(self) -> bool:
        """Check if this card has associated media files."""
        return self.media_files is not None and len(self.media_files) > 0
