"""Add is_test_enabled to organizations.

Adds organization-level override for test access, allowing org admins
to enable tests for all users in their org without per-user configuration.
"""

import sqlalchemy as sa
from alembic import op


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("is_test_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("organizations", "is_test_enabled")
