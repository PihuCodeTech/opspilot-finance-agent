"""Boundary properties of the TruthLens checks (Hypothesis)."""

import datetime as dt
from dataclasses import replace
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from opspilot.domain.enums import CheckCode, CheckStatus, FindingCode
from opspilot.domain.money import round_money, round_unit_price
from opspilot.policy.loader import load_policy
from opspilot.truthlens.checks import (
    check_arithmetic,
    check_bank,
    check_duplicate,
    check_prices,
    check_quantities,
    check_tax,
    run_all,
)
from opspilot.truthlens.findings import derive_findings
from opspilot.truthlens.matching import match_lines
from opspilot.truthlens.types import (
    FULL,
    ErpContext,
    InvoiceFacts,
    InvoiceLineFacts,
    LedgerInvoiceRecord,
    LedgerLineRecord,
    PoLineRecord,
    PoRecord,
    VendorRecord,
)

POLICY = load_policy().config
S = CheckStatus
prices = st.decimals(min_value=Decimal("0.05"), max_value=Decimal("5000"), places=4)
quantities = st.integers(min_value=1, max_value=5000).map(Decimal)
rates = st.sampled_from(
    [Decimal("0"), Decimal("0.05"), Decimal("0.0725"), Decimal("0.0825"), Decimal("0.1")]
)


def invoice(qty: Decimal, price: Decimal, rate: Decimal, **changes: object) -> InvoiceFacts:
    line = InvoiceLineFacts(
        1, "Hex bolt M8 x 40mm, zinc plated", "HB-M8-40", qty, price, round_money(qty * price)
    )
    subtotal = line.line_total
    tax = round_money(subtotal * rate)
    names = [
        "invoice_number",
        "invoice_date",
        "po_number",
        "subtotal",
        "tax",
        "total",
        "bank_account",
        "vendor",
        "lines[1].quantity",
        "lines[1].unit_price",
        "lines[1].line_total",
    ]
    facts = InvoiceFacts(
        "INV-1",
        dt.date(2026, 9, 1),
        "PO-1",
        (line,),
        subtotal,
        tax,
        subtotal + tax,
        "aaaaaaaaaaaa",
        dict.fromkeys(names, FULL),
    )
    return replace(facts, **changes)  # type: ignore[arg-type]


def context(
    po_price: Decimal, received: Decimal, rate: Decimal = Decimal(0), **changes: object
) -> ErpContext:
    po = PoRecord(
        None,
        "PO-1",
        "V-1",
        3,
        True,
        (PoLineRecord(None, 1, "HB-M8-40", "Hex bolt M8 x 40mm, zinc plated", received, po_price),),
    )
    ctx = ErpContext(
        VendorRecord(None, "V-1", True, rate, "aaaaaaaaaaaa"), po, {1: received}, {}, (), {1: 1}
    )
    return replace(ctx, **changes)  # type: ignore[arg-type]


@given(prices)
def test_price_exactly_at_tolerance_passes_and_just_above_fails(po_price: Decimal) -> None:
    at = round_unit_price(po_price * Decimal("1.02"))
    status_at = check_prices(
        invoice(Decimal(1), at, Decimal(0)), context(po_price, Decimal(1)), POLICY
    )[0]
    variance = Decimal(status_at.compared["variance_pct"])
    assert (status_at.status is S.PASS) == (variance <= Decimal("2.00"))
    above = round_unit_price(po_price * Decimal("1.0215"))
    status_above = check_prices(
        invoice(Decimal(1), above, Decimal(0)), context(po_price, Decimal(1)), POLICY
    )[0]
    assert status_above.status is S.FAIL


@given(prices, quantities)
def test_undercharges_never_fail(po_price: Decimal, qty: Decimal) -> None:
    lower = round_unit_price(po_price * Decimal("0.5"))
    outcome = check_prices(invoice(qty, lower, Decimal(0)), context(po_price, qty), POLICY)[0]
    assert outcome.status is S.PASS
    assert outcome.compared["undercharge_info"] is True


@given(
    quantities,
    st.integers(min_value=0, max_value=50).map(Decimal),
    st.integers(min_value=0, max_value=3),
)
def test_cumulative_quantity(received: Decimal, before: Decimal, extra: int) -> None:
    billed_now = max(Decimal(1), received - before) + extra
    ctx = context(Decimal(1), received, billed_before={1: before})
    outcome = check_quantities(invoice(billed_now, Decimal(1), Decimal(0)), ctx, POLICY)[0]
    assert (outcome.status is S.FAIL) == (before + billed_now > received)


@given(quantities, prices, st.integers(min_value=-3, max_value=3))
def test_line_arithmetic_boundary(qty: Decimal, price: Decimal, cents: int) -> None:
    facts = invoice(qty, price, Decimal(0))
    line = replace(facts.lines[0], line_total=facts.lines[0].line_total + Decimal(cents) / 100)
    facts = replace(facts, lines=(line,), subtotal=line.line_total, total=line.line_total)
    outcome = next(
        o for o in check_arithmetic(facts, context(price, qty), POLICY) if o.subject_key == "line:1"
    )
    assert (outcome.status is S.FAIL) == (abs(cents) > 1)


