"""Read-only tools for the investigate loop (`06-architecture.md` §3.1, §10).

Each tool is a typed query scoped to the case: its dataset, its vendor and the
PO it cites. None can write, and none can reach labels, evaluation tables or
the baseline's outputs. Arguments are validated before anything runs; an
invalid call goes back to the model as an error and is never coerced. Bank
details appear only as fingerprint tokens.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Connection, select

from opspilot.agent.deps import PassageRef, PolicySearch
from opspilot.db.models import LedgerInvoice, PoLine, PurchaseOrder, Receipt, ReceiptLine, Vendor
from opspilot.llm.types import ToolSpec
from opspilot.truthlens.checks import normalise_invoice_number

MAX_TOOL_CALLS = 4


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchPolicy(_Args):
    query: str = Field(min_length=3, max_length=200, description="What to look up in the policy")


class GetRecord(_Args):
    kind: Literal["vendor", "purchase_order", "receipts"]


class GetVendorHistory(_Args):
    pass


class GetLedgerInvoices(_Args):
    invoice_number: str = Field(min_length=1, max_length=60)


@dataclass
class Scope:
    dataset_id: uuid.UUID
    vendor_id: uuid.UUID
    po_id: uuid.UUID | None


@dataclass
class ToolBox:
    conn: Connection
    scope: Scope
    search: PolicySearch
    passages: dict[str, PassageRef] = field(default_factory=dict)  # by section ID
    calls: int = 0

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(name, doc, model.model_json_schema())
            for name, (model, doc, _run) in self._tools().items()
        )

    def _tools(self) -> dict[str, tuple[type[_Args], str, Callable[[Any], dict[str, Any]]]]:
        return {
            "search_policy": (SearchPolicy, "Search the procurement policy.", self._search),
            "get_record": (
                GetRecord,
                "The vendor master record, the cited purchase order, or its goods receipts.",
                self._record,
            ),
            "get_vendor_history": (
                GetVendorHistory,
                "Invoices from this vendor already processed (number, date, total).",
                self._history,
            ),
            "get_ledger_invoices": (
                GetLedgerInvoices,
                "Processed invoices from this vendor with a given invoice number.",
                self._ledger,
            ),
        }

    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.calls >= MAX_TOOL_CALLS:
            return {"error": f"tool call limit ({MAX_TOOL_CALLS}) reached"}
        self.calls += 1
        tool = self._tools().get(name)
        if tool is None:
            return {"error": f"unknown tool {name}"}
        model, _doc, run = tool
        try:
            parsed = model.model_validate(args)
        except ValidationError as exc:
            return {"error": exc.json(include_url=False, include_input=False)}
        return run(parsed)

    # ------------------------------------------------------------- tools

    def _search(self, args: SearchPolicy) -> dict[str, Any]:
        found = self.search(args.query)[:3]
        for passage in found:
            self.passages[passage.section_id] = passage
        return {
            "passages": [
                {"section": p.section_id, "title": p.title, "text": p.text[:800]} for p in found
            ]
        }

    def _record(self, args: GetRecord) -> dict[str, Any]:
        if args.kind == "vendor":
            v = self.conn.execute(select(Vendor).where(Vendor.id == self.scope.vendor_id)).one()
            return {
                "vendor_code": v.vendor_code,
                "name": v.name,
                "approved": v.is_approved,
                "tax_rate": str(v.tax_rate),
            }
        if self.scope.po_id is None:
            return {"error": "the invoice cites no purchase order on record"}
        if args.kind == "purchase_order":
            po = self.conn.execute(
                select(PurchaseOrder).where(PurchaseOrder.id == self.scope.po_id)
            ).one()
            lines = self.conn.execute(
                select(PoLine).where(PoLine.po_id == po.id).order_by(PoLine.line_no)
            ).all()
            return {
                "po_number": po.po_number,
                "approval_level": po.approval_level,
                "controller_approved": po.controller_approved,
                "lines": [
                    {
                        "line_no": ln.line_no,
                        "sku": ln.sku,
                        "description": ln.description,
                        "quantity": str(ln.quantity),
                        "unit_price": str(ln.unit_price),
                    }
                    for ln in lines
                ],
            }
        rows = self.conn.execute(
            select(
                Receipt.receipt_number,
                Receipt.received_at,
                PoLine.line_no,
                ReceiptLine.quantity_received,
            )
            .join(ReceiptLine, ReceiptLine.receipt_id == Receipt.id)
            .join(PoLine, PoLine.id == ReceiptLine.po_line_id)
            .where(Receipt.po_id == self.scope.po_id)
            .order_by(Receipt.received_at, PoLine.line_no)
        )
        return {
            "receipts": [
                {
                    "receipt": r.receipt_number,
                    "date": r.received_at.isoformat(),
                    "po_line": r.line_no,
                    "quantity": str(r.quantity_received),
                }
                for r in rows
            ]
        }

    def _invoices(self) -> list[Any]:
        return list(
            self.conn.execute(
                select(LedgerInvoice)
                .where(
                    LedgerInvoice.dataset_id == self.scope.dataset_id,
                    LedgerInvoice.vendor_id == self.scope.vendor_id,
                )
                .order_by(LedgerInvoice.invoice_date)
            ).all()
        )

    def _history(self, _args: GetVendorHistory) -> dict[str, Any]:
        return {
            "invoices": [
                {
                    "number": i.invoice_number,
                    "date": i.invoice_date.isoformat(),
                    "total": str(i.total),
                }
                for i in self._invoices()[-20:]
            ]
        }

    def _ledger(self, args: GetLedgerInvoices) -> dict[str, Any]:
        wanted = normalise_invoice_number(args.invoice_number)
        return {
            "invoices": [
                {
                    "number": i.invoice_number,
                    "date": i.invoice_date.isoformat(),
                    "subtotal": str(i.subtotal),
                    "tax": str(i.tax),
                    "total": str(i.total),
                }
                for i in self._invoices()
                if normalise_invoice_number(i.invoice_number) == wanted
            ]
        }
