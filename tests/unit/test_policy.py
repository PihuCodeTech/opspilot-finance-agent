"""Policy configuration: loads, matches the PRD's documented values, and fails closed."""

import copy
import itertools
import tomllib
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from opspilot.domain.digest import sha256_hex
from opspilot.domain.enums import (
    EXCEPTION_CODES,
    RESTRICTED_CODES,
    ActionType,
    FindingCode,
    ProposalSource,
    ResolutionType,
)
from opspilot.policy.allowlist import FindingState, allowed_actions
from opspilot.policy.loader import DEFAULT_POLICY_PATH, load_policy
from opspilot.policy.resolution_rules import requires_recheck, rule, rules_for
from opspilot.policy.schema import PolicyConfig

A = ActionType
F = FindingCode
AGENT, ANALYST = ProposalSource.AGENT, ProposalSource.ANALYST
HOLD, QUERY, REJECT, PAY = (
    A.PLACE_ON_HOLD,
    A.DRAFT_VENDOR_QUERY,
    A.REJECT_INVOICE,
    A.APPROVE_FOR_PAYMENT,
)

POLICY = load_policy().config


def _raw() -> dict[str, Any]:
    with DEFAULT_POLICY_PATH.open("rb") as f:
        return tomllib.load(f)


# ---- values documented in 04-prd.md D8 and §8 -------------------------------------------


def test_tolerances_and_limits_match_the_prd() -> None:
    t = POLICY.tolerances
    assert t.unit_price_overcharge_pct == Decimal("2.00")
    assert t.line_arithmetic_abs == Decimal("0.01")
    assert t.invoice_total_abs == Decimal("0.05")
    assert t.tax_abs == Decimal("0.01")
    assert t.near_duplicate_total_pct == Decimal("0.50")
    assert t.near_duplicate_window_days == 30
    assert POLICY.approval_levels.level_1_max == Decimal("10000.00")
    assert POLICY.approval_levels.level_2_max == Decimal("50000.00")
    assert POLICY.approval_levels.level_3_requires_controller_approval
    assert POLICY.approver_limits.standard == Decimal("10000.00")
    assert POLICY.currency == "USD"


# ---- allowlists (04-prd.md §3) ---------------------------------------------------------


def _allowed(actor: ProposalSource, *findings: FindingState) -> set[ActionType]:
    return set(allowed_actions(POLICY, actor, findings))


def test_clean_case() -> None:
    assert _allowed(AGENT) == {PAY}
    assert _allowed(ANALYST) == {PAY, HOLD, QUERY}


def test_forensics_signal_alone_counts_as_clean() -> None:
    assert _allowed(AGENT, FindingState(F.FORENSICS_SIGNAL)) == {PAY}


@pytest.mark.parametrize("code", sorted(RESTRICTED_CODES))
def test_restricted_findings(code: FindingCode) -> None:
    assert _allowed(AGENT, FindingState(code)) == {HOLD, QUERY}
    assert _allowed(ANALYST, FindingState(code)) == {HOLD, QUERY, REJECT}


def test_inconclusive_check() -> None:
    assert _allowed(AGENT, FindingState(F.EXTRACTION_UNCERTAIN)) == {HOLD, QUERY}
    assert _allowed(ANALYST, FindingState(F.EXTRACTION_UNCERTAIN)) == {HOLD, QUERY, REJECT}


def test_other_exception() -> None:
    assert _allowed(AGENT, FindingState(F.PRICE_MISMATCH)) == {HOLD, QUERY, REJECT}
    assert _allowed(ANALYST, FindingState(F.PRICE_MISMATCH)) == {HOLD, QUERY, REJECT}


def test_all_resolved_allows_payment_only_for_the_analyst() -> None:
    resolved = (
        FindingState(F.PRICE_MISMATCH, resolved=True),
        FindingState(F.DUPLICATE_INVOICE, resolved=True),
    )
    assert _allowed(ANALYST, *resolved) == {PAY, HOLD, QUERY, REJECT}
    assert PAY not in _allowed(AGENT, *resolved)


def test_lists_intersect_over_unresolved_findings() -> None:
    mixed = (FindingState(F.PRICE_MISMATCH), FindingState(F.BANK_DETAILS_CHANGED))
    assert _allowed(AGENT, *mixed) == {HOLD, QUERY}
    assert _allowed(ANALYST, *mixed) == {HOLD, QUERY, REJECT}


def test_inconclusive_can_never_be_marked_resolved() -> None:
    fake = (
        FindingState(F.EXTRACTION_UNCERTAIN, resolved=True),
        FindingState(F.PRICE_MISMATCH, resolved=True),
    )
    assert PAY not in _allowed(ANALYST, *fake)


def test_payment_is_allowed_exactly_when_the_prd_says_so() -> None:
    """Exhaustive over every finding set of size ≤ 3 with every resolved flag."""
    codes = list(F)
    for size in range(4):
        for combo in itertools.combinations(codes, size):
            for flags in itertools.product([False, True], repeat=size):
                findings = [FindingState(c, r) for c, r in zip(combo, flags, strict=True)]
                relevant = [f for f in findings if f.code is not F.FORENSICS_SIGNAL]
                for actor in (AGENT, ANALYST):
                    result = allowed_actions(POLICY, actor, findings)
                    assert result, (findings, actor)  # never empty
                    if relevant:
                        assert HOLD in result
                    clean = not relevant
                    all_resolved = bool(relevant) and all(
                        f.resolved and f.code not in POLICY.resolutions.recheck_only
                        for f in relevant
                    )
                    expect_pay = clean or (actor is ANALYST and all_resolved)
                    assert (PAY in result) is expect_pay, (findings, actor)


