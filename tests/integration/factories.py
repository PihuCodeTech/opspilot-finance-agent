"""Minimal rows for integration tests, inserted with plain SQL (as the owner)."""

import hashlib
import uuid
from typing import Any

from sqlalchemy import Connection, text

ZERO_HASH = b"\x00" * 32


def _one(conn: Connection, sql: str, **params: Any) -> uuid.UUID:
    value = conn.execute(text(sql), params).scalar_one()
    assert isinstance(value, uuid.UUID)
    return value


def dataset(conn: Connection) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO datasets (name, version, split) VALUES (:n, '1', 'dev') RETURNING id",
        n=f"ds-{uuid.uuid4().hex[:8]}",
    )


def freeze(conn: Connection, dataset_id: uuid.UUID) -> None:
    conn.execute(text("UPDATE datasets SET frozen_at = now() WHERE id = :d"), {"d": dataset_id})


def vendor(conn: Connection, dataset_id: uuid.UUID) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO vendors (dataset_id, vendor_code, name, address, tax_rate, is_approved)"
        " VALUES (:d, :c, 'Halden Industrial Supply', '1 Quay St', 0.0825, true) RETURNING id",
        d=dataset_id,
        c=f"V-{uuid.uuid4().hex[:4]}",
    )


def bank_account(conn: Connection, vendor_id: uuid.UUID, fp: str = "3f9a1c2b7d10") -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO vendor_bank_accounts (vendor_id, bank_fp, last4, valid_from)"
        " VALUES (:v, :fp, '4471', '2026-01-01') RETURNING id",
        v=vendor_id,
        fp=fp,
    )


def purchase_order(conn: Connection, dataset_id: uuid.UUID, vendor_id: uuid.UUID) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO purchase_orders (dataset_id, po_number, vendor_id, approval_level, issued_at)"
        " VALUES (:d, :n, :v, 1, '2026-09-01') RETURNING id",
        d=dataset_id,
        n=f"PO-{uuid.uuid4().hex[:6]}",
        v=vendor_id,
    )


def po_line(conn: Connection, po_id: uuid.UUID) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO po_lines (po_id, line_no, description, quantity, unit_price)"
        " VALUES (:p, 1, 'Hex bolts M8', 100, 0.42) RETURNING id",
        p=po_id,
    )


def ledger_invoice(conn: Connection, dataset_id: uuid.UUID, vendor_id: uuid.UUID) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO ledger_invoices (dataset_id, vendor_id, invoice_number, invoice_date,"
        " subtotal, tax, total) VALUES (:d, :v, 'INV-24/0418', '2026-09-10', 42.00, 3.47, 45.47)"
        " RETURNING id",
        d=dataset_id,
        v=vendor_id,
    )


def document(conn: Connection, kind: str = "invoice") -> uuid.UUID:
    digest = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    return _one(
        conn,
        "INSERT INTO documents (sha256, kind, mime_type, size_bytes, storage_path, source)"
        " VALUES (:h, CAST(:k AS document_kind), 'application/pdf', 1024, :p, 'generated')"
        " RETURNING id",
        h=digest,
        k=kind,
        p=f"documents/{digest}",
    )


def invoice(conn: Connection, dataset_id: uuid.UUID) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO invoices (dataset_id, document_id, received_at)"
        " VALUES (:d, :doc, now()) RETURNING id",
        d=dataset_id,
        doc=document(conn),
    )


def user(conn: Connection, role: str = "analyst", **authority: Any) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO users (username, display_name, password_hash, role, approval_limit,"
        " approval_unlimited) VALUES (:u, 'Test User', 'x', CAST(:r AS user_role), :lim, :unl)"
        " RETURNING id",
        u=f"user-{uuid.uuid4().hex[:8]}",
        r=role,
        lim=authority.get("approval_limit"),
        unl=authority.get("approval_unlimited", False),
    )


def policy_version(conn: Connection) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO policy_versions (version, config, config_sha256, document, document_sha256)"
        " VALUES (:v, '{}'::jsonb, :h, 'doc', :h) RETURNING id",
        v=f"test-{uuid.uuid4().hex[:8]}",
        h="0" * 64,
    )


def case(conn: Connection, invoice_id: uuid.UUID) -> uuid.UUID:
    return _one(conn, "INSERT INTO cases (invoice_id) VALUES (:i) RETURNING id", i=invoice_id)


def run(conn: Connection, case_id: uuid.UUID, policy_version_id: uuid.UUID) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO investigation_runs (case_id, attempt, kind, status, thread_id,"
        " policy_version_id) VALUES (:c, 1, 'investigation', 'succeeded', :t, :p) RETURNING id",
        c=case_id,
        t=f"thread-{uuid.uuid4().hex}",
        p=policy_version_id,
    )


def finding(
    conn: Connection, run_id: uuid.UUID, case_id: uuid.UUID, code: str = "PRICE_MISMATCH"
) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO findings (run_id, case_id, exception_type, subject_key, severity,"
        " explanation, grounding_status) VALUES (:r, :c, CAST(:t AS finding_type), 'line:1',"
        " 'high', 'Unit price above PO', 'ok') RETURNING id",
        r=run_id,
        c=case_id,
        t=code,
    )


def check_result(conn: Connection, run_id: uuid.UUID) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO check_results (run_id, check_code, subject_key, status, compared,"
        " evidence_refs) VALUES (:r, 'price', 'line:1', 'fail', '{}'::jsonb, '[]'::jsonb)"
        " RETURNING id",
        r=run_id,
    )


def proposal(conn: Connection, case_id: uuid.UUID, policy_version_id: uuid.UUID) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO proposals (case_id, action, source, policy_version_id, digest)"
        " VALUES (:c, 'place_on_hold', 'agent', :p, :d) RETURNING id",
        c=case_id,
        p=policy_version_id,
        d=ZERO_HASH,
    )


def decision(
    conn: Connection,
    case_id: uuid.UUID,
    kind: str,
    actor: uuid.UUID | None,
    reason: str | None = None,
) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO decisions (case_id, kind, actor_id, reason, case_version)"
        " VALUES (:c, CAST(:k AS decision_kind), :a, :r, 1) RETURNING id",
        c=case_id,
        k=kind,
        a=actor,
        r=reason,
    )


def supporting_document(
    conn: Connection, case_id: uuid.UUID, added_by: uuid.UUID, kind: str = "credit_note"
) -> uuid.UUID:
    return _one(
        conn,
        "INSERT INTO supporting_documents (case_id, document_id, kind, description, added_by)"
        " VALUES (:c, :d, CAST(:k AS supporting_kind), 'evidence', :u) RETURNING id",
        c=case_id,
        d=document(conn, "supporting"),
        k=kind,
        u=added_by,
    )


def audit_event(conn: Connection, chain: str = "live") -> int:
    value = conn.execute(
        text(
            "INSERT INTO audit_events (chain_id, seq, event_type, payload, prev_hash, hash)"
            " VALUES (:c, 1, 'test', '{}'::jsonb, :z, :h) RETURNING id"
        ),
        {"c": chain, "z": ZERO_HASH, "h": uuid.uuid4().bytes * 2},
    ).scalar_one()
    return int(value)
