"""Roles, grants, baseline views and integrity triggers (06-database.md §5-§7).

Roles are created here without LOGIN and without passwords; `opspilot db
provision-roles` (run as the owner after migrations) enables login with the
passwords from the environment, so no secret is ever stored in a migration.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

from opspilot.db.models import APPEND_ONLY_TABLES

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLES = ("opspilot_app", "opspilot_baseline_ro", "opspilot_scorer", "opspilot_verifier")

# Operational tables the application changes freely (no history kept in the row itself).
APP_READ_WRITE = (
    "users",
    "sessions",
    "documents",
    "invoices",
    "cases",
    "investigation_runs",
    "jobs",
    "eval_runs",
    "eval_item_results",
)
# Simulated ERP data: written by the generator (scorer) and by demo uploads (app);
# frozen datasets are protected by trigger whatever the grant.
ERP_TABLES = (
    "datasets",
    "vendors",
    "vendor_bank_accounts",
    "purchase_orders",
    "po_lines",
    "receipts",
    "receipt_lines",
    "ledger_invoices",
    "ledger_invoice_lines",
)
READ_ONLY_FOR_APP = ("policy_versions", "policy_chunks", "alembic_version")
DATASET_ITEM_COLUMNS_WITHOUT_LABELS = "id, dataset_id, invoice_id, document_id, created_at"

# Frozen-dataset guard: table -> how to reach its dataset ('' = has dataset_id itself).
FROZEN_GUARDED = {
    "vendors": "",
    "purchase_orders": "",
    "receipts": "",
    "ledger_invoices": "",
    "invoices": "",
    "dataset_items": "",
    "vendor_bank_accounts": "vendor_id:vendors",
    "po_lines": "po_id:purchase_orders",
    "receipt_lines": "receipt_id:receipts",
    "ledger_invoice_lines": "ledger_invoice_id:ledger_invoices",
}

BASELINE_VIEWS = {
    "baseline_vendors": """
        SELECT v.id, v.dataset_id, v.vendor_code, v.name, v.address, v.tax_rate, v.is_approved,
               b.bank_fp AS current_bank_fp, b.last4 AS current_bank_last4
          FROM vendors v
          LEFT JOIN vendor_bank_accounts b ON b.vendor_id = v.id AND b.valid_to IS NULL""",
    "baseline_purchase_orders": """
        SELECT id, dataset_id, po_number, vendor_id, approval_level, controller_approved,
               currency, issued_at
          FROM purchase_orders""",
    "baseline_po_lines": """
        SELECT id, po_id, line_no, sku, description, quantity, unit_price FROM po_lines""",
    "baseline_receipts": """
        SELECT id, dataset_id, receipt_number, po_id, received_at FROM receipts""",
    "baseline_receipt_lines": """
        SELECT id, receipt_id, po_line_id, quantity_received FROM receipt_lines""",
    "baseline_ledger_invoices": """
        SELECT id, dataset_id, vendor_id, po_id, invoice_number, invoice_number_norm,
               invoice_date, subtotal, tax, total
          FROM ledger_invoices""",
    "baseline_ledger_invoice_lines": """
        SELECT id, ledger_invoice_id, po_line_id, description, quantity, unit_price, line_total
          FROM ledger_invoice_lines""",
    # The same non-LLM preprocessed text OpsPilot reads (C-19), never OpsPilot's extraction.
    "baseline_invoices": """
        SELECT i.id, i.dataset_id, i.received_at, p.parser_version, p.text AS parsed_text
          FROM invoices i
          JOIN parsed_documents p ON p.document_id = i.document_id""",
}


def _create_roles() -> None:
    for role in RUNTIME_ROLES:
        op.execute(
            f"""DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} NOLOGIN;
                  END IF;
                END $$;"""
        )
    op.execute("ALTER ROLE opspilot_baseline_ro SET statement_timeout = '5s'")
    op.execute("ALTER ROLE opspilot_verifier SET default_transaction_read_only = on")


def _grants() -> None:
    op.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    for role in RUNTIME_ROLES:
        op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")

    # Application (and evaluation inference).
    app_rw = ", ".join(APP_READ_WRITE + ERP_TABLES)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {app_rw} TO opspilot_app")
    op.execute(f"GRANT SELECT, INSERT ON {', '.join(APPEND_ONLY_TABLES)} TO opspilot_app")
    op.execute(f"GRANT SELECT ON {', '.join(READ_ONLY_FOR_APP)} TO opspilot_app")
    # Labels are never readable during inference (rules.md §2, C-22).
    op.execute(
        f"GRANT SELECT ({DATASET_ITEM_COLUMNS_WITHOUT_LABELS}) ON dataset_items TO opspilot_app"
    )

    # Scorer: dataset generation and scoring; the only runtime role that reads labels.
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO opspilot_scorer")
    scorer_rw = ", ".join((*ERP_TABLES, "dataset_items", "documents", "invoices"))
    op.execute(f"GRANT INSERT, UPDATE, DELETE ON {scorer_rw} TO opspilot_scorer")

    # Verifier: read-only everything audit verification needs.
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO opspilot_verifier")

    op.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO opspilot_app, opspilot_scorer"
    )


def _baseline_views() -> None:
    for name, query in BASELINE_VIEWS.items():
        op.execute(f"CREATE VIEW {name} AS {query}")
    op.execute(f"GRANT SELECT ON {', '.join(BASELINE_VIEWS)} TO opspilot_baseline_ro")
    op.execute(f"GRANT SELECT ON {', '.join(BASELINE_VIEWS)} TO opspilot_scorer, opspilot_verifier")


def _append_only_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_modification() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'table % is append-only (% refused)', TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'insufficient_privilege';
        END $$;
        """
    )
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_modification()"
        )


