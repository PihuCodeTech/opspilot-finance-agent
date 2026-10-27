"""Evidence, proposal and resolution digests (`06-database.md` §5).

Each digest is SHA-256 over canonical JSON of the rows it covers. Rows are
taken column by column, without `created_at` (insertion time is recorded by the
audit event, and proposal/resolution digests are computed before their row
exists). Lists are sorted by each row's canonical form, so the digest does not
depend on query order. JSON floats from `jsonb` columns are read as decimals of
their shortest representation, which is what canonical JSON allows.

The `record_*` helpers write the audit event that carries a digest, in the
caller's transaction, so the change and its record commit together.
"""

import uuid
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Table, select

from opspilot.audit.chain import AuditEvent, append_event
from opspilot.db.models import (
    BankVerification,
    Base,
    CheckResult,
    Document,
    Extraction,
    Finding,
    FindingCheckResult,
    FindingCitation,
    FindingResolution,
    FindingResolutionEvidence,
    LineMatch,
    LlmCall,
    ParsedDocument,
    Proposal,
    SupportingDocument,
)
from opspilot.domain.digest import canonical_json, sha256_digest, sha256_hex
from opspilot.domain.enums import ProposalSource

RUN_COMPLETED = "run.completed"
PROPOSAL_CHANGED = "proposal.changed"
FINDING_RESOLVED = "finding.resolved"

_EXCLUDED = frozenset({"created_at"})


class DigestInputError(LookupError):
    """A row the digest needs is missing, so the digest cannot be computed."""


