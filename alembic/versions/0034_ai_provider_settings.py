"""app-wide AI provider/model configuration, editable by an admin

Revision ID: 0034_ai_provider_settings
Revises: 0033_widen_alembic_version
Create Date: 2026-10-03 13:00:00

The provider and model used by every AI call lived only in the environment
(``AI_PROVIDER``/``AI_MODEL``), so switching vendors meant editing the
deployment and recreating the container, and a half-applied switch failed every
call with a generic "AI provider did not return usable flashcards or MCQs".
This table holds an optional admin-chosen override; the environment remains the
fallback and the source of the API key unless the row carries one.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0034_ai_provider_settings'
down_revision = '0033_widen_alembic_version'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ai_provider_settings',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('singleton', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('model', sa.String(length=120), nullable=False),
        sa.Column('api_key_encrypted', sa.Text(), nullable=True),
        sa.Column('updated_by_email', sa.String(length=320), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.UniqueConstraint('singleton', name='uq_ai_provider_settings_singleton'),
    )


def downgrade() -> None:
    op.drop_table('ai_provider_settings')
