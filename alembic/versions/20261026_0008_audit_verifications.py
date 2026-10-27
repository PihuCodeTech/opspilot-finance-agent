"""Recorded `audit verify` results, append-only (owner decision on P11, 2026-10-26).

The verifier role stays read-only: it reads the chains and digests, and the
app role records the result (INSERT only; UPDATE, DELETE and TRUNCATE are
refused by the shared `reject_modification` trigger from 0002).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "audit_verifications"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_as", sa.Text(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("chains_ok", sa.Boolean(), nullable=False),
        sa.Column("digests_ok", sa.Boolean(), nullable=False),
        sa.Column("chains", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("digests_checked", sa.Integer(), nullable=False),
        sa.Column("failures", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("ok = (chains_ok AND digests_ok)", name=op.f(f"ck_{TABLE}_ok_is_both")),
        sa.CheckConstraint(
            "finished_at >= started_at", name=op.f(f"ck_{TABLE}_finished_after_start")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
    )
    op.create_index(
        "ix_audit_verifications_latest", TABLE, [sa.text("finished_at DESC")], unique=False
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_append_only BEFORE UPDATE OR DELETE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION reject_modification()"
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_no_truncate BEFORE TRUNCATE ON {TABLE} "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_modification()"
    )
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO opspilot_app")
    op.execute(f"GRANT SELECT ON {TABLE} TO opspilot_verifier, opspilot_scorer")


def downgrade() -> None:
    op.drop_table(TABLE)
