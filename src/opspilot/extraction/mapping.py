"""Map parsed invoice text to fields (LLM #1), then ground every value in the text.

The model only proposes values. A value counts as read with full confidence
only when it also appears in the parsed text. Numbers are compared as numbers,
dates in each printed format, text after normalisation, and the bank account
must be one of the document's own tokens. Otherwise its confidence is low, and
every check that needs it is `inconclusive` (`04-prd.md` §6.1). A value the
model invents therefore can never pass a check.

Digital PDFs have no OCR uncertainty, so grounding is the whole confidence
here; scanned documents (P15) will combine it with OCR confidence.
"""

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from opspilot.truthlens.types import FULL, InvoiceFacts, InvoiceLineFacts

SCHEMA_VERSION = "invoice-extraction/1"
UNGROUNDED = Decimal("0.5")  # present and well-formed, but not found in the text
MISSING = Decimal(0)

# Every date format the documents use (generator layouts), plus unpadded variants.
DATE_FORMATS = (
    "%Y-%m-%d",
    "%d %b %Y",
    "%m/%d/%Y",
    "%B %d, %Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%b %d, %Y",
    "%d %B %Y",
)
_NUMBER = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])\d+(?:\.\d+)?")
_SPACE = re.compile(r"\s+")
_PO_MENTION = re.compile(r"purchase\s+order|\bP\.?\s?O\.?\s*(?:no|number|#|:)|\bPO-\d", re.I)


class ExtractedLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    sku: str | None = Field(None, description="Item code, if printed")
    quantity: str | None = Field(description="Decimal as printed, e.g. '12' or '2.5'")
    unit_price: str | None = Field(description="Decimal without currency symbol")
    line_total: str | None = Field(description="Decimal without currency symbol")


class InvoiceExtraction(BaseModel):
    """What the model returns. Every value as printed; null when not on the document."""

    model_config = ConfigDict(extra="forbid")

    vendor_name: str | None = Field(description="The seller issuing the invoice")
    invoice_number: str | None
    invoice_date: str | None = Field(description="ISO date YYYY-MM-DD")
    po_number: str | None = Field(description="Purchase order number, null if none printed")
    lines: list[ExtractedLine]
    subtotal: str | None
    tax: str | None
    total: str | None
    bank_account: str | None = Field(
        description="The payment account placeholder exactly as printed, e.g. BANK_ACCT[fp:…]"
    )


@dataclass(frozen=True)
class Grounded:
    data: dict[str, Any]  # normalised values (decimals and dates as strings)
    confidence: dict[str, Decimal]
    bank_fp: str | None
    bank_last4: str | None


def _norm_text(value: str) -> str:
    return _SPACE.sub(" ", value.lower()).strip()


def _numbers(text: str) -> set[Decimal]:
    return {Decimal(m.replace(",", "")) for m in _NUMBER.findall(text)}


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    cleaned = value.replace("$", "").replace(",", "").replace(" ", "").strip()
    try:
        number = Decimal(cleaned)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _date_renderings(value: dt.date) -> set[str]:
    rendered = {value.strftime(f).lower() for f in DATE_FORMATS}
    unpadded = {r.replace(f" 0{value.day},", f" {value.day},") for r in rendered}
    return rendered | unpadded | {r.replace(f"0{value.day} ", f"{value.day} ") for r in rendered}


