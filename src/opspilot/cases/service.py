"""The case service: transitions T1–T17 and the writes allowed in `needs_review`.

Every function runs inside the caller's transaction and follows one order:
lock the case, then check the actor's role, `expected_version`, the state and
the rules, then write, then audit, then enqueue. A failure anywhere raises a
`ServiceError` before anything is written, and the caller's rollback discards
everything else. The web layer (P11) and the worker call only these functions.
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Connection, func, insert, select, update

from opspilot.audit.chain import LIVE, append_event
from opspilot.audit.digests import (
    cited_rows,
    proposal_digest,
    record_finding_resolved,
    record_proposal_changed,
    record_run_completed,
    resolution_content,
    resolution_digest,
)
from opspilot.cases.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    NotReadyError,
    ValidationFailedError,
)
from opspilot.cases.gate import require_action_allowed, require_payment_clear
from opspilot.cases.resolutions import EvidenceEntry, validate_resolution
from opspilot.cases.routing import approval_amount, route
from opspilot.cases.state import (
    Actor,
    EvidenceState,
    chain_for,
    load_actor,
    load_evidence,
    lock_case,
)
from opspilot.db.models import (
    ActionReversal,
    BankVerification,
    Case,
    CheckResult,
    Dataset,
    Decision,
    DecisionResolutionAcceptance,
    Document,
    ExecutedAction,
    Finding,
    FindingResolution,
    FindingResolutionEvidence,
    InvestigationRun,
    Invoice,
    PolicyVersion,
    Proposal,
    SupportingDocument,
)
from opspilot.domain.bank import fingerprint
from opspilot.domain.enums import (
    ActionType,
    ApproverTier,
    CaseStatus,
    CheckCode,
    DatasetSplit,
    DecisionKind,
    DocumentKind,
    JobType,
    ProposalSource,
    ResolutionType,
    RunKind,
    RunStatus,
    SupportingKind,
    UserRole,
    VerificationChannel,
)
from opspilot.extraction.bank_tokens import contains_account_like_digits
from opspilot.jobs.queue import enqueue

S = CaseStatus
REASON_MAX = 1000


# ---------------------------------------------------------------- shared steps


def _actor(conn: Connection, actor_id: uuid.UUID, role: UserRole) -> Actor:
    actor = load_actor(conn, actor_id)
    if actor.role is not role:
        raise ForbiddenError(f"only an {role} can do this")
    return actor


def _open(
    conn: Connection,
    case_id: uuid.UUID,
    expected_version: int | None,
    *statuses: CaseStatus,
) -> dict[str, Any]:
    case = lock_case(conn, case_id)
    if expected_version is not None and case["version"] != expected_version:
        raise ConflictError("the case changed; reload it", code="stale_version")
    if case["status"] not in statuses:
        raise ConflictError(f"not allowed while the case is {case['status']}")
    return case


def _reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not 1 <= len(text) <= REASON_MAX:
        raise ValidationFailedError(
            f"a reason of 1–{REASON_MAX} characters is required", field="reason"
        )
    return text


def _set(conn: Connection, case: dict[str, Any], **values: Any) -> None:
    conn.execute(
        update(Case)
        .where(Case.id == case["id"])
        .values(version=Case.version + 1, updated_at=func.now(), **values)
    )


def _transition(  # noqa: PLR0917  # one internal call shape
    conn: Connection,
    case: dict[str, Any],
    transition: str,
    target: CaseStatus,
    actor_id: uuid.UUID | None,
    payload: Mapping[str, Any] | None = None,
    **values: Any,
) -> None:
    _set(conn, case, status=target, **values)
    _audit(
        conn,
        case,
        "case.transition",
        actor_id,
        {"transition": transition, "from": case["status"], "to": target, **(payload or {})},
    )


def _audit(
    conn: Connection,
    case: dict[str, Any],
    event_type: str,
    actor_id: uuid.UUID | None,
    payload: Mapping[str, Any],
) -> None:
    append_event(
        conn,
        chain_id=chain_for(case),
        event_type=event_type,
        payload=dict(payload),
        actor_id=actor_id,
        case_id=case["id"],
    )


def _decision(
    conn: Connection,
    case: dict[str, Any],
    kind: DecisionKind,
    actor_id: uuid.UUID | None,
    *,
    proposal_id: uuid.UUID | None = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> uuid.UUID:
    decision_id = uuid.uuid4()
    conn.execute(
        insert(Decision).values(
            id=decision_id,
            case_id=case["id"],
            kind=kind,
            actor_id=actor_id,
            proposal_id=proposal_id,
            reason=reason,
            details=details,
            case_version=case["version"],
        )
    )
    return decision_id


def _latest_run(conn: Connection, case_id: uuid.UUID) -> dict[str, Any] | None:
    row = conn.execute(
        select(InvestigationRun.__table__)
        .where(InvestigationRun.case_id == case_id)
        .order_by(InvestigationRun.attempt.desc())
        .limit(1)
    ).first()
    return dict(row._mapping) if row else None


def _active_policy_version(conn: Connection) -> uuid.UUID:
    version = conn.execute(
        select(PolicyVersion.id).where(PolicyVersion.is_active)
    ).scalar_one_or_none()
    if version is None:
        raise ConflictError("no active policy version", code="not_ready")
    return version


def _new_run(
    conn: Connection,
    case: dict[str, Any],
    kind: RunKind,
    *,
    policy_version_id: uuid.UUID | None = None,
    source_run_id: uuid.UUID | None = None,
    recheck_inputs: dict[str, Any] | None = None,
) -> uuid.UUID:
    attempt = (
        conn.execute(
            select(func.max(InvestigationRun.attempt)).where(InvestigationRun.case_id == case["id"])
        ).scalar_one()
        or 0
    ) + 1
    run_id = uuid.uuid4()
    conn.execute(
        insert(InvestigationRun).values(
            id=run_id,
            case_id=case["id"],
            attempt=attempt,
            kind=kind,
            source_run_id=source_run_id,
            recheck_inputs=recheck_inputs,
            status=RunStatus.RUNNING,
            thread_id=f"case-{case['id']}-run-{attempt}",
            policy_version_id=policy_version_id or _active_policy_version(conn),
        )
    )
    _audit(conn, case, "run.started", None, {"run_id": run_id, "kind": kind, "attempt": attempt})
    return run_id


def _enqueue_run(conn: Connection, case: dict[str, Any], run_id: uuid.UUID) -> None:
    enqueue(
        conn,
        JobType.INVESTIGATE,
        case_id=case["id"],
        payload={"run_id": str(run_id)},
        idempotency_key=f"investigate:{run_id}",
    )


def _enqueue_resume(conn: Connection, case: dict[str, Any], decision_id: uuid.UUID) -> None:
    run = _latest_run(conn, case["id"])
    enqueue(
        conn,
        JobType.RESUME,
        case_id=case["id"],
        payload={"decision_id": str(decision_id), "run_id": str(run["id"]) if run else None},
        idempotency_key=f"resume:{decision_id}",
    )


# ---------------------------------------------------------------- T1–T5: runs


def create_case(
    conn: Connection,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
) -> uuid.UUID:
    """T1: one open live case per invoice; enqueues the investigation."""
    _actor(conn, actor_id, UserRole.ANALYST)
    open_case = conn.execute(
        select(Case.id).where(
            Case.invoice_id == invoice_id, Case.eval_run_id.is_(None), Case.status != S.CLOSED
        )
    ).first()
    if open_case is not None:
        raise ConflictError("this invoice already has an open case", code="case_exists")
    case_id = uuid.uuid4()
    conn.execute(insert(Case).values(id=case_id, invoice_id=invoice_id, created_by=actor_id))
    case = lock_case(conn, case_id)
    _audit(conn, case, "case.transition", actor_id, {"transition": "T1", "from": None, "to": S.NEW})
    enqueue(
        conn,
        JobType.INVESTIGATE,
        case_id=case_id,
        payload={},
        idempotency_key=f"investigate:{case_id}:initial",
    )
    return case_id


def create_upload_case(
    conn: Connection, actor_id: uuid.UUID, document_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """T1 for an uploaded file: a new invoice in the `demo` dataset, then its case."""
    _actor(conn, actor_id, UserRole.ANALYST)
    dataset_id = conn.execute(
        select(Dataset.id)
        .where(Dataset.split == DatasetSplit.DEMO)
        .order_by(Dataset.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if dataset_id is None:
        raise NotReadyError("the demo dataset isn't loaded")
    invoice_id = uuid.uuid4()
    conn.execute(
        insert(Invoice).values(
            id=invoice_id, dataset_id=dataset_id, document_id=document_id, received_at=func.now()
        )
    )
    return invoice_id, create_case(conn, actor_id, invoice_id)


def create_eval_case(conn: Connection, eval_run_id: uuid.UUID, invoice_id: uuid.UUID) -> uuid.UUID:
    """T1 for an evaluation run: system-created, on the run's own chain, never in the queue.

    The evaluation runner drives the graph itself, item by item; evaluation cases
    never appear in the live queue or API (`06-api.md` §4).
    """
    case_id = uuid.uuid4()
    conn.execute(insert(Case).values(id=case_id, invoice_id=invoice_id, eval_run_id=eval_run_id))
    case = lock_case(conn, case_id)
    _audit(conn, case, "case.transition", None, {"transition": "T1", "from": None, "to": S.NEW})
    return case_id


def start_investigation(conn: Connection, case_id: uuid.UUID) -> uuid.UUID:
    """T2 (system, from the investigate job): a new run of kind `investigation`."""
    case = _open(conn, case_id, None, S.NEW)
    run_id = _new_run(conn, case, RunKind.INVESTIGATION)
    _transition(conn, case, "T2", S.INVESTIGATING, None, {"run_id": run_id})
    return run_id


def retry_investigation(
    conn: Connection, actor_id: uuid.UUID, case_id: uuid.UUID, expected_version: int
) -> uuid.UUID:
    """T3: a new attempt after a failed run."""
    _actor(conn, actor_id, UserRole.ANALYST)
    case = _open(conn, case_id, expected_version, S.INVESTIGATION_FAILED)
    run_id = _new_run(conn, case, RunKind.INVESTIGATION)
    _transition(conn, case, "T3", S.INVESTIGATING, actor_id, {"run_id": run_id})
    _enqueue_run(conn, case, run_id)
    return run_id


def _running(conn: Connection, run_id: uuid.UUID) -> tuple[dict[str, Any], dict[str, Any]]:
    run = conn.execute(
        select(InvestigationRun.__table__).where(InvestigationRun.id == run_id)
    ).first()
    if run is None:
        raise NotFoundError("run not found")
    case = _open(conn, run.case_id, None, S.INVESTIGATING)
    if run.status != RunStatus.RUNNING or run.kind == RunKind.REOPEN:
        raise ConflictError("the run isn't an investigation in progress")
    latest = _latest_run(conn, case["id"])
    if latest is None or latest["id"] != run_id:
        raise ConflictError("a newer run has replaced this one")
    return dict(run._mapping), case


def record_agent_proposal(conn: Connection, run_id: uuid.UUID, action: ActionType) -> uuid.UUID:
    """The agent's proposal, checked against the **agent** allowlist for this run's findings."""
    run, case = _running(conn, run_id)
    state = load_evidence(conn, case, run_id)
    require_action_allowed(state, action, ProposalSource.AGENT)
    proposal_id = uuid.uuid4()
    row = {
        "id": proposal_id,
        "case_id": case["id"],
        "run_id": run_id,
        "action": action,
        "source": ProposalSource.AGENT,
        "changed_by": None,
        "reason": None,
        "policy_version_id": run["policy_version_id"],
    }
    conn.execute(insert(Proposal).values(**row, digest=proposal_digest(row)))
    return proposal_id


def run_succeeded(conn: Connection, run_id: uuid.UUID, parsed_document_id: uuid.UUID) -> None:
    """T4: the run becomes the case's evidence; its evidence digest goes on the chain."""
    _run, case = _running(conn, run_id)
    has_proposal = conn.execute(
        select(Proposal.id).where(
            Proposal.run_id == run_id, Proposal.source == ProposalSource.AGENT
        )
    ).first()
    if has_proposal is None:
        raise ConflictError("a run can't succeed without the agent's proposal")
    conn.execute(
        update(InvestigationRun)
        .where(InvestigationRun.id == run_id)
        .values(status=RunStatus.SUCCEEDED, finished_at=func.now())
    )
    record_run_completed(
        conn,
        chain_id=chain_for(case),
        run_id=run_id,
        parsed_document_id=parsed_document_id,
        case_id=case["id"],
    )
    _transition(conn, case, "T4", S.NEEDS_REVIEW, None, {"run_id": run_id}, evidence_run_id=run_id)


