"""create mcq_generations and cascade the deck/card/test foreign keys

Revision ID: 0031_mcq_fk_cascade
Revises: 0030_concept_map_revision_pdf
Create Date: 2026-09-20 07:30:00

Two pieces of schema drift are fixed here.

1. ``mcq_generations`` had a model and live route code (``content.py``) but no
   migration ever created the table, so MCQ generation raised
   ``UndefinedTable`` on any database built from migrations.

2. The tables that hang off decks/cards/tests/attempts referenced their
   parents with NO ACTION - ``cards``, ``tests``, ``test_questions``,
   ``test_attempts``, ``test_attempt_answers``, ``reviews``, ``card_states``,
   ``deck_tags`` and ``mcq_generations``. Removing a deck therefore failed
   unless every child row was deleted by hand first (which is exactly what
   ``app/services/purge.py`` has to do). They now cascade at the database
   level, so the schema enforces the same cleanup the ORM does.

Existing constraint names are read from ``pg_constraint`` rather than assumed,
because they were created without explicit names in earlier migrations.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0031_mcq_fk_cascade'
down_revision = '0030_concept_map_revision_pdf'
branch_labels = None
depends_on = None


# (table, column, parent table) for every child FK that must cascade.
CASCADE_FKS = (
    ('cards', 'deck_id', 'decks'),
    ('tests', 'deck_id', 'decks'),
    ('test_questions', 'test_id', 'tests'),
    ('test_questions', 'card_id', 'cards'),
    ('test_attempts', 'test_id', 'tests'),
    ('test_attempt_answers', 'attempt_id', 'test_attempts'),
    ('test_attempt_answers', 'question_id', 'test_questions'),
    ('reviews', 'card_id', 'cards'),
    ('card_states', 'card_id', 'cards'),
    ('deck_tags', 'deck_id', 'decks'),
    ('deck_tags', 'tag_id', 'tags'),
    ('mcq_generations', 'deck_id', 'decks'),
)

MCQ_TABLE = 'mcq_generations'
MCQ_DECK_FK = 'mcq_generations_deck_id_fkey'


def _constraint_name(conn, table: str, column: str):
    """Name of the foreign key on (table, column), or None."""
    return conn.execute(
        sa.text(
            """
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_attribute att
              ON att.attrelid = rel.oid AND att.attnum = ANY (con.conkey)
            WHERE con.contype = 'f'
              AND rel.relname = :table
              AND att.attname = :column
            """
        ),
        {'table': table, 'column': column},
    ).scalar()


def _table_exists(conn, table: str) -> bool:
    return (
        conn.execute(sa.text('SELECT to_regclass(:name)'), {'name': f'public.{table}'}).scalar()
        is not None
    )


def _set_delete_rule(rule: str) -> None:
    """Recreate every child FK with (or without) ON DELETE CASCADE."""
    conn = op.get_bind()
    ondelete = rule if rule != 'NO ACTION' else None
    for table, column, parent in CASCADE_FKS:
        name = _constraint_name(conn, table, column)
        if not name:
            continue
        op.drop_constraint(name, table, type_='foreignkey')
        op.create_foreign_key(
            name, table, parent, [column], ['id'], ondelete=ondelete
        )


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, MCQ_TABLE):
        op.create_table(
            MCQ_TABLE,
            sa.Column(
                'id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
            ),
            sa.Column('deck_id', postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column('status', sa.String(length=50), nullable=False),
            sa.Column('total_cards', sa.Integer(), nullable=True),
            sa.Column('processed_cards', sa.Integer(), nullable=False),
            sa.Column('mcqs_generated', sa.Integer(), nullable=False),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ['deck_id'], ['decks.id'], name=MCQ_DECK_FK, ondelete='CASCADE'
            ),
        )
        op.create_index(
            op.f('ix_mcq_generations_deck_id'), MCQ_TABLE, ['deck_id'], unique=False
        )

    # A pre-existing table (created by hand or by an older deployment) may have
    # no deck_id foreign key at all: add one before rewriting the rules.
    if not _constraint_name(conn, MCQ_TABLE, 'deck_id'):
        op.create_foreign_key(
            MCQ_DECK_FK, MCQ_TABLE, 'decks', ['deck_id'], ['id'], ondelete='CASCADE'
        )

    _set_delete_rule('CASCADE')


def downgrade() -> None:
    _set_delete_rule('NO ACTION')
    op.drop_index(op.f('ix_mcq_generations_deck_id'), table_name=MCQ_TABLE)
    op.drop_table(MCQ_TABLE)
