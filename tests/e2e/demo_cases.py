"""Realistic live cases for the browser journeys and the design review (synthetic data only).

Each invoice is a real text PDF stored by hash, driven through the case service
(T1, T2, the agent proposal, T4, then resolutions, submits, approvals and the
executor), with the rows the agent graph writes: extraction, line matches, check
results in TruthLens's own shapes, findings, citations and a parse with
coordinates, so previews and highlights work. Names, numbers and accounts are
invented; nothing comes from the held-out datasets.
"""

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from opspilot.actions.execute import execute
from opspilot.audit.verify import record, verify
from opspilot.cases import service
from opspilot.cases.resolutions import EvidenceEntry
from opspilot.db.seed import seed
from opspilot.domain.bank import fingerprint
from opspilot.domain.enums import ActionType, ResolutionType, SupportingKind
from opspilot.extraction.preprocess import PARSER_VERSION
from opspilot.policy.loader import load_policy
from tests.integration.web_support import text_pdf

BANK_KEY = "demo-bank-key-for-ui-review-000000000000"


@dataclass
class Line:
    description: str
    sku: str
    quantity: str
    unit_price: str  # as invoiced
    po_price: str
    received: str

    @property
    def total(self) -> str:
        return f"{float(self.quantity) * float(self.unit_price):.2f}"


@dataclass
class Spec:
    key: str
    vendor: str
    number: str
    lines: list[Line]
    tax_rate: str = "0.0825"
    account: str = "GB29 NWBK 6016 1331 9268 19"
    checks: list[dict[str, Any]] = field(default_factory=list)  # failing or inconclusive only

    def subtotal(self) -> float:
        return round(sum(float(line.total) for line in self.lines), 2)

    def tax(self) -> float:
        return round(self.subtotal() * float(self.tax_rate), 2)

    def total(self) -> float:
        return round(self.subtotal() + self.tax(), 2)


@dataclass
class Demo:
    users: dict[str, uuid.UUID]
    cases: dict[str, uuid.UUID]
    password: str


VENDORS = {
    "Halden Industrial Supply": ("V-1001", "14 Quay Street, Leith", True),
    "Porter-Castillo": ("V-1002", "Unit 8, Dock Road, Riverton", True),
    "Marsh & Kettle Ltd": ("V-1003", "2 Mill Lane, Ashby", True),
    "Ostrava Tooling": ("V-1004", "Prumyslova 11, Ostrava", True),
}


def _one(conn: Connection, sql: str, **params: Any) -> Any:
    return conn.execute(text(sql), params).scalar_one()


def _pdf(spec: Spec, po_number: str) -> tuple[bytes, list[dict[str, Any]]]:
    """The invoice as a real text PDF, and the parse coordinates Docling would record."""
    rows: list[tuple[str, float, float]] = [
        ("INVOICE", 72, 740),
        (spec.vendor, 72, 716),
        (f"Invoice number {spec.number}", 72, 690),
        (f"Purchase order {po_number}", 72, 672),
    ]
    y = 630.0
    for line in spec.lines:
        rows.append((f"{line.description}  {line.sku}", 72, y))
        rows.append((f"{line.quantity} x {line.unit_price}", 330, y))
        rows.append((line.total, 470, y))
        y -= 20
    rows += [
        (f"Subtotal {spec.subtotal():.2f}", 330, y - 20),
        (f"Tax {spec.tax():.2f}", 330, y - 40),
        (f"Total due {spec.total():.2f}", 330, y - 60),
        (f"Pay to account: {spec.account}", 72, y - 100),
    ]
    fp = fingerprint(spec.account, BANK_KEY)
    items = []
    for content, x, top in rows:
        shown = f"Pay to account: {fp.token()}" if content.startswith("Pay to account") else content
        for part in shown.split("  ") if "  " in shown else [shown]:
            items.append(
                {
                    "text": part,
                    "page": 1,
                    "bbox": [x, top + 12, x + 6.2 * len(content), top - 3],
                    "origin": "BOTTOMLEFT",
                }
            )
    cells = [
        {
            "text": value,
            "page": 1,
            "bbox": [x + offset, top + 12, x + offset + 6.2 * len(value), top - 3],
            "origin": "BOTTOMLEFT",
        }
        for content, x, top in rows
        if " x " in content
        for value, offset in ((content.split(" x ")[1], 6.2 * (len(content.split(" x ")[0]) + 3)),)
    ]
    return text_pdf(*rows), [{"items": items, "table_cells": cells}]


