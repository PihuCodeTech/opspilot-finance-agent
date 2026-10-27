"""Fix check_decision_reference() for tables without a case_id column.

The 0002 function compared `NEW.case_id` inside one `AND` expression. PL/pgSQL
resolves every record field in an expression, so inserts into
`decision_resolution_acceptances` and `action_reversals` (which have no
`case_id`) always failed. Found by the P8 audit tests. The function is replaced;
no table or row changes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BODY = """
CREATE OR REPLACE FUNCTION check_decision_reference() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  expected text := TG_ARGV[0];
  actual text;
  decision_case uuid;
BEGIN
  SELECT kind::text, case_id INTO actual, decision_case
    FROM decisions WHERE id = NEW.decision_id;
  IF actual IS DISTINCT FROM expected THEN
    RAISE EXCEPTION '% requires a % decision (got %)', TG_TABLE_NAME, expected, actual
      USING ERRCODE = 'check_violation';
  END IF;
  IF TG_TABLE_NAME = 'executed_actions' THEN
    IF decision_case IS DISTINCT FROM NEW.case_id THEN
      RAISE EXCEPTION 'executed action and decision belong to different cases'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
"""


def upgrade() -> None:
    op.execute(_BODY)


def downgrade() -> None:
    # The 0002 definition, kept verbatim so a downgrade restores exactly what was there.
    op.execute(
        _BODY.replace(
            """  IF TG_TABLE_NAME = 'executed_actions' THEN
    IF decision_case IS DISTINCT FROM NEW.case_id THEN
      RAISE EXCEPTION 'executed action and decision belong to different cases'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;""",
            """  IF TG_TABLE_NAME = 'executed_actions' AND decision_case <> NEW.case_id THEN
    RAISE EXCEPTION 'executed action and decision belong to different cases'
      USING ERRCODE = 'check_violation';
  END IF;""",
        )
    )