def run_failed(conn: Connection, run_id: uuid.UUID, reason: str) -> None:
    """T5: the failure reason is recorded; the analyst may retry or dismiss."""
    _run, case = _running(conn, run_id)
    conn.execute(
        update(InvestigationRun)
        .where(InvestigationRun.id == run_id)
        .values(status=RunStatus.FAILED, failure_reason=reason[:500], finished_at=func.now())
    )
    _transition(
        conn, case, "T5", S.INVESTIGATION_FAILED, None, {"run_id": run_id, "reason": reason[:500]}
    )


# ---------------------------------------------------------------- T6–T10: review


def dismiss(
    conn: Connection, actor_id: uuid.UUID, case_id: uuid.UUID, expected_version: int, reason: str
) -> None:
    """T6 (after a failed run) or T8 (in review; the paused graph is resumed to end)."""
    _actor(conn, actor_id, UserRole.ANALYST)
    text = _reason(reason)
    case = _open(conn, case_id, expected_version, S.INVESTIGATION_FAILED, S.NEEDS_REVIEW)
    decision_id = _decision(conn, case, DecisionKind.DISMISS, actor_id, reason=text)
    transition = "T6" if case["status"] == S.INVESTIGATION_FAILED else "T8"
    _transition(
        conn,
        case,
        transition,
        S.CLOSED,
        actor_id,
        {"decision_id": decision_id, "reason": text},
        closed_at=func.now(),
    )
    if transition == "T8":
        _enqueue_resume(conn, case, decision_id)


