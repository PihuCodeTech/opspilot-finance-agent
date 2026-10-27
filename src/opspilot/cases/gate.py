"""Payment and proposal safeguards, shared by every path (`04-prd.md` §3, §6.5a).

The same functions run when a proposal is made, at submit, at approve and in
the executor, so no path can be more permissive than another. They read only
the evidence state; a reason or summary never changes the outcome.
"""

from opspilot.cases.errors import ConflictError, ForbiddenError
from opspilot.cases.state import EvidenceState
from opspilot.domain.enums import RESTRICTED_CODES, ActionType, ProposalSource


def require_payment_clear(state: EvidenceState) -> None:
    """Every finding resolved with a valid resolution, and no inconclusive check."""
    unresolved = state.unresolved()
    if any(f.code in RESTRICTED_CODES for f in unresolved):
        raise ForbiddenError(
            "an unresolved duplicate or bank-detail change blocks payment",
            code="restricted_finding",
        )
    if state.inconclusive:
        raise ConflictError(
            "a check is inconclusive; only a successful recheck clears it",
            code="inconclusive_checks",
        )
    if unresolved:
        raise ConflictError(
            "every finding needs a valid resolution before payment",
            code="unresolved_findings",
            findings=[str(f.row["id"]) for f in unresolved],
        )


def require_action_allowed(
    state: EvidenceState, action: ActionType, source: ProposalSource
) -> None:
    if action is ActionType.APPROVE_FOR_PAYMENT:
        require_payment_clear(state)
    if action not in state.allowed(source):
        raise ForbiddenError(f"{action} isn't allowed for this case", code="action_not_allowed")
