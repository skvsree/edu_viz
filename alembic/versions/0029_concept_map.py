"""add concept_map table

Stores hierarchical graph data (nodes + edges) for visual mind-map
rendering on deck overview pages.

Revision ID: 0029_concept_map
Revises: 0028_bulk_revision_notes_section_titles
Create Date: 2026-07-27
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0029_concept_map"
down_revision = "0028_bulk_revision_notes_section_titles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "concept_maps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("deck_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("graph_data", postgresql.JSONB, nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("node_count", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["deck_id"], ["decks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"], ["bulk_ai_upload_files.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("deck_id", name="uq_concept_map_deck"),
    )
    op.create_index(
        op.f("ix_concept_maps_deck_id"),
        "concept_maps",
        ["deck_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_concept_maps_status"),
        "concept_maps",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_concept_maps_status"),
        table_name="concept_maps",
    )
    op.drop_index(
        op.f("ix_concept_maps_deck_id"),
        table_name="concept_maps",
    )
    op.drop_table("concept_maps")
