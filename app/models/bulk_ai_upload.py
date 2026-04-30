import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class BulkAIUploadStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class BulkAIUploadFileStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    STOPPED = "stopped"


class BulkAIUpload(Base):
    __tablename__ = "bulk_ai_uploads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deck_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("decks.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(String(50), default=BulkAIUploadStatus.PENDING.value, nullable=False)
    total_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    flashcards_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mcqs_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_auto_stop: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    deck = relationship("Deck", back_populates="bulk_ai_uploads")
    files = relationship(
        "BulkAIUploadFile",
        back_populates="bulk_upload",
        cascade="all, delete-orphan",
        order_by="BulkAIUploadFile.created_at",
    )


class BulkAIUploadFile(Base):
    __tablename__ = "bulk_ai_upload_files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bulk_upload_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bulk_ai_uploads.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    created_deck_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("decks.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    extracted_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extracted_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)

    status: Mapped[str] = mapped_column(String(50), default=BulkAIUploadFileStatus.PENDING.value, nullable=False)
    flashcards_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mcqs_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    bulk_upload = relationship("BulkAIUpload", back_populates="files")
    created_deck = relationship("Deck")