def _citation(conn: Connection, words: str) -> tuple[uuid.UUID, str] | None:
    row = conn.execute(
        text(
            "SELECT c.id, c.text FROM policy_chunks c JOIN policy_versions v"
            " ON v.id = c.policy_version_id WHERE v.is_active AND c.text ILIKE :w"
            " ORDER BY c.section_id LIMIT 1"
        ),
        {"w": f"%{words}%"},
    ).first()
    if row is None:
        return None
    sentences = row.text.split(". ")  # whole sentences only, as the agent quotes them
    return row.id, ". ".join(sentences[:2]).rstrip(".") + "."


FINDING_FOR = {
    "price": ("PRICE_MISMATCH", "medium", "unit price"),
    "duplicate": ("DUPLICATE_INVOICE", "high", "duplicate"),
    "bank_details": ("BANK_DETAILS_CHANGED", "high", "bank"),
    "tax": ("TAX_MISCALCULATED", "low", "tax"),
}


class Builder:
    def __init__(self, engine: Engine, store: Path) -> None:
        self.engine = engine
        self.store = store

    def world(self, conn: Connection) -> dict[str, Any]:
        dataset = _one(
            conn,
            "INSERT INTO datasets (name, version, split) VALUES ('demo', :v, 'demo') RETURNING id",
            v=f"ui-{uuid.uuid4().hex[:6]}",
        )
        vendors = {}
        for name, (code, address, approved) in VENDORS.items():
            vendors[name] = _one(
                conn,
                "INSERT INTO vendors (dataset_id, vendor_code, name, address, tax_rate,"
                " is_approved) VALUES (:d, :c, :n, :a, 0.0825, :ok) RETURNING id",
                d=dataset,
                c=code,
                n=name,
                a=address,
                ok=approved,
            )
            fp = fingerprint("GB29 NWBK 6016 1331 9268 19", BANK_KEY)
            conn.execute(
                text(
                    "INSERT INTO vendor_bank_accounts (vendor_id, bank_fp, last4, valid_from)"
                    " VALUES (:v, :fp, :l4, '2026-01-01')"
                ),
                {"v": vendors[name], "fp": fp.bank_fp, "l4": fp.last4},
            )
        return {"dataset": dataset, "vendors": vendors}

    def case(  # one invoice's whole investigation, written out
        self, conn: Connection, w: dict[str, Any], analyst: uuid.UUID, spec: Spec, n: int
    ) -> tuple[uuid.UUID, uuid.UUID]:
        vendor = w["vendors"][spec.vendor]
        po_number = f"PO-26-0{4100 + n}"
        po = _one(
            conn,
            "INSERT INTO purchase_orders (dataset_id, po_number, vendor_id, approval_level,"
            " issued_at) VALUES (:d, :p, :v, 1, '2026-09-01') RETURNING id",
            d=w["dataset"],
            p=po_number,
            v=vendor,
        )
        receipt = _one(
            conn,
            "INSERT INTO receipts (dataset_id, receipt_number, po_id, received_at)"
            " VALUES (:d, :r, :p, '2026-10-02') RETURNING id",
            d=w["dataset"],
            r=f"GR-{5100 + n}",
            p=po,
        )
        po_lines = []
        for i, line in enumerate(spec.lines, start=1):
            po_line = _one(
                conn,
                "INSERT INTO po_lines (po_id, line_no, sku, description, quantity, unit_price)"
                " VALUES (:p, :n, :s, :d, :q, :u) RETURNING id",
                p=po,
                n=i,
                s=line.sku,
                d=line.description,
                q=line.quantity,
                u=line.po_price,
            )
            conn.execute(
                text(
                    "INSERT INTO receipt_lines (receipt_id, po_line_id, quantity_received)"
                    " VALUES (:r, :l, :q)"
                ),
                {"r": receipt, "l": po_line, "q": line.received},
            )
            po_lines.append(po_line)
        data, coordinates = _pdf(spec, po_number)
        digest = hashlib.sha256(data).hexdigest()
        path = f"uploads/{digest[:2]}/{digest}"
        (self.store / path).parent.mkdir(parents=True, exist_ok=True)
        (self.store / path).write_bytes(data)
        document = _one(
            conn,
            "INSERT INTO documents (sha256, kind, mime_type, size_bytes, page_count, storage_path,"
            " source, uploaded_by) VALUES (:h, 'upload', 'application/pdf', :n, 1, :p, 'upload',"
            " :u) RETURNING id",
            h=digest,
            n=len(data),
            p=path,
            u=analyst,
        )
        invoice = _one(
            conn,
            "INSERT INTO invoices (dataset_id, document_id, received_at)"
            " VALUES (:d, :doc, now() - make_interval(hours => :h)) RETURNING id",
            d=w["dataset"],
            doc=document,
            h=30 - n,
        )
        case_id = service.create_case(conn, analyst, invoice)
        run_id = service.start_investigation(conn, case_id)
        fp = fingerprint(spec.account, BANK_KEY)
        lines = [
            {
                "index": i,
                "description": line.description,
                "sku": line.sku,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "line_total": line.total,
            }
            for i, line in enumerate(spec.lines, start=1)
        ]
        conn.execute(
            text(
                "INSERT INTO extractions (run_id, schema_version, data, field_confidence,"
                " coordinates, bank_fp, bank_last4) VALUES (:r, 'v1', CAST(:d AS jsonb),"
                " '{}', '{}', :fp, :l4)"
            ),
            {
                "r": run_id,
                "d": json.dumps(
                    {
                        "vendor_name": spec.vendor,
                        "invoice_number": spec.number,
                        "invoice_date": "2026-10-20",
                        "po_number": po_number,
                        "lines": lines,
                        "subtotal": f"{spec.subtotal():.2f}",
                        "tax": f"{spec.tax():.2f}",
                        "total": f"{spec.total():.2f}",
                        "bank_account": fp.token(),
                    }
                ),
                "fp": fp.bank_fp,
                "l4": fp.last4,
            },
        )
        for i, po_line in enumerate(po_lines, start=1):
            conn.execute(
                text(
                    "INSERT INTO line_matches (run_id, invoice_line_no, po_line_id, method,"
                    " similarity) VALUES (:r, :n, :p, 'sku', 1.0)"
                ),
                {"r": run_id, "n": i, "p": po_line},
            )
        failing = {(c["code"], c.get("subject", "header")) for c in spec.checks}
        passing: list[tuple[str, str, dict[str, Any]]] = [
            ("po_lookup", "header", {"invoice_po_number": po_number, "result": "found"}),
            (
                "vendor_approved",
                "header",
                {"vendor_code": VENDORS[spec.vendor][0], "approved": True},
            ),
            (
                "approval_level",
                "header",
                {"approval_amount": f"{spec.total():.2f}", "po_approval_level": 1},
            ),
            (
                "arithmetic",
                "header",
                {
                    "lines_sum": f"{spec.subtotal():.2f}",
                    "subtotal_gap": "0.00",
                    "total_gap": "0.00",
                },
            ),
            ("duplicate", "header", {"invoice_number": spec.number, "matches": []}),
            (
                "bank_details",
                "header",
                {"invoice_bank_fp": fp.bank_fp, "vendor_master_bank_fp": fp.bank_fp},
            ),
            (
                "tax",
                "header",
                {
                    "stated_tax": f"{spec.tax():.2f}",
                    "computed_tax": f"{spec.tax():.2f}",
                    "difference": "0.00",
                },
            ),
        ]
        for i in range(1, len(spec.lines) + 1):
            passing.append(("price", f"line:{i}", {"variance_pct": "0.00"}))
            passing.append(("quantity", f"line:{i}", {"cumulative_billed": "1", "received": "1"}))
        for code, subject, compared in passing:
            if (code, subject) in failing:
                continue
            refs = (
                [{"entity": "vendor", "id": str(vendor), "field": "is_approved", "value": True}]
                if code == "vendor_approved"
                else []
            )
            self._check(conn, run_id, code, subject, "pass", compared, None, refs)
        for c in spec.checks:
            check_id = self._check(
                conn,
                run_id,
                c["code"],
                c.get("subject", "header"),
                c["status"],
                c["compared"],
                c.get("tolerance"),
                c.get("refs", []),
            )
            finding_code, severity, words = (
                FINDING_FOR[c["code"]]
                if c["status"] == "fail"
                else ("EXTRACTION_UNCERTAIN", "medium", "confidence")
            )
            finding = _one(
                conn,
                "INSERT INTO findings (run_id, case_id, exception_type, subject_key, severity,"
                " explanation, grounding_status) VALUES (:r, :c, CAST(:t AS finding_type), :s,"
                " CAST(:sev AS severity), :e, 'ok') RETURNING id",
                r=run_id,
                c=case_id,
                t=finding_code,
                s=c.get("subject", "header"),
                sev=severity,
                e=c["explanation"],
            )
            conn.execute(
                text("INSERT INTO finding_check_results VALUES (:f, :c)"),
                {"f": finding, "c": check_id},
            )
            cited = _citation(conn, words)
            if cited:
                conn.execute(
                    text("INSERT INTO finding_citations VALUES (:f, :p, :q)"),
                    {"f": finding, "p": cited[0], "q": cited[1]},
                )
        action = ActionType.PLACE_ON_HOLD if spec.checks else ActionType.APPROVE_FOR_PAYMENT
        service.record_agent_proposal(conn, run_id, action)
        parsed = _one(
            conn,
            "INSERT INTO parsed_documents (document_id, parser_version, text, structure,"
            " coordinates, bank_tokens) VALUES (:d, :v, :t, '{}', CAST(:c AS jsonb),"
            " CAST(:b AS jsonb)) RETURNING id",
            d=document,
            v=PARSER_VERSION,
            t=f"Invoice {spec.number}",
            c=json.dumps(coordinates[0]),
            b=json.dumps([{"token": fp.token(), "bank_fp": fp.bank_fp, "last4": fp.last4}]),
        )
        service.run_succeeded(conn, run_id, parsed)
        return case_id, run_id

    @staticmethod
    def _check(  # noqa: PLR0917  # one check_results row
        conn: Connection,
        run_id: uuid.UUID,
        code: str,
        subject: str,
        status: str,
        compared: dict[str, Any],
        tolerance: dict[str, Any] | None,
        refs: list[dict[str, Any]],
    ) -> uuid.UUID:
        check_id: uuid.UUID = _one(
            conn,
            "INSERT INTO check_results (run_id, check_code, subject_key, status, compared,"
            " tolerance, evidence_refs, inconclusive_reason) VALUES (:r, CAST(:c AS check_code),"
            " :s, CAST(:st AS check_status), CAST(:cmp AS jsonb), CAST(:t AS jsonb),"
            " CAST(:refs AS jsonb), :why) RETURNING id",
            r=run_id,
            c=code,
            s=subject,
            st=status,
            cmp=json.dumps(compared),
            t=json.dumps(tolerance) if tolerance else None,
            refs=json.dumps(refs),
            why="tax amount read at 61% confidence" if status == "inconclusive" else None,
        )
        return check_id


