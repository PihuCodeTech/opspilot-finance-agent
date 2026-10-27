"""TruthLens on ground truth: it must reproduce every label exactly.

Inputs are the generator's true field values (no extraction, no LLM), so any
miss or false alarm here is a bug in the checks themselves. Runs on the dev,
adversarial and demo sets; the final set stays reserved for the evaluation.
"""

from collections import Counter
from functools import cache

import pytest
from tests.truthlens_support import context_from_truth, facts_from_truth

from opspilot.domain.enums import CheckStatus, FindingCode
from opspilot.evaluation.generator.build import DEFAULT_SPECS, generate
from opspilot.evaluation.generator.model import World
from opspilot.policy.loader import load_policy
from opspilot.truthlens.checks import run_all
from opspilot.truthlens.findings import derive_findings

POLICY = load_policy().config
SPLITS = ("dev", "adversarial", "demo")


@cache
def world(split: str) -> World:
    return generate(DEFAULT_SPECS[split], POLICY)


@pytest.mark.parametrize("split", SPLITS)
def test_findings_equal_labels_on_every_invoice(split: str) -> None:
    w = world(split)
    mismatches = []
    for item in w.items:
        outcomes = run_all(
            facts_from_truth(item.invoice), context_from_truth(w, item.invoice), POLICY
        )
        found = {(d.code.value, d.subject_key) for d in derive_findings(outcomes)}
        expected = {(e["type"], key) for e in item.labels.exceptions for key in e["subject_keys"]}
        if found != expected:
            mismatches.append((item.invoice.key, sorted(expected), sorted(found)))
    assert mismatches == []


def test_recall_and_false_alarms_on_dev() -> None:
    w = world("dev")
    per_type: Counter[str] = Counter()
    clean_flagged = 0
    for item in w.items:
        codes = {
            d.code
            for d in derive_findings(
                run_all(facts_from_truth(item.invoice), context_from_truth(w, item.invoice), POLICY)
            )
        }
        if item.labels.is_clean:
            clean_flagged += bool(codes)
        for code in item.labels.types & codes:
            per_type[code.value] += 1
    expected = Counter(code.value for item in w.items for code in item.labels.types)
    assert per_type == expected  # recall 1.00 for every type
    assert clean_flagged == 0  # no false alarms


def test_no_check_is_inconclusive_with_full_confidence() -> None:
    w = world("dev")
    for item in w.items:
        outcomes = run_all(
            facts_from_truth(item.invoice), context_from_truth(w, item.invoice), POLICY
        )
        assert all(o.status is not CheckStatus.INCONCLUSIVE for o in outcomes)


def test_every_finding_cites_a_failed_check_with_evidence() -> None:
    w = world("dev")
    for item in w.items:
        outcomes = run_all(
            facts_from_truth(item.invoice), context_from_truth(w, item.invoice), POLICY
        )
        for draft in derive_findings(outcomes):
            assert draft.outcome_indexes
            for index in draft.outcome_indexes:
                assert outcomes[index].status is CheckStatus.FAIL
                assert outcomes[index].evidence_refs
                assert outcomes[index].compared


def test_missing_po_suppresses_po_dependent_checks() -> None:
    w = world("dev")
    item = next(i for i in w.items if FindingCode.MISSING_OR_UNKNOWN_PO in i.labels.types)
    outcomes = run_all(facts_from_truth(item.invoice), context_from_truth(w, item.invoice), POLICY)
    codes = {o.check_code.value for o in outcomes}
    assert not codes & {"price", "quantity", "approval_level"}


def test_deterministic_matching_is_never_wrong_on_dev() -> None:
    """Coverage can be partial (the rest goes to the LLM step), but a match must be right."""
    from opspilot.truthlens.matching import match_lines  # noqa: PLC0415

    w = world("dev")
    matched = wrong = total = 0
    for item in w.items:
        ctx = context_from_truth(w, item.invoice)
        if ctx.po is None or item.invoice.po_number is None:
            continue
        facts = facts_from_truth(item.invoice)
        for m in match_lines(facts.lines, ctx.po.lines):
            truth = ctx.line_mapping[m.invoice_line]
            if truth is None:
                continue
            total += 1
            if m.po_line_no is not None:
                matched += 1
                wrong += m.po_line_no != truth
    assert wrong == 0
    assert matched / total >= 0.6  # SKU printed on ~60% of lines, plus descriptions
