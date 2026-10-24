"""Validated model of `policy/config.toml`.

Validation fails closed: a configuration that would let payment past an
unresolved, restricted or inconclusive finding, or that leaves a finding with
no safe action, is refused at load time (`rules.md` §2).
"""

from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from opspilot.domain.enums import (
    EXCEPTION_CODES,
    RESTRICTED_CODES,
    ActionType,
    FindingCode,
    ResolutionType,
    SupportingKind,
    VerificationChannel,
)


def _no_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("write decimals as strings in the policy file (floats are inexact)")
    return value


ExactDecimal = Annotated[Decimal, BeforeValidator(_no_float), Field(ge=0)]

# Evidence a resolution can require: a supporting-document kind, or one of these records.
BANK_VERIFICATION_RECORD = "bank_verification_record"
LEDGER_INVOICE_REFERENCE = "ledger_invoice_reference"
EVIDENCE_ITEMS = frozenset({k.value for k in SupportingKind}) | {
    BANK_VERIFICATION_RECORD,
    LEDGER_INVOICE_REFERENCE,
}

# Structured values an analyst records with a resolution; each has a server check (P9).
STRUCTURED_FIELDS = frozenset(
    {
        "amended_unit_price",
        "credit_amount",
        "received_quantity",
        "vendor_code",
        "approval_level",
        "both_invoice_numbers",
        "distinguishing_fact",
    }
)

Category = Literal["clean", "inconclusive", "restricted", "other", "all_resolved"]
UNSAFE_CATEGORIES: tuple[Category, ...] = ("inconclusive", "restricted", "other")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Tolerances(_Strict):
    unit_price_overcharge_pct: ExactDecimal
    unit_price_undercharge_info_pct: ExactDecimal
    line_arithmetic_abs: ExactDecimal
    invoice_total_abs: ExactDecimal
    tax_abs: ExactDecimal
    near_duplicate_total_pct: ExactDecimal
    near_duplicate_window_days: int = Field(gt=0)


class ApprovalLevels(_Strict):
    level_1_max: ExactDecimal
    level_2_max: ExactDecimal
    level_3_requires_controller_approval: bool

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.level_1_max < self.level_2_max:
            raise ValueError("level_1_max must be below level_2_max")
        return self


class ApproverLimits(_Strict):
    standard: ExactDecimal


class BankVerification(_Strict):
    contact_source: Literal["vendor_master"]
    channels: tuple[VerificationChannel, ...] = Field(min_length=1)


class AgentAllowlist(_Strict):
    clean: frozenset[ActionType]
    inconclusive: frozenset[ActionType]
    restricted: frozenset[ActionType]
    other: frozenset[ActionType]


class AnalystAllowlist(AgentAllowlist):
    all_resolved: frozenset[ActionType]


class Allowlists(_Strict):
    agent: AgentAllowlist
    analyst: AnalystAllowlist

    @model_validator(mode="after")
    def _fail_closed(self) -> Self:
        for actor, lists in (("agent", self.agent), ("analyst", self.analyst)):
            for category in UNSAFE_CATEGORIES:
                allowed: frozenset[ActionType] = getattr(lists, category)
                if ActionType.APPROVE_FOR_PAYMENT in allowed:
                    raise ValueError(f"{actor}.{category} must not allow approve_for_payment")
                if ActionType.PLACE_ON_HOLD not in allowed:
                    raise ValueError(f"{actor}.{category} must include place_on_hold")
            if not lists.clean:
                raise ValueError(f"{actor}.clean must not be empty")
        if ActionType.PLACE_ON_HOLD not in self.analyst.all_resolved:
            raise ValueError("analyst.all_resolved must include place_on_hold")
        return self


class ResolutionRule(_Strict):
    finding: FindingCode
    type: ResolutionType
    evidence: tuple[str, ...] = Field(min_length=1)
    structured: tuple[str, ...]
    check: str = Field(min_length=1)

    @model_validator(mode="after")
    def _known_evidence(self) -> Self:
        unknown = set(self.evidence) - EVIDENCE_ITEMS
        if unknown:
            raise ValueError(f"unknown evidence items: {sorted(unknown)}")
        unknown_fields = set(self.structured) - STRUCTURED_FIELDS
        if unknown_fields:
            raise ValueError(f"unknown structured fields: {sorted(unknown_fields)}")
        return self


class Resolutions(_Strict):
    recheck_only: frozenset[FindingCode]
    no_resolution_needed: frozenset[FindingCode]
    rules: tuple[ResolutionRule, ...]

    @model_validator(mode="after")
    def _complete_and_safe(self) -> Self:
        ruled = {r.finding for r in self.rules}
        if ruled & self.recheck_only:
            raise ValueError("recheck-only findings must not have resolution rules")
        if ruled & self.no_resolution_needed:
            raise ValueError("findings needing no resolution must not have rules")
        if FindingCode.EXTRACTION_UNCERTAIN not in self.recheck_only:
            raise ValueError("EXTRACTION_UNCERTAIN must be recheck-only")
        uncovered = EXCEPTION_CODES - ruled - self.recheck_only
        if uncovered:
            raise ValueError(f"exception findings with no way to clear: {sorted(uncovered)}")
        stray = self.no_resolution_needed & EXCEPTION_CODES
        if stray:
            raise ValueError(f"exception findings can't skip resolution: {sorted(stray)}")
        # Restricted findings clear only through their dedicated, stronger rule.
        required_type = {
            FindingCode.DUPLICATE_INVOICE: ResolutionType.NOT_DUPLICATE,
            FindingCode.BANK_DETAILS_CHANGED: ResolutionType.BANK_VERIFIED,
        }
        for rule in self.rules:
            if rule.finding in RESTRICTED_CODES and rule.type is not required_type[rule.finding]:
                raise ValueError(
                    f"{rule.finding} can only be cleared by {required_type[rule.finding]}"
                )
        bank_rules = [r for r in self.rules if r.finding is FindingCode.BANK_DETAILS_CHANGED]
        if any(BANK_VERIFICATION_RECORD not in r.evidence for r in bank_rules):
            raise ValueError("bank changes require a bank verification record")
        duplicate_rules = [r for r in self.rules if r.finding is FindingCode.DUPLICATE_INVOICE]
        if any(
            not {SupportingKind.VENDOR_CONFIRMATION.value, LEDGER_INVOICE_REFERENCE}
            <= set(r.evidence)
            for r in duplicate_rules
        ):
            raise ValueError(
                "duplicates require a vendor confirmation and the matched ledger invoice"
            )
        return self


class PolicyConfig(_Strict):
    version: str = Field(min_length=1)
    currency: Literal["USD"]
    rounding: Literal["half_up"]
    tolerances: Tolerances
    approval_levels: ApprovalLevels
    approver_limits: ApproverLimits
    bank_verification: BankVerification
    allowlists: Allowlists
    resolutions: Resolutions
