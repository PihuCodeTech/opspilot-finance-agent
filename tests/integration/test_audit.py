"""Audit chains and digests: tampering is detected, chains stay linear (06-database.md §5)."""

import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text

from opspilot.audit.chain import LIVE, append_event, eval_chain
from opspilot.audit.digests import (
    cited_rows,
    load_resolution_content,
    proposal_digest,
    record_finding_resolved,
    record_proposal_changed,
    record_run_completed,
    resolution_content,
    resolution_digest,
    rows_of,
)
from opspilot.audit.verify import VerifyReport, verify
from opspilot.db.models import Proposal
from tests.integration import factories as f

pytestmark = pytest.mark.integration


def tamper(conn: Connection, table: str, sql: str, params: dict[str, Any] | None = None) -> None:
    """What a privileged account could do: bypass the append-only triggers (rolled back)."""
    conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
    conn.execute(text(sql), params or {})
    conn.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))


def chain(report: VerifyReport, chain_id: str) -> Any:
    return next(c for c in report.chains if c.chain_id == chain_id)


def mismatches_about(report: VerifyReport, ident: object) -> list[str]:
    return [m for m in report.mismatches if str(ident) in m]


@pytest.fixture
def as_role(conn: Connection) -> Iterator[Callable[[str], None]]:
    def switch(role: str) -> None:
        conn.execute(text(f"SET LOCAL ROLE {role}"))

    yield switch
    conn.execute(text("RESET ROLE"))


def test_events_link_in_order_and_verify(conn: Connection) -> None:
    chain_id = f"test:{uuid.uuid4()}"
    events = [
        append_event(conn, chain_id=chain_id, event_type="t", payload={"n": n}) for n in range(3)
    ]
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[0].prev_hash == b"\x00" * 32
    assert events[1].prev_hash == events[0].hash and events[2].prev_hash == events[1].hash
    report = verify(conn)
    found = chain(report, chain_id)
    assert found.error is None and found.events == 3 and found.head_hash == events[2].hash


def test_app_appends_and_verifier_verifies(
    conn: Connection, as_role: Callable[[str], None]
) -> None:
    chain_id = f"test:{uuid.uuid4()}"
    as_role("opspilot_app")
    append_event(conn, chain_id=chain_id, event_type="t", payload={})
    as_role("opspilot_verifier")
    assert chain(verify(conn), chain_id).error is None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            "UPDATE audit_events SET payload = '{\"n\": 99}' WHERE chain_id = :c AND seq = 2",
            "seq 2: content does not match",
        ),
        (
            "UPDATE audit_events SET occurred_at = occurred_at + interval '1 second'"
            " WHERE chain_id = :c AND seq = 2",
            "seq 2: content does not match",
        ),
        ("DELETE FROM audit_events WHERE chain_id = :c AND seq = 2", "seq 3: expected seq 2"),
        (
            "UPDATE audit_events SET prev_hash = hash WHERE chain_id = :c AND seq = 3",
            "seq 3: prev_hash",
        ),
    ],
)
def test_tampered_event_is_detected(conn: Connection, change: str, expected: str) -> None:
    chain_id = f"test:{uuid.uuid4()}"
    for n in range(4):
        append_event(conn, chain_id=chain_id, event_type="t", payload={"n": n})
    tamper(conn, "audit_events", change, {"c": chain_id})
    error = chain(verify(conn), chain_id).error
    assert error is not None and error.startswith(expected), error


def test_chains_are_independent(conn: Connection) -> None:
    live_like, evaluation = f"test:{uuid.uuid4()}", eval_chain(uuid.uuid4())
    a = append_event(conn, chain_id=live_like, event_type="t", payload={})
    b = append_event(conn, chain_id=evaluation, event_type="t", payload={})
    assert a.seq == b.seq == 1  # each chain starts from its own zero hash
    tamper(conn, "audit_events", "DELETE FROM audit_events WHERE chain_id = :c", {"c": live_like})
    append_event(conn, chain_id=evaluation, event_type="t", payload={})
    report = verify(conn)
    assert chain(report, evaluation).error is None and chain(report, evaluation).events == 2