def change_proposal(
    conn: Connection,
    actor_id: uuid.UUID,
    case_id: uuid.UUID,
    expected_version: int,
    *,
    action: ActionType,
    reason: str,
) -> uuid.UUID:
    """T7: a new analyst proposal, within the analyst allowlist for unresolved findings."""
    _actor(conn, actor_id, UserRole.ANALYST)
    text = _reason(reason)
    case = _open(conn, case_id, expected_version, S.NEEDS_REVIEW)
    state = load_evidence(conn, case)
    require_action_allowed(state, action, ProposalSource.ANALYST)
    proposal_id = uuid.uuid4()
    row = {
        "id": proposal_id,
        "case_id": case_id,
        "run_id": None,
        "action": action,
        "source": ProposalSource.ANALYST,
        "changed_by": actor_id,
        "reason": text,
        "policy_version_id": state.policy_version_id,
    }
    conn.execute(insert(Proposal).values(**row, digest=proposal_digest(row)))
    record_proposal_changed(
        conn, chain_id=chain_for(case), proposal=row, actor_id=actor_id, case_id=case_id
    )
    _transition(conn, case, "T7", S.NEEDS_REVIEW, actor_id, {"proposal_id": proposal_id})
    return proposal_id


def _current_proposal(state: EvidenceState) -> dict[str, Any]:
    if state.proposal is None:
        raise ConflictError("there is no proposal to act on", code="no_proposal")
    return state.proposal


