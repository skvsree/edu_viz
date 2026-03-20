"""create default organization for non-org users and decks

Revision ID: 0007
Revises: 0006
Create Date: 2026-03-20

"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

DEFAULT_ORG_NAME = "Default Organization"


def upgrade() -> None:
    bind = op.get_bind()

    default_org = bind.execute(
        sa.text("SELECT id FROM organizations WHERE name = :name LIMIT 1"),
        {"name": DEFAULT_ORG_NAME},
    ).scalar_one_or_none()

    if default_org is None:
        default_org = uuid.uuid4()
        bind.execute(
            sa.text(
                """
                INSERT INTO organizations (id, name, is_ai_enabled, created_at)
                VALUES (:id, :name, false, NOW())
                """
            ),
            {"id": default_org, "name": DEFAULT_ORG_NAME},
        )

    bind.execute(
        sa.text(
            """
            UPDATE users
            SET organization_id = :organization_id
            WHERE organization_id IS NULL
            """
        ),
        {"organization_id": default_org},
    )

    bind.execute(
        sa.text(
            """
            UPDATE decks
            SET organization_id = :organization_id
            WHERE organization_id IS NULL AND is_global = false
            """
        ),
        {"organization_id": default_org},
    )


def downgrade() -> None:
    bind = op.get_bind()

    default_org = bind.execute(
        sa.text("SELECT id FROM organizations WHERE name = :name LIMIT 1"),
        {"name": DEFAULT_ORG_NAME},
    ).scalar_one_or_none()

    if default_org is None:
        return

    bind.execute(
        sa.text(
            """
            UPDATE decks
            SET organization_id = NULL
            WHERE organization_id = :organization_id AND is_global = false
            """
        ),
        {"organization_id": default_org},
    )

    bind.execute(
        sa.text(
            """
            UPDATE users
            SET organization_id = NULL
            WHERE organization_id = :organization_id
            """
        ),
        {"organization_id": default_org},
    )

    bind.execute(
        sa.text("DELETE FROM organizations WHERE id = :organization_id"),
        {"organization_id": default_org},
    )