def test_concurrent_writers_keep_one_linear_chain(owner_engine: Engine) -> None:
    chain_id = f"test:concurrent:{uuid.uuid4()}"
    writers, per_writer = 6, 5
    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []

    def write() -> None:
        try:
            barrier.wait()
            for _ in range(per_writer):
                with owner_engine.begin() as c:
                    append_event(c, chain_id=chain_id, event_type="t", payload={})
        except BaseException as exc:  # noqa: BLE001  # surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=write) for _ in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with owner_engine.connect() as c:
        found = chain(verify(c), chain_id)
    assert found.error is None and found.events == found.head_seq == writers * per_writer


def test_payload_digests_are_not_mistaken_for_account_numbers(conn: Connection) -> None:
    digest = "1234567890" * 6 + "abcd"  # a hex digest that happens to start with ten digits
    event = append_event(
        conn,
        chain_id=f"test:{uuid.uuid4()}",
        event_type="t",
        payload={"digest": digest, "id": uuid.uuid4()},
    )
    assert event.payload["digest"] == digest


# ---------------------------------------------------------------- run evidence


@pytest.fixture
def run_graph(conn: Connection) -> dict[str, Any]:
    ds = f.dataset(conn)
    invoice = f.invoice(conn, ds)
    case = f.case(conn, invoice)
    policy = f.policy_version(conn)
    run = f.run(conn, case, policy)
    document = conn.execute(
        text("SELECT document_id FROM invoices WHERE id = :i"), {"i": invoice}
    ).scalar_one()
    parsed = conn.execute(
        text(
            "INSERT INTO parsed_documents (document_id, parser_version, text, structure,"
            " coordinates) VALUES (:d, 'test', 'Invoice INV-1', '{}', '{}') RETURNING id"
        ),
        {"d": document},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO extractions (run_id, schema_version, data, field_confidence, coordinates)"
            " VALUES (:r, '1', '{\"total\": \"120.00\"}', '{\"total\": 0.97}', '{}')"
        ),
        {"r": run},
    )
    check = f.check_result(conn, run)
    finding = f.finding(conn, run, case)
    conn.execute(
        text("INSERT INTO finding_check_results VALUES (:f, :c)"), {"f": finding, "c": check}
    )
    proposal_id = uuid.uuid4()
    row = {
        "id": proposal_id,
        "case_id": case,
        "run_id": run,
        "action": "place_on_hold",
        "source": "agent",
        "changed_by": None,
        "reason": None,
        "policy_version_id": policy,
    }
    conn.execute(
        text(
            "INSERT INTO proposals (id, case_id, run_id, action, source, policy_version_id,"
            " digest) VALUES (:id, :case_id, :run_id, 'place_on_hold', 'agent',"
            " :policy_version_id, :digest)"
        ),
        row | {"digest": proposal_digest_from_db(conn, row)},
    )
    conn.execute(
        text(
            "INSERT INTO llm_calls (run_id, purpose, provider, model, request_sha256, request,"
            " response, status) VALUES (:r, 'extract', 'replay', 'm', :h, '{\"t\": 0.0}',"
            " '{\"ok\": true}', 'ok')"
        ),
        {"r": run, "h": "a" * 64},
    )
    record_run_completed(conn, chain_id=LIVE, run_id=run, parsed_document_id=parsed, case_id=case)
    return {
        "case": case,
        "run": run,
        "parsed": parsed,
        "finding": finding,
        "check": check,
        "policy": policy,
        "proposal": proposal_id,
        "dataset": ds,
    }


def proposal_digest_from_db(conn: Connection, row: dict[str, Any]) -> bytes:
    """Digest the row as the database will return it (enums and IDs typed)."""
    from opspilot.domain.enums import ActionType, ProposalSource  # noqa: PLC0415

    return proposal_digest(
        row | {"action": ActionType(row["action"]), "source": ProposalSource(row["source"])}
    )


def test_completed_run_verifies(conn: Connection, run_graph: dict[str, Any]) -> None:
    report = verify(conn)
    assert not mismatches_about(report, run_graph["run"]), report.mismatches
    assert not mismatches_about(report, run_graph["proposal"]), report.mismatches


