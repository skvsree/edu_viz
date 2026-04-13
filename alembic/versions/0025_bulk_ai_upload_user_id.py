"""add user id to bulk ai uploads

Revision ID: 0025_bulk_ai_upload_user_id
Revises: 0024_bulk_ai_upload_storage_key
Create Date: 2026-04-13 04:55:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0025_bulk_ai_upload_user_id'
down_revision = '0024_bulk_ai_upload_storage_key'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('bulk_ai_uploads', sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.alter_column('bulk_ai_uploads', 'deck_id', existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.create_foreign_key(
        'fk_bulk_ai_uploads_user_id_users',
        'bulk_ai_uploads',
        'users',
        ['user_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_index(op.f('ix_bulk_ai_uploads_user_id'), 'bulk_ai_uploads', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_bulk_ai_uploads_user_id'), table_name='bulk_ai_uploads')
    op.drop_constraint('fk_bulk_ai_uploads_user_id_users', 'bulk_ai_uploads', type_='foreignkey')
    op.alter_column('bulk_ai_uploads', 'deck_id', existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.drop_column('bulk_ai_uploads', 'user_id')
