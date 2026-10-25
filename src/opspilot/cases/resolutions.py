"""Finding-resolution validation (`04-prd.md` §3, `06-api.md` §5b).

A resolution is valid only if its type is allowed for the finding, every piece
of required evidence is present and of the right kind, and the structured
values pass the server check for its rule. A summary is required but never
sufficient. The same function validates a new resolution (T16) and
re-validates a stored one at approval and at execution (fail closed).

Every value checked comes from the database: the finding's linked check
results (what TruthLens compared), the case's supporting documents and bank
verifications, and ERP records. Document text is never read here.
"""

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Connection, select

from opspilot.audit.digests import rows_of
from opspilot.cases.errors import ConflictError, ValidationFailedError
from opspilot.db.models import (
    BankVerification,
    CheckResult,
    FindingCheckResult,
    LedgerInvoice,
    PurchaseOrder,
    SupportingDocument,
)
from opspilot.domain.enums import (
    CheckStatus,
    FindingCode,
    ResolutionType,
    SupportingKind,
)
from opspilot.domain.money import percent_change, round_money
from opspilot.policy.resolution_rules import requires_recheck, rule
from opspilot.policy.schema import (
    BANK_VERIFICATION_RECORD,
    LEDGER_INVOICE_REFERENCE,
    PolicyConfig,
    ResolutionRule,
)
from opspilot.truthlens.checks import normalise_invoice_number

LEDGER_ENTITY = "ledger_invoices"
DISTINGUISHING_FACTS = frozenset({"different_goods", "different_period", "different_po"})


@dataclass(frozen=True)
class EvidenceEntry:
    """Exactly one reference, as in `finding_resolution_evidence`."""

    supporting_document_id: uuid.UUID | None = None
    bank_verification_id: uuid.UUID | None = None
    erp_entity: str | None = None
    erp_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        refs = [self.supporting_document_id, self.bank_verification_id, self.erp_id]
        if sum(r is not None for r in refs) != 1 or (self.erp_id is None) != (
            self.erp_entity is None
        ):
            raise ValidationFailedError("each evidence entry references exactly one record")


class CheckFailedError(ValidationFailedError):
    code = "resolution_check_failed"


class EvidenceMissingError(ValidationFailedError):
    code = "resolution_evidence_missing"


# ---------------------------------------------------------------- inputs


def _decimal(structured: Mapping[str, Any], key: str, *, positive: bool = True) -> Decimal:
    value = structured.get(key)
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ValidationFailedError(f"{key} must be a decimal string", field=key)
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValidationFailedError(f"{key} must be a decimal string", field=key) from None
    if not number.is_finite() or (positive and number <= 0):
        raise ValidationFailedError(f"{key} must be a positive amount", field=key)
    return number


def _failed_checks(conn: Connection, finding_id: uuid.UUID) -> list[dict[str, Any]]:
    linked = select(FindingCheckResult.check_result_id).where(
        FindingCheckResult.finding_id == finding_id
    )
    checks = rows_of(conn, CheckResult, CheckResult.id.in_(linked))
    failed = [c for c in checks if c["status"] == CheckStatus.FAIL]
    if not failed:
        raise CheckFailedError("the finding has no failed check to compare against")
    return failed


def _amount(check: Mapping[str, Any], key: str) -> Decimal:
    value = check["compared"].get(key)
    if value is None:
        raise CheckFailedError(f"the check result has no {key}")
    return Decimal(str(value))


# ---------------------------------------------------------------- server checks


def _invoice_price_within_amended(ctx: "_Ctx") -> None:
    amended = _decimal(ctx.structured, "amended_unit_price")
    limit = ctx.policy.tolerances.unit_price_overcharge_pct
    for check in ctx.checks:
        if percent_change(_amount(check, "invoice_unit_price"), amended) > limit:
            raise CheckFailedError("the invoice unit price is above the amended price")


def _credit_covers(ctx: "_Ctx", required: Decimal, what: str) -> None:
    credit = _decimal(ctx.structured, "credit_amount")
    if required <= 0:
        raise CheckFailedError(f"there is no {what} for a credit note to cover")
    if credit < required:
        raise CheckFailedError(f"the credit {credit} is less than the {what} {required}")


def _credit_covers_overcharge(ctx: "_Ctx") -> None:
    required = sum(
        (
            round_money(
                (_amount(c, "invoice_unit_price") - _amount(c, "po_unit_price"))
                * _amount(c, "invoice_quantity")
            )
            for c in ctx.checks
        ),
        Decimal(0),
    )
    _credit_covers(ctx, required, "overcharge")


def _received_covers_billed(ctx: "_Ctx") -> None:
    received = _decimal(ctx.structured, "received_quantity")
    needed = max(_amount(c, "cumulative_billed") for c in ctx.checks)
    if received < needed:
        raise CheckFailedError(f"received {received} is less than the {needed} billed")


