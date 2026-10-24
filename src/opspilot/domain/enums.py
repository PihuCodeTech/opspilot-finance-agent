"""Closed sets used across the system. Values equal the database enum strings
(`06-database.md`) and the API's wire values (`06-api.md`)."""

from enum import StrEnum


class CaseStatus(StrEnum):
    NEW = "new"
    INVESTIGATING = "investigating"
    INVESTIGATION_FAILED = "investigation_failed"
    NEEDS_REVIEW = "needs_review"
    PENDING_APPROVAL = "pending_approval"
    HELD_FOR_APPROVER = "held_for_approver"
    APPROVED = "approved"
    CLOSED = "closed"


class ActorRole(StrEnum):
    ANALYST = "analyst"
    APPROVER = "approver"
    AUDITOR = "auditor"
    SYSTEM = "system"


class CaseEvent(StrEnum):
    """The events of transitions T1–T17 (`04-prd.md` §2)."""

    CREATE = "create"
    START_INVESTIGATION = "start_investigation"
    RETRY = "retry"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    DISMISS = "dismiss"
    CHANGE_PROPOSAL = "change_proposal"
    SUBMIT = "submit"
    APPROVER_AVAILABLE = "approver_available"
    APPROVE = "approve"
    DECLINE = "decline"
    ACTION_EXECUTED = "action_executed"
    REOPEN = "reopen"
    RECORD_RESOLUTION = "record_resolution"
    REQUEST_RECHECK = "request_recheck"


class DecisionKind(StrEnum):
    SUBMIT = "submit"
    DISMISS = "dismiss"
    APPROVE = "approve"
    DECLINE = "decline"
    REOPEN = "reopen"
    ESCALATE = "escalate"


class ActionType(StrEnum):
    APPROVE_FOR_PAYMENT = "approve_for_payment"
    PLACE_ON_HOLD = "place_on_hold"
    REJECT_INVOICE = "reject_invoice"
    DRAFT_VENDOR_QUERY = "draft_vendor_query"


class ProposalSource(StrEnum):
    AGENT = "agent"
    ANALYST = "analyst"


class FindingCode(StrEnum):
    MISSING_OR_UNKNOWN_PO = "MISSING_OR_UNKNOWN_PO"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    QTY_OVER_RECEIVED = "QTY_OVER_RECEIVED"
    ARITHMETIC_MISMATCH = "ARITHMETIC_MISMATCH"
    TAX_MISCALCULATED = "TAX_MISCALCULATED"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    VENDOR_NOT_APPROVED = "VENDOR_NOT_APPROVED"
    APPROVAL_LIMIT_EXCEEDED = "APPROVAL_LIMIT_EXCEEDED"
    BANK_DETAILS_CHANGED = "BANK_DETAILS_CHANGED"
    # Non-exception findings (`04-prd.md` §3)
    EXTRACTION_UNCERTAIN = "EXTRACTION_UNCERTAIN"
    FORENSICS_SIGNAL = "FORENSICS_SIGNAL"


EXCEPTION_CODES: frozenset[FindingCode] = frozenset(
    code
    for code in FindingCode
    if code not in {FindingCode.EXTRACTION_UNCERTAIN, FindingCode.FORENSICS_SIGNAL}
)
RESTRICTED_CODES: frozenset[FindingCode] = frozenset(
    {FindingCode.DUPLICATE_INVOICE, FindingCode.BANK_DETAILS_CHANGED}
)


class CheckStatus(StrEnum):
    PASS = "pass"  # noqa: S105  # a check result, not a password
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class RunKind(StrEnum):
    INVESTIGATION = "investigation"
    RECHECK = "recheck"
    REOPEN = "reopen"


class ResolutionType(StrEnum):
    PRICE_AGREED = "price_agreed"
    CREDIT_ISSUED = "credit_issued"
    ADDITIONAL_RECEIPT = "additional_receipt"
    VENDOR_APPROVED = "vendor_approved"
    PO_APPROVAL_OBTAINED = "po_approval_obtained"
    NOT_DUPLICATE = "not_duplicate"
    BANK_VERIFIED = "bank_verified"


class SupportingKind(StrEnum):
    CREDIT_NOTE = "credit_note"
    PO_AMENDMENT = "po_amendment"
    GOODS_RECEIPT = "goods_receipt"
    VENDOR_CONFIRMATION = "vendor_confirmation"
    VENDOR_APPROVAL = "vendor_approval"
    PO_APPROVAL = "po_approval"
    BANK_VERIFICATION = "bank_verification"
    INVOICE_COPY = "invoice_copy"


class VerificationChannel(StrEnum):
    CALLBACK_TO_MASTER_CONTACT = "callback_to_master_contact"
    SIGNED_LETTER_TO_MASTER_ADDRESS = "signed_letter_to_master_address"
    VENDOR_PORTAL_CHANGE_REQUEST = "vendor_portal_change_request"


class ApproverTier(StrEnum):
    STANDARD = "standard"
    CONTROLLER = "controller"
