"""Integrity guarantees the database enforces by itself (06-database.md §3, §5, §7)."""

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from opspilot.db.models import APPEND_ONLY_TABLES
from tests.integration import factories as f

pytestmark = pytest.mark.integration

INSUFFICIENT_PRIVILEGE = "42501"
CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"


def _fails(conn: Connection, sql: str, params: dict[str, Any] | None = None, *, code: str) -> None:
    """Run in a savepoint and assert it fails with the given SQLSTATE."""
    savepoint = conn.begin_nested()
    with pytest.raises(DBAPIError) as info:
        conn.execute(text(sql), params or {})
    savepoint.rollback()
    assert getattr(info.value.orig, "sqlstate", None) == code, info.value


def _succeeds(conn: Connection, sql: str, params: dict[str, Any] | None = None) -> None:
    savepoint = conn.begin_nested()
    conn.execute(text(sql), params or {})
    savepoint.rollback()


@pytest.fixture
def graph(conn: Connection) -> dict[str, Any]:
    ds = f.dataset(conn)
    v = f.vendor(conn, ds)
    analyst = f.user(conn, "analyst")
    approver = f.user(conn, "approver", approval_limit=10000)
    pv = f.policy_version(conn)
    inv = f.invoice(conn, ds)
    c = f.case(conn, inv)
    r = f.run(conn, c, pv)
    approve = f.decision(conn, c, "approve", approver)
    return {
        "dataset": ds,
        "vendor": v,
        "analyst": analyst,
        "approver": approver,
        "policy_version": pv,
        "invoice": inv,
        "case": c,
        "run": r,
        "finding": f.finding(conn, r, c),
        "check_result": f.check_result(conn, r),
        "proposal": f.proposal(conn, c, pv),
        "approve": approve,
        "audit_event": f.audit_event(conn),
    }


# ---------------------------------------------------------------- append-only


def test_every_append_only_table_has_both_triggers(conn: Connection) -> None:
    rows = conn.execute(
        text(
            "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
            " WHERE NOT t.tgisinternal"
        )
    ).all()
    names = {(table, trig) for table, trig in rows}
    for table in APPEND_ONLY_TABLES:
        assert (table, f"{table}_append_only") in names, table
        assert (table, f"{table}_no_truncate") in names, table


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ("audit_events", "audit_event"),
        ("decisions", "approve"),
        ("proposals", "proposal"),
        ("findings", "finding"),
        ("check_results", "check_result"),
    ],
)
def test_append_only_rows_cannot_be_changed_even_by_the_owner(
    conn: Connection, graph: dict[str, Any], table: str, key: str
) -> None:
    params = {"id": graph[key]}
    _fails(
        conn,
        f"UPDATE {table} SET created_at = created_at WHERE id = :id"  # noqa: S608
        if table != "audit_events"
        else "UPDATE audit_events SET seq = seq WHERE id = :id",
        params,
        code=INSUFFICIENT_PRIVILEGE,
    )
    _fails(conn, f"DELETE FROM {table} WHERE id = :id", params, code=INSUFFICIENT_PRIVILEGE)  # noqa: S608
    _fails(conn, f"TRUNCATE {table} CASCADE", code=INSUFFICIENT_PRIVILEGE)


# ---------------------------------------------------------------- frozen datasets


