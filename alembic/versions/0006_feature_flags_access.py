"""feature flags for tests and ai

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-14

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("is_ai_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("is_test_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column("organizations", "is_ai_enabled", server_default=None)
    op.alter_column("users", "is_test_enabled", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "is_test_enabled")
    op.drop_column("organizations", "is_ai_enabled")
