"""Add anki card fields (content_html, media_files, cloze_number)

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('cards', sa.Column('content_html', sa.Text(), nullable=True))
    op.add_column('cards', sa.Column('media_files', postgresql.JSONB(), nullable=True))
    op.add_column('cards', sa.Column('cloze_number', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('cards', 'cloze_number')
    op.drop_column('cards', 'media_files')
    op.drop_column('cards', 'content_html')