def test_frozen_dataset_is_immutable(conn: Connection, graph: dict[str, Any]) -> None:
    ds, v = graph["dataset"], graph["vendor"]
    po = f.purchase_order(conn, ds, v)
    line = f.po_line(conn, po)
    ledger = f.ledger_invoice(conn, ds, v)
    f.freeze(conn, ds)

    _fails(
        conn,
        "INSERT INTO vendors (dataset_id, vendor_code, name, address, tax_rate, is_approved)"
        " VALUES (:d, 'V-NEW', 'n', 'a', 0, true)",
        {"d": ds},
        code=INSUFFICIENT_PRIVILEGE,
    )
    _fails(
        conn,
        "UPDATE po_lines SET unit_price = 9 WHERE id = :i",
        {"i": line},
        code=INSUFFICIENT_PRIVILEGE,
    )
    _fails(
        conn,
        "DELETE FROM ledger_invoices WHERE id = :i",
        {"i": ledger},
        code=INSUFFICIENT_PRIVILEGE,
    )
    _fails(
        conn,
        "UPDATE vendors SET is_approved = false WHERE id = :i",
        {"i": v},
        code=INSUFFICIENT_PRIVILEGE,
    )
    _fails(
        conn,
        "UPDATE datasets SET frozen_at = NULL WHERE id = :i",
        {"i": ds},
        code=INSUFFICIENT_PRIVILEGE,
    )
    _fails(conn, "DELETE FROM datasets WHERE id = :i", {"i": ds}, code=INSUFFICIENT_PRIVILEGE)


def test_unfrozen_dataset_can_change(conn: Connection, graph: dict[str, Any]) -> None:
    _succeeds(conn, "UPDATE vendors SET is_approved = false WHERE id = :i", {"i": graph["vendor"]})


# ---------------------------------------------------------------- roles and grants


@pytest.fixture
def as_role(conn: Connection) -> Iterator[Callable[[str], None]]:
    def switch(role: str) -> None:
        conn.execute(text(f"SET LOCAL ROLE {role}"))

    yield switch
    conn.execute(text("RESET ROLE"))


def test_inference_role_cannot_read_labels(
    conn: Connection, as_role: Callable[[str], None]
) -> None:
    as_role("opspilot_app")
    _succeeds(conn, "SELECT id, dataset_id, invoice_id FROM dataset_items")
    _fails(conn, "SELECT labels FROM dataset_items", code=INSUFFICIENT_PRIVILEGE)
    _fails(conn, "SELECT * FROM dataset_items", code=INSUFFICIENT_PRIVILEGE)


def test_app_role_cannot_rewrite_history(
    conn: Connection, graph: dict[str, Any], as_role: Callable[[str], None]
) -> None:
    as_role("opspilot_app")
    _fails(
        conn,
        "UPDATE decisions SET reason = 'x' WHERE id = :i",
        {"i": graph["approve"]},
        code=INSUFFICIENT_PRIVILEGE,
    )
    _fails(conn, "DELETE FROM audit_events", code=INSUFFICIENT_PRIVILEGE)
    _succeeds(conn, "UPDATE cases SET version = version + 1 WHERE id = :i", {"i": graph["case"]})


def test_baseline_role_sees_only_its_views(
    conn: Connection, graph: dict[str, Any], as_role: Callable[[str], None]
) -> None:
    f.bank_account(conn, graph["vendor"])
    as_role("opspilot_baseline_ro")
    for table in (
        "cases",
        "findings",
        "extractions",
        "vendors",
        "vendor_bank_accounts",
        "dataset_items",
    ):
        _fails(conn, f"SELECT 1 FROM {table} LIMIT 1", code=INSUFFICIENT_PRIVILEGE)  # noqa: S608
    row = conn.execute(
        text("SELECT current_bank_fp, current_bank_last4 FROM baseline_vendors WHERE id = :v"),
        {"v": graph["vendor"]},
    ).one()
    assert tuple(row) == ("3f9a1c2b7d10", "4471")  # the same short fingerprint OpsPilot uses
    _succeeds(conn, "SELECT parsed_text FROM baseline_invoices")


def test_verifier_cannot_write(conn: Connection, as_role: Callable[[str], None]) -> None:
    as_role("opspilot_verifier")
    _succeeds(conn, "SELECT count(*) FROM audit_events")
    _fails(
        conn,
        "INSERT INTO datasets (name, version, split) VALUES ('x', '1', 'dev')",
        code=INSUFFICIENT_PRIVILEGE,
    )


