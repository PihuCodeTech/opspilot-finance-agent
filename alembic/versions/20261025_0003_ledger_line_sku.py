"""Ledger invoice lines keep the SKU as printed (duplicate check line sets).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Frozen datasets are immutable by trigger; adding a nullable column changes no rows.
    op.add_column("ledger_invoice_lines", sa.Column("sku", sa.Text(), nullable=True))
    op.execute(
        "CREATE OR REPLACE VIEW baseline_ledger_invoice_lines AS"
        " SELECT id, ledger_invoice_id, po_line_id, description, quantity, unit_price,"
        " line_total, sku FROM ledger_invoice_lines"
    )


def downgrade() -> None:
    # Dropping the column needs the view rebuilt without it (grants are re-applied).
    op.execute("DROP VIEW baseline_ledger_invoice_lines")
    op.execute(
        "CREATE VIEW baseline_ledger_invoice_lines AS"
        " SELECT id, ledger_invoice_id, po_line_id, description, quantity, unit_price, line_total"
        " FROM ledger_invoice_lines"
    )
    op.execute("GRANT SELECT ON baseline_ledger_invoice_lines TO opspilot_baseline_ro")
    op.execute(
        "GRANT SELECT ON baseline_ledger_invoice_lines TO opspilot_scorer, opspilot_verifier"
    )
    op.drop_column("ledger_invoice_lines", "sku")
