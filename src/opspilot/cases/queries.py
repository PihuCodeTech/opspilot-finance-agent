"""Read models for the API and the UI: what a caller may see of live cases.

Both web surfaces read through these functions, so the rules that shape a
response live in one place:

- **Evaluation cases are invisible.** Every case read starts at
  `require_live_case`, which answers 404 for an evaluation case, exactly as for
  a case that doesn't exist (`06-api.md` §4, §10).
- **Bank data is masked.** No fingerprint or token leaves this module: every
  `*bank_fp` value and every `BANK_ACCT[fp:…]` token becomes `••••` + the
  last four digits (`mask_bank`).
- **What the caller may do** is computed with the service's own checks
  (allowlists, approver authority), so the UI never decides it.
"""

import base64
import datetime as dt
import itertools
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, and_, func, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import distinct_on

from opspilot.cases import service
from opspilot.cases.errors import ForbiddenError, NotFoundError, ValidationFailedError
from opspilot.cases.state import Actor, EvidenceState, load_evidence
from opspilot.db.models import (
    ActionReversal,
    AuditEvent,
    AuditVerification,
    BankVerification,
    Case,
    CheckResult,
    Decision,
    Document,
    ExecutedAction,
    Extraction,
    Finding,
    FindingCheckResult,
    FindingCitation,
    FindingResolution,
    FindingResolutionEvidence,
    InvestigationRun,
    Invoice,
    Job,
    LineMatch,
    LlmCall,
    PolicyChunk,
    PoLine,
    Proposal,
    ReceiptLine,
    SupportingDocument,
    User,
    Vendor,
)
from opspilot.domain.enums import (
    ActionType,
    ApproverTier,
    CaseStatus,
    CheckCode,
    FindingCode,
    ProposalSource,
    UserRole,
)

S = CaseStatus
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MASK = "••••"

# ---------------------------------------------------------------- pagination


def page_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if not 1 <= limit <= MAX_LIMIT:
        raise ValidationFailedError(f"limit must be 1–{MAX_LIMIT}", field="limit")
    return limit


