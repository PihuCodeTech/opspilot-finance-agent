"""Recorded `audit verify` results (owner decision on P11): stored, append-only, shown carefully."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from opspilot.audit.verify import VerifyReport, record, verify
from opspilot.cases.queries import VERIFICATION_STATEMENT
from opspilot.domain.enums import SupportingKind
from tests.integration import case_support as cs
from tests.integration.web_support import make_web

pytestmark = pytest.mark.integration

T0 = dt.datetime(2026, 10, 26, 12, 0, tzinfo=dt.UTC)


def _refused(conn: Connection, sql: str) -> None:
    savepoint = conn.begin_nested()
    with pytest.raises(DBAPIError):
        conn.execute(text(sql))
    savepoint.rollback()


def test_results_are_recorded_append_only_and_reported_per_case(
    conn: Connection, tmp_path: Path
) -> None:
    web = make_web(conn, tmp_path)
    w = web.world
    case_id = w.case([cs.approval(), cs.price()])
    client = web.client(w.analyst)

    conn.execute(text("SET LOCAL ROLE opspilot_verifier"))  # read as the read-only role
    verified_as = conn.execute(text("SELECT current_user")).scalar_one()
    report = verify(conn)
    _refused(  # the verifier can't write results, only read
        conn,
        "INSERT INTO audit_verifications (started_at, finished_at, verified_as, ok, chains_ok,"
        " digests_ok, chains, digests_checked, failures)"
        " VALUES (now(), now(), 'x', true, true, true, '[]', 0, '[]')",
    )
    conn.execute(text("SET LOCAL ROLE opspilot_app"))  # the app role records the result
    record(conn, report, started_at=T0, finished_at=T0, verified_as=verified_as)

    shown = client.get(f"/api/v1/cases/{case_id}/audit").json()["verification"]
    live = next(c for c in report.chains if c.chain_id == "live")
    assert shown["verified_as"] == "opspilot_verifier"
    assert shown["chain_ok"] is (live.error is None)
    assert shown["overall_ok"] is report.ok
    assert shown["verified_through_seq"] == live.head_seq
    assert shown["covers_all_case_events"] is True
    assert shown["statement"] == VERIFICATION_STATEMENT
    assert "not tamper-proof" in shown["statement"]

    w.supporting(case_id, SupportingKind.CREDIT_NOTE)  # a new event after the verification
    later = client.get(f"/api/v1/cases/{case_id}/audit").json()["verification"]
    assert later["covers_all_case_events"] is False

    run_id = conn.execute(
        text("SELECT evidence_run_id FROM cases WHERE id = :c"), {"c": case_id}
    ).scalar_one()
    broken = VerifyReport(
        chains=report.chains, mismatches=[f"run {run_id}: evidence does not match its digest"]
    )
    record(
        conn,
        broken,
        started_at=T0 + dt.timedelta(minutes=1),
        finished_at=T0 + dt.timedelta(minutes=2),
        verified_as=verified_as,
    )
    failed = client.get(f"/api/v1/cases/{case_id}/audit").json()["verification"]
    assert failed["evidence_ok"] is False and failed["overall_ok"] is False
    assert failed["case_failures"] == [f"run {run_id}: evidence does not match its digest"]
    other = w.case([cs.approval()])
    unaffected = client.get(f"/api/v1/cases/{other}/audit").json()["verification"]
    assert unaffected["evidence_ok"] is True  # the failure names another case's run

    conn.execute(text("RESET ROLE"))
    _refused(conn, "UPDATE audit_verifications SET ok = true")  # not even the owner
    _refused(conn, "DELETE FROM audit_verifications")
    conn.execute(text("SET LOCAL ROLE opspilot_app"))
    _refused(conn, "UPDATE audit_verifications SET ok = true")


def test_an_old_verification_is_shown_as_overdue(conn: Connection, tmp_path: Path) -> None:
    web = make_web(conn, tmp_path)
    case_id = web.world.case([cs.approval()])
    client = web.client(web.world.analyst)
    now = dt.datetime.now(dt.UTC)
    report = VerifyReport()
    two_days_ago = now - dt.timedelta(days=2)
    record(conn, report, started_at=two_days_ago, finished_at=two_days_ago, verified_as="x")
    old = client.get(f"/api/v1/cases/{case_id}/audit").json()["verification"]
    assert old["overdue"] is True
    page = client.get(f"/cases/{case_id}")
    assert "The daily check is overdue" in page.text
    record(conn, report, started_at=now, finished_at=now, verified_as="x")
    fresh = client.get(f"/api/v1/cases/{case_id}/audit").json()["verification"]
    assert fresh["overdue"] is False
