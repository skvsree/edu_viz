"""Per-step events for a background job, so the UI can show a job log.

The bulk worker printed its progress to stdout, which meant answering "why is
this slow / why did a chunk lose a mode" required container logs. These rows
carry the same messages to the jobs page.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class JobEventLevel:
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    bulk_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bulk_ai_uploads.id", ondelete="CASCADE"),
        nullable=True,
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bulk_ai_upload_files.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    level: Mapped[str] = mapped_column(
        String(16), default=JobEventLevel.INFO, nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
