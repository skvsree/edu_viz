"""Concept map model.

Each deck gets at most one concept map — a hierarchical graph of
chapters → topics → key points that helps the user visually revise
and recall the structure of a chapter or unit.

The map is stored as JSONB (nodes + edges) and can be rendered as an
interactive mind map in the frontend.

Relationship to BulkAIUploadRevisionNote:
  - Revision notes = linear text PDF (read linearly)
  - Concept map   = visual graph (scan, navigate, connect ideas)
Both are generated from the same source text during upload.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class ConceptMapStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class ConceptMap(Base):
    __tablename__ = "concept_maps"
    __table_args__ = (
        UniqueConstraint("deck_id", name="uq_concept_map_deck"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    deck_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("decks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # The BulkAIUploadFile that this map was generated from
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bulk_ai_upload_files.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default=ConceptMapStatus.PENDING.value,
        nullable=False,
        index=True,
    )

    # JSONB payload: {nodes: [...], edges: [...]}
    # node = {id, type, label, depth, parent_id, notes}
    # edge = {source, target, relation}
    graph_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True
    )

    # Denormalised display metadata
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    node_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Revision notes PDF generated via async job pipeline
    revision_pdf_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )  # None=not requested, pending, processing, ready, failed
    revision_pdf_storage_key: Mapped[str | None] = mapped_column(
        String(512), nullable=True
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

    deck = relationship("Deck", back_populates="concept_maps")