# ---- resolution rules (04-prd.md §3) -----------------------------------------------------


def test_resolution_types_per_finding_match_the_prd() -> None:
    expected = {
        F.PRICE_MISMATCH: {ResolutionType.PRICE_AGREED, ResolutionType.CREDIT_ISSUED},
        F.QTY_OVER_RECEIVED: {ResolutionType.ADDITIONAL_RECEIPT, ResolutionType.CREDIT_ISSUED},
        F.ARITHMETIC_MISMATCH: {ResolutionType.CREDIT_ISSUED},
        F.TAX_MISCALCULATED: {ResolutionType.CREDIT_ISSUED},
        F.VENDOR_NOT_APPROVED: {ResolutionType.VENDOR_APPROVED},
        F.APPROVAL_LIMIT_EXCEEDED: {ResolutionType.PO_APPROVAL_OBTAINED},
        F.DUPLICATE_INVOICE: {ResolutionType.NOT_DUPLICATE},
        F.BANK_DETAILS_CHANGED: {ResolutionType.BANK_VERIFIED},
        F.MISSING_OR_UNKNOWN_PO: set(),
        F.EXTRACTION_UNCERTAIN: set(),
        F.FORENSICS_SIGNAL: set(),
    }
    actual = {code: {r.type for r in rules_for(POLICY, code)} for code in F}
    assert actual == expected


def test_recheck_only_findings() -> None:
    assert requires_recheck(POLICY, F.MISSING_OR_UNKNOWN_PO)
    assert requires_recheck(POLICY, F.EXTRACTION_UNCERTAIN)
    assert not requires_recheck(POLICY, F.PRICE_MISMATCH)


def test_restricted_findings_need_their_strong_evidence() -> None:
    dup = rule(POLICY, F.DUPLICATE_INVOICE, ResolutionType.NOT_DUPLICATE)
    bank = rule(POLICY, F.BANK_DETAILS_CHANGED, ResolutionType.BANK_VERIFIED)
    assert dup is not None and {"vendor_confirmation", "ledger_invoice_reference"} <= set(
        dup.evidence
    )
    assert bank is not None and {"bank_verification_record", "bank_verification"} <= set(
        bank.evidence
    )


def test_every_exception_has_a_way_to_clear() -> None:
    for code in EXCEPTION_CODES:
        assert rules_for(POLICY, code) or requires_recheck(POLICY, code), code


# ---- fail closed: unsafe configurations are refused --------------------------------------


def _mutate(path: list[str], value: Any) -> dict[str, Any]:
    raw = copy.deepcopy(_raw())
    node = raw
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return raw


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["allowlists", "agent", "restricted"], ["place_on_hold", "approve_for_payment"]),
        (["allowlists", "analyst", "inconclusive"], ["place_on_hold", "approve_for_payment"]),
        (["allowlists", "analyst", "other"], ["place_on_hold", "approve_for_payment"]),
        (["allowlists", "agent", "other"], ["draft_vendor_query"]),  # no hold
        (["allowlists", "agent", "clean"], []),
        (["tolerances", "tax_abs"], 0.01),  # float
        (["tolerances", "tax_abs"], "-0.01"),  # negative
        (["approval_levels", "level_2_max"], "5000.00"),  # below level 1
        (["currency"], "EUR"),
        (["bank_verification", "contact_source"], "invoice"),
        (["resolutions", "recheck_only"], ["MISSING_OR_UNKNOWN_PO"]),  # uncertainty resolvable
    ],
)
def test_unsafe_configuration_is_refused(path: list[str], value: Any) -> None:
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(_mutate(path, value))


def _with_rules(edit: Any) -> dict[str, Any]:
    raw = copy.deepcopy(_raw())
    raw["resolutions"]["rules"] = edit(raw["resolutions"]["rules"])
    return raw


@pytest.mark.parametrize(
    "edit",
    [
        # a restricted finding cleared by an ordinary resolution type
        lambda rules: [
            *rules,
            {
                "finding": "DUPLICATE_INVOICE",
                "type": "credit_issued",
                "evidence": ["credit_note"],
                "structured": [],
                "check": "x",
            },
        ],
        # a duplicate cleared without the matched ledger invoice
        lambda rules: [
            r if r["finding"] != "DUPLICATE_INVOICE" else {**r, "evidence": ["vendor_confirmation"]}
            for r in rules
        ],
        # a bank change cleared without a verification record
        lambda rules: [
            r
            if r["finding"] != "BANK_DETAILS_CHANGED"
            else {**r, "evidence": ["bank_verification"]}
            for r in rules
        ],
        # a recheck-only finding given a resolution rule
        lambda rules: [
            *rules,
            {
                "finding": "MISSING_OR_UNKNOWN_PO",
                "type": "vendor_approved",
                "evidence": ["vendor_approval"],
                "structured": [],
                "check": "x",
            },
        ],
        # an exception left with no way to clear
        lambda rules: [r for r in rules if r["finding"] != "TAX_MISCALCULATED"],
        # evidence that doesn't exist / a reason-only rule
        lambda rules: [*rules[:-1], {**rules[-1], "evidence": ["free_text_reason"]}],
        lambda rules: [*rules[:-1], {**rules[-1], "evidence": []}],
    ],
)
def test_unsafe_resolution_rules_are_refused(edit: Any) -> None:
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(_with_rules(edit))


def test_config_digest_is_stable_and_sensitive() -> None:
    assert load_policy().config_sha256 == load_policy().config_sha256
    changed = PolicyConfig.model_validate(_mutate(["tolerances", "tax_abs"], "0.02"))
    assert sha256_hex(changed.model_dump()) != load_policy().config_sha256
