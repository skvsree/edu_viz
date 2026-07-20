"""Bulk revision-notes PDF model.

Each BulkAIUploadChildFile gets at most one revision-notes PDF. The notes
are produced by the LOW-INK revision renderer in
``app.services.revision_notes`` and uploaded to the configured storage
backend under ``bulk_uploads/{bulk_id}/revision_notes/{child_id}.pdf``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class BulkRevisionNoteStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class BulkAIUploadRevisionNote(Base):
    __tablename__ = "bulk_ai_upload_revision_notes"
    __table_args__ = (
        UniqueConstraint(
            "child_file_id", name="uq_bulk_revision_notes_child_file"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bulk_upload_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bulk_ai_uploads.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    child_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bulk_ai_upload_child_files.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # The BulkAIUploadFile attempt this PDF was generated from. Set to NULL
    # when the source attempt is deleted (we keep the PDF anyway).
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bulk_ai_upload_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    deck_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("decks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default=BulkRevisionNoteStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    storage_key: Mapped[str | None] = mapped_column(
        String(512), nullable=True, index=True
    )
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    topic_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sub-section titles from the book's table of contents. When set, the
    # renderer uses these to label chunked-fallback topics instead of the
    # generic "Section N" placeholder. Optional - older rows may have NULL.
    section_titles: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    bulk_upload = relationship("BulkAIUpload")
    child_file = relationship("BulkAIUploadChildFile")
    source_file = relationship("BulkAIUploadFile")
    deck = relationship("Deck")
