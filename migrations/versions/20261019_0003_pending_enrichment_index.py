"""pending enrichment index

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_liquidation_events_pending",
        "liquidation_events",
        ["kind", "block_number"],
        unique=False,
        postgresql_where=sa.text("NOT enriched"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_liquidation_events_pending",
        table_name="liquidation_events",
        postgresql_where=sa.text("NOT enriched"),
    )