def test_role_settings(conn: Connection) -> None:
    settings: dict[str, list[str]] = {
        row[0]: row[1]
        for row in conn.execute(
            text("SELECT rolname, rolconfig FROM pg_roles WHERE rolname LIKE 'opspilot_%'")
        )
    }
    assert "statement_timeout=5s" in settings["opspilot_baseline_ro"]
    assert "default_transaction_read_only=on" in settings["opspilot_verifier"]


# ---------------------------------------------------------------- cross-row rules


def test_executed_action_needs_one_approve_decision_of_the_same_case(
    conn: Connection, graph: dict[str, Any]
) -> None:
    sql = (
        "INSERT INTO executed_actions (case_id, decision_id, action)"
        " VALUES (:c, :d, 'place_on_hold')"
    )
    submit = f.decision(conn, graph["case"], "submit", graph["analyst"])
    _fails(conn, sql, {"c": graph["case"], "d": submit}, code=CHECK_VIOLATION)

    other_case = f.case(conn, f.invoice(conn, graph["dataset"]))
    _fails(conn, sql, {"c": other_case, "d": graph["approve"]}, code=CHECK_VIOLATION)

    conn.execute(text(sql), {"c": graph["case"], "d": graph["approve"]})
    _fails(conn, sql, {"c": graph["case"], "d": graph["approve"]}, code=UNIQUE_VIOLATION)


@pytest.mark.parametrize(
    "code", ["EXTRACTION_UNCERTAIN", "MISSING_OR_UNKNOWN_PO", "FORENSICS_SIGNAL"]
)
def test_recheck_only_findings_cannot_be_resolved_by_record(
    conn: Connection, graph: dict[str, Any], code: str
) -> None:
    finding = f.finding(conn, graph["run"], graph["case"], code)
    _fails(
        conn,
        "INSERT INTO finding_resolutions (finding_id, case_id, resolution_type, summary,"
        " structured, resolved_by, policy_version_id, digest)"
        " VALUES (:f, :c, 'credit_issued', 'credit note received', '{}'::jsonb, :u, :p, :d)",
        {
            "f": finding,
            "c": graph["case"],
            "u": graph["analyst"],
            "p": graph["policy_version"],
            "d": f.ZERO_HASH,
        },
        code=CHECK_VIOLATION,
    )


def test_resolution_needs_a_summary_and_a_resolvable_finding(
    conn: Connection, graph: dict[str, Any]
) -> None:
    sql = (
        "INSERT INTO finding_resolutions (finding_id, case_id, resolution_type, summary,"
        " structured, resolved_by, policy_version_id, digest)"
        " VALUES (:f, :c, 'credit_issued', :s, '{}'::jsonb, :u, :p, :d)"
    )
    params = {
        "f": graph["finding"],
        "c": graph["case"],
        "u": graph["analyst"],
        "p": graph["policy_version"],
        "d": f.ZERO_HASH,
    }
    _fails(conn, sql, {**params, "s": "   "}, code=CHECK_VIOLATION)
    _succeeds(conn, sql, {**params, "s": "Credit note CN-3318 covers the overcharge"})


def test_bank_verification_rules(conn: Connection, graph: dict[str, Any]) -> None:
    sql = (
        "INSERT INTO bank_verifications (case_id, vendor_id, channel, contact_source,"
        " contact_reference, verified_bank_fp, verified_last4,"
        " evidence_supporting_document_id, verified_by)"
        " VALUES (:c, :v, 'callback_to_master_contact', :src, '+1 555 0100', '3f9a1c2b7d10',"
        " '4471', :e, :u)"
    )
    good_evidence = f.supporting_document(
        conn, graph["case"], graph["analyst"], "bank_verification"
    )
    wrong_evidence = f.supporting_document(conn, graph["case"], graph["analyst"], "credit_note")
    base = {"c": graph["case"], "v": graph["vendor"], "u": graph["analyst"]}
    _fails(conn, sql, {**base, "src": "invoice", "e": good_evidence}, code=CHECK_VIOLATION)
    _fails(conn, sql, {**base, "src": "vendor_master", "e": wrong_evidence}, code=CHECK_VIOLATION)
    _succeeds(conn, sql, {**base, "src": "vendor_master", "e": good_evidence})