def _credit_covers_excess_quantity(ctx: "_Ctx") -> None:
    required = sum(
        (
            round_money(
                (_amount(c, "cumulative_billed") - _amount(c, "received"))
                * _amount(c, "invoice_unit_price")
            )
            for c in ctx.checks
        ),
        Decimal(0),
    )
    _credit_covers(ctx, required, "excess billed")


def _overstatement(check: Mapping[str, Any]) -> Decimal:
    compared = check["compared"]
    if "difference" in compared:  # a line, or tax: stated − computed
        return _amount(check, "difference")
    # invoice header: stated total − (sum of lines + stated tax)
    return _amount(check, "subtotal_gap") + _amount(check, "total_gap")


def _credit_covers_overstatement(ctx: "_Ctx") -> None:
    required = sum((max(_overstatement(c), Decimal(0)) for c in ctx.checks), Decimal(0))
    _credit_covers(ctx, required, "overstatement")


def _approval_names_vendor(ctx: "_Ctx") -> None:
    code = ctx.structured.get("vendor_code")
    expected = {c["compared"].get("vendor_code") for c in ctx.checks}
    if not isinstance(code, str) or {code} != expected:
        raise CheckFailedError("the vendor code doesn't match the invoice's vendor")


def required_approval_level(policy: PolicyConfig, amount: Decimal) -> int:
    levels = policy.approval_levels
    if amount > levels.level_2_max:
        return 3
    return 2 if amount > levels.level_1_max else 1


def _approval_at_required_level(ctx: "_Ctx") -> None:
    level = ctx.structured.get("approval_level")
    if isinstance(level, bool) or level not in (1, 2, 3):
        raise ValidationFailedError("approval_level must be 1, 2 or 3", field="approval_level")
    needed = max(
        required_approval_level(ctx.policy, _amount(c, "approval_amount")) for c in ctx.checks
    )
    if level < needed:
        raise CheckFailedError(f"the approval is at level {level}; level {needed} is required")


def _not_duplicate(ctx: "_Ctx") -> None:
    check = ctx.checks[0]
    compared = check["compared"]
    ledger_ids = [e.erp_id for e in ctx.evidence if e.erp_entity == LEDGER_ENTITY]
    match = next(
        (
            m
            for m in compared.get("matches", [])
            if m.get("ledger_invoice_id") in {str(i) for i in ledger_ids}
        ),
        None,
    )
    if match is None:
        raise CheckFailedError("the referenced ledger invoice isn't the one the check matched")
    numbers = ctx.structured.get("both_invoice_numbers")
    if (
        not isinstance(numbers, list)
        or len(numbers) != 2  # noqa: PLR2004  # exactly the two invoices
        or not all(isinstance(n, str) for n in numbers)
        or {normalise_invoice_number(n) for n in numbers}
        != {compared["normalised"], normalise_invoice_number(match["ledger_invoice"])}
    ):
        raise CheckFailedError("the confirmation must name both invoice numbers")
    fact = ctx.structured.get("distinguishing_fact")
    if fact not in DISTINGUISHING_FACTS:
        raise ValidationFailedError(
            f"distinguishing_fact must be one of {sorted(DISTINGUISHING_FACTS)}",
            field="distinguishing_fact",
        )
    if fact == "different_goods" and match.get("same_lines", True):
        raise CheckFailedError("the ERP shows the same goods on both invoices")
    if fact == "different_period":
        gap = abs(
            dt.date.fromisoformat(compared["invoice_date"])
            - dt.date.fromisoformat(match["invoice_date"])
        )
        if gap.days <= ctx.policy.tolerances.near_duplicate_window_days:
            raise CheckFailedError("the ERP shows both invoices in the same period")
    if fact == "different_po":
        ledger_po = ctx.conn.execute(
            select(PurchaseOrder.po_number)
            .join(LedgerInvoice, LedgerInvoice.po_id == PurchaseOrder.id)
            .where(LedgerInvoice.id == uuid.UUID(match["ledger_invoice_id"]))
        ).scalar_one_or_none()
        ours = compared.get("invoice_po_number")
        if (
            ledger_po is None
            or ours is None
            or (normalise_invoice_number(ledger_po) == normalise_invoice_number(ours))
        ):
            raise CheckFailedError("the ERP doesn't show the invoices on different POs")


def _bank_verified(ctx: "_Ctx") -> None:
    invoice_fps = {c["compared"].get("invoice_bank_fp") for c in ctx.checks}
    channels = ctx.policy.bank_verification.channels
    for verification in ctx.verifications:
        if verification["contact_source"] != ctx.policy.bank_verification.contact_source:
            raise CheckFailedError("the contact must come from the vendor master")
        if verification["channel"] not in channels:
            raise CheckFailedError("the verification channel isn't allowed")
        if {verification["verified_bank_fp"]} != invoice_fps:
            raise CheckFailedError("the verified account doesn't match the invoice's account")
        cited_docs = {e.supporting_document_id for e in ctx.evidence}
        if verification["evidence_supporting_document_id"] not in cited_docs:
            raise EvidenceMissingError("cite the verification's own evidence document")