@given(quantities, prices, rates, st.integers(min_value=-4, max_value=4))
def test_tax_boundary(qty: Decimal, price: Decimal, rate: Decimal, cents: int) -> None:
    facts = invoice(qty, price, rate)
    facts = replace(
        facts, tax=facts.tax + Decimal(cents) / 100, total=facts.total + Decimal(cents) / 100
    )
    outcome = check_tax(facts, context(price, qty, rate), POLICY)
    assert (outcome.status is S.FAIL) == (abs(cents) > 1)


@given(st.integers(min_value=0, max_value=60), st.integers(min_value=-120, max_value=120))
def test_near_duplicate_window_and_total(days: int, basis_points: int) -> None:
    facts = invoice(Decimal(10), Decimal(100), Decimal(0))
    ledger_total = round_money(facts.total * (1 + Decimal(basis_points) / 10000))
    ledger = LedgerInvoiceRecord(
        None,
        "OTHER-9",
        facts.invoice_date - dt.timedelta(days=days),
        ledger_total,
        (LedgerLineRecord("HB-M8-40", "x", Decimal(10)),),
    )
    outcome = check_duplicate(
        facts, context(Decimal(100), Decimal(10), vendor_ledger=(ledger,)), POLICY
    )
    pct = abs((facts.total - ledger_total) * 100 / ledger_total)
    assert (outcome.status is S.FAIL) == (
        days <= 30 and pct.quantize(Decimal("0.01")) <= Decimal("0.50")
    )


def test_exact_duplicate_ignores_formatting() -> None:
    facts = invoice(Decimal(1), Decimal(1), Decimal(0), number="INV-2026-00418")
    ledger = LedgerInvoiceRecord(None, "inv 2026/00418", dt.date(2025, 1, 1), Decimal(999), ())
    outcome = check_duplicate(
        facts, context(Decimal(1), Decimal(1), vendor_ledger=(ledger,)), POLICY
    )
    assert outcome.status is S.FAIL and outcome.compared["matches"][0]["basis"] == "exact_number"


@given(st.decimals(min_value=Decimal(0), max_value=Decimal("0.8999"), places=4))
def test_low_confidence_is_inconclusive_never_pass(conf: Decimal) -> None:
    facts = invoice(Decimal(5), Decimal(10), Decimal(0))
    facts = replace(
        facts, confidence={**facts.confidence, "lines[1].unit_price": conf, "tax": conf}
    )
    outcomes = run_all(facts, context(Decimal(10), Decimal(5)), POLICY)
    affected = [o for o in outcomes if o.check_code in {CheckCode.PRICE, CheckCode.TAX}]
    assert affected and all(o.status is S.INCONCLUSIVE and o.inconclusive_reason for o in affected)
    assert FindingCode.EXTRACTION_UNCERTAIN in {d.code for d in derive_findings(outcomes)}


def test_missing_confidence_counts_as_unread() -> None:
    facts = replace(invoice(Decimal(5), Decimal(10), Decimal(0)), confidence={})
    assert all(
        o.status is S.INCONCLUSIVE for o in run_all(facts, context(Decimal(10), Decimal(5)), POLICY)
    )


def test_missing_bank_account_is_inconclusive() -> None:
    outcome = check_bank(
        invoice(Decimal(1), Decimal(1), Decimal(0), bank_fp=None),
        context(Decimal(1), Decimal(1)),
        POLICY,
    )
    assert outcome.status is S.INCONCLUSIVE


def test_matching_by_sku_description_and_similarity() -> None:
    po_lines = (
        PoLineRecord(
            None, 1, "HB-M8-40", "Hex bolt M8 x 40mm, zinc plated", Decimal(1), Decimal(1)
        ),
        PoLineRecord(None, 2, None, "Nitrile gloves, large, box of 100", Decimal(1), Decimal(1)),
        PoLineRecord(None, 3, None, "Cat6 cable, 305m reel, blue", Decimal(1), Decimal(1)),
    )
    lines = (
        InvoiceLineFacts(
            1, "Bolt, hex, M8 x 40, ZP", "hb-m8-40", Decimal(1), Decimal(1), Decimal(1)
        ),
        InvoiceLineFacts(
            2, "nitrile gloves large box of 100", None, Decimal(1), Decimal(1), Decimal(1)
        ),
        InvoiceLineFacts(
            3, "Cat6 cable 305m reel (blue)", None, Decimal(1), Decimal(1), Decimal(1)
        ),
        InvoiceLineFacts(4, "Something else entirely", None, Decimal(1), Decimal(1), Decimal(1)),
    )
    result = {m.invoice_line: (m.po_line_no, m.method.value) for m in match_lines(lines, po_lines)}
    assert result == {
        1: (1, "sku"),
        2: (2, "description"),
        3: (3, "description"),
        4: (None, "unmatched"),
    }


def test_every_check_is_pure() -> None:
    facts, ctx = invoice(Decimal(3), Decimal(7), Decimal("0.05")), context(Decimal(7), Decimal(3))
    assert run_all(facts, ctx, POLICY) == run_all(facts, ctx, POLICY)