def test_supporting_documents_must_be_stored_as_supporting(
    conn: Connection, graph: dict[str, Any]
) -> None:
    _fails(
        conn,
        "INSERT INTO supporting_documents (case_id, document_id, kind, description, added_by)"
        " VALUES (:c, :d, 'credit_note', 'x', :u)",
        {"c": graph["case"], "d": f.document(conn, "invoice"), "u": graph["analyst"]},
        code=CHECK_VIOLATION,
    )


def test_decision_rules(conn: Connection, graph: dict[str, Any]) -> None:
    sql = (
        "INSERT INTO decisions (case_id, kind, actor_id, reason, case_version)"
        " VALUES (:c, CAST(:k AS decision_kind), :a, :r, 1)"
    )
    c, a = graph["case"], graph["approver"]
    _fails(conn, sql, {"c": c, "k": "decline", "a": a, "r": None}, code=CHECK_VIOLATION)
    _fails(conn, sql, {"c": c, "k": "dismiss", "a": a, "r": " "}, code=CHECK_VIOLATION)
    _fails(conn, sql, {"c": c, "k": "escalate", "a": a, "r": None}, code=CHECK_VIOLATION)
    _fails(conn, sql, {"c": c, "k": "approve", "a": None, "r": None}, code=CHECK_VIOLATION)
    _succeeds(conn, sql, {"c": c, "k": "escalate", "a": None, "r": None})


def test_analyst_proposals_need_a_reason(conn: Connection, graph: dict[str, Any]) -> None:
    _fails(
        conn,
        "INSERT INTO proposals (case_id, action, source, changed_by, policy_version_id, digest)"
        " VALUES (:c, 'reject_invoice', 'analyst', :u, :p, :d)",
        {"c": graph["case"], "u": graph["analyst"], "p": graph["policy_version"], "d": f.ZERO_HASH},
        code=CHECK_VIOLATION,
    )


@pytest.mark.parametrize(
    ("role", "authority", "ok"),
    [
        ("analyst", {}, True),
        ("analyst", {"approval_limit": 10000}, False),
        ("auditor", {"approval_unlimited": True}, False),
        ("approver", {"approval_limit": 10000}, True),
        ("approver", {"approval_unlimited": True}, True),
        ("approver", {}, False),  # an approver needs some authority
        ("approver", {"approval_limit": 10000, "approval_unlimited": True}, False),
    ],
)
def test_one_role_and_coherent_approval_authority(
    conn: Connection, role: str, authority: dict[str, Any], ok: bool
) -> None:
    savepoint = conn.begin_nested()
    try:
        f.user(conn, role, **authority)
    except DBAPIError as exc:
        assert not ok, exc
        assert getattr(exc.orig, "sqlstate", None) == CHECK_VIOLATION
    else:
        assert ok
    finally:
        savepoint.rollback()


def test_one_open_live_case_per_invoice(conn: Connection, graph: dict[str, Any]) -> None:
    _fails(
        conn,
        "INSERT INTO cases (invoice_id) VALUES (:i)",
        {"i": graph["invoice"]},
        code=UNIQUE_VIOLATION,
    )


def test_ledger_invoice_numbers_are_normalised(conn: Connection, graph: dict[str, Any]) -> None:
    ledger = f.ledger_invoice(conn, graph["dataset"], graph["vendor"])
    norm = conn.execute(
        text("SELECT invoice_number_norm FROM ledger_invoices WHERE id = :i"), {"i": ledger}
    ).scalar_one()
    assert norm == "inv240418"


def test_ids_are_generated(conn: Connection) -> None:
    assert isinstance(f.dataset(conn), uuid.UUID)
