"""Render generated invoices to PDF (Jinja2 → HTML → WeasyPrint).

Fonts are the vendored Geist files, so PDFs come out the same on any machine
with the same WeasyPrint version. WeasyPrint is imported lazily: it needs the
Pango system library, which the Docker image and CI provide.
"""

import datetime as dt
import hashlib
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from opspilot.domain.money import round_money
from opspilot.evaluation.generator.layouts import (
    LAYOUTS,
    format_amount,
    format_date,
    format_quantity,
)
from opspilot.evaluation.generator.model import Item, World

_TEMPLATES = Path(__file__).with_name("templates")
FONTS_DIR = Path(__file__).resolve().parents[2] / "web" / "static" / "fonts" / "geist"
BUYER = "Northgate Components Ltd, 14 Foundry Lane, Riverton"
# A fixed date keeps PDF metadata, and so file hashes, reproducible.
_PDF_CREATED = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=select_autoescape(["html", "j2"]),
    keep_trailing_newline=True,
)


def invoice_context(item: Item, world: World) -> dict[str, Any]:
    invoice = item.invoice
    vendor = world.vendor(invoice.vendor_code)
    layout = LAYOUTS[invoice.layout]
    lines = [
        {
            "description": line.description,
            "sku": line.sku or "",
            "quantity": format_quantity(line.quantity),
            "unit_price": format_amount(
                line.unit_price,
                layout,
                decimals=2 if line.unit_price == round_money(line.unit_price) else 4,
            ),
            "line_total": format_amount(line.line_total, layout),
        }
        for line in invoice.lines
    ]
    rate = vendor.tax_rate * 100
    return {
        "layout": layout,
        "number": invoice.number,
        "date": format_date(invoice.invoice_date, layout),
        "po_number": invoice.po_number,
        "vendor_name": vendor.name,
        "vendor_address": vendor.address,
        "buyer": BUYER,
        "lines": lines,
        "subtotal": format_amount(invoice.subtotal, layout),
        "tax": format_amount(invoice.tax, layout),
        "tax_rate": f"{rate.normalize():f}%",
        "total": format_amount(invoice.total, layout),
        "bank_account": invoice.bank_account,
        "notes": invoice.notes,
    }


def invoice_html(item: Item, world: World) -> str:
    return _env.get_template("invoice.html.j2").render(**invoice_context(item, world))


def render_pdf(html: str) -> bytes:
    # Needs Pango; imported only when rendering. WeasyPrint ships no type information.
    from weasyprint import HTML  # type: ignore[import-untyped]  # noqa: PLC0415

    document = HTML(string=html, base_url=str(FONTS_DIR)).render()
    document.metadata.created = _PDF_CREATED.isoformat()
    document.metadata.modified = _PDF_CREATED.isoformat()
    pdf = document.write_pdf(pdf_identifier=b"opspilot-synthetic")
    if pdf is None:
        raise RuntimeError("WeasyPrint produced no PDF")
    return bytes(pdf)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
