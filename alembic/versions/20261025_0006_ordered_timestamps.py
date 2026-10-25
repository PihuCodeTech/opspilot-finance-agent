"""Order-preserving insertion times for proposals, decisions and resolutions.

The case service treats the latest proposal, resolution and decision as the
current one. `now()` is the transaction's start time, so two rows written in
one transaction tied and the "latest" was decided by a random UUID. Found by
the P9 service tests. Only column defaults change; no rows change.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("proposals", "decisions", "finding_resolutions")


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN created_at SET DEFAULT clock_timestamp()")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN created_at SET DEFAULT now()")
