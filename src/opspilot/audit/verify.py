"""Independent verification of audit chains and digests (`opspilot audit verify`).

Runs as `opspilot_verifier` (read-only). Recomputes every chain link and every
evidence, proposal and resolution digest from the stored rows. Within a chain
only the first broken link is reported, since every later link depends on it;
digest checks are independent and each mismatch is reported.

Tamper-evident, not tamper-proof: an account with owner rights could rewrite a
whole chain consistently, so each chain's head hash is printed for recording
elsewhere. Removing the newest events is only visible against such a record.
"""

import datetime as dt
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, insert, select

from opspilot.audit.chain import ZERO_HASH, event_content, event_hash
from opspilot.audit.digests import (
    FINDING_RESOLVED,
    PROPOSAL_CHANGED,
    RUN_COMPLETED,
    DigestInputError,
    evidence_digest,
    load_resolution_content,
    proposal_digest,
    resolution_digest,
    rows_of,
    table_of,
)
from opspilot.db.models import (
    AuditEvent,
    AuditVerification,
    Case,
    DecisionResolutionAcceptance,
    FindingResolution,
    InvestigationRun,
    Invoice,
    ParsedDocument,
    Proposal,
)
from opspilot.domain.enums import ProposalSource, RunStatus


@dataclass(frozen=True)
class ChainReport:
    chain_id: str
    events: int
    head_seq: int
    head_hash: bytes
    error: str | None = None  # the first broken link, if any


@dataclass
class VerifyReport:
    chains: list[ChainReport] = field(default_factory=list)
    digests_checked: int = 0
    mismatches: list[str] = field(default_factory=list)

    @property
    def chains_ok(self) -> bool:
        return all(c.error is None for c in self.chains)

    @property
    def ok(self) -> bool:
        return self.chains_ok and not self.mismatches


def _events(conn: Connection) -> Iterator[dict[str, Any]]:
    table = table_of(AuditEvent)
    # Statement-level: Connection.execution_options would change the caller's connection.
    result = conn.execute(
        select(table).order_by(table.c.chain_id, table.c.seq).execution_options(yield_per=500)
    )
    for row in result:
        yield dict(row._mapping)


def _check_link(event: dict[str, Any], expected_seq: int, prev: bytes) -> str | None:
    where = f"seq {event['seq']}"
    if event["seq"] != expected_seq:
        return f"{where}: expected seq {expected_seq} (an event is missing or out of order)"
    if bytes(event["prev_hash"]) != prev:
        return f"{where}: prev_hash does not match the previous event's hash"
    if event_hash(prev, event_content(event)) != bytes(event["hash"]):
        return f"{where}: content does not match its hash (event was changed)"
    return None


def _verify_chains(conn: Connection, report: VerifyReport) -> list[dict[str, Any]]:
    """Walk every chain in order; return the events that carry digests for the next checks."""
    carriers: list[dict[str, Any]] = []
    current: str | None = None
    count, head_seq, prev, error = 0, 0, ZERO_HASH, None

    def close() -> None:
        if current is not None:
            report.chains.append(ChainReport(current, count, head_seq, prev, error))

    for event in _events(conn):
        if event["chain_id"] != current:
            close()
            current, count, head_seq, prev, error = event["chain_id"], 0, 0, ZERO_HASH, None
        count += 1
        if error is None:
            error = _check_link(event, head_seq + 1, prev)
        head_seq, prev = event["seq"], bytes(event["hash"])
        if event["event_type"] in {RUN_COMPLETED, PROPOSAL_CHANGED, FINDING_RESOLVED}:
            carriers.append(event)
    close()
    return carriers