def _frozen_dataset_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION guard_frozen_dataset() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          spec text := TG_ARGV[0];
          rec jsonb;
          ds uuid;
          frozen timestamptz;
        BEGIN
          FOR rec IN SELECT x FROM (VALUES
              (CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) END),
              (CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN to_jsonb(NEW) END)) AS t(x)
              WHERE x IS NOT NULL
          LOOP
            IF spec = '' THEN
              ds := (rec ->> 'dataset_id')::uuid;
            ELSE
              EXECUTE format('SELECT dataset_id FROM %I WHERE id = $1', split_part(spec, ':', 2))
                INTO ds USING (rec ->> split_part(spec, ':', 1))::uuid;
            END IF;
            SELECT frozen_at INTO frozen FROM datasets WHERE id = ds;
            IF frozen IS NOT NULL THEN
              RAISE EXCEPTION 'dataset % is frozen; % on % refused', ds, TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'insufficient_privilege';
            END IF;
          END LOOP;
          RETURN COALESCE(NEW, OLD);
        END $$;
        """
    )
    for table, spec in FROZEN_GUARDED.items():
        op.execute(
            f"CREATE TRIGGER {table}_frozen_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION guard_frozen_dataset('{spec}')"
        )
    op.execute(
        """
        CREATE FUNCTION guard_dataset_freeze() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.frozen_at IS NOT NULL THEN
            RAISE EXCEPTION 'dataset % is frozen and can''t be changed or deleted', OLD.id
              USING ERRCODE = 'insufficient_privilege';
          END IF;
          RETURN COALESCE(NEW, OLD);
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER datasets_frozen_guard BEFORE UPDATE OR DELETE ON datasets "
        "FOR EACH ROW EXECUTE FUNCTION guard_dataset_freeze()"
    )


