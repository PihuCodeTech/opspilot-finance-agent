"""Approval routing and escalation (`04-prd.md` §8).

The amount is the larger of the stated and the recomputed total, as TruthLens
recorded it on the approval-level check. When the evidence run has no
conclusive approval-level check (no valid PO, or an uncertain reading), the
amount can't be verified and the case needs a controller (fail closed).
"""

import uuid
from decimal import Decimal

from sqlalchemy import Connection, select

from opspilot.db.models import CheckResult, User
from opspilot.domain.enums import ApproverTier, CheckCode, CheckStatus, UserRole


def approval_amount(conn: Connection, run_id: uuid.UUID) -> Decimal | None:
    compared = conn.execute(
        select(CheckResult.compared).where(
            CheckResult.run_id == run_id,
            CheckResult.check_code == CheckCode.APPROVAL_LEVEL,
            CheckResult.status != CheckStatus.INCONCLUSIVE,
        )
    ).scalar_one_or_none()
    if compared is None or compared.get("approval_amount") is None:
        return None
    return Decimal(str(compared["approval_amount"]))


def route(conn: Connection, amount: Decimal | None) -> ApproverTier | None:
    """The tier for this amount among active approvers, or None when nobody can approve."""
    approver = (User.role == UserRole.APPROVER) & User.is_active
    if amount is not None:
        standard = conn.execute(
            select(User.id).where(approver, User.approval_limit >= amount).limit(1)
        ).first()
        if standard is not None:
            return ApproverTier.STANDARD
    controller = conn.execute(select(User.id).where(approver, User.approval_unlimited).limit(1))
    return ApproverTier.CONTROLLER if controller.first() is not None else None
