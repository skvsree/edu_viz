"""add user deck favorites

Revision ID: 0010
Revises: '0009_add_analytics_tables'
Create Date: 2026-03-30

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0010"
down_revision = '0009_add_analytics_tables'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_deck_favorites",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("deck_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deck_id"],
            ["decks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "deck_id"),
    )
    op.create_index(
        "ix_user_deck_favorites_deck_id",
        "user_deck_favorites",
        ["deck_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_user_deck_favorites_deck_id", table_name="user_deck_favorites")
    op.drop_table("user_deck_favorites")
