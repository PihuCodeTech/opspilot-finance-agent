"""Build TruthLens inputs from a generated world's ground truth (no extraction, no LLM)."""

from decimal import Decimal

from opspilot.domain.bank import fingerprint
from opspilot.evaluation.generator.model import IncomingInvoice, World
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

KEY = "truthlens-test-key-00000000000000000000000"


def facts_from_truth(invoice: IncomingInvoice) -> InvoiceFacts:
    lines = tuple(
        InvoiceLineFacts(i, ln.description, ln.sku, ln.quantity, ln.unit_price, ln.line_total)
        for i, ln in enumerate(invoice.lines, start=1)
    )
    names = [
        "invoice_number",
        "invoice_date",
        "po_number",
        "subtotal",
        "tax",
        "total",
        "bank_account",
        "vendor",
    ]
    names += [
        f"lines[{ln.index}].{f}" for ln in lines for f in ("quantity", "unit_price", "line_total")
    ]
    return InvoiceFacts(
        number=invoice.number,
        invoice_date=invoice.invoice_date,
        po_number=invoice.po_number,
        lines=lines,
        subtotal=invoice.subtotal,
        tax=invoice.tax,
        total=invoice.total,
        bank_fp=fingerprint(invoice.bank_account, KEY).bank_fp,
        confidence=dict.fromkeys(names, FULL),
    )


def context_from_truth(world: World, invoice: IncomingInvoice) -> ErpContext:
    vendor = world.vendor(invoice.vendor_code)
    po = world.po(invoice.po_number)
    po_record = None
    received: dict[int, Decimal] = {}
    billed: dict[int, Decimal] = {}
    if po is not None:
        po_record = PoRecord(
            None,
            po.number,
            po.vendor_code,
            po.approval_level,
            po.controller_approved,
            tuple(
                PoLineRecord(None, pl.line_no, pl.sku, pl.description, pl.quantity, pl.unit_price)
                for pl in po.lines
            ),
        )
        for receipt in world.receipts:
            if receipt.po_number == po.number:
                for rl in receipt.lines:
                    received[rl.po_line_no] = received.get(rl.po_line_no, Decimal(0)) + rl.quantity
        for ledger in world.ledger:
            if ledger.po_number == po.number:
                for bl in ledger.lines:
                    if bl.po_line_no is not None:
                        billed[bl.po_line_no] = billed.get(bl.po_line_no, Decimal(0)) + bl.quantity
    ledger_records = tuple(
        LedgerInvoiceRecord(
            None,
            led.number,
            led.invoice_date,
            led.total,
            tuple(LedgerLineRecord(ln.sku, ln.description, ln.quantity) for ln in led.lines),
        )
        for led in world.ledger
        if led.vendor_code == vendor.code
    )
    return ErpContext(
        vendor=VendorRecord(
            None,
            vendor.code,
            vendor.approved,
            vendor.tax_rate,
            fingerprint(vendor.current_account.number, KEY).bank_fp,
        ),
        po=po_record,
        received=received,
        billed_before=billed,
        vendor_ledger=ledger_records,
        line_mapping={i: ln.po_line_no for i, ln in enumerate(invoice.lines, start=1)},
    )
