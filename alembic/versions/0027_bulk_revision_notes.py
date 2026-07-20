"""add bulk revision notes table

Revision ID: 0027_bulk_revision_notes
Revises: 0026_bulk_child_files
Create Date: 2026-07-20 12:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0027_bulk_revision_notes"
down_revision = "0026_bulk_child_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bulk_ai_upload_revision_notes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("bulk_upload_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("deck_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("topic_count", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["bulk_upload_id"], ["bulk_ai_uploads.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["child_file_id"], ["bulk_ai_upload_child_files.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"], ["bulk_ai_upload_files.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["deck_id"], ["decks.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "child_file_id", name="uq_bulk_revision_notes_child_file"
        ),
    )
    op.create_index(
        op.f("ix_bulk_ai_upload_revision_notes_bulk_upload_id"),
        "bulk_ai_upload_revision_notes",
        ["bulk_upload_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_bulk_ai_upload_revision_notes_child_file_id"),
        "bulk_ai_upload_revision_notes",
        ["child_file_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_bulk_ai_upload_revision_notes_deck_id"),
        "bulk_ai_upload_revision_notes",
        ["deck_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_bulk_ai_upload_revision_notes_status"),
        "bulk_ai_upload_revision_notes",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_bulk_ai_upload_revision_notes_storage_key"),
        "bulk_ai_upload_revision_notes",
        ["storage_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_bulk_ai_upload_revision_notes_storage_key"),
        table_name="bulk_ai_upload_revision_notes",
    )
    op.drop_index(
        op.f("ix_bulk_ai_upload_revision_notes_status"),
        table_name="bulk_ai_upload_revision_notes",
    )
    op.drop_index(
        op.f("ix_bulk_ai_upload_revision_notes_deck_id"),
        table_name="bulk_ai_upload_revision_notes",
    )
    op.drop_index(
        op.f("ix_bulk_ai_upload_revision_notes_child_file_id"),
        table_name="bulk_ai_upload_revision_notes",
    )
    op.drop_index(
        op.f("ix_bulk_ai_upload_revision_notes_bulk_upload_id"),
        table_name="bulk_ai_upload_revision_notes",
    )
    op.drop_table("bulk_ai_upload_revision_notes")
