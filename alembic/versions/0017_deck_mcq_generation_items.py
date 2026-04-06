"""add deck mcq generation item status table

Revision ID: 0017_deck_mcq_generation_items
Revises: 0016_ai_credentials
Create Date: 2026-04-06 01:20:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0017_deck_mcq_generation_items'
down_revision = '0016_ai_credentials'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'deck_mcq_generation_items',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('deck_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('source_card_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['deck_id'], ['decks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['source_card_id'], ['cards.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('deck_id', 'source_card_id', name='uq_deck_mcq_generation_items_deck_source_card'),
    )
    op.create_index(op.f('ix_deck_mcq_generation_items_deck_id'), 'deck_mcq_generation_items', ['deck_id'], unique=False)
    op.create_index(op.f('ix_deck_mcq_generation_items_source_card_id'), 'deck_mcq_generation_items', ['source_card_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_deck_mcq_generation_items_source_card_id'), table_name='deck_mcq_generation_items')
    op.drop_index(op.f('ix_deck_mcq_generation_items_deck_id'), table_name='deck_mcq_generation_items')
    op.drop_table('deck_mcq_generation_items')
