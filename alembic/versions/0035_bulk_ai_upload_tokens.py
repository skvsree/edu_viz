"""add token usage columns to bulk ai uploads and files

Revision ID: 0035_bulk_ai_upload_tokens
Revises: 0034_ai_provider_settings
Create Date: 2026-11-27 06:30:00.000000

Adds input/output/cache-hit token counters so every bulk AI upload can report
how many tokens it burned. This is the T1 baseline the token-optimisation work
is measured against.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0035_bulk_ai_upload_tokens'
down_revision = '0034_ai_provider_settings'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ('bulk_ai_uploads', 'bulk_ai_upload_files'):
        op.add_column(table, sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'))
        op.add_column(table, sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'))
        op.add_column(table, sa.Column('cache_hit_tokens', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    for table in ('bulk_ai_uploads', 'bulk_ai_upload_files'):
        op.drop_column(table, 'cache_hit_tokens')
        op.drop_column(table, 'output_tokens')
        op.drop_column(table, 'input_tokens')
