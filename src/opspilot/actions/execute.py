"""`actions.execute`: gate layer 2 (`06-architecture.md` §3.3).

Called by the resumed graph after an approval, but it trusts nothing that came
before it. In one transaction, with the case locked, it re-checks everything:
- the state
- the latest decision and the approver's authority
- the proposal against its allowlist
- for payment: every resolution re-validated, no inconclusive check, and the
  stored acceptances equal to the current resolutions

Then it runs the simulated executor and writes `executed_actions`, which the
database allows once per approve decision (layer 3). Any gap refuses (fail
closed).
"""

import uuid
from typing import Any

from sqlalchemy import Connection, insert, select

from opspilot.cases.errors import ConflictError, ForbiddenError
from opspilot.cases.gate import require_action_allowed, require_payment_clear
from opspilot.cases.service import check_acceptances, check_approver, mark_closed_after_execution
from opspilot.cases.state import load_actor, load_evidence, lock_case
from opspilot.db.models import Decision, DecisionResolutionAcceptance, ExecutedAction, Proposal
from opspilot.domain.enums import ActionType, CaseStatus, DecisionKind, ProposalSource


def _simulate(action: ActionType, case_id: uuid.UUID, decision_id: uuid.UUID) -> dict[str, Any]:
    """No real payment system is ever called: the executors only describe what they'd do."""
    reference = f"SIM-{str(decision_id)[:8].upper()}"
    return {
        "simulated": True,
        "reference": reference,
        "effect": {
            ActionType.APPROVE_FOR_PAYMENT: "invoice released for payment",
            ActionType.PLACE_ON_HOLD: "invoice placed on hold",
            ActionType.REJECT_INVOICE: "invoice rejected",
            ActionType.DRAFT_VENDOR_QUERY: "vendor query drafted for sending",
        }[action],
        "case_id": str(case_id),
    }


def execute(conn: Connection, case_id: uuid.UUID, decision_id: uuid.UUID) -> uuid.UUID:
    case = lock_case(conn, case_id)
    if case["status"] != CaseStatus.APPROVED:
        raise ConflictError("the case isn't approved")
    latest = conn.execute(
        select(Decision.__table__)
        .where(Decision.case_id == case_id)
        # case_version increases with every change to the case; created_at breaks
        # the tie between decisions written in the same change (submit + escalate).
        .order_by(Decision.case_version.desc(), Decision.created_at.desc())
        .limit(1)
    ).first()
    if latest is None or latest.id != decision_id or latest.kind != DecisionKind.APPROVE:
        raise ConflictError("this isn't the case's latest approve decision")
    check_approver(conn, load_actor(conn, latest.actor_id), case)

    state = load_evidence(conn, case)
    proposal = conn.execute(
        select(Proposal.__table__).where(Proposal.id == latest.proposal_id)
    ).first()
    if proposal is None or state.proposal is None or proposal.id != state.proposal["id"]:
        raise ConflictError("the proposal changed after approval")
    action = ActionType(proposal.action)
    require_action_allowed(state, action, ProposalSource(proposal.source))
    if action is ActionType.APPROVE_FOR_PAYMENT:
        require_payment_clear(state)
        stored = conn.execute(
            select(
                DecisionResolutionAcceptance.resolution_id,
                DecisionResolutionAcceptance.resolution_digest,
            ).where(DecisionResolutionAcceptance.decision_id == decision_id)
        ).all()
        try:
            check_acceptances(
                state, [(r.resolution_id, bytes(r.resolution_digest)) for r in stored]
            )
        except Exception as exc:
            raise ForbiddenError("the approval no longer matches the resolutions") from exc

    executed_id = uuid.uuid4()
    conn.execute(
        insert(ExecutedAction).values(
            id=executed_id,
            case_id=case_id,
            decision_id=decision_id,
            action=action,
            payload=_simulate(action, case_id, decision_id),
        )
    )
    mark_closed_after_execution(conn, case, decision_id, action)
    return executed_id