_SERVER_CHECKS = {
    "invoice_price_within_amended_price": _invoice_price_within_amended,
    "credit_covers_overcharge": _credit_covers_overcharge,
    "cumulative_received_covers_billed": _received_covers_billed,
    "credit_covers_excess_quantity": _credit_covers_excess_quantity,
    "credit_covers_overstatement": _credit_covers_overstatement,
    "approval_names_vendor": _approval_names_vendor,
    "approval_at_required_level": _approval_at_required_level,
    "confirmation_names_both_invoices": _not_duplicate,
    "verified_fingerprint_matches_invoice": _bank_verified,
}


@dataclass(frozen=True)
class _Ctx:
    conn: Connection
    policy: PolicyConfig
    rule: ResolutionRule
    checks: list[dict[str, Any]]
    evidence: Sequence[EvidenceEntry]
    structured: Mapping[str, Any]
    verifications: list[dict[str, Any]]


# ---------------------------------------------------------------- validation


def _check_evidence(
    conn: Connection,
    case_id: uuid.UUID,
    the_rule: ResolutionRule,
    evidence: Sequence[EvidenceEntry],
    checks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Every required item present, every entry used and from this case. Returns verifications."""
    doc_ids = [e.supporting_document_id for e in evidence if e.supporting_document_id]
    docs = {
        d["id"]: d for d in rows_of(conn, SupportingDocument, SupportingDocument.id.in_(doc_ids))
    }
    bank_ids = [e.bank_verification_id for e in evidence if e.bank_verification_id]
    verifications = rows_of(conn, BankVerification, BankVerification.id.in_(bank_ids))
    if len(docs) != len(set(doc_ids)) or any(d["case_id"] != case_id for d in docs.values()):
        raise ValidationFailedError("a supporting document isn't part of this case")
    if len(verifications) != len(set(bank_ids)) or any(
        v["case_id"] != case_id for v in verifications
    ):
        raise ValidationFailedError("a bank verification isn't part of this case")
    matched_ledgers = {
        m.get("ledger_invoice_id") for c in checks for m in c["compared"].get("matches", [])
    }
    kinds = {SupportingKind(d["kind"]).value for d in docs.values()}
    present = set(kinds)
    if verifications:
        present.add(BANK_VERIFICATION_RECORD)
    for entry in evidence:
        if entry.erp_entity is not None:
            if entry.erp_entity != LEDGER_ENTITY or str(entry.erp_id) not in matched_ledgers:
                raise ValidationFailedError("an ERP reference must be the matched ledger invoice")
            present.add(LEDGER_INVOICE_REFERENCE)
    missing = [item for item in the_rule.evidence if item not in present]
    if missing:
        raise EvidenceMissingError("required evidence is missing", missing=missing)
    unexpected = sorted(present - set(the_rule.evidence))
    if unexpected:
        raise ValidationFailedError(
            "evidence of a kind this resolution doesn't use", unexpected=unexpected
        )
    return verifications


def validate_resolution(
    conn: Connection,
    policy: PolicyConfig,
    finding: Mapping[str, Any],
    *,
    resolution_type: ResolutionType,
    summary: str,
    evidence: Sequence[EvidenceEntry],
    structured: Mapping[str, Any],
) -> None:
    code = FindingCode(finding["exception_type"])
    if requires_recheck(policy, code):
        raise ConflictError(
            f"{code} is cleared only by a successful recheck", code="recheck_required"
        )
    the_rule = rule(policy, code, resolution_type)
    if the_rule is None:
        raise ValidationFailedError(
            f"{resolution_type} doesn't resolve {code}", code="resolution_type_not_allowed"
        )
    if not summary.strip():
        raise ValidationFailedError("a summary is required", field="summary")
    missing_fields = sorted(set(the_rule.structured) - set(structured))
    extra_fields = sorted(set(structured) - set(the_rule.structured))
    if missing_fields or extra_fields:
        raise ValidationFailedError(
            "structured values don't match the rule",
            missing=missing_fields,
            unexpected=extra_fields,
        )
    checks = _failed_checks(conn, finding["id"])
    verifications = _check_evidence(conn, finding["case_id"], the_rule, evidence, checks)
    server_check = _SERVER_CHECKS.get(the_rule.check)
    if server_check is None:  # a policy naming a check this code doesn't have: fail closed
        raise CheckFailedError(f"no server check named {the_rule.check}")
    server_check(_Ctx(conn, policy, the_rule, checks, evidence, structured, verifications))
