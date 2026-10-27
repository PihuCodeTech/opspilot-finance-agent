"""What TruthLens reads and returns. Plain, immutable data; no I/O.

`InvoiceFacts` is what was read from the document (with a confidence per
field). `ErpContext` is the system of record for that invoice: the vendor, the
PO, receipts and the frozen ledger. A check never reads anything else.
"""

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from opspilot.domain.enums import CheckCode, CheckStatus

FULL = Decimal(1)


@dataclass(frozen=True)
class InvoiceLineFacts:
    index: int  # 1-based position on the invoice: the subject "line:N"
    description: str
    sku: str | None
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class InvoiceFacts:
    number: str
    invoice_date: dt.date
    po_number: str | None
    lines: tuple[InvoiceLineFacts, ...]
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    bank_fp: str | None  # from the shared token; None when no account was printed
    # Field name -> confidence in [0, 1]. Names: invoice_number, invoice_date, po_number,
    # subtotal, tax, total, bank_account, vendor, lines[N].quantity|unit_price|line_total.
    confidence: dict[str, Decimal] = field(default_factory=dict)

    def confidence_of(self, name: str) -> Decimal:
        return self.confidence.get(name, Decimal(0))  # missing counts as unread (fail closed)


@dataclass(frozen=True)
class VendorRecord:
    id: uuid.UUID | None
    code: str
    approved: bool
    tax_rate: Decimal
    current_bank_fp: str | None


@dataclass(frozen=True)
class PoLineRecord:
    id: uuid.UUID | None
    line_no: int
    sku: str | None
    description: str
    quantity: Decimal
    unit_price: Decimal


@dataclass(frozen=True)
class PoRecord:
    id: uuid.UUID | None
    number: str
    vendor_code: str
    approval_level: int
    controller_approved: bool
    lines: tuple[PoLineRecord, ...]

    def line(self, line_no: int) -> PoLineRecord | None:
        return next((ln for ln in self.lines if ln.line_no == line_no), None)


@dataclass(frozen=True)
class LedgerLineRecord:
    sku: str | None
    description: str
    quantity: Decimal


@dataclass(frozen=True)
class LedgerInvoiceRecord:
    id: uuid.UUID | None
    number: str
    invoice_date: dt.date
    total: Decimal
    lines: tuple[LedgerLineRecord, ...]


@dataclass(frozen=True)
class ErpContext:
    vendor: VendorRecord
    # The PO the invoice cites, as found (None if absent/unknown); its vendor may differ.
    po: PoRecord | None
    received: dict[int, Decimal]  # PO line no -> quantity received
    billed_before: dict[int, Decimal]  # PO line no -> quantity on earlier ledger invoices
    vendor_ledger: tuple[LedgerInvoiceRecord, ...]  # the vendor's frozen ledger invoices
    line_mapping: dict[int, int | None]  # invoice line index -> PO line no (from matching)


@dataclass(frozen=True)
class CheckOutcome:
    check_code: CheckCode
    subject_key: str  # "header" or "line:N"
    status: CheckStatus
    compared: dict[str, Any]
    tolerance: dict[str, Any] | None
    evidence_refs: tuple[dict[str, Any], ...]
    inconclusive_reason: str | None = None