def _payload_hex(event: dict[str, Any], key: str) -> bytes | None:
    value = event["payload"].get(key)
    try:
        return bytes.fromhex(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _payload_id(event: dict[str, Any], key: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(event["payload"].get(key)))
    except ValueError:
        return None


def _where(event: dict[str, Any]) -> str:
    return f"{event['chain_id']} seq {event['seq']} ({event['event_type']})"


def _verify_runs(conn: Connection, events: list[dict[str, Any]], report: VerifyReport) -> None:
    completed: set[uuid.UUID] = set()
    for event in (e for e in events if e["event_type"] == RUN_COMPLETED):
        run_id = _payload_id(event, "run_id")
        parsed_id = _payload_id(event, "parsed_document_id")
        stored = _payload_hex(event, "evidence_digest")
        report.digests_checked += 1
        if run_id is None or parsed_id is None or stored is None:
            report.mismatches.append(f"{_where(event)}: payload lacks run, document or digest")
            continue
        completed.add(run_id)
        document_matches = conn.execute(
            select(ParsedDocument.id)
            .join(Invoice, Invoice.document_id == ParsedDocument.document_id)
            .join(Case, Case.invoice_id == Invoice.id)
            .join(InvestigationRun, InvestigationRun.case_id == Case.id)
            .where(InvestigationRun.id == run_id, ParsedDocument.id == parsed_id)
        ).first()
        if document_matches is None:
            report.mismatches.append(f"run {run_id}: parsed document is not this case's invoice")
            continue
        try:
            recomputed = evidence_digest(conn, run_id, parsed_id)
        except DigestInputError as exc:
            report.mismatches.append(f"run {run_id}: {exc}")
            continue
        if recomputed != stored:
            report.mismatches.append(f"run {run_id}: evidence does not match its recorded digest")
    succeeded = conn.execute(
        select(InvestigationRun.id).where(InvestigationRun.status == RunStatus.SUCCEEDED)
    ).scalars()
    for run_id in sorted(set(succeeded) - completed, key=str):
        report.mismatches.append(f"run {run_id}: succeeded without a {RUN_COMPLETED} event")


def _verify_proposals(conn: Connection, events: list[dict[str, Any]], report: VerifyReport) -> None:
    proposals = {p["id"]: p for p in rows_of(conn, Proposal)}
    for proposal in proposals.values():
        report.digests_checked += 1
        if proposal_digest(proposal) != bytes(proposal["digest"]):
            report.mismatches.append(f"proposal {proposal['id']}: content does not match digest")
    recorded: set[uuid.UUID] = set()
    for event in (e for e in events if e["event_type"] == PROPOSAL_CHANGED):
        proposal_id = _payload_id(event, "proposal_id")
        row = proposals.get(proposal_id) if proposal_id else None
        if proposal_id is None or row is None:
            report.mismatches.append(f"{_where(event)}: proposal not found")
            continue
        recorded.add(proposal_id)
        if _payload_hex(event, "proposal_digest") != bytes(row["digest"]):
            report.mismatches.append(f"{_where(event)}: digest differs from proposal {proposal_id}")
    for proposal in proposals.values():
        if proposal["source"] == ProposalSource.ANALYST and proposal["id"] not in recorded:
            report.mismatches.append(f"proposal {proposal['id']}: no {PROPOSAL_CHANGED} event")


def _verify_resolutions(
    conn: Connection, events: list[dict[str, Any]], report: VerifyReport
) -> None:
    stored = {r.id: bytes(r.digest) for r in conn.execute(select(FindingResolution))}
    for resolution_id, digest in stored.items():
        report.digests_checked += 1
        try:
            content = load_resolution_content(conn, resolution_id)
        except DigestInputError as exc:
            report.mismatches.append(str(exc))
            continue
        if resolution_digest(content) != digest:
            report.mismatches.append(f"resolution {resolution_id}: evidence does not match digest")
    recorded: set[uuid.UUID] = set()
    for event in (e for e in events if e["event_type"] == FINDING_RESOLVED):
        resolution_id = _payload_id(event, "resolution_id")
        if resolution_id is None or resolution_id not in stored:
            report.mismatches.append(f"{_where(event)}: resolution not found")
            continue
        recorded.add(resolution_id)
        if _payload_hex(event, "resolution_digest") != stored[resolution_id]:
            report.mismatches.append(f"{_where(event)}: digest differs from {resolution_id}")
    for resolution_id in sorted(stored.keys() - recorded, key=str):
        report.mismatches.append(f"resolution {resolution_id}: no {FINDING_RESOLVED} event")
    for acceptance in conn.execute(select(DecisionResolutionAcceptance)):
        report.digests_checked += 1
        if stored.get(acceptance.resolution_id) != bytes(acceptance.resolution_digest):
            report.mismatches.append(
                f"decision {acceptance.decision_id}: accepted digest differs from resolution"
                f" {acceptance.resolution_id}"
            )


def verify(conn: Connection) -> VerifyReport:
    """Read-only; run inside one REPEATABLE READ transaction for a consistent snapshot."""
    report = VerifyReport()
    carriers = _verify_chains(conn, report)
    _verify_runs(conn, carriers, report)
    _verify_proposals(conn, carriers, report)
    _verify_resolutions(conn, carriers, report)
    return report


def record(
    conn: Connection,
    report: VerifyReport,
    *,
    started_at: dt.datetime,
    finished_at: dt.datetime,
    verified_as: str,
) -> uuid.UUID:
    """Store one result (append-only), written by the app role after a read-only run."""
    verification_id = uuid.uuid4()
    conn.execute(
        insert(AuditVerification).values(
            id=verification_id,
            started_at=started_at,
            finished_at=finished_at,
            verified_as=verified_as,
            ok=report.ok,
            chains_ok=report.chains_ok,
            digests_ok=not report.mismatches,
            chains=[
                {
                    "chain_id": c.chain_id,
                    "events": c.events,
                    "first_seq": 1 if c.events else 0,
                    "head_seq": c.head_seq,
                    "head_hash": c.head_hash.hex(),
                    "error": c.error,
                }
                for c in report.chains
            ],
            digests_checked=report.digests_checked,
            failures=[
                *(f"chain {c.chain_id}: {c.error}" for c in report.chains if c.error),
                *report.mismatches,
            ],
        )
    )
    return verification_id
