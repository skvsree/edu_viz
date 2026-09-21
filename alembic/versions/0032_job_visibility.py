"""per-file generation progress and a persisted job event log

Revision ID: 0032_job_visibility
Revises: 0031_mcq_fk_cascade
Create Date: 2026-09-21 08:45:00

Bulk generation progress was only visible in container stdout: the jobs page
could show how many cards a file had produced, but not how far through the
document the worker was, nor why a mode had been dropped. These columns let the
page show "chunk 3/6 - passes 12/18 - 1 failed" live, and ``job_events`` keeps
the worker's per-step messages so the page can show the log for a job instead of
someone tailing ``docker compose logs``.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0032_job_visibility'
down_revision = '0031_mcq_fk_cascade'
branch_labels = None
depends_on = None

PROGRESS_COLUMNS = (
    ('chunks_total', sa.Integer()),
    ('chunks_completed', sa.Integer()),
    ('passes_total', sa.Integer()),
    ('passes_completed', sa.Integer()),
    ('passes_failed', sa.Integer()),
)


def upgrade() -> None:
    for name, column_type in PROGRESS_COLUMNS:
        op.add_column(
            'bulk_ai_upload_files',
            sa.Column(name, column_type, nullable=False, server_default='0'),
        )
    op.add_column(
        'bulk_ai_upload_files',
        sa.Column('current_stage', sa.String(length=160), nullable=True),
    )
    op.add_column(
        'bulk_ai_upload_files',
        sa.Column('last_event_at', sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        'job_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'job_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('jobs.id', ondelete='CASCADE'),
            nullable=True,
        ),
        sa.Column(
            'bulk_upload_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('bulk_ai_uploads.id', ondelete='CASCADE'),
            nullable=True,
        ),
        sa.Column(
            'file_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('bulk_ai_upload_files.id', ondelete='CASCADE'),
            nullable=True,
        ),
        sa.Column('level', sa.String(length=16), nullable=False, server_default='info'),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
    )
    op.create_index('ix_job_events_job_id', 'job_events', ['job_id'])
    op.create_index('ix_job_events_file_id', 'job_events', ['file_id'])
    op.create_index(
        'ix_job_events_job_created', 'job_events', ['job_id', 'created_at']
    )


def downgrade() -> None:
    op.drop_index('ix_job_events_job_created', table_name='job_events')
    op.drop_index('ix_job_events_file_id', table_name='job_events')
    op.drop_index('ix_job_events_job_id', table_name='job_events')
    op.drop_table('job_events')
    op.drop_column('bulk_ai_upload_files', 'last_event_at')
    op.drop_column('bulk_ai_upload_files', 'current_stage')
    for name, _column_type in reversed(PROGRESS_COLUMNS):
        op.drop_column('bulk_ai_upload_files', name)