def _version(conn: Connection, case_id: uuid.UUID) -> int:
    return int(_one(conn, "SELECT version FROM cases WHERE id = :c", c=case_id))


def _supporting(  # noqa: PLR0917  # one call shape, used three times
    conn: Connection,
    builder: Builder,
    analyst: uuid.UUID,
    case_id: uuid.UUID,
    kind: SupportingKind,
    about: str,
) -> uuid.UUID:
    data = text_pdf((about, 72.0, 700.0))
    digest = hashlib.sha256(data).hexdigest()
    path = f"uploads/{digest[:2]}/{digest}"
    (builder.store / path).parent.mkdir(parents=True, exist_ok=True)
    (builder.store / path).write_bytes(data)
    document = _one(
        conn,
        "INSERT INTO documents (sha256, kind, mime_type, size_bytes, page_count, storage_path,"
        " source, uploaded_by) VALUES (:h, 'supporting', 'application/pdf', :n, 1, :p,"
        " 'upload', :u) RETURNING id",
        h=digest,
        n=len(data),
        p=path,
        u=analyst,
    )
    supporting_id: uuid.UUID = service.add_supporting_document(
        conn, analyst, case_id, document_id=document, kind=kind, description=about
    )
    return supporting_id


def price_check(line: int, invoiced: str, po: str, pct: str) -> dict[str, Any]:
    return {
        "code": "price",
        "subject": f"line:{line}",
        "status": "fail",
        "compared": {
            "invoice_unit_price": invoiced,
            "invoice_quantity": "120",
            "po_unit_price": po,
            "variance_pct": pct,
            "undercharge_info": False,
        },
        "tolerance": {"max_overcharge_pct": "2.00"},
        "refs": [
            {
                "entity": "invoice",
                "id": None,
                "field": f"lines[{line}].unit_price",
                "value": invoiced,
            }
        ],
        "explanation": f"Line {line} is billed at {invoiced} against {po} on the PO, {pct}% above"
        " the 2.00% tolerance.",
    }


