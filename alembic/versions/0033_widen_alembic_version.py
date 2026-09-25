"""widen alembic_version.version_num so long revision ids can be stamped

Revision ID: 0033_widen_alembic_version
Revises: 0032_job_visibility
Create Date: 2026-09-25 08:10:00

Alembic creates ``alembic_version.version_num`` as VARCHAR(32), but a revision id
longer than that cannot be stamped: the migration body runs, then the final
``UPDATE alembic_version`` dies with ``StringDataRightTruncation`` and the whole
transaction - schema changes included - is rolled back. Prod sat at
``0027_bulk_revision_notes`` for exactly this reason, because the 39-character
``0028_bulk_revision_notes_section_titles`` id did not fit in the column; the
entrypoint aborted before uvicorn started and the site served 502 everywhere.

0028 widens the column, but only on the 0027 -> 0028 step, so that guarantee
depends on where a database sits in the chain. This migration makes it
unconditional: every database that reaches 0033 gets the wide column, whatever
its current width, so no future long revision id can break a stamp again.

Widening a varchar is metadata-only in Postgres (no table rewrite), and the ALTER
is skipped entirely when the column is already wide enough, so this is safe to
run on a database that 0028 already widened.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0033_widen_alembic_version'
down_revision = '0032_job_visibility'
branch_labels = None
depends_on = None

# Comfortably above the longest revision id in this repository (39 chars) while
# staying inside Alembic's own limits for the version table.
WIDE_LENGTH = 255

_VERSION_NUM_WIDTH = sa.text(
    'SELECT character_maximum_length '
    'FROM information_schema.columns '
    'WHERE table_name = \'alembic_version\' '
    'AND column_name = \'version_num\''
)


def _current_width() -> int | None:
    """Current length of alembic_version.version_num (None if not a varchar)."""
    return op.get_bind().execute(_VERSION_NUM_WIDTH).scalar()


def _widen() -> None:
    op.alter_column(
        'alembic_version',
        'version_num',
        existing_type=sa.String(length=32),
        type_=sa.String(length=WIDE_LENGTH),
        existing_nullable=False,
    )


def upgrade() -> None:
    # --sql / offline mode has no connection to inspect, so emit the ALTER and
    # let Postgres no-op it when the column is already wide.
    if op.get_context().as_sql:
        _widen()
        return

    width = _current_width()
    if width is not None and width >= WIDE_LENGTH:
        return
    _widen()


def downgrade() -> None:
    # Deliberately a no-op. Narrowing the column back to VARCHAR(32) would make
    # the revision id of this migration and of 0028 unstampable again, so a
    # downgrade would leave the database unable to record its own position.
    # Leaving the column wide is backwards compatible - every id fits.
    pass
