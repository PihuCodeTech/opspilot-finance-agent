"""In-memory world produced by the generator, before rendering and loading.

Money and quantities are exact decimals. Bank account numbers here are
synthetic test fixtures (they are also printed on the generated PDFs); the
loader stores only their fingerprints in the database.
"""

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from opspilot.domain.enums import ActionType, FindingCode


@dataclass
class BankAccount:
    number: str
    valid_from: dt.date
    valid_to: dt.date | None = None


@dataclass
class Vendor:
    code: str
    name: str
    address: str
    tax_rate: Decimal
    approved: bool
    accounts: list[BankAccount]
    contact_phone: str

    @property
    def current_account(self) -> BankAccount:
        return next(a for a in self.accounts if a.valid_to is None)


@dataclass
class PoLine:
    line_no: int
    sku: str
    description: str
    quantity: Decimal
    unit_price: Decimal


@dataclass
class PurchaseOrder:
    number: str
    vendor_code: str
    approval_level: int
    controller_approved: bool
    issued_at: dt.date
    lines: list[PoLine]


@dataclass
class ReceiptLine:
    po_line_no: int
    quantity: Decimal


@dataclass
class Receipt:
    number: str
    po_number: str
    received_at: dt.date
    lines: list[ReceiptLine]


@dataclass
class InvoiceLine:
    description: str
    sku: str | None
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal
    po_line_no: int | None  # truth only: which PO line this bills (never printed)


@dataclass
class LedgerInvoice:
    """An invoice the ERP has already processed (duplicates, prior billing)."""

    number: str
    vendor_code: str
    po_number: str | None
    invoice_date: dt.date
    lines: list[InvoiceLine]
    subtotal: Decimal
    tax: Decimal
    total: Decimal


@dataclass
class IncomingInvoice:
    key: str  # stable item id within the dataset, e.g. "dev-0042"
    number: str
    vendor_code: str
    po_number: str | None
    invoice_date: dt.date
    received_at: dt.datetime
    lines: list[InvoiceLine]
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    bank_account: str
    layout: str
    notes: str = ""


@dataclass
class Labels:
    exceptions: list[dict[str, Any]]  # [{"type": FindingCode, "subject_keys": [...]}]
    acceptable_actions: list[ActionType]
    is_clean: bool
    is_adversarial: bool
    variant_tags: list[str]
    extraction_truth: dict[str, Any]

    @property
    def types(self) -> set[FindingCode]:
        return {FindingCode(e["type"]) for e in self.exceptions}


@dataclass
class Item:
    invoice: IncomingInvoice
    labels: Labels


@dataclass
class World:
    split: str
    seed: int
    generator_version: str
    vendors: list[Vendor] = field(default_factory=list)
    purchase_orders: list[PurchaseOrder] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    ledger: list[LedgerInvoice] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)

    def vendor(self, code: str) -> Vendor:
        return next(v for v in self.vendors if v.code == code)

    def po(self, number: str | None) -> PurchaseOrder | None:
        return next((p for p in self.purchase_orders if p.number == number), None)
