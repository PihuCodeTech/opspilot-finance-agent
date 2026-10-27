"""`resolve_records`: the system of record for one invoice (`06-architecture.md` §3.1).

Everything is scoped to the invoice's dataset and read from the frozen ERP
tables: vendor master, the PO the invoice cites, receipts, and the ledger of
invoices already processed. Other incoming invoices are never read, so a
result can't depend on processing order.
"""

import re
import uuid
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import Connection, func, select

from opspilot.db.models import (
    LedgerInvoice,
    LedgerInvoiceLine,
    PoLine,
    PurchaseOrder,
    Receipt,
    ReceiptLine,
    Vendor,
    VendorBankAccount,
)
from opspilot.truthlens.types import (
    ErpContext,
    LedgerInvoiceRecord,
    LedgerLineRecord,
    PoLineRecord,
    PoRecord,
    VendorRecord,
)

_NAME = re.compile(r"[^a-z0-9]+")


class VendorNotIdentifiedError(LookupError):
    """The vendor name read from the invoice matches no vendor in the master."""


def normalise_name(name: str) -> str:
    return _NAME.sub(" ", name.lower()).strip()


def find_vendor(conn: Connection, dataset_id: uuid.UUID, name: str | None) -> VendorRecord:
    if not name:
        raise VendorNotIdentifiedError("no vendor name was read")
    wanted = normalise_name(name)
    rows = conn.execute(
        select(
            Vendor.id, Vendor.vendor_code, Vendor.name, Vendor.is_approved, Vendor.tax_rate
        ).where(Vendor.dataset_id == dataset_id)
    ).all()
    hits = [r for r in rows if normalise_name(r.name) == wanted]
    if len(hits) != 1:
        raise VendorNotIdentifiedError(f"{len(hits)} vendors match the name read")
    vendor = hits[0]
    bank_fp = conn.execute(
        select(VendorBankAccount.bank_fp).where(
            VendorBankAccount.vendor_id == vendor.id, VendorBankAccount.valid_to.is_(None)
        )
    ).scalar_one_or_none()
    return VendorRecord(vendor.id, vendor.vendor_code, vendor.is_approved, vendor.tax_rate, bank_fp)


def _po(conn: Connection, dataset_id: uuid.UUID, number: str | None) -> PoRecord | None:
    if not number:
        return None
    po = conn.execute(
        select(PurchaseOrder, Vendor.vendor_code)
        .join(Vendor, Vendor.id == PurchaseOrder.vendor_id)
        .where(
            PurchaseOrder.dataset_id == dataset_id,
            func.upper(PurchaseOrder.po_number) == number.strip().upper(),
        )
    ).first()
    if po is None:
        return None
    order = po
    lines = conn.execute(
        select(PoLine).where(PoLine.po_id == order.id).order_by(PoLine.line_no)
    ).all()
    return PoRecord(
        order.id,
        order.po_number,
        po.vendor_code,
        order.approval_level,
        order.controller_approved,
        tuple(
            PoLineRecord(ln.id, ln.line_no, ln.sku, ln.description, ln.quantity, ln.unit_price)
            for ln in lines
        ),
    )


def _quantities_by_line(
    conn: Connection, po: PoRecord
) -> tuple[dict[int, Decimal], dict[int, Decimal]]:
    line_no = {ln.id: ln.line_no for ln in po.lines}
    received: dict[int, Decimal] = defaultdict(Decimal)
    for po_line_id, quantity in conn.execute(
        select(ReceiptLine.po_line_id, ReceiptLine.quantity_received)
        .join(Receipt, Receipt.id == ReceiptLine.receipt_id)
        .where(Receipt.po_id == po.id)
    ):
        received[line_no[po_line_id]] += quantity
    billed: dict[int, Decimal] = defaultdict(Decimal)
    for billed_line_id, quantity in conn.execute(
        select(LedgerInvoiceLine.po_line_id, LedgerInvoiceLine.quantity).where(
            LedgerInvoiceLine.po_line_id.in_(list(line_no))
        )
    ):
        if billed_line_id is not None:
            billed[line_no[billed_line_id]] += quantity
    return dict(received), dict(billed)


def _ledger(
    conn: Connection, dataset_id: uuid.UUID, vendor_id: uuid.UUID | None
) -> tuple[LedgerInvoiceRecord, ...]:
    invoices = conn.execute(
        select(LedgerInvoice)
        .where(LedgerInvoice.dataset_id == dataset_id, LedgerInvoice.vendor_id == vendor_id)
        .order_by(LedgerInvoice.invoice_date, LedgerInvoice.invoice_number)
    ).all()
    lines: dict[uuid.UUID, list[LedgerLineRecord]] = defaultdict(list)
    for row in conn.execute(
        select(LedgerInvoiceLine).where(
            LedgerInvoiceLine.ledger_invoice_id.in_([i.id for i in invoices])
        )
    ).all():
        lines[row.ledger_invoice_id].append(
            LedgerLineRecord(row.sku, row.description, row.quantity)
        )
    return tuple(
        LedgerInvoiceRecord(i.id, i.invoice_number, i.invoice_date, i.total, tuple(lines[i.id]))
        for i in invoices
    )


def resolve_records(
    conn: Connection, dataset_id: uuid.UUID, vendor: VendorRecord, po_number: str | None
) -> ErpContext:
    po = _po(conn, dataset_id, po_number)
    received, billed = _quantities_by_line(conn, po) if po else ({}, {})
    return ErpContext(
        vendor=vendor,
        po=po,
        received=received,
        billed_before=billed,
        vendor_ledger=_ledger(conn, dataset_id, vendor.id),
        line_mapping={},
    )
