"""ai content, mcqs, tests

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-14

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cards", sa.Column("mcq_options", sa.JSON(), nullable=True))
    op.add_column("cards", sa.Column("mcq_answer_index", sa.Integer(), nullable=True))
    op.add_column("cards", sa.Column("source_label", sa.String(length=100), nullable=True))

    op.create_table(
        "tests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("deck_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("decks.id"), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("question_count", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_tests_deck_id"), "tests", ["deck_id"], unique=False)
    op.create_index(op.f("ix_tests_created_by_user_id"), "tests", ["created_by_user_id"], unique=False)

    op.create_table(
        "test_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("test_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tests.id"), nullable=False),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cards.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
    )
    op.create_index(op.f("ix_test_questions_test_id"), "test_questions", ["test_id"], unique=False)
    op.create_index(op.f("ix_test_questions_card_id"), "test_questions", ["card_id"], unique=False)

    op.create_table(
        "test_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("test_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tests.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_questions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="completed"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_test_attempts_test_id"), "test_attempts", ["test_id"], unique=False)
    op.create_index(op.f("ix_test_attempts_user_id"), "test_attempts", ["user_id"], unique=False)

    op.create_table(
        "test_attempt_answers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("test_attempts.id"), nullable=False),
        sa.Column("question_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("test_questions.id"), nullable=False),
        sa.Column("selected_option_index", sa.Integer(), nullable=True),
        sa.Column("is_correct", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index(op.f("ix_test_attempt_answers_attempt_id"), "test_attempt_answers", ["attempt_id"], unique=False)
    op.create_index(op.f("ix_test_attempt_answers_question_id"), "test_attempt_answers", ["question_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_test_attempt_answers_question_id"), table_name="test_attempt_answers")
    op.drop_index(op.f("ix_test_attempt_answers_attempt_id"), table_name="test_attempt_answers")
    op.drop_table("test_attempt_answers")
    op.drop_index(op.f("ix_test_attempts_user_id"), table_name="test_attempts")
    op.drop_index(op.f("ix_test_attempts_test_id"), table_name="test_attempts")
    op.drop_table("test_attempts")
    op.drop_index(op.f("ix_test_questions_card_id"), table_name="test_questions")
    op.drop_index(op.f("ix_test_questions_test_id"), table_name="test_questions")
    op.drop_table("test_questions")
    op.drop_index(op.f("ix_tests_created_by_user_id"), table_name="tests")
    op.drop_index(op.f("ix_tests_deck_id"), table_name="tests")
    op.drop_table("tests")
    op.drop_column("cards", "source_label")
    op.drop_column("cards", "mcq_answer_index")
    op.drop_column("cards", "mcq_options")
