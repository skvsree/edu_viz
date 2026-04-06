"""add ai upload generations table

Revision ID: 0018_ai_upload_generations
Revises: 0017_deck_mcq_generation_items
Create Date: 2026-04-06 08:20:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0018_ai_upload_generations'
down_revision = '0017_deck_mcq_generation_items'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ai_upload_generations',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('deck_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('total_chunks', sa.Integer(), nullable=True),
        sa.Column('processed_chunks', sa.Integer(), nullable=False),
        sa.Column('flashcards_generated', sa.Integer(), nullable=False),
        sa.Column('mcqs_generated', sa.Integer(), nullable=False),
        sa.Column('duplicates_skipped', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['deck_id'], ['decks.id'], ondelete='CASCADE'),
    )
    op.create_index(op.f('ix_ai_upload_generations_deck_id'), 'ai_upload_generations', ['deck_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ai_upload_generations_deck_id'), table_name='ai_upload_generations')
    op.drop_table('ai_upload_generations')
