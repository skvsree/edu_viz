import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class DeckAccessScope(str, enum.Enum):
    GLOBAL = "global"
    ORG = "org"
    USER = "user"


class Deck(Base):
    __tablename__ = "decks"
    __table_args__ = (
        Index(
            "ix_decks_global_normalized_name_active",
            "normalized_name",
            unique=True,
            postgresql_where=text("access_level = 'global' AND is_deleted = false"),
        ),
        Index(
            "ix_decks_org_normalized_name_active",
            "organization_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("access_level = 'org' AND is_deleted = false"),
        ),
        Index(
            "ix_decks_user_normalized_name_active",
            "user_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("access_level = 'user' AND is_deleted = false"),
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
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("folders.id"),
        index=True,
        nullable=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # access_level replaces is_global: global=anyone can read, org=org members, user=owner only
    access_level: Mapped[DeckAccessScope] = mapped_column(
        Enum(
            DeckAccessScope,
            name="deck_scope",
            create_constraint=False,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=DeckAccessScope.USER,
        nullable=False,
    )
    is_global: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    organization = relationship("Organization", back_populates="decks")
    folder = relationship("Folder", back_populates="decks")
    cards = relationship("Card", back_populates="deck", cascade="all, delete-orphan")
    tags = relationship("Tag", secondary="deck_tags", back_populates="decks")
    mcq_generations = relationship("MCQGeneration", back_populates="deck", cascade="all, delete-orphan")
    mcq_generation_items = relationship("DeckMcqGenerationItem", back_populates="deck", cascade="all, delete-orphan")
    ai_upload_generations = relationship("AIUploadGeneration", back_populates="deck", cascade="all, delete-orphan")
    bulk_ai_uploads = relationship("BulkAIUpload", back_populates="deck", cascade="all, delete-orphan")
    deck_accesses = relationship("DeckAccess", back_populates="deck", cascade="all, delete-orphan")
