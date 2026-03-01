"""users: add b2c_sub; drop password_hash requirement

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-01

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing installs (if any) will need manual data migration.
    # For fresh installs, this is fine.
    op.add_column("users", sa.Column("b2c_sub", sa.String(length=255), nullable=True))
    op.create_index(op.f("ix_users_b2c_sub"), "users", ["b2c_sub"], unique=True)

    op.alter_column("users", "password_hash", nullable=True)
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=True)

    # Make b2c_sub required after backfill
    op.execute("UPDATE users SET b2c_sub = email WHERE b2c_sub IS NULL")
    op.alter_column("users", "b2c_sub", nullable=False)


def downgrade() -> None:
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=False)
    op.alter_column("users", "password_hash", nullable=False)

    op.drop_index(op.f("ix_users_b2c_sub"), table_name="users")
    op.drop_column("users", "b2c_sub")
