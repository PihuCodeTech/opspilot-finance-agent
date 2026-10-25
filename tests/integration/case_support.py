"""Cases in a known state for service tests, built through the real service calls.

A scenario lists the TruthLens check results an investigation produced (shaped
exactly as `truthlens/checks.py` records them). The builder runs T1, T2, the
agent proposal and T4, so every case reaches `needs_review` the way a real one
does, with the audit trail and evidence digest written.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, text

from opspilot.cases import service
from opspilot.domain.bank import fingerprint
from opspilot.domain.enums import (
    ActionType,
    CheckCode,
    CheckStatus,
    FindingCode,
    SupportingKind,
)
from opspilot.policy.loader import load_policy
from opspilot.truthlens.findings import EXCEPTION_FOR, SEVERITY
from tests.integration import factories as f

BANK_KEY = "test-bank-key-0000000000000000000000"
MASTER_ACCOUNT = "GB29NWBK60161331926819"
CHANGED_ACCOUNT = "GB94BARC10201530093459"
INVOICE_NUMBER = "INV-2026-0042"
LEDGER_NUMBER = "INV-2026-0017"


@dataclass(frozen=True)
class Check:
    code: str  # CheckCode value
    status: CheckStatus
    compared: dict[str, Any]
    subject: str = "header"
    refs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class World:
    conn: Connection
    analyst: uuid.UUID
    analyst2: uuid.UUID
    approver: uuid.UUID  # standard, limit 10,000
    controller: uuid.UUID
    auditor: uuid.UUID
    dataset: uuid.UUID
    vendor: uuid.UUID
    policy_version: uuid.UUID
    document_text: str = f"Invoice {INVOICE_NUMBER}"

    def ledger(self, number: str = LEDGER_NUMBER, date: str = "2026-09-01") -> uuid.UUID:
        ledger_id: uuid.UUID = self.conn.execute(
            text(
                "INSERT INTO ledger_invoices (dataset_id, vendor_id, invoice_number, invoice_date,"
                " subtotal, tax, total) VALUES (:d, :v, :n, :dt, 100, 8.25, 108.25) RETURNING id"
            ),
            {"d": self.dataset, "v": self.vendor, "n": number, "dt": date},
        ).scalar_one()
        return ledger_id

    def supporting(self, case_id: uuid.UUID, kind: SupportingKind) -> uuid.UUID:
        document = f.document(self.conn, "supporting")
        return service.add_supporting_document(
            self.conn,
            self.analyst,
            case_id,
            document_id=document,
            kind=kind,
            description=f"{kind} on file",
        )

    def version(self, case_id: uuid.UUID) -> int:
        return int(
            self.conn.execute(
                text("SELECT version FROM cases WHERE id = :c"), {"c": case_id}
            ).scalar_one()
        )

    def status(self, case_id: uuid.UUID) -> str:
        return str(
            self.conn.execute(
                text("SELECT status::text FROM cases WHERE id = :c"), {"c": case_id}
            ).scalar_one()
        )

    def finding(self, case_id: uuid.UUID, code: FindingCode) -> uuid.UUID:
        finding_id: uuid.UUID = self.conn.execute(
            text(
                "SELECT f.id FROM findings f JOIN cases c ON c.evidence_run_id = f.run_id"
                " WHERE c.id = :c AND f.exception_type = CAST(:t AS finding_type)"
            ),
            {"c": case_id, "t": code.value},
        ).scalar_one()
        return finding_id

    def case(self, checks: list[Check], *, agent_action: ActionType | None = None) -> uuid.UUID:
        """A case in `needs_review` whose evidence run produced these checks."""
        invoice = f.invoice(self.conn, self.dataset)
        case_id = service.create_case(self.conn, self.analyst, invoice)
        run_id = service.start_investigation(self.conn, case_id)
        self.finish(case_id, run_id, checks, agent_action=agent_action)
        return case_id

    def finish(
        self,
        case_id: uuid.UUID,
        run_id: uuid.UUID,
        checks: list[Check],
        *,
        agent_action: ActionType | None = None,
    ) -> None:
        """What the agent graph will do (P10): write the run's evidence, propose, then T4."""
        vendor_check = Check(
            "vendor_approved",
            CheckStatus.PASS,
            {"vendor_code": "V-0001", "approved": True},
            refs=[{"entity": "vendor", "id": str(self.vendor), "field": "is_approved"}],
        )
        has_vendor = any(c.code == "vendor_approved" for c in checks)
        all_checks = checks if has_vendor else [vendor_check, *checks]
        for check in all_checks:
            self._check(case_id, run_id, check)
        exceptions = any(c.status is not CheckStatus.PASS for c in all_checks)
        action = agent_action or (
            ActionType.PLACE_ON_HOLD if exceptions else ActionType.APPROVE_FOR_PAYMENT
        )
        service.record_agent_proposal(self.conn, run_id, action)
        document = self.conn.execute(
            text(
                "SELECT i.document_id FROM invoices i JOIN cases c ON c.invoice_id = i.id"
                " WHERE c.id = :c"
            ),
            {"c": case_id},
        ).scalar_one()
        parsed = self.conn.execute(
            text(
                "INSERT INTO parsed_documents (document_id, parser_version, text, structure,"
                " coordinates) VALUES (:d, :v, :t, '{}', '{}') RETURNING id"
            ),
            {"d": document, "v": f"test-{run_id}", "t": self.document_text},
        ).scalar_one()
        service.run_succeeded(self.conn, run_id, parsed)

    def _check(self, case_id: uuid.UUID, run_id: uuid.UUID, check: Check) -> None:
        inconclusive = check.status is CheckStatus.INCONCLUSIVE
        check_id = self.conn.execute(
            text(
                "INSERT INTO check_results (run_id, check_code, subject_key, status, compared,"
                " evidence_refs, inconclusive_reason) VALUES (:r, CAST(:c AS check_code), :s,"
                " CAST(:st AS check_status), CAST(:cmp AS jsonb), CAST(:refs AS jsonb), :why)"
                " RETURNING id"
            ),
            {
                "r": run_id,
                "c": check.code,
                "s": check.subject,
                "st": check.status.value,
                "cmp": json.dumps(check.compared),
                "refs": json.dumps(check.refs),
                "why": "low-confidence reading" if inconclusive else None,
            },
        ).scalar_one()
        if check.status is CheckStatus.PASS:
            return
        code = (
            FindingCode.EXTRACTION_UNCERTAIN
            if inconclusive
            else EXCEPTION_FOR[CheckCode(check.code)]
        )
        finding_id = self.conn.execute(
            text(
                "INSERT INTO findings (run_id, case_id, exception_type, subject_key, severity,"
                " explanation, grounding_status) VALUES (:r, :c, CAST(:t AS finding_type), :s,"
                " CAST(:sev AS severity), 'explained', 'ok') RETURNING id"
            ),
            {
                "r": run_id,
                "c": case_id,
                "t": code.value,
                "s": check.subject,
                "sev": SEVERITY[code].value,
            },
        ).scalar_one()
        self.conn.execute(
            text("INSERT INTO finding_check_results VALUES (:f, :c)"),
            {"f": finding_id, "c": check_id},
        )


