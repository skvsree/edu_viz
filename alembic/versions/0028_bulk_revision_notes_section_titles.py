"""Add section_titles JSONB to bulk_ai_upload_revision_notes

Allows callers to supply the book's real sub-section titles from the
table of contents so the revision-notes PDF doesn't fall back to the
generic "Section N" placeholder for chapters whose prose has no
detectable inline headings (most NCERT chapters).

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "0028_bulk_revision_notes_section_titles"
down_revision = "0027_bulk_revision_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bulk_ai_upload_revision_notes",
        sa.Column("section_titles", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bulk_ai_upload_revision_notes", "section_titles")
