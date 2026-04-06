import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class Deck(Base):
    __tablename__ = "decks"
    __table_args__ = (
        Index(
            "ux_decks_global_normalized_name_active",
            "normalized_name",
            unique=True,
            postgresql_where=text("is_global = true AND is_deleted = false"),
        ),
        Index(
            "ux_decks_org_normalized_name_active",
            "organization_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("is_global = false AND is_deleted = false AND organization_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True, nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        index=True,
        nullable=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_global: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    organization = relationship("Organization", back_populates="decks")
    cards = relationship("Card", back_populates="deck", cascade="all, delete-orphan")
    tags = relationship("Tag", secondary="deck_tags", back_populates="decks")
    mcq_generations = relationship("MCQGeneration", back_populates="deck", cascade="all, delete-orphan")
    mcq_generation_items = relationship("DeckMcqGenerationItem", back_populates="deck", cascade="all, delete-orphan")
