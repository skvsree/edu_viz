import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class DeckAccessLevel(str, enum.Enum):
    NONE = "none"
    READ = "read"
    WRITE = "write"
    DELETE = "delete"


class DeckAccess(Base):
    __tablename__ = "deck_accesses"
    __table_args__ = (
        UniqueConstraint("deck_id", "user_id", name="uq_deck_access_deck_user"),
        Index("ix_deck_accesses_user", "user_id"),
        Index("ix_deck_accesses_deck", "deck_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deck_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decks.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    access_level: Mapped[str] = mapped_column(
        Enum(DeckAccessLevel, name="deck_access_level", create_constraint=False),
        nullable=False,
        default=DeckAccessLevel.READ,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    deck = relationship("Deck", back_populates="deck_accesses")
    user = relationship("User", back_populates="deck_accesses")
