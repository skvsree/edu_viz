"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-03-01

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "decks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_decks_user_id"), "decks", ["user_id"], unique=False)

    op.create_table(
        "cards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("deck_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("decks.id"), nullable=False),
        sa.Column("front", sa.Text(), nullable=False),
        sa.Column("back", sa.Text(), nullable=False),
        sa.Column("card_type", sa.String(length=50), nullable=False, server_default="basic"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_cards_deck_id"), "cards", ["deck_id"], unique=False)

    op.create_table(
        "card_states",
        sa.Column("card_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cards.id"), primary_key=True, nullable=False),
        sa.Column("stability", sa.Float(), nullable=False, server_default="0"),
        sa.Column("difficulty", sa.Float(), nullable=False, server_default="5"),
        sa.Column("due", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lapses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="NEW"),
        sa.Column("last_review", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_card_states_due"), "card_states", ["due"], unique=False)

    op.create_table(
        "reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cards.id"), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("review_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_days", sa.Integer(), nullable=False),
        sa.Column("elapsed_days", sa.Integer(), nullable=False),
    )
    op.create_index(op.f("ix_reviews_card_id"), "reviews", ["card_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_reviews_card_id"), table_name="reviews")
    op.drop_table("reviews")

    op.drop_index(op.f("ix_card_states_due"), table_name="card_states")
    op.drop_table("card_states")

    op.drop_index(op.f("ix_cards_deck_id"), table_name="cards")
    op.drop_table("cards")

    op.drop_index(op.f("ix_decks_user_id"), table_name="decks")
    op.drop_table("decks")

    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