def build(engine: Engine, store: Path, password: str) -> Demo:  # the whole demo
    seed(engine, load_policy(), demo_password=password)
    b = Builder(engine, store)
    with engine.begin() as conn:
        users = {
            r.username: r.id for r in conn.execute(text("SELECT id, username FROM users")).all()
        }
        priya, tomas = users["priya.raman"], users["tomas.berg"]
        sam = users["sam.okafor"]
        w = b.world(conn)
        bolts = [
            Line("Hex bolts M8 x 40 zinc", "HB-840", "120", "0.45", "0.42", "120"),
            Line("Flat washers M8 stainless", "FW-8SS", "1200", "0.09", "0.09", "1200"),
        ]
        cases: dict[str, uuid.UUID] = {}
        cases["price"], _ = b.case(
            conn,
            w,
            priya,
            Spec(
                "price",
                "Halden Industrial Supply",
                "INV-24-0418",
                bolts,
                checks=[price_check(1, "0.45", "0.42", "7.14")],
            ),
            1,
        )
        cases["clean"], _ = b.case(
            conn,
            w,
            priya,
            Spec(
                "clean",
                "Marsh & Kettle Ltd",
                "MK-30917",
                [
                    Line(
                        "Nitrile gloves, large, box of 100",
                        "NG-L100",
                        "40",
                        "8.5700",
                        "8.5700",
                        "40",
                    ),
                    Line("Degreaser 5 litre", "DG-5L", "12", "21.8100", "21.8100", "12"),
                ],
            ),
            2,
        )
        ledger = _one(
            conn,
            "INSERT INTO ledger_invoices (dataset_id, vendor_id, invoice_number, invoice_date,"
            " subtotal, tax, total) VALUES (:d, :v, 'A-25551', '2026-10-06', 2347.57, 117.38,"
            " 2464.95) RETURNING id",
            d=w["dataset"],
            v=w["vendors"]["Porter-Castillo"],
        )
        master = fingerprint("GB29 NWBK 6016 1331 9268 19", BANK_KEY)
        changed = "GB94 BARC 1020 1530 0934 59"
        cases["restricted"], _ = b.case(
            conn,
            w,
            tomas,
            Spec(
                "restricted",
                "Porter-Castillo",
                "A-25569",
                [Line("Fire extinguisher service, 6kg ABC", "FX-6", "4", "35.00", "35.00", "4")],
                account=changed,
                checks=[
                    {
                        "code": "duplicate",
                        "status": "fail",
                        "compared": {
                            "invoice_number": "A-25569",
                            "normalised": "a25569",
                            "invoice_date": "2026-10-20",
                            "invoice_po_number": "PO-26-04103",
                            "matches": [
                                {
                                    "ledger_invoice": "A-25551",
                                    "ledger_invoice_id": str(ledger),
                                    "basis": "near_total_date_lines",
                                    "total": "151.55",
                                    "invoice_date": "2026-10-06",
                                    "same_lines": True,  # this basis requires the same lines
                                }
                            ],
                        },
                        "tolerance": {"near_total_pct": "1.00", "window_days": 30},
                        "refs": [
                            {
                                "entity": "ledger_invoice",
                                "id": str(ledger),
                                "field": "invoice_number",
                                "value": "A-25551",
                            }
                        ],
                        "explanation": "A-25569 resembles ledger invoice A-25551 from 2026-10-06:"
                        " a near total within 14 days.",
                    },
                    {
                        "code": "bank_details",
                        "status": "fail",
                        "compared": {
                            "invoice_bank_fp": fingerprint(changed, BANK_KEY).bank_fp,
                            "vendor_master_bank_fp": master.bank_fp,
                        },
                        "refs": [
                            {
                                "entity": "invoice",
                                "id": None,
                                "field": "bank_account",
                                "value": fingerprint(changed, BANK_KEY).token(),
                            },
                            {
                                "entity": "vendor_bank_account",
                                "id": str(w["vendors"]["Porter-Castillo"]),
                                "field": "bank_fp",
                                "value": master.bank_fp,
                            },
                        ],
                        "explanation": "The invoice asks for payment to an account ending 3459;"
                        " the vendor master holds one ending 6819.",
                    },
                ],
            ),
            3,
        )
        cases["resolved"], _ = b.case(
            conn,
            w,
            priya,
            Spec(
                "resolved",
                "Halden Industrial Supply",
                "INV-24-0431",
                bolts,
                checks=[price_check(1, "0.45", "0.42", "7.14")],
            ),
            4,
        )
        note = _supporting(
            conn,
            b,
            priya,
            cases["resolved"],
            SupportingKind.CREDIT_NOTE,
            "Credit note CN-3318 for 3.60",
        )
        finding = _one(
            conn,
            "SELECT f.id FROM findings f JOIN cases c ON c.evidence_run_id = f.run_id"
            " WHERE c.id = :c",
            c=cases["resolved"],
        )
        service.record_resolution(
            conn,
            priya,
            cases["resolved"],
            _version(conn, cases["resolved"]),
            finding_id=finding,
            resolution_type=ResolutionType.CREDIT_ISSUED,
            summary="Vendor issued credit note CN-3318 for the overcharge on line 1",
            evidence=[EvidenceEntry(supporting_document_id=note)],
            structured={"credit_amount": "3.60"},
        )
        service.change_proposal(
            conn,
            priya,
            cases["resolved"],
            _version(conn, cases["resolved"]),
            action=ActionType.APPROVE_FOR_PAYMENT,
            reason="Credit note covers the overcharge",
        )
        service.submit(conn, priya, cases["resolved"], _version(conn, cases["resolved"]))
        cases["controller"], _ = b.case(
            conn,
            w,
            tomas,
            Spec(
                "controller",
                "Ostrava Tooling",
                "OT-2026-118",
                [Line("Pallet truck 2.5 t", "PT-25", "6", "2315.00", "2315.00", "6")],
            ),
            5,
        )
        service.change_proposal(
            conn,
            tomas,
            cases["controller"],
            _version(conn, cases["controller"]),
            action=ActionType.PLACE_ON_HOLD,
            reason="Hold until the trucks are inspected",
        )
        service.submit(conn, tomas, cases["controller"], _version(conn, cases["controller"]))
        cases["paid"], _ = b.case(
            conn,
            w,
            tomas,
            Spec(
                "paid",
                "Marsh & Kettle Ltd",
                "MK-30881",
                [Line("Absorbent pads, 50 pack", "AP-50", "20", "18.40", "18.40", "20")],
            ),
            6,
        )
        service.submit(conn, tomas, cases["paid"], _version(conn, cases["paid"]))
        decision = service.approve(conn, sam, cases["paid"], _version(conn, cases["paid"]))
        execute(conn, cases["paid"], decision)
        cases["inconclusive"], _ = b.case(
            conn,
            w,
            priya,
            Spec(
                "inconclusive",
                "Marsh & Kettle Ltd",
                "MK-30954",
                [Line("Safety glasses, clear", "SG-C", "60", "4.20", "4.20", "60")],
                checks=[
                    {
                        "code": "tax",
                        "status": "inconclusive",
                        "compared": {"uncertain_fields": ["tax"]},
                        "explanation": "The tax amount was read at 61% confidence, below the 90%"
                        " needed to compare it.",
                    }
                ],
            ),
            7,
        )
        failed_invoice = _one(
            conn,
            "INSERT INTO invoices (dataset_id, document_id, received_at) VALUES (:d, :doc, now())"
            " RETURNING id",
            d=w["dataset"],
            doc=_one(
                conn,
                "INSERT INTO documents (sha256, kind, mime_type, size_bytes, storage_path,"
                " source) VALUES (:h, 'upload', 'application/pdf', 10, 'uploads/none',"
                " 'upload') RETURNING id",
                h=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            ),
        )
        cases["failed"] = service.create_case(conn, priya, failed_invoice)
        run = service.start_investigation(conn, cases["failed"])
        service.run_failed(
            conn, run, "VendorNotIdentifiedError: no vendor named 'Nordwind Logistik' in the master"
        )
        waiting_invoice = _one(
            conn,
            "INSERT INTO invoices (dataset_id, document_id, received_at) VALUES (:d, :doc, now())"
            " RETURNING id",
            d=w["dataset"],
            doc=_one(
                conn,
                "INSERT INTO documents (sha256, kind, mime_type, size_bytes, storage_path,"
                " source) VALUES (:h, 'upload', 'application/pdf', 10, 'uploads/none',"
                " 'upload') RETURNING id",
                h=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            ),
        )
        cases["investigating"] = service.create_case(conn, tomas, waiting_invoice)
        service.start_investigation(conn, cases["investigating"])
    started = dt.datetime.now(dt.UTC)
    with engine.begin() as conn:  # read as the read-only verifier role, as the CLI does
        conn.execute(text("SET LOCAL ROLE opspilot_verifier"))
        verified_as = str(conn.execute(text("SELECT current_user")).scalar_one())
        report = verify(conn)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL ROLE opspilot_app"))
        record(
            conn,
            report,
            started_at=started,
            finished_at=dt.datetime.now(dt.UTC),
            verified_as=verified_as,
        )
    return Demo(users, cases, password)
