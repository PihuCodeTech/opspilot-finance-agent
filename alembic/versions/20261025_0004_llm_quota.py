"""Persisted LLM quota counters (rules.md §2 rule 10: enforce provider quotas).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_quota_windows",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("window_kind", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "window_kind IN ('minute', 'day')", name=op.f("ck_llm_quota_windows_window_kind")
        ),
        sa.CheckConstraint("requests >= 0", name=op.f("ck_llm_quota_windows_non_negative")),
        sa.PrimaryKeyConstraint(
            "provider", "model", "window_kind", "window_start", name=op.f("pk_llm_quota_windows")
        ),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON llm_quota_windows TO opspilot_app")
    op.execute("GRANT SELECT ON llm_quota_windows TO opspilot_scorer, opspilot_verifier")


def downgrade() -> None:
    op.drop_table("llm_quota_windows")
