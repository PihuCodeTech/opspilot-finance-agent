"""The generator's reference evaluator: which exceptions does an invoice really have?

The generator only keeps a scenario when this evaluator finds exactly the
exceptions that were injected, so no clean invoice carries an accidental
exception and no label is wrong. It reads tolerances from the policy
configuration. TruthLens (P5) is a separate implementation tested against the
labels this produces; the test suite holds a third, independent oracle.
"""

from collections import Counter
from decimal import Decimal

from opspilot.domain.bank import normalise_account
from opspilot.domain.enums import FindingCode
from opspilot.domain.money import percent_change, round_money
from opspilot.evaluation.generator.model import IncomingInvoice, LedgerInvoice, World
from opspilot.policy.schema import PolicyConfig

F = FindingCode
Subjects = dict[FindingCode, list[str]]


def normalise_number(number: str) -> str:
    return "".join(ch for ch in number.lower() if ch.isalnum())


def line_set(lines: list) -> Counter[tuple[str | None, Decimal]]:  # type: ignore[type-arg]
    return Counter((line.sku or line.description, line.quantity) for line in lines)


def recomputed_total(invoice: IncomingInvoice, tax_rate: Decimal) -> Decimal:
    subtotal = sum(
        (round_money(line.quantity * line.unit_price) for line in invoice.lines), Decimal(0)
    )
    return round_money(subtotal + round_money(subtotal * tax_rate))


def approval_amount(invoice: IncomingInvoice, tax_rate: Decimal) -> Decimal:
    """The larger of the stated and recomputed totals (`04-prd.md` §8, C-10)."""
    return max(invoice.total, recomputed_total(invoice, tax_rate))


def _is_duplicate(invoice: IncomingInvoice, ledger: LedgerInvoice, policy: PolicyConfig) -> bool:
    if ledger.vendor_code != invoice.vendor_code:
        return False
    if normalise_number(ledger.number) == normalise_number(invoice.number):
        return True
    t = policy.tolerances
    if ledger.total == 0:
        return False
    within_total = abs(percent_change(invoice.total, ledger.total)) <= t.near_duplicate_total_pct
    within_days = (
        abs((invoice.invoice_date - ledger.invoice_date).days) <= t.near_duplicate_window_days
    )
    return within_total and within_days and line_set(invoice.lines) == line_set(ledger.lines)


def evaluate(world: World, invoice: IncomingInvoice, policy: PolicyConfig) -> Subjects:  # noqa: PLR0912
    t = policy.tolerances
    found: Subjects = {}

    def add(code: FindingCode, subject: str) -> None:
        found.setdefault(code, []).append(subject)

    vendor = world.vendor(invoice.vendor_code)
    if not vendor.approved:
        add(F.VENDOR_NOT_APPROVED, "header")
    if normalise_account(invoice.bank_account) != normalise_account(vendor.current_account.number):
        add(F.BANK_DETAILS_CHANGED, "header")

    for index, line in enumerate(invoice.lines, start=1):
        if (
            abs(round_money(line.quantity * line.unit_price) - line.line_total)
            > t.line_arithmetic_abs
        ):
            add(F.ARITHMETIC_MISMATCH, f"line:{index}")
    stated_lines = sum((line.line_total for line in invoice.lines), Decimal(0))
    if (
        abs(stated_lines - invoice.subtotal) > t.invoice_total_abs
        or abs(invoice.subtotal + invoice.tax - invoice.total) > t.invoice_total_abs
    ):
        add(F.ARITHMETIC_MISMATCH, "header")
    if abs(invoice.tax - round_money(invoice.subtotal * vendor.tax_rate)) > t.tax_abs:
        add(F.TAX_MISCALCULATED, "header")

    if any(_is_duplicate(invoice, ledger, policy) for ledger in world.ledger):
        add(F.DUPLICATE_INVOICE, "header")

    po = world.po(invoice.po_number)
    if po is None or po.vendor_code != invoice.vendor_code:
        add(F.MISSING_OR_UNKNOWN_PO, "header")
        return found  # PO-dependent checks have nothing to compare against

    po_lines = {line.line_no: line for line in po.lines}
    for index, line in enumerate(invoice.lines, start=1):
        po_line = po_lines.get(line.po_line_no) if line.po_line_no is not None else None
        if po_line is None:
            continue
        if percent_change(line.unit_price, po_line.unit_price) > t.unit_price_overcharge_pct:
            add(F.PRICE_MISMATCH, f"line:{index}")
        received = sum(
            (
                r.quantity
                for rec in world.receipts
                if rec.po_number == po.number
                for r in rec.lines
                if r.po_line_no == po_line.line_no
            ),
            Decimal(0),
        )
        billed_before = sum(
            (
                bl.quantity
                for led in world.ledger
                if led.po_number == po.number
                for bl in led.lines
                if bl.po_line_no == po_line.line_no
            ),
            Decimal(0),
        )
        if billed_before + line.quantity > received:
            add(F.QTY_OVER_RECEIVED, f"line:{index}")

    amount = approval_amount(invoice, vendor.tax_rate)
    levels = policy.approval_levels
    limit = {1: levels.level_1_max, 2: levels.level_2_max}.get(po.approval_level)
    over_level = limit is not None and amount > limit
    needs_controller = amount > levels.level_2_max and not po.controller_approved
    if over_level or needs_controller:
        add(F.APPROVAL_LIMIT_EXCEEDED, "header")
    return found
