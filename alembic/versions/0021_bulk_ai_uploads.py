"""add bulk ai uploads tables

Revision ID: 0021_bulk_ai_uploads
Revises: 0020_deck_access_level
Create Date: 2026-04-12 12:30:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0021_bulk_ai_uploads'
down_revision = '0020_deck_access_level'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'bulk_ai_uploads',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('deck_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('total_files', sa.Integer(), nullable=False),
        sa.Column('processed_files', sa.Integer(), nullable=False),
        sa.Column('flashcards_generated', sa.Integer(), nullable=False),
        sa.Column('mcqs_generated', sa.Integer(), nullable=False),
        sa.Column('skipped_files', sa.Integer(), nullable=False),
        sa.Column('failed_files', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('is_auto_stop', sa.Boolean(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['deck_id'], ['decks.id'], ondelete='CASCADE'),
    )
    op.create_index(op.f('ix_bulk_ai_uploads_deck_id'), 'bulk_ai_uploads', ['deck_id'], unique=False)

    op.create_table(
        'bulk_ai_upload_files',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('bulk_upload_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('extracted_title', sa.String(length=255), nullable=True),
        sa.Column('extracted_description', sa.Text(), nullable=True),
        sa.Column('content_text', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('flashcards_generated', sa.Integer(), nullable=False),
        sa.Column('mcqs_generated', sa.Integer(), nullable=False),
        sa.Column('duplicate_count', sa.Integer(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['bulk_upload_id'], ['bulk_ai_uploads.id'], ondelete='CASCADE'),
    )
    op.create_index(op.f('ix_bulk_ai_upload_files_bulk_upload_id'), 'bulk_ai_upload_files', ['bulk_upload_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_bulk_ai_upload_files_bulk_upload_id'), table_name='bulk_ai_upload_files')
    op.drop_table('bulk_ai_upload_files')
    op.drop_index(op.f('ix_bulk_ai_uploads_deck_id'), table_name='bulk_ai_uploads')
    op.drop_table('bulk_ai_uploads')