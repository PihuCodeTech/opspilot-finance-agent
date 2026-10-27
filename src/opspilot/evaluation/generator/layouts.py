"""Invoice layouts: the same facts printed in deliberately different ways.

Field labels, date and number formats, column order and arrangement all vary,
so extraction has to understand invoices rather than memorise one template.
`sidebar` appears only in the final evaluation set (held-out layout).
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Layout:
    name: str
    title: str
    number_label: str
    date_label: str
    po_label: str
    bank_label: str
    date_format: str  # strftime pattern
    thousands: str  # "," or ""
    currency_prefix: str
    columns: tuple[str, ...]  # from: description, sku, quantity, unit_price, line_total
    column_labels: dict[str, str]
    arrangement: str  # CSS class controlling the page grid


_LABELS = {
    "description": "Description",
    "sku": "Item",
    "quantity": "Qty",
    "unit_price": "Unit price",
    "line_total": "Amount",
}

LAYOUTS: dict[str, Layout] = {
    "classic": Layout(
        "classic",
        "INVOICE",
        "Invoice No.",
        "Invoice date",
        "PO number",
        "Bank account",
        "%Y-%m-%d",
        ",",
        "$",
        ("sku", "description", "quantity", "unit_price", "line_total"),
        _LABELS,
        "classic",
    ),
    "modern": Layout(
        "modern",
        "Invoice",
        "Invoice #",
        "Date",
        "Your order",
        "Pay to account",
        "%d %b %Y",
        ",",
        "USD ",
        ("description", "quantity", "unit_price", "line_total"),
        {**_LABELS, "unit_price": "Rate", "line_total": "Total"},
        "modern",
    ),
    "compact": Layout(
        "compact",
        "TAX INVOICE",
        "No.",
        "Issued",
        "P.O.",
        "Remit to",
        "%m/%d/%Y",
        "",
        "$",
        ("quantity", "description", "unit_price", "line_total"),
        {**_LABELS, "quantity": "Units"},
        "compact",
    ),
    "ledger": Layout(
        "ledger",
        "Statement of charges",
        "Reference",
        "Dated",
        "Purchase order",
        "Account for payment",
        "%B %d, %Y",
        ",",
        "$",
        ("description", "sku", "unit_price", "quantity", "line_total"),
        {**_LABELS, "sku": "Code", "line_total": "Extended"},
        "ledger",
    ),
    "bold": Layout(
        "bold",
        "BILL",
        "Bill number",
        "Bill date",
        "Order ref",
        "Bank details",
        "%d.%m.%Y",
        ",",
        "$",
        ("description", "quantity", "unit_price", "line_total"),
        {**_LABELS, "quantity": "Quantity", "unit_price": "Price each"},
        "bold",
    ),
    "sidebar": Layout(
        "sidebar",
        "Invoice",
        "Document number",
        "Document date",
        "Customer PO",
        "Settlement account",
        "%Y/%m/%d",
        ",",
        "US$",
        ("line_total", "description", "quantity", "unit_price"),
        {**_LABELS, "line_total": "Line value", "unit_price": "Each"},
        "sidebar",
    ),
}


def format_amount(value: Decimal, layout: Layout, *, decimals: int = 2) -> str:
    text = f"{value:,.{decimals}f}" if layout.thousands else f"{value:.{decimals}f}"
    return text


def format_date(value: dt.date, layout: Layout) -> str:
    return value.strftime(layout.date_format)


def format_quantity(value: Decimal) -> str:
    normalised = value.normalize()
    return f"{normalised:f}" if normalised == normalised.to_integral_value() else f"{value:f}"
