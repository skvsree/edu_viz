"""add ai credentials table

Revision ID: 0016_ai_credentials
Revises: 0015
Create Date: 2026-04-04 09:28:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '0016_ai_credentials'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ai_credentials',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('scope_type', sa.String(length=20), nullable=False),
        sa.Column('scope_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('auth_type', sa.String(length=20), nullable=False),
        sa.Column('secret_encrypted', sa.Text(), nullable=False),
        sa.Column('refresh_token_encrypted', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('scope_type', 'scope_id', 'provider', name='uq_ai_credentials_scope_provider'),
    )
    op.create_index(op.f('ix_ai_credentials_scope_id'), 'ai_credentials', ['scope_id'], unique=False)
    op.create_index(op.f('ix_ai_credentials_provider'), 'ai_credentials', ['provider'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ai_credentials_provider'), table_name='ai_credentials')
    op.drop_index(op.f('ix_ai_credentials_scope_id'), table_name='ai_credentials')
    op.drop_table('ai_credentials')