def encode_cursor(*parts: object) -> str:
    raw = "|".join(p.isoformat() if isinstance(p, dt.datetime) else str(p) for p in parts)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, count: int) -> list[str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        raise ValidationFailedError("invalid cursor", field="cursor") from None
    parts = raw.split("|")
    if len(parts) != count:
        raise ValidationFailedError("invalid cursor", field="cursor")
    return parts


def _time_id_cursor(cursor: str | None) -> tuple[dt.datetime, uuid.UUID] | None:
    if cursor is None:
        return None
    at, ident = decode_cursor(cursor, 2)
    try:
        return dt.datetime.fromisoformat(at), uuid.UUID(ident)
    except ValueError:
        raise ValidationFailedError("invalid cursor", field="cursor") from None


@dataclass(frozen=True)
class Page:
    items: list[dict[str, Any]]
    next_cursor: str | None
    total: int | None = None  # rows matching the filters, when the caller asked for it


# ---------------------------------------------------------------- masking


_TOKEN = re.compile(r"BANK_ACCT\[fp:([0-9a-f]{12})\]")


def _is_fingerprint(record: dict[Any, Any], key: object, item: object) -> bool:
    """A `*bank_fp` field, or an evidence reference `{"field": "bank_fp", "value": …}`."""
    if not isinstance(key, str) or not isinstance(item, str):
        return False
    if key.endswith("bank_fp"):
        return True
    return key == "value" and str(record.get("field", "")).endswith("bank_fp")


def _fingerprints(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_fingerprint(value, key, item):
                found.add(item)
            _fingerprints(item, found)
    elif isinstance(value, list | tuple):
        for item in value:
            _fingerprints(item, found)
    elif isinstance(value, str):
        found.update(_TOKEN.findall(value))


def _last4s(conn: Connection, fps: set[str]) -> dict[str, str]:
    if not fps:
        return {}
    rows = conn.execute(
        text(
            "SELECT bank_fp, last4 FROM vendor_bank_accounts WHERE bank_fp = ANY(:f)"
            " UNION SELECT bank_fp, bank_last4 FROM extractions"
            "   WHERE bank_fp = ANY(:f) AND bank_last4 IS NOT NULL"
            " UNION SELECT verified_bank_fp, verified_last4 FROM bank_verifications"
            "   WHERE verified_bank_fp = ANY(:f)"
            " UNION SELECT t->>'bank_fp', t->>'last4'"
            "   FROM parsed_documents, jsonb_array_elements(bank_tokens) t"
            "   WHERE t->>'bank_fp' = ANY(:f)"
        ),
        {"f": sorted(fps)},
    ).all()
    return {fp: last4 for fp, last4 in rows if last4}


def _masked(fp: str, last4s: dict[str, str]) -> str:
    return MASK + last4s.get(fp, "")


def _apply_mask(value: Any, last4s: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            k: _masked(v, last4s) if _is_fingerprint(value, k, v) else _apply_mask(v, last4s)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_apply_mask(v, last4s) for v in value]
    if isinstance(value, str):
        return _TOKEN.sub(lambda m: f"BANK_ACCT[{_masked(m.group(1), last4s)}]", value)
    return value


def mask_bank(conn: Connection, value: Any) -> Any:
    """Replace every bank fingerprint (field or token) with `••••` + its last 4 digits."""
    found: set[str] = set()
    _fingerprints(value, found)
    return _apply_mask(value, _last4s(conn, found))


# ---------------------------------------------------------------- visibility


def require_live_case(conn: Connection, case_id: uuid.UUID) -> dict[str, Any]:
    """The case, if it is live. Evaluation cases are 404 for every role."""
    row = conn.execute(select(Case.__table__).where(Case.id == case_id)).first()
    if row is None or row.eval_run_id is not None:
        raise NotFoundError("case not found")
    return dict(row._mapping)


def require_reader(actor: Actor) -> None:
    if actor.role not in {UserRole.ANALYST, UserRole.APPROVER, UserRole.AUDITOR}:
        raise ForbiddenError("this account can't read cases")


def require_role(actor: Actor, role: UserRole) -> None:
    """For endpoints whose role check precedes any service call (listing, uploads)."""
    if actor.role is not role:
        raise ForbiddenError(f"only an {role} can do this")


# ---------------------------------------------------------------- summaries


def _vendor_ids(conn: Connection, run_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """The vendor each run identified, from its vendor check's evidence reference."""
    ids = [r for r in run_ids if r is not None]
    if not ids:
        return {}
    rows = conn.execute(
        select(CheckResult.run_id, CheckResult.evidence_refs).where(
            CheckResult.run_id.in_(ids), CheckResult.check_code == CheckCode.VENDOR_APPROVED
        )
    ).all()
    found = {}
    for run_id, refs in rows:
        ref = next((r for r in refs or [] if r.get("entity") == "vendor" and r.get("id")), None)
        if ref is not None:
            found[run_id] = uuid.UUID(ref["id"])
    return found


def _summaries(conn: Connection, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    run_ids = [c["evidence_run_id"] for c in cases if c["evidence_run_id"]]
    extractions = {
        row.run_id: row.data
        for row in conn.execute(
            select(Extraction.run_id, Extraction.data).where(Extraction.run_id.in_(run_ids))
        )
    }
    findings: dict[uuid.UUID, list[str]] = {}
    for run_id, code in conn.execute(
        select(Finding.run_id, Finding.exception_type)
        .where(Finding.run_id.in_(run_ids))
        .order_by(Finding.exception_type)
    ):
        findings.setdefault(run_id, [])
        if str(code) not in findings[run_id]:
            findings[run_id].append(str(code))
    vendor_ids = _vendor_ids(conn, run_ids)
    vendors = {
        row.id: row
        for row in conn.execute(
            select(Vendor.id, Vendor.vendor_code, Vendor.name).where(
                Vendor.id.in_(set(vendor_ids.values()))
            )
        )
    }
    proposals = _current_proposals(conn, [c["id"] for c in cases])
    people = _users(conn, [c["submitted_by"] for c in cases])
    outcomes = _outcomes(conn, [c["id"] for c in cases if c["status"] == S.CLOSED])
    items = []
    for case in cases:
        run_id = case["evidence_run_id"]
        data = extractions.get(run_id) or {}
        vendor_id = vendor_ids.get(run_id) if run_id else None
        vendor = vendors.get(vendor_id) if vendor_id else None
        proposal = proposals.get(case["id"])
        items.append(
            {
                "id": case["id"],
                "status": case["status"],
                "vendor": {"id": vendor.id, "code": vendor.vendor_code, "name": vendor.name}
                if vendor
                else None,
                "vendor_name_read": data.get("vendor_name"),
                "invoice_number": data.get("invoice_number"),
                "po_number": data.get("po_number"),
                "total": data.get("total"),
                "finding_types": findings.get(run_id, []),
                "proposed_action": proposal["action"] if proposal else None,
                "required_approval_amount": case["required_approval_amount"],
                "required_approver_tier": case["required_approver_tier"],
                "submitted_by": people.get(case["submitted_by"]),
                "submitted_at": case["submitted_at"],
                "outcome": (outcomes.get(case["id"]) or {}).get("action"),
                "created_at": case["created_at"],
                "updated_at": case["updated_at"],
                "version": case["version"],
            }
        )
    return items


def _outcomes(conn: Connection, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """The executed, unreversed action of each closed case (a dismissed case has none)."""
    if not case_ids:
        return {}
    rows = conn.execute(
        select(
            ExecutedAction.case_id,
            ExecutedAction.action,
            ExecutedAction.executed_at,
            Decision.actor_id,
        )
        .join(Decision, Decision.id == ExecutedAction.decision_id)
        .outerjoin(ActionReversal, ActionReversal.executed_action_id == ExecutedAction.id)
        .where(ExecutedAction.case_id.in_(case_ids), ActionReversal.id.is_(None))
    ).all()
    return {
        r.case_id: {"action": r.action, "executed_at": r.executed_at, "approved_by": r.actor_id}
        for r in rows
    }


def _current_proposals(
    conn: Connection, case_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    if not case_ids:
        return {}
    rows = conn.execute(
        select(Proposal.__table__)
        .where(Proposal.case_id.in_(case_ids))
        .ext(distinct_on(Proposal.case_id))
        .order_by(Proposal.case_id, Proposal.created_at.desc(), Proposal.id.desc())
    ).all()
    return {row.case_id: dict(row._mapping) for row in rows}


def _page(
    conn: Connection, query: Any, limit: int, cursor: str | None, *, newest_first: bool = True
) -> Page:
    position = _time_id_cursor(cursor)
    if position is not None:
        key = tuple_(Case.created_at, Case.id)
        query = query.where(key < position if newest_first else key > position)
    order = (Case.created_at.desc(), Case.id.desc()) if newest_first else (Case.created_at, Case.id)
    rows = [dict(r._mapping) for r in conn.execute(query.order_by(*order).limit(limit + 1))]
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if more else None
    return Page(mask_bank(conn, _summaries(conn, rows)), next_cursor)


def list_cases(  # the documented query parameters
    conn: Connection,
    actor: Actor,
    *,
    status: CaseStatus | None = None,
    exception_type: FindingCode | None = None,
    vendor_id: uuid.UUID | None = None,
    q: str | None = None,
    oldest_first: bool = False,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page:
    """The live queue, newest first (or oldest first). Evaluation cases never appear.

    `q` finds an invoice or PO number as read from the document (a substring, any case).
    """
    require_reader(actor)
    query = select(Case.__table__).where(Case.eval_run_id.is_(None))
    if status is not None:
        query = query.where(Case.status == status)
    if q is not None and q.strip():
        needle = "%" + _like_escape(q.strip()[:SEARCH_MAX]) + "%"
        query = query.where(
            select(Extraction.id)
            .where(
                Extraction.run_id == Case.evidence_run_id,
                or_(
                    Extraction.data["invoice_number"].astext.ilike(needle, escape="\\"),
                    Extraction.data["po_number"].astext.ilike(needle, escape="\\"),
                ),
            )
            .exists()
        )
    if exception_type is not None:
        query = query.where(
            select(Finding.id)
            .where(Finding.run_id == Case.evidence_run_id, Finding.exception_type == exception_type)
            .exists()
        )
    if vendor_id is not None:
        query = query.where(
            select(CheckResult.id)
            .where(
                CheckResult.run_id == Case.evidence_run_id,
                CheckResult.check_code == CheckCode.VENDOR_APPROVED,
                CheckResult.evidence_refs.contains([{"entity": "vendor", "id": str(vendor_id)}]),
            )
            .exists()
        )
    page = _page(conn, query, page_limit(limit), cursor, newest_first=not oldest_first)
    total = conn.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    return Page(page.items, page.next_cursor, int(total))


SEARCH_MAX = 64


def queue_vendors(conn: Connection, actor: Actor) -> list[dict[str, Any]]:
    """Vendors identified on live cases, for the queue's vendor filter."""
    require_reader(actor)
    runs = conn.execute(
        select(Case.evidence_run_id).where(
            Case.eval_run_id.is_(None), Case.evidence_run_id.is_not(None)
        )
    ).scalars()
    ids = set(_vendor_ids(conn, [r for r in runs if r is not None]).values())
    rows = conn.execute(
        select(Vendor.id, Vendor.name).where(Vendor.id.in_(ids)).order_by(Vendor.name)
    ).all()
    return [{"id": r.id, "name": r.name} for r in rows]


PROGRESS_STEPS = (
    ("records", "Gathering records"),
    ("extract", "Reading document"),
    ("match", "Matching lines"),
    ("checks", "Running checks"),
    ("policy", "Consulting policy"),
    ("explain", "Explaining"),
)


def progress(conn: Connection, actor: Actor, case_id: uuid.UUID) -> dict[str, Any]:
    """Where the latest run has got to, read from what it has written, and any quota wait."""
    require_reader(actor)
    case = require_live_case(conn, case_id)
    run = conn.execute(
        select(InvestigationRun.__table__)
        .where(InvestigationRun.case_id == case_id)
        .order_by(InvestigationRun.attempt.desc())
        .limit(1)
    ).first()
    done: set[str] = set()
    if run is not None:

        def wrote(model: Any) -> bool:
            return (
                conn.execute(select(model.id).where(model.run_id == run.id).limit(1)).first()
                is not None
            )

        if wrote(Extraction):
            done |= {"records", "extract"}
        if wrote(LineMatch):
            done.add("match")
        if wrote(CheckResult):
            done.add("checks")
        if conn.execute(
            select(LlmCall.id)
            .where(LlmCall.run_id == run.id, LlmCall.purpose == "investigate")
            .limit(1)
        ).first():
            done.add("policy")
        if wrote(Finding) or wrote(Proposal):
            done |= {"policy", "explain"}
    current = next((key for key, _ in PROGRESS_STEPS if key not in done), None)
    waiting = conn.execute(
        select(Job.run_after)
        .where(
            Job.case_id == case_id,
            Job.status == "queued",
            Job.run_after > func.now(),
            Job.last_error.ilike("%quota%"),
        )
        .order_by(Job.run_after)
        .limit(1)
    ).scalar()
    return {
        "status": case["status"],
        "version": case["version"],
        "steps": [
            {"key": key, "label": label, "done": key in done, "current": key == current}
            for key, label in PROGRESS_STEPS
        ],
        "quota_resumes_at": waiting,
    }


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def queue_counts(conn: Connection, actor: Actor) -> dict[str, int]:
    """Live cases per status, for the queue's subline and filters."""
    require_reader(actor)
    rows = conn.execute(
        select(Case.status, func.count()).where(Case.eval_run_id.is_(None)).group_by(Case.status)
    ).all()
    return {str(status): int(count) for status, count in rows}


def _approvable(actor: Actor) -> Any:
    """Pending cases this approver may approve: tier and limit, never their own submission."""
    rule = and_(
        Case.eval_run_id.is_(None),
        Case.status == S.PENDING_APPROVAL,
        or_(Case.submitted_by.is_(None), Case.submitted_by != actor.id),
    )
    if actor.approval_unlimited:
        return rule
    return and_(
        rule,
        Case.required_approver_tier == ApproverTier.STANDARD,
        Case.required_approval_amount.is_not(None),
        Case.required_approval_amount <= (actor.approval_limit or 0),
    )


def approval_counts(conn: Connection, actor: Actor) -> dict[str, Any]:
    """What waits for this approver, what needs someone else, and what is held."""
    if actor.role is not UserRole.APPROVER:
        return {"mine": 0, "others": 0, "held": 0, "oldest_submitted_at": None}
    mine = conn.execute(
        select(func.count(), func.min(Case.submitted_at)).where(_approvable(actor))
    ).one()
    pending = conn.execute(
        select(func.count()).where(Case.eval_run_id.is_(None), Case.status == S.PENDING_APPROVAL)
    ).scalar_one()
    held = conn.execute(
        select(func.count()).where(Case.eval_run_id.is_(None), Case.status == S.HELD_FOR_APPROVER)
    ).scalar_one()
    return {
        "mine": int(mine[0]),
        "others": int(pending) - int(mine[0]),
        "held": int(held),
        "oldest_submitted_at": mine[1],
    }


def list_approvals(
    conn: Connection,
    actor: Actor,
    *,
    held: bool = False,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page:
    """Cases this approver may approve; or, with `held`, the held cases (read-only)."""
    require_role(actor, UserRole.APPROVER)
    query = select(Case.__table__)
    if held:
        query = query.where(Case.eval_run_id.is_(None), Case.status == S.HELD_FOR_APPROVER)
    else:
        query = query.where(_approvable(actor))
    return _page(conn, query, page_limit(limit), cursor, newest_first=False)


# ---------------------------------------------------------------- the case page


def _users(conn: Connection, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, dict[str, Any]]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return {
        row.id: {"id": row.id, "display_name": row.display_name, "role": row.role}
        for row in conn.execute(
            select(User.id, User.display_name, User.role).where(User.id.in_(wanted))
        )
    }


def _run_summary(conn: Connection, run: Any) -> dict[str, Any]:
    checks = conn.execute(
        select(CheckResult.__table__)
        .where(CheckResult.run_id == run.id)
        .order_by(CheckResult.check_code, CheckResult.subject_key)
    ).all()
    return {
        "id": run.id,
        "kind": run.kind,
        "attempt": run.attempt,
        "status": run.status,
        "failure_reason": run.failure_reason,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "llm_call_count": run.llm_call_count,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "inconclusive": any(c.status == "inconclusive" for c in checks),
        "checks": [
            {
                "id": c.id,
                "check_code": c.check_code,
                "subject_key": c.subject_key,
                "status": c.status,
                "compared": c.compared,
                "tolerance": c.tolerance,
                "evidence_refs": c.evidence_refs,
                "inconclusive_reason": c.inconclusive_reason,
            }
            for c in checks
        ],
    }


def _resolution_view(conn: Connection, finding: Any) -> dict[str, Any] | None:
    resolution = finding.resolution
    if resolution is None:
        return None
    evidence = conn.execute(
        select(FindingResolutionEvidence.__table__).where(
            FindingResolutionEvidence.resolution_id == resolution["id"]
        )
    ).all()
    return {
        "id": resolution["id"],
        "resolution_type": resolution["resolution_type"],
        "summary": resolution["summary"],
        "structured": resolution["structured"],
        "digest": bytes(resolution["digest"]).hex(),
        "resolved_by": resolution["resolved_by"],
        "created_at": resolution["created_at"],
        "valid": finding.resolution_valid,
        "evidence": [
            {
                "supporting_document_id": e.supporting_document_id,
                "bank_verification_id": e.bank_verification_id,
                "erp_entity": e.erp_entity,
                "erp_id": e.erp_id,
            }
            for e in evidence
        ],
    }


def _findings(conn: Connection, state: EvidenceState) -> list[dict[str, Any]]:
    ids = [f.row["id"] for f in state.findings]
    links: dict[uuid.UUID, list[uuid.UUID]] = {}
    for finding_id, check_id in conn.execute(
        select(FindingCheckResult.finding_id, FindingCheckResult.check_result_id).where(
            FindingCheckResult.finding_id.in_(ids)
        )
    ):
        links.setdefault(finding_id, []).append(check_id)
    citations: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for row in conn.execute(
        select(
            FindingCitation.finding_id,
            FindingCitation.quoted_text,
            PolicyChunk.section_id,
            PolicyChunk.title,
        )
        .join(PolicyChunk, PolicyChunk.id == FindingCitation.policy_chunk_id)
        .where(FindingCitation.finding_id.in_(ids))
        .order_by(PolicyChunk.section_id)
    ):
        citations.setdefault(row.finding_id, []).append(
            {"section_id": row.section_id, "title": row.title, "quoted_text": row.quoted_text}
        )
    needing = {f.row["id"] for f in state.needing_resolution()}
    recheck_only = state.policy.resolutions.recheck_only
    return [
        {
            "id": f.row["id"],
            "exception_type": f.code,
            "subject_key": f.row["subject_key"],
            "severity": f.row["severity"],
            "explanation": f.row["explanation"],
            "grounding_status": f.row["grounding_status"],
            "check_result_ids": sorted(links.get(f.row["id"], []), key=str),
            "citations": citations.get(f.row["id"], []),
            "needs_resolution": f.row["id"] in needing,
            "resolvable_by_record": f.row["id"] in needing and f.code not in recheck_only,
            "resolved": f.resolution_valid and f.code not in recheck_only,
            "resolution": _resolution_view(conn, f),
            # What a resolution of this finding must carry, from the policy (forms are built
            # from it; the server re-checks everything).
            "resolution_rules": [
                {
                    "type": rule.type,
                    "evidence": list(rule.evidence),
                    "structured": list(rule.structured),
                }
                for rule in state.policy.resolutions.rules
                if rule.finding is f.code
            ],
        }
        for f in state.findings
    ]


def _caller_options(
    conn: Connection, actor: Actor, case: dict[str, Any], state: EvidenceState | None
) -> dict[str, Any]:
    """What this caller may do now, using the service's own checks, with reasons when not."""
    status = case["status"]
    operations: list[str] = []
    blocked: dict[str, str] = {}
    actions: list[ActionType] = []
    if actor.role is UserRole.ANALYST:
        if status == S.INVESTIGATION_FAILED:
            operations += ["investigate", "dismiss"]
        elif status == S.NEEDS_REVIEW:
            operations += [
                "change_proposal",
                "submit",
                "dismiss",
                "record_resolution",
                "recheck",
                "add_supporting_document",
                "record_bank_verification",
            ]
            if state is not None:
                actions = sorted(state.allowed(ProposalSource.ANALYST))
    elif actor.role is UserRole.APPROVER:
        if status == S.PENDING_APPROVAL:
            operations.append("decline")
            try:
                service.check_approver(conn, actor, case)
                operations.append("approve")
            except ForbiddenError as exc:
                blocked["approve"] = exc.code
        elif status == S.CLOSED and _executed_unreversed(conn, case["id"]):
            operations.append("reopen")
    return {"operations": operations, "blocked": blocked, "allowed_actions": actions}


def _executed_unreversed(conn: Connection, case_id: uuid.UUID) -> bool:
    return (
        conn.execute(
            select(ExecutedAction.id)
            .outerjoin(ActionReversal, ActionReversal.executed_action_id == ExecutedAction.id)
            .where(ExecutedAction.case_id == case_id, ActionReversal.id.is_(None))
        ).first()
        is not None
    )


SCAN_NOTICE = (
    "Scanned image: bank details can't be redacted until OCR arrives (P15). There is no"
    " preview, and it is never sent to the AI; the original is for authorised review only."
)


def redaction(mime_type: str) -> dict[str, Any]:
    """Whether previews can black out bank details for this file type (PDF text layer)."""
    supported = mime_type == "application/pdf"
    return {"redaction_supported": supported, "notice": None if supported else SCAN_NOTICE}


def _invoice(conn: Connection, case: dict[str, Any]) -> dict[str, Any]:
    invoice = conn.execute(
        select(
            Invoice.id,
            Invoice.document_id,
            Invoice.received_at,
            Document.page_count,
            Document.mime_type,
        )
        .join(Document, Document.id == Invoice.document_id)
        .where(Invoice.id == case["invoice_id"])
    ).one()
    extraction = (
        conn.execute(
            select(Extraction.data, Extraction.field_confidence, Extraction.bank_last4).where(
                Extraction.run_id == case["evidence_run_id"]
            )
        ).first()
        if case["evidence_run_id"]
        else None
    )
    data = dict(extraction.data) if extraction else {}
    data.pop("bank_account", None)  # shown below, masked
    return {
        "id": invoice.id,
        "document_id": invoice.document_id,
        "page_count": invoice.page_count,
        "mime_type": invoice.mime_type,
        **redaction(invoice.mime_type),
        "received_at": invoice.received_at,
        "extracted": data or None,
        "field_confidence": extraction.field_confidence if extraction else None,
        "bank_account": MASK + extraction.bank_last4
        if extraction and extraction.bank_last4
        else None,
    }


def _match_strip(conn: Connection, run_id: uuid.UUID | None) -> list[dict[str, Any]]:
    """One row per invoice line: the invoice's figures, its PO line, what was received,
    how the match was made, and the checks the line breaks (the case page's signature)."""
    if run_id is None:
        return []
    data = conn.execute(select(Extraction.data).where(Extraction.run_id == run_id)).scalar()
    lines = (data or {}).get("lines") or []
    matches = {
        m.invoice_line_no: m
        for m in conn.execute(select(LineMatch.__table__).where(LineMatch.run_id == run_id))
    }
    po_ids = {m.po_line_id for m in matches.values() if m.po_line_id}
    po_lines = {
        row.id: row for row in conn.execute(select(PoLine.__table__).where(PoLine.id.in_(po_ids)))
    }
    received = dict(
        conn.execute(
            select(ReceiptLine.po_line_id, func.sum(ReceiptLine.quantity_received))
            .where(ReceiptLine.po_line_id.in_(po_ids))
            .group_by(ReceiptLine.po_line_id)
        ).all()
    )
    breaks: dict[str, list[dict[str, str]]] = {}
    for row in conn.execute(
        select(CheckResult.subject_key, CheckResult.check_code, CheckResult.status).where(
            CheckResult.run_id == run_id,
            CheckResult.status != "pass",
            CheckResult.subject_key.like("line:%"),
        )
    ):
        breaks.setdefault(row.subject_key, []).append(
            {"check_code": str(row.check_code), "status": str(row.status)}
        )
    strip = []
    for position, line in enumerate(lines, start=1):
        index = int(line.get("index", position))
        match = matches.get(index)
        po = po_lines.get(match.po_line_id) if match and match.po_line_id else None
        strip.append(
            {
                "line": index,
                "description": line.get("description"),
                "sku": line.get("sku"),
                "quantity": line.get("quantity"),
                "unit_price": line.get("unit_price"),
                "line_total": line.get("line_total"),
                "po": {
                    "line_no": po.line_no,
                    "description": po.description,
                    "quantity": po.quantity,
                    "unit_price": po.unit_price,
                }
                if po
                else None,
                "received": received.get(po.id) if po else None,
                "method": match.method if match else None,
                "similarity": match.similarity if match else None,
                "breaks": breaks.get(f"line:{index}", []),
            }
        )
    return strip


def case_detail(conn: Connection, actor: Actor, case_id: uuid.UUID) -> dict[str, Any]:
    """`CaseDetail` (`06-api.md` §4), as this caller sees it."""
    require_reader(actor)
    case = require_live_case(conn, case_id)
    state = load_evidence(conn, case) if case["evidence_run_id"] else None
    runs = conn.execute(
        select(InvestigationRun.__table__)
        .where(InvestigationRun.case_id == case_id)
        .order_by(InvestigationRun.attempt.desc())
    ).all()
    evidence_run = next((r for r in runs if r.id == case["evidence_run_id"]), None)
    vendor_id = _vendor_ids(conn, [case["evidence_run_id"]]).get(case["evidence_run_id"])
    vendor = (
        conn.execute(
            select(
                Vendor.id, Vendor.vendor_code, Vendor.name, Vendor.is_approved, Vendor.address
            ).where(Vendor.id == vendor_id)
        ).first()
        if vendor_id
        else None
    )
    vendor_address = vendor.address if vendor else None  # the master's contact, never the invoice's
    outcome = _outcomes(conn, [case_id]).get(case_id) if case["status"] == S.CLOSED else None
    if outcome is not None:
        outcome["approved_by"] = _users(conn, [outcome["approved_by"]]).get(outcome["approved_by"])
    supporting = conn.execute(
        select(SupportingDocument.__table__, Document.mime_type)
        .join(Document, Document.id == SupportingDocument.document_id)
        .where(SupportingDocument.case_id == case_id)
        .order_by(SupportingDocument.created_at, SupportingDocument.id)
    ).all()
    verifications = conn.execute(
        select(BankVerification.__table__)
        .where(BankVerification.case_id == case_id)
        .order_by(BankVerification.created_at, BankVerification.id)
    ).all()
    decisions = conn.execute(
        select(Decision.__table__)
        .where(Decision.case_id == case_id)
        .order_by(Decision.created_at, Decision.id)
    ).all()
    proposal = state.proposal if state else _current_proposals(conn, [case_id]).get(case_id)
    people = _users(
        conn,
        [
            case["created_by"],
            case["submitted_by"],
            proposal["changed_by"] if proposal else None,
            *(d.actor_id for d in decisions),
        ],
    )
    detail = {
        "id": case["id"],
        "status": case["status"],
        "version": case["version"],
        "created_at": case["created_at"],
        "updated_at": case["updated_at"],
        "closed_at": case["closed_at"],
        "created_by": people.get(case["created_by"]),
        "submitted_by": people.get(case["submitted_by"]),
        "submitted_at": case["submitted_at"],
        "required_approval_amount": case["required_approval_amount"],
        "required_approver_tier": case["required_approver_tier"],
        "invoice": _invoice(conn, case),
        "vendor": {
            "id": vendor.id,
            "code": vendor.vendor_code,
            "name": vendor.name,
            "is_approved": vendor.is_approved,
        }
        if vendor
        else None,
        "evidence_run": _run_summary(conn, evidence_run) if evidence_run else None,
        "latest_run": {
            "id": runs[0].id,
            "kind": runs[0].kind,
            "attempt": runs[0].attempt,
            "status": runs[0].status,
            "failure_reason": runs[0].failure_reason,
        }
        if runs
        else None,
        "findings": _findings(conn, state) if state else [],
        "match_strip": _match_strip(conn, case["evidence_run_id"]),
        "supporting_documents": [
            {
                "id": s.id,
                "document_id": s.document_id,
                "kind": s.kind,
                "description": s.description,
                "mime_type": s.mime_type,
                **redaction(s.mime_type),
                "added_by": s.added_by,
                "created_at": s.created_at,
            }
            for s in supporting
        ],
        "bank_verifications": [
            {
                "id": v.id,
                "channel": v.channel,
                "contact_source": v.contact_source,
                "contact_reference": v.contact_reference,
                "account": MASK + v.verified_last4,
                "evidence_document_id": v.evidence_supporting_document_id,
                "verified_by": v.verified_by,
                "created_at": v.created_at,
            }
            for v in verifications
        ],
        "proposal": {
            "id": proposal["id"],
            "action": proposal["action"],
            "source": proposal["source"],
            "reason": proposal["reason"],
            "changed_by": people.get(proposal["changed_by"]),
            "created_at": proposal["created_at"],
        }
        if proposal
        else None,
        "decisions": [
            {
                "id": d.id,
                "kind": d.kind,
                "actor": people.get(d.actor_id),
                "proposal_id": d.proposal_id,
                "reason": d.reason,
                "details": d.details,
                "created_at": d.created_at,
            }
            for d in decisions
        ],
        "outcome": outcome,
        "bank_channels": list(state.policy.bank_verification.channels) if state else [],
        "vendor_address": vendor_address,
        **_caller_options(conn, actor, case, state),
    }
    masked: dict[str, Any] = mask_bank(conn, detail)
    return masked


# ---------------------------------------------------------------- audit and trace


def case_audit(
    conn: Connection,
    actor: Actor,
    case_id: uuid.UUID,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The case's events on the live chain, oldest first, with each run's evidence digest
    and what the latest recorded `audit verify` found (`verification`, None if never run).
    """
    require_reader(actor)
    require_live_case(conn, case_id)
    size = page_limit(limit)
    query = select(AuditEvent.__table__).where(
        AuditEvent.chain_id == "live", AuditEvent.case_id == case_id
    )
    if cursor is not None:
        (seq,) = decode_cursor(cursor, 1)
        if not seq.isdigit():
            raise ValidationFailedError("invalid cursor", field="cursor")
        query = query.where(AuditEvent.seq > int(seq))
    rows = conn.execute(query.order_by(AuditEvent.seq).limit(size + 1)).all()
    more = len(rows) > size
    rows = rows[:size]
    digests = {
        e.payload.get("run_id"): e.payload.get("evidence_digest")
        for e in rows
        if e.event_type == "run.completed"
    }
    people = _users(conn, [e.actor_id for e in rows])
    items = [
        {
            "seq": e.seq,
            "occurred_at": e.occurred_at,
            "event_type": e.event_type,
            "actor_id": e.actor_id,
            "actor_name": (people.get(e.actor_id) or {}).get("display_name"),
            "payload": e.payload,
            "hash": bytes(e.hash).hex(),
            "prev_hash": bytes(e.prev_hash).hex(),
        }
        for e in rows
    ]
    return {
        "items": mask_bank(conn, items),
        "next_cursor": encode_cursor(rows[-1].seq) if more else None,
        "evidence_digests": {k: v for k, v in digests.items() if k},
        "verification": _verification(conn, case_id),
    }


VERIFICATION_DUE = dt.timedelta(hours=36)  # daily, with slack for a late cleanup run
VERIFICATION_STATEMENT = (
    "The records were consistent when checked. Verification is tamper-evident, not"
    " tamper-proof: it doesn't prove that tampering is impossible, and events after"
    " the verified range haven't been checked yet."
)


def _case_record_ids(conn: Connection, case_id: uuid.UUID) -> set[str]:
    ids = set()
    for model in (InvestigationRun, Proposal, FindingResolution):
        ids |= {
            str(i) for i in conn.execute(select(model.id).where(model.case_id == case_id)).scalars()
        }
    return ids


def _verification(conn: Connection, case_id: uuid.UUID) -> dict[str, Any] | None:
    """The latest recorded verification, as it bears on this case's live events."""
    latest = conn.execute(
        select(AuditVerification.__table__)
        .order_by(AuditVerification.finished_at.desc(), AuditVerification.created_at.desc())
        .limit(1)
    ).first()
    if latest is None:
        return None
    live = next((c for c in latest.chains if c["chain_id"] == "live"), None)
    through = live["head_seq"] if live else 0
    seqs = (
        conn.execute(
            select(AuditEvent.seq).where(
                AuditEvent.chain_id == "live", AuditEvent.case_id == case_id
            )
        )
        .scalars()
        .all()
    )
    ids = _case_record_ids(conn, case_id) | {f"live seq {n} " for n in seqs}
    mine = [f for f in latest.failures if any(i in f + " " for i in ids)]
    summary: dict[str, Any] = mask_bank(
        conn,
        {
            "id": latest.id,
            "verified_at": latest.finished_at,
            "verified_as": latest.verified_as,
            "chain_ok": live is not None and live["error"] is None,
            "evidence_ok": not mine,
            "overall_ok": latest.ok,
            "verified_through_seq": through,
            "covers_all_case_events": bool(seqs) and max(seqs) <= through,
            # The daily check should have run since: an old result is shown as overdue.
            "overdue": latest.finished_at < dt.datetime.now(dt.UTC) - VERIFICATION_DUE,
            "case_failures": mine,
            "statement": VERIFICATION_STATEMENT,
        },
    )
    return summary


def _tool_calls(calls: Sequence[Any]) -> list[dict[str, Any]]:
    """Tool calls and their results, read back from the recorded investigate requests."""
    found: list[dict[str, Any]] = []
    for call in calls:
        if call.purpose != "investigate":
            continue
        messages = (call.request or {}).get("messages", [])
        for asked, answered in itertools.pairwise(messages):
            if asked.get("function_calls") and answered.get("function_results"):
                for c, r in zip(
                    asked["function_calls"], answered["function_results"], strict=False
                ):
                    entry = {"name": c["name"], "args": c["args"], "result": r["response"]}
                    if entry not in found:
                        found.append(entry)
    return found


def run_trace(
    conn: Connection, actor: Actor, case_id: uuid.UUID, run_id: uuid.UUID
) -> dict[str, Any]:
    """Steps, tool calls and LLM call metadata for one run of a live case (tokens masked)."""
    require_reader(actor)
    require_live_case(conn, case_id)
    run = conn.execute(
        select(InvestigationRun.__table__).where(
            InvestigationRun.id == run_id, InvestigationRun.case_id == case_id
        )
    ).first()
    if run is None:
        raise NotFoundError("run not found")
    calls = conn.execute(
        select(LlmCall.__table__)
        .where(LlmCall.run_id == run_id)
        .order_by(LlmCall.created_at, LlmCall.id)
    ).all()
    counts = dict(
        conn.execute(
            select(CheckResult.status, func.count())
            .where(CheckResult.run_id == run_id)
            .group_by(CheckResult.status)
        ).all()
    )
    has = {
        "extraction": Extraction,
        "findings": Finding,
        "proposal": Proposal,
    }
    present = {
        name: conn.execute(
            select(model.id).where(model.run_id == run_id).limit(1)  # type: ignore[attr-defined]  # each has run_id
        ).first()
        is not None
        for name, model in has.items()
    }
    steps = [
        {"step": "extract", "done": present["extraction"]},
        {
            "step": "run_checks",
            "done": bool(counts),
            "counts": {str(k): v for k, v in counts.items()},
        },
        {"step": "explain_and_propose", "done": present["findings"] or present["proposal"]},
        {"step": "await_decision", "done": run.status == "succeeded"},
    ]
    trace = {
        "run": {
            "id": run.id,
            "kind": run.kind,
            "attempt": run.attempt,
            "status": run.status,
            "provider": run.provider,
            "model": run.model,
            "prompt_versions": run.prompt_versions,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "execution_ms": run.execution_ms,
            "quota_wait_ms": run.quota_wait_ms,
        },
        "steps": steps,
        "tool_calls": _tool_calls(calls),
        "llm_calls": [
            {
                "purpose": c.purpose,
                "provider": c.provider,
                "model": c.model,
                "status": c.status,
                "tokens_in": c.tokens_in,
                "tokens_out": c.tokens_out,
                "latency_ms": c.latency_ms,
                "quota_wait_ms": c.quota_wait_ms,
                "function_calls": (c.response or {}).get("function_calls", []),
                "created_at": c.created_at,
            }
            for c in calls
        ],
        "errors": [
            e
            for e in (
                [{"source": "run", "reason": run.failure_reason}] if run.failure_reason else []
            )
            + [
                {"source": f"llm:{c.purpose}", "reason": c.status}
                for c in calls
                if c.status != "ok"
            ]
        ],
    }
    masked: dict[str, Any] = mask_bank(conn, trace)
    return masked


# ---------------------------------------------------------------- documents


def require_visible_document(conn: Connection, document_id: uuid.UUID) -> dict[str, Any]:
    """A document belonging to a live case: its invoice or one of its supporting documents."""
    live_invoice = (
        select(Case.id)
        .join(Invoice, Invoice.id == Case.invoice_id)
        .where(Invoice.document_id == document_id, Case.eval_run_id.is_(None))
    )
    live_supporting = (
        select(Case.id)
        .join(SupportingDocument, SupportingDocument.case_id == Case.id)
        .where(SupportingDocument.document_id == document_id, Case.eval_run_id.is_(None))
    )
    row = conn.execute(
        select(Document.__table__).where(
            Document.id == document_id,
            or_(live_invoice.exists(), live_supporting.exists()),
        )
    ).first()
    if row is None:
        raise NotFoundError("document not found")
    return dict(row._mapping)


def highlight_values(conn: Connection, document_id: uuid.UUID, finding_id: uuid.UUID) -> list[str]:
    """The invoice values a finding's checks cite, for boxes on the page image."""
    finding = conn.execute(
        select(Finding.id)
        .join(Case, Case.id == Finding.case_id)
        .join(Invoice, Invoice.id == Case.invoice_id)
        .where(
            Finding.id == finding_id,
            Invoice.document_id == document_id,
            Case.eval_run_id.is_(None),
        )
    ).first()
    if finding is None:
        raise NotFoundError("finding not found for this document")
    refs = conn.execute(
        select(CheckResult.evidence_refs)
        .join(FindingCheckResult, FindingCheckResult.check_result_id == CheckResult.id)
        .where(FindingCheckResult.finding_id == finding_id)
    ).scalars()
    values = []
    for group in refs:
        for ref in group or []:
            value = ref.get("value")
            if ref.get("entity") == "invoice" and isinstance(value, str | int) and str(value):
                values.append(str(value))
    return values