def _plain(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def row_content(row: Mapping[str, Any], *, exclude: Iterable[str] = ()) -> dict[str, Any]:
    skip = _EXCLUDED | set(exclude)
    return {k: _plain(v) for k, v in row.items() if k not in skip}


def _sorted(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=canonical_json)


def table_of(model: type[Base]) -> Table:
    table = model.__table__
    if not isinstance(table, Table):
        raise TypeError(f"{model.__name__} is not mapped to a table")
    return table


def rows_of(conn: Connection, model: type[Base], *where: Any) -> list[dict[str, Any]]:
    result = conn.execute(select(table_of(model)).where(*where))
    return [dict(r._mapping) for r in result]


# ---------------------------------------------------------------- proposals


def proposal_content(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """The proposal as written: every column except its own digest and insertion time."""
    return row_content(proposal, exclude={"digest"})


def proposal_digest(proposal: Mapping[str, Any]) -> bytes:
    return sha256_digest(proposal_content(proposal))


def record_proposal_changed(
    conn: Connection,
    *,
    chain_id: str,
    proposal: Mapping[str, Any],
    actor_id: uuid.UUID | None,
    case_id: uuid.UUID,
) -> AuditEvent:
    return append_event(
        conn,
        chain_id=chain_id,
        event_type=PROPOSAL_CHANGED,
        payload={"proposal_id": proposal["id"], "proposal_digest": proposal_digest(proposal).hex()},
        actor_id=actor_id,
        case_id=case_id,
    )


# ---------------------------------------------------------------- resolutions


def resolution_content(
    resolution: Mapping[str, Any],
    evidence: Iterable[Mapping[str, Any]],
    supporting_documents: Iterable[Mapping[str, Any]],
    bank_verifications: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """`supporting_documents` rows carry the referenced file's `sha256` alongside the row."""
    return {
        "resolution": row_content(resolution, exclude={"digest"}),
        "evidence": _sorted(row_content(e) for e in evidence),
        "supporting_documents": _sorted(row_content(s) for s in supporting_documents),
        "bank_verifications": _sorted(row_content(b) for b in bank_verifications),
    }


def resolution_digest(content: Mapping[str, Any]) -> bytes:
    return sha256_digest(content)


def cited_rows(
    conn: Connection, evidence: Iterable[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The supporting documents (with file SHA-256) and bank verifications the evidence cites.

    A writer calls this before inserting the resolution: the cited rows already exist.
    """
    evidence = list(evidence)
    bank_ids = {e["bank_verification_id"] for e in evidence if e.get("bank_verification_id")}
    verifications = rows_of(conn, BankVerification, BankVerification.id.in_(bank_ids))
    if len(verifications) != len(bank_ids):
        raise DigestInputError("resolution cites a missing bank verification")
    supporting_ids = {
        e["supporting_document_id"] for e in evidence if e.get("supporting_document_id")
    }
    supporting_ids |= {v["evidence_supporting_document_id"] for v in verifications}
    supporting = [
        dict(r._mapping)
        for r in conn.execute(
            select(table_of(SupportingDocument), Document.sha256)
            .join(Document, Document.id == SupportingDocument.document_id)
            .where(SupportingDocument.id.in_(supporting_ids))
        )
    ]
    if len(supporting) != len(supporting_ids):
        raise DigestInputError("resolution cites a missing supporting document")
    return supporting, verifications


def load_resolution_content(conn: Connection, resolution_id: uuid.UUID) -> dict[str, Any]:
    resolutions = rows_of(conn, FindingResolution, FindingResolution.id == resolution_id)
    if not resolutions:
        raise DigestInputError(f"resolution {resolution_id} not found")
    evidence = rows_of(
        conn, FindingResolutionEvidence, FindingResolutionEvidence.resolution_id == resolution_id
    )
    try:
        supporting, verifications = cited_rows(conn, evidence)
    except DigestInputError as exc:
        raise DigestInputError(f"resolution {resolution_id}: {exc}") from exc
    return resolution_content(resolutions[0], evidence, supporting, verifications)


def record_finding_resolved(
    conn: Connection,
    *,
    chain_id: str,
    resolution_id: uuid.UUID,
    digest: bytes,
    actor_id: uuid.UUID,
    case_id: uuid.UUID,
) -> AuditEvent:
    return append_event(
        conn,
        chain_id=chain_id,
        event_type=FINDING_RESOLVED,
        payload={"resolution_id": resolution_id, "resolution_digest": digest.hex()},
        actor_id=actor_id,
        case_id=case_id,
    )


# ---------------------------------------------------------------- run evidence


def _llm_call_content(call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": call["id"],
        "purpose": call["purpose"],
        "provider": call["provider"],
        "model": call["model"],
        "status": call["status"],
        "request_sha256": call["request_sha256"],
        "request_digest": sha256_hex(_plain(call["request"])),
        "response_digest": sha256_hex(_plain(call["response"])),
    }


def evidence_content(
    conn: Connection, run_id: uuid.UUID, parsed_document_id: uuid.UUID
) -> dict[str, Any]:
    parsed = rows_of(conn, ParsedDocument, ParsedDocument.id == parsed_document_id)
    if not parsed:
        raise DigestInputError(f"parsed document {parsed_document_id} not found")
    findings = rows_of(conn, Finding, Finding.run_id == run_id)
    finding_ids = [f["id"] for f in findings]
    proposals = rows_of(
        conn, Proposal, Proposal.run_id == run_id, Proposal.source == ProposalSource.AGENT
    )
    return {
        "run_id": run_id,
        # The parsed text can be large; the row is represented by its own digest.
        "parsed_document": sha256_hex(row_content(parsed[0])),
        "extractions": _sorted(
            row_content(r) for r in rows_of(conn, Extraction, Extraction.run_id == run_id)
        ),
        "line_matches": _sorted(
            row_content(r) for r in rows_of(conn, LineMatch, LineMatch.run_id == run_id)
        ),
        "check_results": _sorted(
            row_content(r) for r in rows_of(conn, CheckResult, CheckResult.run_id == run_id)
        ),
        "findings": _sorted(row_content(r) for r in findings),
        "finding_check_results": _sorted(
            row_content(r)
            for r in rows_of(
                conn, FindingCheckResult, FindingCheckResult.finding_id.in_(finding_ids)
            )
        ),
        "finding_citations": _sorted(
            row_content(r)
            for r in rows_of(conn, FindingCitation, FindingCitation.finding_id.in_(finding_ids))
        ),
        "agent_proposals": _sorted(row_content(r) for r in proposals),
        "llm_calls": _sorted(
            _llm_call_content(r) for r in rows_of(conn, LlmCall, LlmCall.run_id == run_id)
        ),
    }


def evidence_digest(conn: Connection, run_id: uuid.UUID, parsed_document_id: uuid.UUID) -> bytes:
    return sha256_digest(evidence_content(conn, run_id, parsed_document_id))


def record_run_completed(
    conn: Connection,
    *,
    chain_id: str,
    run_id: uuid.UUID,
    parsed_document_id: uuid.UUID,
    case_id: uuid.UUID,
) -> AuditEvent:
    """Called after the run's evidence rows are inserted, in the same transaction."""
    digest = evidence_digest(conn, run_id, parsed_document_id)
    payload = {
        "run_id": run_id,
        "parsed_document_id": parsed_document_id,
        "evidence_digest": digest.hex(),
    }
    return append_event(
        conn, chain_id=chain_id, event_type=RUN_COMPLETED, payload=payload, case_id=case_id
    )
