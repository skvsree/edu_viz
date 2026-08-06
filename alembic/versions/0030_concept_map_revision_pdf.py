"""Add revision PDF columns to concept_maps

Revision ID: 0030_concept_map_revision_pdf
Revises: 0029_concept_map
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0030_concept_map_revision_pdf"
down_revision = "0029_concept_map"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "concept_maps",
        sa.Column(
            "revision_pdf_status",
            sa.String(20),
            nullable=True,
            index=True,
        ),
    )
    op.add_column(
        "concept_maps",
        sa.Column(
            "revision_pdf_storage_key",
            sa.String(512),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("concept_maps", "revision_pdf_storage_key")
    op.drop_column("concept_maps", "revision_pdf_status")
