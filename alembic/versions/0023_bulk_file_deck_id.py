"""add created deck id to bulk ai upload files

Revision ID: 0023_bulk_file_deck_id
Revises: 0022_jobs
Create Date: 2026-04-13 02:28:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0023_bulk_file_deck_id'
down_revision = '0022_jobs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'bulk_ai_upload_files',
        sa.Column('created_deck_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_bulk_ai_upload_files_created_deck_id_decks',
        'bulk_ai_upload_files',
        'decks',
        ['created_deck_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        op.f('ix_bulk_ai_upload_files_created_deck_id'),
        'bulk_ai_upload_files',
        ['created_deck_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_bulk_ai_upload_files_created_deck_id'), table_name='bulk_ai_upload_files')
    op.drop_constraint('fk_bulk_ai_upload_files_created_deck_id_decks', 'bulk_ai_upload_files', type_='foreignkey')
    op.drop_column('bulk_ai_upload_files', 'created_deck_id')
