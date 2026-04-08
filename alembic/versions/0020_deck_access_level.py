"""Add access_level to decks and deck_accesses table.

Revision ID: 0020_deck_access_level
Revises: 0019_folders
Create Date: 2026-04-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, ENUM

revision = "0020_deck_access_level"
down_revision = "0019_folders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum types explicitly once.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_type WHERE typname = 'deck_access_level' AND typtype = 'e'
            ) THEN
                CREATE TYPE deck_access_level AS ENUM ('none', 'read', 'write', 'delete');
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM pg_type WHERE typname = 'deck_scope' AND typtype = 'e'
            ) THEN
                CREATE TYPE deck_scope AS ENUM ('global', 'org', 'user');
            END IF;
        END $$;
        """
    )

    # Create deck_accesses using existing enum type; avoid auto create/drop behavior.
    deck_access_enum = ENUM(
        'none', 'read', 'write', 'delete',
        name='deck_access_level',
        create_type=False,
    )
    deck_scope_enum = ENUM(
        'global', 'org', 'user',
        name='deck_scope',
        create_type=False,
    )

    op.create_table(
        'deck_accesses',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'deck_id',
            UUID(as_uuid=True),
            sa.ForeignKey('decks.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'user_id',
            UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'access_level',
            deck_access_enum,
            nullable=False,
            server_default='read',
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('deck_id', 'user_id', name='uq_deck_access_deck_user'),
    )
    op.create_index('ix_deck_accesses_user', 'deck_accesses', ['user_id'])
    op.create_index('ix_deck_accesses_deck', 'deck_accesses', ['deck_id'])

    op.add_column(
        'decks',
        sa.Column(
            'access_level',
            deck_scope_enum,
            nullable=True,
        ),
    )

    op.execute(
        """
        UPDATE decks
        SET access_level = CASE
            WHEN is_global = true THEN 'global'::deck_scope
            WHEN organization_id IS NOT NULL THEN 'org'::deck_scope
            ELSE 'user'::deck_scope
        END
        """
    )

    op.alter_column('decks', 'access_level', nullable=False)

    op.drop_index('ux_decks_global_normalized_name_active', table_name='decks')
    op.drop_index('ux_decks_org_normalized_name_active', table_name='decks')

    op.create_index(
        'ux_decks_global_normalized_name_active',
        'decks',
        ['normalized_name'],
        unique=True,
        postgresql_where=sa.text("access_level = 'global' AND is_deleted = false"),
    )
    op.create_index(
        'ux_decks_org_normalized_name_active',
        'decks',
        ['organization_id', 'normalized_name'],
        unique=True,
        postgresql_where=sa.text("access_level = 'org' AND is_deleted = false"),
    )
    op.create_index(
        'ix_decks_user_normalized_name_active',
        'decks',
        ['user_id', 'normalized_name'],
        unique=True,
        postgresql_where=sa.text("access_level = 'user' AND is_deleted = false"),
    )

    op.execute("UPDATE decks SET is_global = (access_level = 'global')")


def downgrade() -> None:
    op.drop_index('ix_decks_user_normalized_name_active', table_name='decks')
    op.drop_index('ux_decks_org_normalized_name_active', table_name='decks')
    op.drop_index('ux_decks_global_normalized_name_active', table_name='decks')

    # Restore pre-migration indexes.
    op.create_index(
        'ux_decks_global_normalized_name_active',
        'decks',
        ['normalized_name'],
        unique=True,
        postgresql_where=sa.text('is_global = true AND is_deleted = false'),
    )
    op.create_index(
        'ux_decks_org_normalized_name_active',
        'decks',
        ['organization_id', 'normalized_name'],
        unique=True,
        postgresql_where=sa.text('organization_id IS NOT NULL AND is_global = false AND is_deleted = false'),
    )

    op.drop_column('decks', 'access_level')
    op.drop_index('ix_deck_accesses_deck', table_name='deck_accesses')
    op.drop_index('ix_deck_accesses_user', table_name='deck_accesses')
    op.drop_table('deck_accesses')

    op.execute('DROP TYPE IF EXISTS deck_scope')
    op.execute('DROP TYPE IF EXISTS deck_access_level')
