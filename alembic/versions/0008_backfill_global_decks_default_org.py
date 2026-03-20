"""backfill global decks into default organization

Revision ID: 0008
Revises: 0007
Create Date: 2026-03-20

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
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
        return

    bind.execute(
        sa.text(
            """
            UPDATE decks
            SET organization_id = :organization_id
            WHERE organization_id IS NULL
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
            WHERE organization_id = :organization_id AND is_global = true
            """
        ),
        {"organization_id": default_org},
    )
