"""Add content_html_back to cards table

Revision ID: 0014
Revises: 0013_add_anki_card_fields
Create Date: 2026-04-03

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers
revision = '0014_add_card_content_html_back'
down_revision = '0013_add_anki_card_fields'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('cards', sa.Column('content_html_back', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('cards', 'content_html_back')