def submit(
    conn: Connection,
    actor_id: uuid.UUID,
    case_id: uuid.UUID,
    expected_version: int,
    note: str | None = None,
) -> CaseStatus:
    """T9 (an approver can take it) or T10 (held: nobody can); escalations are recorded."""
    _actor(conn, actor_id, UserRole.ANALYST)
    case = _open(conn, case_id, expected_version, S.NEEDS_REVIEW)
    state = load_evidence(conn, case)
    proposal = _current_proposal(state)
    require_action_allowed(
        state, ActionType(proposal["action"]), ProposalSource(proposal["source"])
    )
    amount = approval_amount(conn, state.run_id)
    tier = route(conn, amount)
    _decision(
        conn,
        case,
        DecisionKind.SUBMIT,
        actor_id,
        proposal_id=proposal["id"],
        reason=note.strip() if note and note.strip() else None,
    )
    if tier is not ApproverTier.STANDARD:
        _escalate(conn, case, amount, tier)
    target = S.HELD_FOR_APPROVER if tier is None else S.PENDING_APPROVAL
    _transition(
        conn,
        case,
        "T10" if tier is None else "T9",
        target,
        actor_id,
        {"amount": amount, "tier": tier},
        submitted_by=actor_id,
        submitted_at=func.now(),
        required_approval_amount=amount,
        required_approver_tier=tier or ApproverTier.CONTROLLER,
    )
    return target


