"""add storage key for bulk ai upload files

Revision ID: 0024_bulk_ai_upload_storage_key
Revises: 0023_bulk_file_deck_id
Create Date: 2026-04-13 04:36:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0024_bulk_ai_upload_storage_key'
down_revision = '0023_bulk_file_deck_id'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('bulk_ai_upload_files', sa.Column('storage_key', sa.String(length=512), nullable=True))
    op.create_index(op.f('ix_bulk_ai_upload_files_storage_key'), 'bulk_ai_upload_files', ['storage_key'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_bulk_ai_upload_files_storage_key'), table_name='bulk_ai_upload_files')
    op.drop_column('bulk_ai_upload_files', 'storage_key')
