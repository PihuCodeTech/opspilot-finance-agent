"""Recomputed totals and the approval amount (`04-prd.md` §8, C-10).

Routing to an approver must never rely on the stated total alone: it comes
from an untrusted document. The approval amount is the larger of the stated
total and the total recomputed from line quantities × unit prices plus tax.
"""

from decimal import Decimal

from opspilot.domain.money import round_money
from opspilot.truthlens.types import InvoiceFacts


def recomputed_total(facts: InvoiceFacts, tax_rate: Decimal) -> Decimal:
    subtotal = sum((round_money(ln.quantity * ln.unit_price) for ln in facts.lines), Decimal(0))
    return round_money(subtotal + round_money(subtotal * tax_rate))


def approval_amount(facts: InvoiceFacts, tax_rate: Decimal) -> Decimal:
    return max(facts.total, recomputed_total(facts, tax_rate))