def _escalate(
    conn: Connection, case: dict[str, Any], amount: Any, tier: ApproverTier | None
) -> None:
    details = {
        "amount": str(amount) if amount is not None else None,
        "reason": "amount_unverified" if amount is None else "above_standard_limits",
        "outcome": "controller" if tier else "held_for_approver",
    }
    _decision(conn, case, DecisionKind.ESCALATE, None, details=details)
    _audit(conn, case, "case.escalated", None, details)


def recheck_held_cases(conn: Connection) -> list[uuid.UUID]:
    """T11 (system): held cases move to `pending_approval` once someone can approve them."""
    moved = []
    held = conn.execute(select(Case.id).where(Case.status == S.HELD_FOR_APPROVER)).scalars().all()
    for case_id in held:
        case = _open(conn, case_id, None, S.HELD_FOR_APPROVER)
        tier = route(conn, case["required_approval_amount"])
        if tier is None:
            continue
        _transition(
            conn, case, "T11", S.PENDING_APPROVAL, None, {"tier": tier}, required_approver_tier=tier
        )
        moved.append(case_id)
    return moved


# ---------------------------------------------------------------- T12, T13, T15: approvers


def _changed_proposal(conn: Connection, case_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return (
        conn.execute(
            select(Proposal.id).where(Proposal.case_id == case_id, Proposal.changed_by == user_id)
        ).first()
        is not None
    )


def check_approver(conn: Connection, actor: Actor, case: dict[str, Any]) -> None:
    """Authority over this case: tier, limit, and not approving one's own work."""
    tier = case["required_approver_tier"]
    if tier is ApproverTier.CONTROLLER and not actor.approval_unlimited:
        raise ForbiddenError("this case needs a controller", code="tier_not_eligible")
    if not actor.can_approve(tier, case["required_approval_amount"]):
        raise ForbiddenError("the amount is above your approval limit", code="over_limit")
    if actor.id == case["submitted_by"] or _changed_proposal(conn, case["id"], actor.id):
        raise ForbiddenError(
            "you can't approve a case you submitted or changed", code="self_approval"
        )


def check_acceptances(state: EvidenceState, accepted: Sequence[tuple[uuid.UUID, bytes]]) -> None:
    """The approver accepted exactly the current resolutions, by ID and digest."""
    current = state.current_resolutions()
    given = dict(accepted)
    if len(given) != len(accepted) or set(given) != set(current):
        raise ValidationFailedError(
            "accept every current resolution, and only those", code="acceptance_mismatch"
        )
    if any(bytes(given[i]) != digest for i, digest in current.items()):
        raise ConflictError("a resolution changed since you reviewed it", code="stale_acceptance")


def approve(
    conn: Connection,
    actor_id: uuid.UUID,
    case_id: uuid.UUID,
    expected_version: int,
    *,
    accepted_resolutions: Sequence[tuple[uuid.UUID, bytes]] = (),
    note: str | None = None,
) -> uuid.UUID:
    """T12. For payment: every finding resolved, nothing inconclusive, every resolution accepted."""
    actor = _actor(conn, actor_id, UserRole.APPROVER)
    case = _open(conn, case_id, expected_version, S.PENDING_APPROVAL)
    check_approver(conn, actor, case)
    state = load_evidence(conn, case)
    proposal = _current_proposal(state)
    action = ActionType(proposal["action"])
    require_action_allowed(state, action, ProposalSource(proposal["source"]))
    accepted: dict[uuid.UUID, bytes] = {}
    if action is ActionType.APPROVE_FOR_PAYMENT:
        require_payment_clear(state)
        check_acceptances(state, accepted_resolutions)
        accepted = state.current_resolutions()
    decision_id = _decision(
        conn,
        case,
        DecisionKind.APPROVE,
        actor_id,
        proposal_id=proposal["id"],
        reason=note.strip() if note and note.strip() else None,
    )
    for resolution_id, digest in accepted.items():
        conn.execute(
            insert(DecisionResolutionAcceptance).values(
                decision_id=decision_id, resolution_id=resolution_id, resolution_digest=digest
            )
        )
    _transition(
        conn,
        case,
        "T12",
        S.APPROVED,
        actor_id,
        {
            "decision_id": decision_id,
            "action": action,
            "accepted": {str(k): v.hex() for k, v in accepted.items()},
        },
    )
    _enqueue_resume(conn, case, decision_id)
    return decision_id


def decline(
    conn: Connection, actor_id: uuid.UUID, case_id: uuid.UUID, expected_version: int, reason: str
) -> None:
    """T13: any approver, no limit; the graph stays paused."""
    _actor(conn, actor_id, UserRole.APPROVER)
    text = _reason(reason)
    case = _open(conn, case_id, expected_version, S.PENDING_APPROVAL)
    decision_id = _decision(conn, case, DecisionKind.DECLINE, actor_id, reason=text)
    _transition(
        conn, case, "T13", S.NEEDS_REVIEW, actor_id, {"decision_id": decision_id, "reason": text}
    )


def reopen(
    conn: Connection, actor_id: uuid.UUID, case_id: uuid.UUID, expected_version: int, reason: str
) -> uuid.UUID:
    """T15: reverse the executed action; a `reopen` run (no LLM) on a new thread."""
    _actor(conn, actor_id, UserRole.APPROVER)
    text = _reason(reason)
    case = _open(conn, case_id, expected_version, S.CLOSED)
    executed = conn.execute(
        select(ExecutedAction.id)
        .outerjoin(ActionReversal, ActionReversal.executed_action_id == ExecutedAction.id)
        .where(ExecutedAction.case_id == case_id, ActionReversal.id.is_(None))
    ).scalar_one_or_none()
    if executed is None:
        raise ConflictError("only a case closed with an executed action can be reopened")
    decision_id = _decision(conn, case, DecisionKind.REOPEN, actor_id, reason=text)
    conn.execute(
        insert(ActionReversal).values(executed_action_id=executed, decision_id=decision_id)
    )
    source = conn.execute(
        select(InvestigationRun.policy_version_id).where(
            InvestigationRun.id == case["evidence_run_id"]
        )
    ).scalar_one()
    run_id = _new_run(
        conn, case, RunKind.REOPEN, policy_version_id=source, source_run_id=case["evidence_run_id"]
    )
    _transition(
        conn,
        case,
        "T15",
        S.NEEDS_REVIEW,
        actor_id,
        {"decision_id": decision_id, "reversed_action_id": executed, "run_id": run_id},
        closed_at=None,
    )
    _enqueue_run(conn, case, run_id)
    return run_id


# ---------------------------------------------------------------- T16, T17 and evidence writes


def record_resolution(
    conn: Connection,
    actor_id: uuid.UUID,
    case_id: uuid.UUID,
    expected_version: int,
    *,
    finding_id: uuid.UUID,
    resolution_type: ResolutionType,
    summary: str,
    evidence: Sequence[EvidenceEntry],
    structured: Mapping[str, Any],
) -> tuple[uuid.UUID, bytes]:
    """T16: a structured resolution with its required evidence; append-only, digested."""
    _actor(conn, actor_id, UserRole.ANALYST)
    case = _open(conn, case_id, expected_version, S.NEEDS_REVIEW)
    finding = conn.execute(
        select(Finding.__table__).where(
            Finding.id == finding_id, Finding.run_id == case["evidence_run_id"]
        )
    ).first()
    if finding is None:
        raise NotFoundError("finding not found in the case's current evidence")
    state = load_evidence(conn, case)
    validate_resolution(
        conn,
        state.policy,
        dict(finding._mapping),
        resolution_type=resolution_type,
        summary=summary,
        evidence=evidence,
        structured=structured,
    )
    resolution_id = uuid.uuid4()
    resolution = {
        "id": resolution_id,
        "finding_id": finding_id,
        "case_id": case_id,
        "resolution_type": resolution_type,
        "summary": summary.strip(),
        "structured": dict(structured),
        "resolved_by": actor_id,
        "policy_version_id": state.policy_version_id,
    }
    rows = [
        {
            "id": uuid.uuid4(),
            "resolution_id": resolution_id,
            "supporting_document_id": e.supporting_document_id,
            "bank_verification_id": e.bank_verification_id,
            "erp_entity": e.erp_entity,
            "erp_id": e.erp_id,
        }
        for e in evidence
    ]
    digest = resolution_digest(resolution_content(resolution, rows, *cited_rows(conn, rows)))
    conn.execute(insert(FindingResolution).values(**resolution, digest=digest))
    conn.execute(insert(FindingResolutionEvidence), rows)
    record_finding_resolved(
        conn,
        chain_id=chain_for(case),
        resolution_id=resolution_id,
        digest=digest,
        actor_id=actor_id,
        case_id=case_id,
    )
    _transition(
        conn,
        case,
        "T16",
        S.NEEDS_REVIEW,
        actor_id,
        {"finding_id": finding_id, "resolution_id": resolution_id},
    )
    return resolution_id, digest


def request_recheck(
    conn: Connection,
    actor_id: uuid.UUID,
    case_id: uuid.UUID,
    expected_version: int,
    reason: str,
    *,
    replacement_document_id: uuid.UUID | None = None,
    po_number: str | None = None,
) -> uuid.UUID:
    """T17: a new `recheck` run, optionally with a clearer copy or a corrected PO number."""
    _actor(conn, actor_id, UserRole.ANALYST)
    text = _reason(reason)
    case = _open(conn, case_id, expected_version, S.NEEDS_REVIEW)
    if replacement_document_id is not None:
        kind = conn.execute(
            select(SupportingDocument.kind).where(
                SupportingDocument.id == replacement_document_id,
                SupportingDocument.case_id == case_id,
            )
        ).scalar_one_or_none()
        if kind is not SupportingKind.INVOICE_COPY:
            raise ValidationFailedError("the replacement must be this case's invoice_copy document")
    inputs = {
        "reason": text,
        "replacement_document_id": str(replacement_document_id)
        if replacement_document_id
        else None,
        "po_number": po_number.strip() if po_number and po_number.strip() else None,
    }
    run_id = _new_run(conn, case, RunKind.RECHECK, recheck_inputs=inputs)
    _transition(conn, case, "T17", S.INVESTIGATING, actor_id, {"run_id": run_id, **inputs})
    _enqueue_run(conn, case, run_id)
    return run_id


def add_supporting_document(
    conn: Connection,
    actor_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    document_id: uuid.UUID,
    kind: SupportingKind,
    description: str,
) -> uuid.UUID:
    """Attach an uploaded (already validated) file as evidence; audited, no state change."""
    _actor(conn, actor_id, UserRole.ANALYST)
    case = _open(conn, case_id, None, S.NEEDS_REVIEW)
    doc_kind = conn.execute(
        select(Document.kind).where(Document.id == document_id)
    ).scalar_one_or_none()
    if doc_kind is not DocumentKind.SUPPORTING:
        raise ValidationFailedError("the file must be stored as a supporting document")
    supporting_id = uuid.uuid4()
    conn.execute(
        insert(SupportingDocument).values(
            id=supporting_id,
            case_id=case_id,
            document_id=document_id,
            kind=kind,
            description=_reason(description),
            added_by=actor_id,
        )
    )
    _set(conn, case)
    _audit(
        conn,
        case,
        "supporting_document.added",
        actor_id,
        {"supporting_document_id": supporting_id, "kind": kind},
    )
    return supporting_id


def _case_vendor(conn: Connection, run_id: uuid.UUID | None) -> uuid.UUID | None:
    refs = conn.execute(
        select(CheckResult.evidence_refs).where(
            CheckResult.run_id == run_id, CheckResult.check_code == CheckCode.VENDOR_APPROVED
        )
    ).scalar_one_or_none()
    vendor = next((r for r in refs or [] if r.get("entity") == "vendor" and r.get("id")), None)
    return uuid.UUID(vendor["id"]) if vendor else None


def record_bank_verification(
    conn: Connection,
    actor_id: uuid.UUID,
    case_id: uuid.UUID,
    expected_version: int,
    *,
    channel: str,
    contact_source: str,
    contact_reference: str,
    verified_account_number: str,
    evidence_document_id: uuid.UUID,
    bank_key: str,
) -> tuple[uuid.UUID, str, str]:
    """The account number is reduced to `bank_fp` + last 4 here and never stored or logged."""
    _actor(conn, actor_id, UserRole.ANALYST)
    case = _open(conn, case_id, expected_version, S.NEEDS_REVIEW)
    state = load_evidence(conn, case)

    def invalid(message: str) -> ValidationFailedError:
        return ValidationFailedError(message, code="verification_invalid")

    if contact_source != state.policy.bank_verification.contact_source:
        raise invalid("contact details must come from the vendor master, never the invoice")
    if channel not in {c.value for c in state.policy.bank_verification.channels}:
        raise invalid("this verification channel isn't allowed")
    reference = contact_reference.strip()
    if not reference or contains_account_like_digits(reference):
        raise invalid("describe the contact used, without account numbers")
    evidence_kind = conn.execute(
        select(SupportingDocument.kind).where(
            SupportingDocument.id == evidence_document_id, SupportingDocument.case_id == case_id
        )
    ).scalar_one_or_none()
    if evidence_kind is not SupportingKind.BANK_VERIFICATION:
        raise invalid("the evidence must be this case's bank_verification document")
    vendor_id = _case_vendor(conn, state.run_id)
    if vendor_id is None:
        raise invalid("the case's vendor isn't known")
    try:
        fp = fingerprint(verified_account_number, bank_key)
    except ValueError:
        raise invalid("that isn't an account number") from None
    if not fp.last4.isdigit():
        raise invalid("that isn't an account number")
    verification_id = uuid.uuid4()
    conn.execute(
        insert(BankVerification).values(
            id=verification_id,
            case_id=case_id,
            vendor_id=vendor_id,
            channel=VerificationChannel(channel),
            contact_source=contact_source,
            contact_reference=reference,
            verified_bank_fp=fp.bank_fp,
            verified_last4=fp.last4,
            evidence_supporting_document_id=evidence_document_id,
            verified_by=actor_id,
        )
    )
    _set(conn, case)
    _audit(
        conn,
        case,
        "bank_verification.recorded",
        actor_id,
        {
            "bank_verification_id": verification_id,
            "channel": channel,
            "bank_account": fp.token(),
            "last4": fp.last4,
        },
    )
    return verification_id, fp.bank_fp, fp.last4


def mark_closed_after_execution(
    conn: Connection, case: dict[str, Any], decision_id: uuid.UUID, action: ActionType
) -> None:
    """T14 (system): called by the executor after it wrote `executed_actions`."""
    _transition(
        conn,
        case,
        "T14",
        S.CLOSED,
        None,
        {"decision_id": decision_id, "action": action},
        closed_at=func.now(),
    )


READER_ROLES = frozenset({UserRole.ANALYST, UserRole.APPROVER, UserRole.AUDITOR})


def record_document_download(
    conn: Connection, actor_id: uuid.UUID, document_id: uuid.UUID, sha256: str
) -> list[uuid.UUID]:
    """An original left the system: one audit event on every live case holding the document.

    Originals keep the printed bank number (they are the evidence), so each
    download is attributable. No state changes and the case version stays put.
    """
    actor = load_actor(conn, actor_id)
    if actor.role not in READER_ROLES:
        raise ForbiddenError("this account can't read documents")
    case_ids = sorted(
        {
            *conn.execute(
                select(Case.id)
                .join(Invoice, Invoice.id == Case.invoice_id)
                .where(Invoice.document_id == document_id, Case.eval_run_id.is_(None))
            ).scalars(),
            *conn.execute(
                select(SupportingDocument.case_id)
                .join(Case, Case.id == SupportingDocument.case_id)
                .where(SupportingDocument.document_id == document_id, Case.eval_run_id.is_(None))
            ).scalars(),
        },
        key=str,
    )
    if not case_ids:
        raise NotFoundError("document not found")
    for case_id in case_ids:
        append_event(
            conn,
            chain_id=LIVE,
            event_type="document.downloaded",
            payload={"document_id": document_id, "sha256": sha256},
            actor_id=actor_id,
            case_id=case_id,
        )
    return case_ids
