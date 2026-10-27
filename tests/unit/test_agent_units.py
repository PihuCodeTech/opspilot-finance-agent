"""Grounding of extracted values and validation of explanations (no database, no model)."""

import uuid
from decimal import Decimal

from opspilot.agent.deps import PassageRef
from opspilot.agent.explain import (
    CheckView,
    ExplainOutput,
    FindingNote,
    FindingView,
    default_action,
    prompt_safe,
    template,
    validate,
)
from opspilot.domain.enums import ActionType, FindingCode
from opspilot.extraction.bank_tokens import contains_account_like_digits
from opspilot.extraction.mapping import ExtractedLine, InvoiceExtraction, facts_from, ground

TEXT = """## STATEMENT OF CHARGES
## Peck, Watkins and Quinn
Reference A-856
Dated June 11, 2026
Purchase order PO-26-010013
| HVAC maintenance visit | SV-HVAC-V | 1,185.00 | 1 | 1,185.00 |
Subtotal $1,185.00  Tax (7.25%) $85.91  Total due $1,270.91
Account for payment: BANK_ACCT[fp:d22d884d4d11]"""
TOKENS = [{"token": "BANK_ACCT[fp:d22d884d4d11]", "bank_fp": "d22d884d4d11", "last4": "8275"}]


def extraction(**changes: object) -> InvoiceExtraction:
    base: dict[str, object] = {
        "vendor_name": "Peck, Watkins and Quinn",
        "invoice_number": "A-856",
        "invoice_date": "2026-06-11",
        "po_number": "PO-26-010013",
        "lines": [
            ExtractedLine(
                description="HVAC maintenance visit",
                sku="SV-HVAC-V",
                quantity="1",
                unit_price="1185.00",
                line_total="1,185.00",
            )
        ],
        "subtotal": "$1,185.00",
        "tax": "85.91",
        "total": "1270.91",
        "bank_account": "BANK_ACCT[fp:d22d884d4d11]",
    }
    return InvoiceExtraction.model_validate(base | changes)


def test_values_printed_on_the_document_are_fully_confident() -> None:
    g = ground(extraction(), TEXT, TOKENS)
    assert all(v == 1 for v in g.confidence.values()), g.confidence
    assert (g.bank_fp, g.bank_last4) == ("d22d884d4d11", "8275")
    assert g.data["subtotal"] == "1185.00" and g.data["lines"][0]["unit_price"] == "1185.00"


def test_an_invented_value_is_not_trusted() -> None:
    g = ground(extraction(total="1200.00", invoice_date="2026-06-12"), TEXT, TOKENS)
    assert g.confidence["total"] == Decimal("0.5")
    assert g.confidence["invoice_date"] == Decimal("0.5")
    facts = facts_from(g.data, {k: str(v) for k, v in g.confidence.items()}, g.bank_fp, True)
    assert facts.confidence_of("total") < Decimal("0.90")  # below the policy floor


def test_a_bank_token_must_be_the_documents_own() -> None:
    g = ground(extraction(bank_account="BANK_ACCT[fp:000000000000]"), TEXT, TOKENS)
    assert g.bank_fp is None and g.confidence["bank_account"] == Decimal("0.5")
    only = ground(extraction(bank_account=None), TEXT, TOKENS)  # the only account on it
    assert only.bank_fp == "d22d884d4d11"


def test_no_po_is_trusted_only_if_the_document_mentions_none() -> None:
    assert ground(extraction(po_number=None), TEXT, TOKENS).confidence["po_number"] < 1
    no_po = TEXT.replace("Purchase order PO-26-010013\n", "")
    assert ground(extraction(po_number=None), no_po, TOKENS).confidence["po_number"] == 1


def test_unparseable_and_missing_values_have_zero_confidence() -> None:
    g = ground(extraction(tax="eighty", invoice_date="11 June"), TEXT, TOKENS)
    assert g.confidence["tax"] == 0 and g.confidence["invoice_date"] == 0


# ---------------------------------------------------------------- explanations

CHECK = CheckView(
    "price@line:1",
    "fail",
    {"invoice_unit_price": "10.50", "po_unit_price": "10.00", "variance_pct": "5.00"},
    {"max_overcharge_pct": "2.00"},
    "line:1",
)
FINDING = FindingView(
    "PRICE_MISMATCH@line:1", FindingCode.PRICE_MISMATCH, "line:1", ("price@line:1",)
)
PASSAGE = PassageRef(uuid.uuid4(), "3.1", "Unit price", "may exceed the PO price by at most 2.00%.")
ALLOWED = frozenset(
    {ActionType.PLACE_ON_HOLD, ActionType.DRAFT_VENDOR_QUERY, ActionType.REJECT_INVOICE}
)


def output(explanation: str, action: str = "place_on_hold", **note: object) -> ExplainOutput:
    fields: dict[str, object] = {
        "finding": FINDING.key,
        "explanation": explanation,
        "check_results": ["price@line:1"],
        "policy_sections": ["3.1"],
    }
    return ExplainOutput(
        findings=[FindingNote.model_validate(fields | note)], proposed_action=action, summary="s"
    )


def run(out: ExplainOutput) -> list[str]:
    return validate(out, [FINDING], {CHECK.key: CHECK}, {"3.1": PASSAGE}, ALLOWED)


def test_a_grounded_explanation_passes() -> None:
    assert (
        run(
            output(
                "Line 1 is billed at 10.50 against 10.00 on the PO, a 5.00% variance "
                "above the 2.00% tolerance in section 3.1."
            )
        )
        == []
    )


def test_numbers_must_come_from_the_cited_evidence() -> None:
    errors = run(output("The vendor overcharged by 12.75 in total."))
    assert any("12.75" in e for e in errors)


def test_citations_must_exist_and_action_must_be_allowed() -> None:
    errors = run(
        output(
            "Price above the PO.",
            action="approve_for_payment",
            check_results=["price@line:9"],
            policy_sections=["9.9"],
        )
    )
    assert any("unknown check results" in e for e in errors)
    assert any("unknown policy sections" in e for e in errors)
    assert any("cite at least one check result" in e for e in errors)
    assert any("proposed_action" in e for e in errors)


def test_every_finding_explained_exactly_once() -> None:
    empty = ExplainOutput(findings=[], proposed_action="place_on_hold", summary="")
    assert any("exactly once" in e for e in run(empty))


def test_template_uses_only_compared_values_and_an_allowed_action() -> None:
    out = template([FINDING], {CHECK.key: CHECK}, {FindingCode.PRICE_MISMATCH: ["3.1"]}, ALLOWED)
    assert run(out) == []
    assert out.proposed_action == "place_on_hold" == default_action(ALLOWED)


def test_ids_never_reach_a_prompt() -> None:
    compared = {
        "matches": [
            {
                "ledger_invoice": "INV-17",
                "ledger_invoice_id": "12345678-9012-4abc-8def-123456789012",
                "same_lines": False,
            }
        ],
        "invoice_number": "INV-42",
    }
    safe = prompt_safe(compared)
    assert safe == {
        "matches": [{"ledger_invoice": "INV-17", "same_lines": False}],
        "invoice_number": "INV-42",
    }
    assert contains_account_like_digits(str(compared)) and not contains_account_like_digits(
        str(safe)
    )