class _Grounder:
    def __init__(self, text: str, bank_tokens: list[dict[str, str]]) -> None:
        self.text = _norm_text(text)
        self.numbers = _numbers(text)
        self.tokens = {t["token"]: t for t in bank_tokens}
        self.data: dict[str, Any] = {}
        self.confidence: dict[str, Decimal] = {}

    def text_field(self, name: str, value: str | None) -> None:
        self.data[name] = value.strip() if value else None
        if not value or not value.strip():
            self.confidence[name] = MISSING
        else:
            found = _norm_text(value) in self.text
            self.confidence[name] = FULL if found else UNGROUNDED

    def number(self, name: str, value: str | None) -> Decimal | None:
        number = _decimal(value)
        self.data[name] = format(number, "f") if number is not None else None
        if number is None:
            self.confidence[name] = MISSING
        else:
            self.confidence[name] = FULL if number in self.numbers else UNGROUNDED
        return number

    def date(self, name: str, value: str | None) -> None:
        try:
            parsed = dt.date.fromisoformat(value) if value else None
        except ValueError:
            parsed = None
        self.data[name] = parsed.isoformat() if parsed else None
        if parsed is None:
            self.confidence[name] = MISSING
        else:
            found = any(r in self.text for r in _date_renderings(parsed))
            self.confidence[name] = FULL if found else UNGROUNDED

    def po_number(self, value: str | None) -> None:
        if value and value.strip():
            self.text_field("po_number", value)
            return
        # "No PO" is a reading too: trusted only if the document mentions no PO at all.
        self.data["po_number"] = None
        self.confidence["po_number"] = UNGROUNDED if _PO_MENTION.search(self.text) else FULL

    def bank(self, value: str | None) -> tuple[str | None, str | None]:
        token = value.strip() if value else None
        if token is None and len(self.tokens) == 1:
            token = next(iter(self.tokens))  # the document's only account: no reading needed
        hit = self.tokens.get(token) if token else None
        self.data["bank_account"] = token if hit else None
        if hit is None:
            self.confidence["bank_account"] = MISSING if token is None else UNGROUNDED
            return None, None
        self.confidence["bank_account"] = FULL
        return hit["bank_fp"], hit["last4"]


def ground(extraction: InvoiceExtraction, text: str, bank_tokens: list[dict[str, str]]) -> Grounded:
    g = _Grounder(text, bank_tokens)
    g.text_field("vendor_name", extraction.vendor_name)
    g.text_field("invoice_number", extraction.invoice_number)
    g.date("invoice_date", extraction.invoice_date)
    g.po_number(extraction.po_number)
    for name in ("subtotal", "tax", "total"):
        g.number(name, getattr(extraction, name))
    lines = []
    for index, line in enumerate(extraction.lines, start=1):
        prefix = f"lines[{index}]"
        g.text_field(f"{prefix}.description", line.description)
        sku = line.sku.strip() if line.sku and line.sku.strip() else None
        if sku is not None:
            g.text_field(f"{prefix}.sku", sku)
        for name in ("quantity", "unit_price", "line_total"):
            g.number(f"{prefix}.{name}", getattr(line, name))
        lines.append(
            {
                "index": index,
                "description": line.description.strip(),
                "sku": sku,
                "quantity": g.data[f"{prefix}.quantity"],
                "unit_price": g.data[f"{prefix}.unit_price"],
                "line_total": g.data[f"{prefix}.line_total"],
            }
        )
    bank_fp, last4 = g.bank(extraction.bank_account)
    data = {
        "vendor_name": g.data["vendor_name"],
        "invoice_number": g.data["invoice_number"],
        "invoice_date": g.data["invoice_date"],
        "po_number": g.data["po_number"],
        "subtotal": g.data["subtotal"],
        "tax": g.data["tax"],
        "total": g.data["total"],
        "bank_account": g.data["bank_account"],
        "lines": lines,
    }
    confidence = {k: v for k, v in g.confidence.items() if ".description" not in k}
    # Line descriptions and SKUs are used for matching, not compared by checks.
    confidence = {k: v for k, v in confidence.items() if not k.endswith(".sku")}
    return Grounded(data, confidence, bank_fp, last4)


def _d(value: str | None) -> Decimal:
    return Decimal(value) if value is not None else Decimal(0)  # unread: confidence is 0


def facts_from(
    data: dict[str, Any], confidence: dict[str, Any], bank_fp: str | None, vendor_ok: bool
) -> InvoiceFacts:
    """Rebuild TruthLens input from a stored extraction; unread values carry confidence 0."""
    lines = tuple(
        InvoiceLineFacts(
            ln["index"],
            ln["description"],
            ln["sku"],
            _d(ln["quantity"]),
            _d(ln["unit_price"]),
            _d(ln["line_total"]),
        )
        for ln in data["lines"]
    )
    scores = {k: Decimal(str(v)) for k, v in confidence.items()}
    scores["vendor"] = FULL if vendor_ok else MISSING
    date = data["invoice_date"]
    return InvoiceFacts(
        number=data["invoice_number"] or "",
        invoice_date=dt.date.fromisoformat(date) if date else dt.date.min,
        po_number=data["po_number"],
        lines=lines,
        subtotal=_d(data["subtotal"]),
        tax=_d(data["tax"]),
        total=_d(data["total"]),
        bank_fp=bank_fp,
        confidence=scores,
    )