@pytest.mark.parametrize(
    ("table", "change"),
    [
        ("findings", "UPDATE findings SET explanation = 'edited' WHERE run_id = :run"),
        ("check_results", "UPDATE check_results SET status = 'pass' WHERE run_id = :run"),
        (
            "extractions",
            "UPDATE extractions SET field_confidence = '{\"total\": 0.99}' WHERE run_id = :run",
        ),
        ("finding_check_results", "DELETE FROM finding_check_results WHERE finding_id = :finding"),
        (
            "parsed_documents",
            "UPDATE parsed_documents SET text = 'Invoice INV-2' WHERE id = :parsed",
        ),
        ("llm_calls", "UPDATE llm_calls SET response = '{\"ok\": false}' WHERE run_id = :run"),
    ],
)
def test_tampered_evidence_is_detected(
    conn: Connection, run_graph: dict[str, Any], table: str, change: str
) -> None:
    tamper(conn, table, change, run_graph)
    found = mismatches_about(verify(conn), run_graph["run"])
    assert any("evidence does not match" in m for m in found), found


def test_tampered_proposal_is_detected(conn: Connection, run_graph: dict[str, Any]) -> None:
    tamper(
        conn,
        "proposals",
        "UPDATE proposals SET action = 'reject_invoice' WHERE id = :proposal",
        run_graph,
    )
    report = verify(conn)
    assert mismatches_about(report, run_graph["proposal"])
    assert mismatches_about(report, run_graph["run"])  # also part of the run's evidence


def test_run_without_completion_event_fails_closed(conn: Connection) -> None:
    ds = f.dataset(conn)
    run = f.run(conn, f.case(conn, f.invoice(conn, ds)), f.policy_version(conn))
    assert any("without a run.completed event" in m for m in mismatches_about(verify(conn), run))


def test_analyst_proposal_change_is_recorded_and_checked(
    conn: Connection, run_graph: dict[str, Any]
) -> None:
    analyst = f.user(conn, "analyst")
    row = {
        "id": uuid.uuid4(),
        "case_id": run_graph["case"],
        "run_id": None,
        "action": "draft_vendor_query",
        "source": "analyst",
        "changed_by": analyst,
        "reason": "Vendor should confirm the price first",
        "policy_version_id": run_graph["policy"],
    }
    conn.execute(
        text(
            "INSERT INTO proposals (id, case_id, action, source, changed_by, reason,"
            " policy_version_id, digest) VALUES (:id, :case_id, 'draft_vendor_query', 'analyst',"
            " :changed_by, :reason, :policy_version_id, :digest)"
        ),
        row | {"digest": proposal_digest_from_db(conn, row)},
    )
    assert any("no proposal.changed event" in m for m in mismatches_about(verify(conn), row["id"]))
    stored = rows_of(conn, Proposal, Proposal.id == row["id"])[0]
    record_proposal_changed(
        conn, chain_id=LIVE, proposal=stored, actor_id=analyst, case_id=run_graph["case"]
    )
    assert not mismatches_about(verify(conn), row["id"])


# ---------------------------------------------------------------- resolutions


