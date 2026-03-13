"""users: rename b2c_sub to identity_sub

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-14

"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(op.f("ix_users_b2c_sub"), table_name="users")
    op.alter_column("users", "b2c_sub", new_column_name="identity_sub")
    op.create_index(op.f("ix_users_identity_sub"), "users", ["identity_sub"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_identity_sub"), table_name="users")
    op.alter_column("users", "identity_sub", new_column_name="b2c_sub")
    op.create_index(op.f("ix_users_b2c_sub"), "users", ["b2c_sub"], unique=True)
