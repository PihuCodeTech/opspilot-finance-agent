"""What the case service reads before deciding anything.

The actor comes from `users` and the case is locked `FOR UPDATE`. The policy is
the version the evidence run was made under. The evidence state covers the
findings of the current evidence run with each finding's current resolution
(re-validated), whether any check is inconclusive, and the current proposal.
All of it is read from the database inside the caller's transaction.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, select

from opspilot.audit.chain import LIVE, eval_chain
from opspilot.audit.digests import rows_of
from opspilot.cases.errors import ForbiddenError, NotFoundError, ServiceError
from opspilot.cases.resolutions import EvidenceEntry, validate_resolution
from opspilot.db.models import (
    Case,
    CheckResult,
    Finding,
    FindingResolution,
    FindingResolutionEvidence,
    InvestigationRun,
    PolicyVersion,
    Proposal,
    User,
)
from opspilot.domain.enums import (
    ActionType,
    ApproverTier,
    CheckStatus,
    FindingCode,
    ProposalSource,
    UserRole,
)
from opspilot.policy.allowlist import FindingState, allowed_actions
from opspilot.policy.schema import PolicyConfig


@dataclass(frozen=True)
class Actor:
    id: uuid.UUID
    role: UserRole
    approval_limit: Decimal | None
    approval_unlimited: bool

    def can_approve(self, tier: ApproverTier | None, amount: Decimal | None) -> bool:
        """Tier eligibility and monetary authority; an unknown amount needs a controller."""
        if self.role is not UserRole.APPROVER:
            return False
        if self.approval_unlimited:
            return True
        if tier is not ApproverTier.STANDARD or amount is None or self.approval_limit is None:
            return False
        return amount <= self.approval_limit


def load_actor(conn: Connection, user_id: uuid.UUID) -> Actor:
    user = conn.execute(select(User).where(User.id == user_id)).first()
    if user is None or not user.is_active:
        raise ForbiddenError("this account can't act")
    return Actor(user.id, UserRole(user.role), user.approval_limit, user.approval_unlimited)


def lock_case(conn: Connection, case_id: uuid.UUID) -> dict[str, Any]:
    table = Case.__table__
    row = conn.execute(select(table).where(table.c.id == case_id).with_for_update()).first()
    if row is None:
        raise NotFoundError("case not found")
    return dict(row._mapping)


def chain_for(case: dict[str, Any]) -> str:
    return eval_chain(case["eval_run_id"]) if case["eval_run_id"] else LIVE


def load_policy_config(conn: Connection, policy_version_id: uuid.UUID) -> PolicyConfig:
    config = conn.execute(
        select(PolicyVersion.config).where(PolicyVersion.id == policy_version_id)
    ).scalar_one_or_none()
    if config is None:
        raise ServiceError("policy version not found")
    return PolicyConfig.model_validate(config)


@dataclass(frozen=True)
class FindingView:
    row: dict[str, Any]
    resolution: dict[str, Any] | None  # the current (latest) resolution, if any
    resolution_valid: bool

    @property
    def code(self) -> FindingCode:
        return FindingCode(self.row["exception_type"])


@dataclass(frozen=True)
class EvidenceState:
    run_id: uuid.UUID
    policy_version_id: uuid.UUID
    policy: PolicyConfig
    findings: list[FindingView]
    inconclusive: bool
    proposal: dict[str, Any] | None

    def finding_states(self) -> list[FindingState]:
        return [FindingState(f.code, f.resolution_valid) for f in self.findings]

    def needing_resolution(self) -> list[FindingView]:
        skip = self.policy.resolutions.no_resolution_needed
        return [f for f in self.findings if f.code not in skip]

    def unresolved(self) -> list[FindingView]:
        recheck = self.policy.resolutions.recheck_only
        return [f for f in self.needing_resolution() if not f.resolution_valid or f.code in recheck]

    def current_resolutions(self) -> dict[uuid.UUID, bytes]:
        return {
            f.resolution["id"]: bytes(f.resolution["digest"])
            for f in self.needing_resolution()
            if f.resolution is not None
        }

    def allowed(self, source: ProposalSource) -> frozenset[ActionType]:
        return allowed_actions(self.policy, source, self.finding_states())


def _current_resolution(conn: Connection, finding_id: uuid.UUID) -> dict[str, Any] | None:
    rows = rows_of(conn, FindingResolution, FindingResolution.finding_id == finding_id)
    # A newer resolution supersedes by insertion; the ID breaks a same-instant tie.
    return max(rows, key=lambda r: (r["created_at"], str(r["id"])), default=None)


def resolution_is_valid(
    conn: Connection, policy: PolicyConfig, finding: dict[str, Any], resolution: dict[str, Any]
) -> bool:
    evidence = [
        EvidenceEntry(
            e["supporting_document_id"], e["bank_verification_id"], e["erp_entity"], e["erp_id"]
        )
        for e in rows_of(
            conn,
            FindingResolutionEvidence,
            FindingResolutionEvidence.resolution_id == resolution["id"],
        )
    ]
    try:
        validate_resolution(
            conn,
            policy,
            finding,
            resolution_type=resolution["resolution_type"],
            summary=resolution["summary"],
            evidence=evidence,
            structured=resolution["structured"],
        )
    except ServiceError:
        return False
    return True


def current_proposal(conn: Connection, case_id: uuid.UUID) -> dict[str, Any] | None:
    rows = rows_of(conn, Proposal, Proposal.case_id == case_id)
    return max(rows, key=lambda r: (r["created_at"], str(r["id"])), default=None)


def load_evidence(
    conn: Connection, case: dict[str, Any], run_id: uuid.UUID | None = None
) -> EvidenceState:
    """The case's current evidence, or a given run's (the agent proposes before T4)."""
    run_id = run_id or case["evidence_run_id"]
    if run_id is None:
        raise ServiceError("the case has no evidence run yet")
    policy_version_id = conn.execute(
        select(InvestigationRun.policy_version_id).where(InvestigationRun.id == run_id)
    ).scalar_one()
    policy = load_policy_config(conn, policy_version_id)
    findings = []
    for row in sorted(
        rows_of(conn, Finding, Finding.run_id == run_id),
        key=lambda r: (str(r["exception_type"]), r["subject_key"]),
    ):
        resolution = _current_resolution(conn, row["id"])
        valid = resolution is not None and resolution_is_valid(conn, policy, row, resolution)
        findings.append(FindingView(row, resolution, valid))
    inconclusive = (
        conn.execute(
            select(CheckResult.id)
            .where(CheckResult.run_id == run_id, CheckResult.status == CheckStatus.INCONCLUSIVE)
            .limit(1)
        ).first()
        is not None
    )
    return EvidenceState(
        run_id,
        policy_version_id,
        policy,
        findings,
        inconclusive,
        current_proposal(conn, case["id"]),
    )