def _reference_triggers() -> None:
    """Cross-row rules a CHECK constraint can't express."""
    op.execute(
        """
        CREATE FUNCTION check_decision_reference() RETURNS trigger LANGUAGE plpgsql AS $$
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
          IF TG_TABLE_NAME = 'executed_actions' AND decision_case <> NEW.case_id THEN
            RAISE EXCEPTION 'executed action and decision belong to different cases'
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    for table, kind in (
        ("executed_actions", "approve"),
        ("action_reversals", "reopen"),
        ("decision_resolution_acceptances", "approve"),
    ):
        op.execute(
            f"CREATE TRIGGER {table}_decision_kind BEFORE INSERT ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION check_decision_reference('{kind}')"
        )

    op.execute(
        """
        CREATE FUNCTION check_supporting_document() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (SELECT kind FROM documents WHERE id = NEW.document_id) <> 'supporting' THEN
            RAISE EXCEPTION 'supporting documents must be stored with kind supporting'
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER supporting_documents_kind BEFORE INSERT ON supporting_documents
          FOR EACH ROW EXECUTE FUNCTION check_supporting_document();

        CREATE FUNCTION check_bank_verification() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          evidence_kind supporting_kind;
          evidence_case uuid;
        BEGIN
          SELECT kind, case_id INTO evidence_kind, evidence_case
            FROM supporting_documents WHERE id = NEW.evidence_supporting_document_id;
          IF evidence_kind IS DISTINCT FROM 'bank_verification' OR evidence_case <> NEW.case_id THEN
            RAISE EXCEPTION 'bank verification evidence must be a bank_verification document of the same case'
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER bank_verifications_evidence BEFORE INSERT ON bank_verifications
          FOR EACH ROW EXECUTE FUNCTION check_bank_verification();

        CREATE FUNCTION check_finding_resolution() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          finding_kind finding_type;
          finding_case uuid;
        BEGIN
          SELECT exception_type, case_id INTO finding_kind, finding_case
            FROM findings WHERE id = NEW.finding_id;
          IF finding_kind IN ('EXTRACTION_UNCERTAIN', 'MISSING_OR_UNKNOWN_PO', 'FORENSICS_SIGNAL') THEN
            RAISE EXCEPTION 'finding type % can''t be resolved by a record (recheck or no resolution)',
              finding_kind USING ERRCODE = 'check_violation';
          END IF;
          IF finding_case <> NEW.case_id THEN
            RAISE EXCEPTION 'resolution and finding belong to different cases'
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER finding_resolutions_resolvable BEFORE INSERT ON finding_resolutions
          FOR EACH ROW EXECUTE FUNCTION check_finding_resolution();
        """
    )


def upgrade() -> None:
    _create_roles()
    _grants()
    _baseline_views()
    _append_only_triggers()
    _frozen_dataset_triggers()
    _reference_triggers()


def downgrade() -> None:
    for name in (
        "finding_resolutions_resolvable ON finding_resolutions",
        "bank_verifications_evidence ON bank_verifications",
        "supporting_documents_kind ON supporting_documents",
        "executed_actions_decision_kind ON executed_actions",
        "action_reversals_decision_kind ON action_reversals",
        "decision_resolution_acceptances_decision_kind ON decision_resolution_acceptances",
        "datasets_frozen_guard ON datasets",
        *(f"{t}_frozen_guard ON {t}" for t in FROZEN_GUARDED),
        *(f"{t}_append_only ON {t}" for t in APPEND_ONLY_TABLES),
        *(f"{t}_no_truncate ON {t}" for t in APPEND_ONLY_TABLES),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    for function in (
        "check_finding_resolution()",
        "check_bank_verification()",
        "check_supporting_document()",
        "check_decision_reference()",
        "guard_dataset_freeze()",
        "guard_frozen_dataset()",
        "reject_modification()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
    for view in BASELINE_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {view}")
    # Roles are cluster-wide and may serve other databases: revoke here, never drop.
    for role in RUNTIME_ROLES:
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role}")
        op.execute(f"REVOKE USAGE ON SCHEMA public FROM {role}")