def make_world(conn: Connection) -> World:
    """Fresh users and ERP rows; approvers from other tests are deactivated (rolled back)."""
    conn.execute(text("UPDATE users SET is_active = false WHERE role = 'approver'"))
    policy = load_policy()
    active = conn.execute(text("SELECT id FROM policy_versions WHERE is_active")).scalar()
    if active is None:
        active = conn.execute(
            text(
                "INSERT INTO policy_versions (version, config, config_sha256, document,"
                " document_sha256, is_active) VALUES (:v, CAST(:c AS jsonb), :h, 'doc', :h, true)"
                " RETURNING id"
            ),
            {
                "v": f"test-{uuid.uuid4().hex[:8]}",
                "c": json.dumps(policy.config.model_dump(mode="json")),
                "h": policy.config_sha256,
            },
        ).scalar_one()
    dataset = f.dataset(conn)
    return World(
        conn=conn,
        analyst=f.user(conn, "analyst"),
        analyst2=f.user(conn, "analyst"),
        approver=f.user(conn, "approver", approval_limit=10000),
        controller=f.user(conn, "approver", approval_unlimited=True),
        auditor=f.user(conn, "auditor"),
        dataset=dataset,
        vendor=f.vendor(conn, dataset),
        policy_version=active,
    )


# ---------------------------------------------------------------- check results (TruthLens shape)


def approval(amount: str = "5000.00", status: CheckStatus = CheckStatus.PASS) -> Check:
    return Check("approval_level", status, {"approval_amount": amount, "po_approval_level": 1})


def price(invoice: str = "10.50", po: str = "10.00", qty: str = "100") -> Check:
    return Check(
        "price",
        CheckStatus.FAIL,
        {
            "invoice_unit_price": invoice,
            "invoice_quantity": qty,
            "po_unit_price": po,
            "variance_pct": "5.00",
            "undercharge_info": False,
        },
        subject="line:1",
    )


def duplicate(
    ledger_id: uuid.UUID, *, same_lines: bool = False, ledger_date: str = "2026-09-01"
) -> Check:
    return Check(
        "duplicate",
        CheckStatus.FAIL,
        {
            "invoice_number": INVOICE_NUMBER,
            "normalised": "inv20260042",
            "invoice_date": "2026-10-20",
            "invoice_po_number": "PO-1",
            "matches": [
                {
                    "ledger_invoice": LEDGER_NUMBER,
                    "ledger_invoice_id": str(ledger_id),
                    "basis": "near_total_date_lines",
                    "total": "108.25",
                    "invoice_date": ledger_date,
                    "same_lines": same_lines,
                }
            ],
        },
        refs=[{"entity": "ledger_invoice", "id": str(ledger_id), "field": "invoice_number"}],
    )


def bank_change() -> Check:
    return Check(
        "bank_details",
        CheckStatus.FAIL,
        {
            "invoice_bank_fp": fingerprint(CHANGED_ACCOUNT, BANK_KEY).bank_fp,
            "vendor_master_bank_fp": fingerprint(MASTER_ACCOUNT, BANK_KEY).bank_fp,
        },
    )


def inconclusive(code: str = "tax") -> Check:
    return Check(code, CheckStatus.INCONCLUSIVE, {"uncertain_fields": ["tax"]})


def missing_po() -> Check:
    return Check("po_lookup", CheckStatus.FAIL, {"invoice_po_number": None, "result": "absent"})


def overstated_tax(difference: str = "4.00") -> Check:
    return Check(
        "tax",
        CheckStatus.FAIL,
        {"stated_tax": "12.25", "computed_tax": "8.25", "difference": difference},
    )
