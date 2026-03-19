"""phase 2 foundation: orgs, roles, deck scope, tags

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-14

"""

from __future__ import annotations

import re
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

NAME_NORMALIZER_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(value: str) -> str:
    return NAME_NORMALIZER_RE.sub("", (value or "").strip().lower())


def unique_name(base: str, *, used: set[str]) -> str:
    candidate = base or "deck"
    suffix = 2
    while candidate in used:
        candidate = f"{base}{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_organizations_name"), "organizations", ["name"], unique=True)

    op.add_column("users", sa.Column("role", sa.String(length=50), nullable=True, server_default="user"))
    op.add_column(
        "users",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=True),
    )
    op.create_index(op.f("ix_users_organization_id"), "users", ["organization_id"], unique=False)

    op.add_column(
        "decks",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=True),
    )
    op.add_column("decks", sa.Column("normalized_name", sa.String(length=255), nullable=True))
    op.add_column("decks", sa.Column("is_global", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("decks", sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("decks", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_decks_organization_id"), "decks", ["organization_id"], unique=False)

    op.create_table(
        "tags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("normalized_name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_tags_organization_id"), "tags", ["organization_id"], unique=False)
    op.create_index("ux_tags_org_normalized_name", "tags", ["organization_id", "normalized_name"], unique=True)

    op.create_table(
        "deck_tags",
        sa.Column("deck_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("decks.id"), primary_key=True, nullable=False),
        sa.Column("tag_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tags.id"), primary_key=True, nullable=False),
    )

    bind = op.get_bind()

    existing_org_names: set[str] = set()
    user_rows = bind.execute(sa.text("SELECT id, email FROM users ORDER BY created_at ASC")).mappings().all()
    for row in user_rows:
        raw_name = f"Personal workspace {row['email'] or row['id']}"
        org_name = raw_name
        suffix = 2
        while org_name in existing_org_names:
            org_name = f"{raw_name} {suffix}"
            suffix += 1
        existing_org_names.add(org_name)

        org_id = uuid.uuid4()
        bind.execute(
            sa.text(
                """
                INSERT INTO organizations (id, name, created_at)
                VALUES (:id, :name, NOW())
                """
            ),
            {"id": org_id, "name": org_name},
        )
        bind.execute(
            sa.text(
                """
                UPDATE users
                SET role = 'admin', organization_id = :organization_id
                WHERE id = :user_id
                """
            ),
            {"organization_id": org_id, "user_id": row["id"]},
        )

        deck_rows = bind.execute(
            sa.text("SELECT id, name FROM decks WHERE user_id = :user_id ORDER BY created_at ASC"),
            {"user_id": row["id"]},
        ).mappings()
        used_names: set[str] = set()
        for deck_row in deck_rows:
            normalized = unique_name(normalize_name(deck_row["name"]), used=used_names)
            bind.execute(
                sa.text(
                    """
                    UPDATE decks
                    SET organization_id = :organization_id, normalized_name = :normalized_name
                    WHERE id = :deck_id
                    """
                ),
                {
                    "organization_id": org_id,
                    "normalized_name": normalized,
                    "deck_id": deck_row["id"],
                },
            )

    bind.execute(
        sa.text(
            """
            UPDATE decks
            SET normalized_name = 'deck'
            WHERE normalized_name IS NULL
            """
        )
    )

    op.alter_column("users", "role", nullable=False, existing_type=sa.String(length=50))
    op.alter_column("decks", "normalized_name", nullable=False, existing_type=sa.String(length=255))

    op.create_index(
        "ux_decks_global_normalized_name_active",
        "decks",
        ["normalized_name"],
        unique=True,
        postgresql_where=sa.text("is_global = true AND is_deleted = false"),
    )
    op.create_index(
        "ux_decks_org_normalized_name_active",
        "decks",
        ["organization_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("is_global = false AND is_deleted = false AND organization_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_decks_org_normalized_name_active", table_name="decks")
    op.drop_index("ux_decks_global_normalized_name_active", table_name="decks")

    op.drop_table("deck_tags")

    op.drop_index("ux_tags_org_normalized_name", table_name="tags")
    op.drop_index(op.f("ix_tags_organization_id"), table_name="tags")
    op.drop_table("tags")

    op.drop_index(op.f("ix_decks_organization_id"), table_name="decks")
    op.drop_column("decks", "deleted_at")
    op.drop_column("decks", "is_deleted")
    op.drop_column("decks", "is_global")
    op.drop_column("decks", "normalized_name")
    op.drop_column("decks", "organization_id")

    op.drop_index(op.f("ix_users_organization_id"), table_name="users")
    op.drop_column("users", "organization_id")
    op.drop_column("users", "role")

    op.drop_index(op.f("ix_organizations_name"), table_name="organizations")
    op.drop_table("organizations")