@pytest.fixture
def resolution_graph(conn: Connection, run_graph: dict[str, Any]) -> dict[str, Any]:
    case = run_graph["case"]
    analyst = f.user(conn, "analyst")
    approver = f.user(conn, "approver", approval_limit=10000)
    vendor = f.vendor(conn, run_graph["dataset"])
    credit_note = f.supporting_document(conn, case, analyst, "credit_note")
    letter = f.supporting_document(conn, case, analyst, "bank_verification")
    verification = conn.execute(
        text(
            "INSERT INTO bank_verifications (case_id, vendor_id, channel, contact_source,"
            " contact_reference, verified_bank_fp, verified_last4, evidence_supporting_document_id,"
            " verified_by) VALUES (:c, :v, 'callback_to_master_contact', 'vendor_master',"
            " 'master phone on file', '3f9a1c2b7d10', '4471', :e, :u) RETURNING id"
        ),
        {"c": case, "v": vendor, "e": letter, "u": analyst},
    ).scalar_one()
    # As a writer does it: every ID up front, the digest computed before anything is inserted.
    resolution = {
        "id": uuid.uuid4(),
        "finding_id": run_graph["finding"],
        "case_id": case,
        "resolution_type": "credit_issued",
        "summary": "Credit note covers the overcharge",
        "structured": {"credit_amount": "12.00"},
        "resolved_by": analyst,
        "policy_version_id": run_graph["policy"],
    }
    evidence = [
        {
            "id": uuid.uuid4(),
            "resolution_id": resolution["id"],
            "supporting_document_id": sd,
            "bank_verification_id": bv,
            "erp_entity": None,
            "erp_id": None,
        }
        for sd, bv in ((credit_note, None), (None, verification))
    ]
    digest = resolution_digest(
        resolution_content(resolution, evidence, *cited_rows(conn, evidence))
    )
    conn.execute(
        text(
            "INSERT INTO finding_resolutions (id, finding_id, case_id, resolution_type, summary,"
            " structured, resolved_by, policy_version_id, digest) VALUES (:id, :finding_id,"
            " :case_id, 'credit_issued', :summary, CAST(:structured AS jsonb), :resolved_by,"
            " :policy_version_id, :digest)"
        ),
        resolution | {"structured": json.dumps(resolution["structured"]), "digest": digest},
    )
    conn.execute(
        text(
            "INSERT INTO finding_resolution_evidence (id, resolution_id, supporting_document_id,"
            " bank_verification_id) VALUES (:id, :resolution_id, :supporting_document_id,"
            " :bank_verification_id)"
        ),
        evidence,
    )
    assert resolution_digest(load_resolution_content(conn, resolution["id"])) == digest
    record_finding_resolved(
        conn,
        chain_id=LIVE,
        resolution_id=resolution["id"],
        digest=digest,
        actor_id=analyst,
        case_id=case,
    )
    decision = f.decision(conn, case, "approve", approver)
    conn.execute(
        text("INSERT INTO decision_resolution_acceptances VALUES (:d, :r, :g)"),
        {"d": decision, "r": resolution["id"], "g": digest},
    )
    return {
        "resolution": resolution["id"],
        "credit_note": credit_note,
        "verification": verification,
        "decision": decision,
        "letter": letter,
    }


def test_resolution_and_acceptance_verify(
    conn: Connection, resolution_graph: dict[str, Any]
) -> None:
    report = verify(conn)
    assert not mismatches_about(report, resolution_graph["resolution"]), report.mismatches
    assert not mismatches_about(report, resolution_graph["decision"]), report.mismatches


@pytest.mark.parametrize(
    ("table", "change"),
    [
        (
            "finding_resolutions",
            "UPDATE finding_resolutions SET summary = 'edited' WHERE id = :resolution",
        ),
        (
            "finding_resolution_evidence",
            "DELETE FROM finding_resolution_evidence WHERE supporting_document_id = :credit_note",
        ),
        (
            "bank_verifications",
            "UPDATE bank_verifications SET verified_last4 = '0000' WHERE id = :verification",
        ),
        (
            "documents",
            "UPDATE documents SET sha256 = :fake WHERE id ="
            " (SELECT document_id FROM supporting_documents WHERE id = :letter)",
        ),
    ],
)
def test_tampered_resolution_evidence_is_detected(
    conn: Connection, resolution_graph: dict[str, Any], table: str, change: str
) -> None:
    fake = hashlib.sha256(b"forged").hexdigest()
    tamper(conn, table, change, resolution_graph | {"fake": fake})
    found = mismatches_about(verify(conn), resolution_graph["resolution"])
    assert any("evidence does not match digest" in m for m in found), found


def test_tampered_acceptance_is_detected(
    conn: Connection, resolution_graph: dict[str, Any]
) -> None:
    tamper(
        conn,
        "decision_resolution_acceptances",
        "UPDATE decision_resolution_acceptances SET resolution_digest = :z"
        " WHERE decision_id = :decision",
        resolution_graph | {"z": b"\x01" * 32},
    )
    found = mismatches_about(verify(conn), resolution_graph["decision"])
    assert any("accepted digest differs" in m for m in found), found
