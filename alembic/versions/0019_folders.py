"""add folders table and folder_id to decks

Revision ID: 0019_folders
Revises: 0018_ai_upload_generations
Create Date: 2026-04-06 18:05:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0019_folders'
down_revision = '0018_ai_upload_generations'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create folders table
    op.create_table(
        'folders',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('parent_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['parent_id'], ['folders.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id']),
    )

    # Create indexes
    op.create_index('ix_folders_parent_id', 'folders', ['parent_id'], unique=False)
    op.create_index('ix_folders_user_id', 'folders', ['user_id'], unique=False)
    op.create_index('ix_folders_organization_id', 'folders', ['organization_id'], unique=False)

    # Unique constraint: no duplicate sibling folder names for same user
    # Using partial index approach for nullable columns
    op.create_index(
        'ix_folders_parent_user_name',
        'folders',
        ['parent_id', 'user_id', 'name'],
        unique=True,
        postgresql_where=sa.text('parent_id IS NOT NULL'),
    )

    # Unique constraint: no duplicate sibling folder names at root level for same user
    op.create_index(
        'ix_folders_root_user_name',
        'folders',
        ['user_id', 'name'],
        unique=True,
        postgresql_where=sa.text('parent_id IS NULL'),
    )

    # Unique constraint: no duplicate sibling folder names for same organization
    op.create_index(
        'ix_folders_parent_org_name',
        'folders',
        ['parent_id', 'organization_id', 'name'],
        unique=True,
        postgresql_where=sa.text('parent_id IS NOT NULL AND organization_id IS NOT NULL'),
    )

    # Unique constraint: no duplicate sibling folder names at root level for same organization
    op.create_index(
        'ix_folders_root_org_name',
        'folders',
        ['organization_id', 'name'],
        unique=True,
        postgresql_where=sa.text('parent_id IS NULL AND organization_id IS NOT NULL'),
    )

    # Add folder_id to decks table
    op.add_column(
        'decks',
        sa.Column('folder_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index('ix_decks_folder_id', 'decks', ['folder_id'], unique=False)
    op.create_foreign_key(
        'fk_decks_folder_id',
        'decks',
        'folders',
        ['folder_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    # Remove foreign key and column from decks
    op.drop_constraint('fk_decks_folder_id', 'decks', type_='foreignkey')
    op.drop_index('ix_decks_folder_id', table_name='decks')
    op.drop_column('decks', 'folder_id')

    # Drop folder indexes
    op.drop_index('ix_folders_root_org_name', table_name='folders')
    op.drop_index('ix_folders_parent_org_name', table_name='folders')
    op.drop_index('ix_folders_root_user_name', table_name='folders')
    op.drop_index('ix_folders_parent_user_name', table_name='folders')
    op.drop_index('ix_folders_organization_id', table_name='folders')
    op.drop_index('ix_folders_user_id', table_name='folders')
    op.drop_index('ix_folders_parent_id', table_name='folders')

    # Drop folders table
    op.drop_table('folders')
