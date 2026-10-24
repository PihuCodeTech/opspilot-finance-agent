"""The case transition table T1–T17, transcribed from `04-prd.md` §2.

This table answers only "may this actor fire this event from this state, and
where does it lead?". The rule conditions in the PRD's last column (allowlists,
resolutions, limits, escalation) are enforced by the case service in P9.
"""

from dataclasses import dataclass

from opspilot.domain.enums import ActorRole, CaseEvent, CaseStatus

S = CaseStatus
E = CaseEvent
R = ActorRole


@dataclass(frozen=True)
class Transition:
    id: str
    source: CaseStatus | None  # None: the case doesn't exist yet (T1)
    event: CaseEvent
    target: CaseStatus
    actor: ActorRole


TRANSITIONS: tuple[Transition, ...] = (
    Transition("T1", None, E.CREATE, S.NEW, R.ANALYST),
    Transition("T2", S.NEW, E.START_INVESTIGATION, S.INVESTIGATING, R.SYSTEM),
    Transition("T3", S.INVESTIGATION_FAILED, E.RETRY, S.INVESTIGATING, R.ANALYST),
    Transition("T4", S.INVESTIGATING, E.RUN_SUCCEEDED, S.NEEDS_REVIEW, R.SYSTEM),
    Transition("T5", S.INVESTIGATING, E.RUN_FAILED, S.INVESTIGATION_FAILED, R.SYSTEM),
    Transition("T6", S.INVESTIGATION_FAILED, E.DISMISS, S.CLOSED, R.ANALYST),
    Transition("T7", S.NEEDS_REVIEW, E.CHANGE_PROPOSAL, S.NEEDS_REVIEW, R.ANALYST),
    Transition("T8", S.NEEDS_REVIEW, E.DISMISS, S.CLOSED, R.ANALYST),
    Transition("T9", S.NEEDS_REVIEW, E.SUBMIT, S.PENDING_APPROVAL, R.ANALYST),
    Transition("T10", S.NEEDS_REVIEW, E.SUBMIT, S.HELD_FOR_APPROVER, R.ANALYST),
    Transition("T11", S.HELD_FOR_APPROVER, E.APPROVER_AVAILABLE, S.PENDING_APPROVAL, R.SYSTEM),
    Transition("T12", S.PENDING_APPROVAL, E.APPROVE, S.APPROVED, R.APPROVER),
    Transition("T13", S.PENDING_APPROVAL, E.DECLINE, S.NEEDS_REVIEW, R.APPROVER),
    Transition("T14", S.APPROVED, E.ACTION_EXECUTED, S.CLOSED, R.SYSTEM),
    Transition("T15", S.CLOSED, E.REOPEN, S.NEEDS_REVIEW, R.APPROVER),
    Transition("T16", S.NEEDS_REVIEW, E.RECORD_RESOLUTION, S.NEEDS_REVIEW, R.ANALYST),
    Transition("T17", S.NEEDS_REVIEW, E.REQUEST_RECHECK, S.INVESTIGATING, R.ANALYST),
)


def transitions_for(
    source: CaseStatus | None, event: CaseEvent, actor: ActorRole
) -> tuple[Transition, ...]:
    """All transitions this actor may take for this event from this state.

    Usually one; `submit` has two (T9 or T10), chosen by the service from approver availability.
    An empty result means the request is an invalid transition (409) or forbidden for the role.
    """
    return tuple(
        t for t in TRANSITIONS if t.source == source and t.event == event and t.actor == actor
    )


def is_allowed(source: CaseStatus | None, event: CaseEvent, actor: ActorRole) -> bool:
    return bool(transitions_for(source, event, actor))
