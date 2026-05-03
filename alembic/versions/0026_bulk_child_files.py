"""add bulk upload child files

Revision ID: 0026_bulk_child_files
Revises: 0025_bulk_ai_upload_user_id
Create Date: 2026-05-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0026_bulk_child_files'
down_revision = '0025_bulk_ai_upload_user_id'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'bulk_ai_upload_child_files',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('bulk_upload_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('latest_attempt_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('child_key', sa.String(length=255), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('display_title', sa.String(length=255), nullable=True),
        sa.Column('storage_key', sa.String(length=512), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['bulk_upload_id'], ['bulk_ai_uploads.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['latest_attempt_id'], ['bulk_ai_upload_files.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('bulk_upload_id', 'child_key', name='uq_bulk_ai_upload_child_file_key'),
    )
    op.create_index(op.f('ix_bulk_ai_upload_child_files_bulk_upload_id'), 'bulk_ai_upload_child_files', ['bulk_upload_id'], unique=False)
    op.create_index(op.f('ix_bulk_ai_upload_child_files_latest_attempt_id'), 'bulk_ai_upload_child_files', ['latest_attempt_id'], unique=False)
    op.create_index(op.f('ix_bulk_ai_upload_child_files_storage_key'), 'bulk_ai_upload_child_files', ['storage_key'], unique=False)

    op.add_column('bulk_ai_upload_files', sa.Column('child_file_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index(op.f('ix_bulk_ai_upload_files_child_file_id'), 'bulk_ai_upload_files', ['child_file_id'], unique=False)
    op.create_foreign_key(
        'fk_baif_child_file_id',
        'bulk_ai_upload_files',
        'bulk_ai_upload_child_files',
        ['child_file_id'],
        ['id'],
        ondelete='CASCADE',
    )

    op.execute(
        """
        INSERT INTO bulk_ai_upload_child_files (
            id,
            bulk_upload_id,
            latest_attempt_id,
            child_key,
            original_filename,
            display_title,
            storage_key,
            file_size,
            created_at
        )
        SELECT
            gen_random_uuid(),
            latest.bulk_upload_id,
            latest.id,
            latest.original_filename || ':' || COALESCE(latest.created_deck_id::text, latest.id::text),
            latest.original_filename,
            COALESCE(NULLIF(latest.extracted_title, ''), regexp_replace(latest.original_filename, '\\.[^.]+$', '')),
            latest.storage_key,
            latest.file_size,
            COALESCE(latest.created_at, now())
        FROM (
            SELECT DISTINCT ON (COALESCE(created_deck_id::text, ''), bulk_upload_id, original_filename)
                id,
                bulk_upload_id,
                original_filename,
                extracted_title,
                storage_key,
                file_size,
                created_at,
                created_deck_id
            FROM bulk_ai_upload_files
            WHERE bulk_upload_id IS NOT NULL
            ORDER BY COALESCE(created_deck_id::text, ''), bulk_upload_id, original_filename, created_at DESC, completed_at DESC NULLS LAST
        ) AS latest
        """
    )

    op.execute(
        """
        UPDATE bulk_ai_upload_files AS f
        SET child_file_id = c.id
        FROM bulk_ai_upload_child_files AS c
        WHERE f.bulk_upload_id IS NOT NULL
          AND c.bulk_upload_id = f.bulk_upload_id
          AND c.original_filename = f.original_filename
          AND c.child_key = (
              f.original_filename || ':' || COALESCE(f.created_deck_id::text, f.id::text)
          )
        """
    )

    op.execute(
        """
        WITH ranked AS (
            SELECT
                f.id,
                f.child_file_id,
                row_number() OVER (
                    PARTITION BY f.child_file_id
                    ORDER BY f.created_at DESC, f.completed_at DESC NULLS LAST, f.started_at DESC NULLS LAST, f.id DESC
                ) AS rn
            FROM bulk_ai_upload_files f
            WHERE f.child_file_id IS NOT NULL
        )
        UPDATE bulk_ai_upload_child_files c
        SET latest_attempt_id = ranked.id
        FROM ranked
        WHERE ranked.child_file_id = c.id
          AND ranked.rn = 1
        """
    )


def downgrade() -> None:
    op.drop_constraint('fk_baif_child_file_id', 'bulk_ai_upload_files', type_='foreignkey')
    op.drop_index(op.f('ix_bulk_ai_upload_files_child_file_id'), table_name='bulk_ai_upload_files')
    op.drop_column('bulk_ai_upload_files', 'child_file_id')
    op.drop_index(op.f('ix_bulk_ai_upload_child_files_storage_key'), table_name='bulk_ai_upload_child_files')
    op.drop_index(op.f('ix_bulk_ai_upload_child_files_latest_attempt_id'), table_name='bulk_ai_upload_child_files')
    op.drop_index(op.f('ix_bulk_ai_upload_child_files_bulk_upload_id'), table_name='bulk_ai_upload_child_files')
    op.drop_table('bulk_ai_upload_child_files')
