"""The transition table must equal PRD §2 (T1–T17), and nothing else may be allowed."""

import itertools

import pytest

from opspilot.domain.enums import ActorRole, CaseEvent, CaseStatus
from opspilot.domain.transitions import TRANSITIONS, is_allowed, transitions_for

S, E, R = CaseStatus, CaseEvent, ActorRole

# Transcribed from 04-prd.md §2 (id, from, event, to, actor).
EXPECTED = {
    ("T1", None, E.CREATE, S.NEW, R.ANALYST),
    ("T2", S.NEW, E.START_INVESTIGATION, S.INVESTIGATING, R.SYSTEM),
    ("T3", S.INVESTIGATION_FAILED, E.RETRY, S.INVESTIGATING, R.ANALYST),
    ("T4", S.INVESTIGATING, E.RUN_SUCCEEDED, S.NEEDS_REVIEW, R.SYSTEM),
    ("T5", S.INVESTIGATING, E.RUN_FAILED, S.INVESTIGATION_FAILED, R.SYSTEM),
    ("T6", S.INVESTIGATION_FAILED, E.DISMISS, S.CLOSED, R.ANALYST),
    ("T7", S.NEEDS_REVIEW, E.CHANGE_PROPOSAL, S.NEEDS_REVIEW, R.ANALYST),
    ("T8", S.NEEDS_REVIEW, E.DISMISS, S.CLOSED, R.ANALYST),
    ("T9", S.NEEDS_REVIEW, E.SUBMIT, S.PENDING_APPROVAL, R.ANALYST),
    ("T10", S.NEEDS_REVIEW, E.SUBMIT, S.HELD_FOR_APPROVER, R.ANALYST),
    ("T11", S.HELD_FOR_APPROVER, E.APPROVER_AVAILABLE, S.PENDING_APPROVAL, R.SYSTEM),
    ("T12", S.PENDING_APPROVAL, E.APPROVE, S.APPROVED, R.APPROVER),
    ("T13", S.PENDING_APPROVAL, E.DECLINE, S.NEEDS_REVIEW, R.APPROVER),
    ("T14", S.APPROVED, E.ACTION_EXECUTED, S.CLOSED, R.SYSTEM),
    ("T15", S.CLOSED, E.REOPEN, S.NEEDS_REVIEW, R.APPROVER),
    ("T16", S.NEEDS_REVIEW, E.RECORD_RESOLUTION, S.NEEDS_REVIEW, R.ANALYST),
    ("T17", S.NEEDS_REVIEW, E.REQUEST_RECHECK, S.INVESTIGATING, R.ANALYST),
}


def test_table_equals_the_prd() -> None:
    actual = {(t.id, t.source, t.event, t.target, t.actor) for t in TRANSITIONS}
    assert actual == EXPECTED


def test_every_combination_outside_the_table_is_rejected() -> None:
    allowed = {(src, ev, actor) for _, src, ev, _, actor in EXPECTED}
    for src, ev, actor in itertools.product([None, *S], E, R):
        assert is_allowed(src, ev, actor) is ((src, ev, actor) in allowed), (src, ev, actor)


def test_auditors_can_do_nothing() -> None:
    for src, ev in itertools.product([None, *S], E):
        assert not is_allowed(src, ev, R.AUDITOR)


def test_submit_has_two_outcomes_chosen_by_the_service() -> None:
    targets = {t.target for t in transitions_for(S.NEEDS_REVIEW, E.SUBMIT, R.ANALYST)}
    assert targets == {S.PENDING_APPROVAL, S.HELD_FOR_APPROVER}


@pytest.mark.parametrize("event", [E.APPROVE, E.DECLINE])
def test_analysts_cannot_decide(event: CaseEvent) -> None:
    assert not is_allowed(S.PENDING_APPROVAL, event, R.ANALYST)


def test_nothing_leaves_approved_except_execution() -> None:
    leaving = {t.event for t in TRANSITIONS if t.source is S.APPROVED}
    assert leaving == {E.ACTION_EXECUTED}
