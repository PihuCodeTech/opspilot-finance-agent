"""A committed invoice scenario and a scripted stand-in model for graph tests.

The stand-in answers by request purpose, like a model that read the document
correctly. Each behaviour can be switched to adversarial: lie in the
extraction, propose payment regardless, or return nonsense. No network is
used, ever. Data is committed because LLM calls are recorded in their own
transactions (as in production); each scenario uses its own dataset.
"""

import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from opspilot.agent.deps import AgentDeps, PassageRef
from opspilot.agent.graph import Agent
from opspilot.config import LlmMode
from opspilot.domain.enums import JobType
from opspilot.extraction.preprocess import PARSER_VERSION
from opspilot.jobs.queue import Job
from opspilot.llm.client import LlmClient
from opspilot.llm.quota import QuotaGate
from opspilot.llm.types import FunctionCall, LlmRequest, LlmResponse
from opspilot.policy.retrieve import retrieve

BANK_FP = "3f9a1c2b7d10"
TOKEN = f"BANK_ACCT[fp:{BANK_FP}]"


@dataclass
class Invoice:
    number: str
    po_number: str
    unit_price: str  # PO price is 0.42
    quantity: str = "100"
    note: str = ""  # extra text printed on the document (adversarial instructions)

    @property
    def line_total(self) -> Decimal:
        return (Decimal(self.unit_price) * Decimal(self.quantity)).quantize(Decimal("0.01"))

    @property
    def tax(self) -> Decimal:
        return (self.line_total * Decimal("0.0825")).quantize(Decimal("0.01"))

    def text(self, vendor: str) -> str:
        return (
            f"## INVOICE\n## {vendor}\nInvoice number {self.number}\nDated 2026-10-20\n"
            f"Purchase order {self.po_number}\n"
            "| Description | Code | Unit price | Qty | Extended |\n"
            f"| Hex bolts M8 | HB-8 | {self.unit_price} | {self.quantity} | {self.line_total} |\n"
            f"Subtotal {self.line_total}\nTax (8.25%) {self.tax}\n"
            f"Total due {self.line_total + self.tax}\n"
            f"Account for payment: {TOKEN}\n{self.note}"
        )

    def extraction(self, vendor: str) -> dict[str, Any]:
        return {
            "vendor_name": vendor,
            "invoice_number": self.number,
            "invoice_date": "2026-10-20",
            "po_number": self.po_number,
            "lines": [
                {
                    "description": "Hex bolts M8",
                    "sku": "HB-8",
                    "quantity": self.quantity,
                    "unit_price": self.unit_price,
                    "line_total": str(self.line_total),
                }
            ],
            "subtotal": str(self.line_total),
            "tax": str(self.tax),
            "total": str(self.line_total + self.tax),
            "bank_account": TOKEN,
        }


@dataclass
class StandInModel:
    """Answers like a model that read correctly, unless told to misbehave."""

    vendor: str
    invoices: dict[str, Invoice]
    name: str = "fake"
    model: str = "stand-in-1"
    lie_unit_price: str | None = None  # extraction reports this instead of the printed price
    always_propose: str | None = None  # explanation proposes this whatever is allowed
    invent_numbers: bool = False  # explanations quote a number from nowhere
    seen: list[LlmRequest] = field(default_factory=list)

    def purposes(self) -> list[str]:
        return [r.purpose for r in self.seen]

    def generate(self, request: LlmRequest) -> LlmResponse:
        self.seen.append(request)
        handler = getattr(self, f"_{request.purpose}")
        response: LlmResponse = handler(request)
        return response

    def _extract(self, request: LlmRequest) -> LlmResponse:
        document = request.messages[0].text or ""
        invoice = next(i for n, i in self.invoices.items() if f"Invoice number {n}\n" in document)
        data = invoice.extraction(self.vendor)
        if self.lie_unit_price is not None:
            data["lines"][0]["unit_price"] = self.lie_unit_price
        return LlmResponse(text=json.dumps(data), tokens_in=500, tokens_out=200)

    def _match(self, _request: LlmRequest) -> LlmResponse:
        return LlmResponse(text=json.dumps({"matches": []}), tokens_in=100, tokens_out=10)

    def _investigate(self, request: LlmRequest) -> LlmResponse:
        if request.tools:  # first round: look things up
            return LlmResponse(
                text=None,
                function_calls=(
                    FunctionCall("search_policy", {"query": "unit price tolerance"}),
                    FunctionCall("get_record", {"kind": "purchase_order"}),
                ),
                tokens_in=300,
                tokens_out=30,
            )
        return LlmResponse(
            text="The PO price is lower than the invoice price.", tokens_in=400, tokens_out=20
        )

    def _explain(self, request: LlmRequest) -> LlmResponse:
        payload = json.loads(request.messages[0].text or "{}")
        notes = []
        for finding in payload["findings"]:
            key = finding["checks"][0]
            compared = payload["check_results"][key]["compared"]
            values = [str(v) for v in compared.values() if isinstance(v, str)][:2]
            sentence = "The check failed with " + " and ".join(values) + "."
            if self.invent_numbers:
                sentence += " The vendor owes 999.99."
            notes.append(
                {
                    "finding": finding["finding"],
                    "explanation": sentence,
                    "check_results": [key],
                    "policy_sections": [],
                }
            )
        allowed = payload["allowed_actions"]
        action = self.always_propose or (
            "approve_for_payment" if "approve_for_payment" in allowed else "place_on_hold"
        )
        return LlmResponse(
            text=json.dumps({"findings": notes, "proposed_action": action, "summary": "ok"}),
            tokens_in=800,
            tokens_out=150,
        )


@dataclass
class Scenario:
    engine: Engine
    dataset: uuid.UUID
    vendor_name: str
    po_number: str
    analyst: uuid.UUID
    approver: uuid.UUID
    model: StandInModel

    def invoice(self, invoice: Invoice) -> uuid.UUID:
        """An incoming invoice whose document is already parsed (the shared, non-LLM step)."""
        self.model.invoices[invoice.number] = invoice
        with self.engine.begin() as conn:
            document = conn.execute(
                text(
                    "INSERT INTO documents (sha256, kind, mime_type, size_bytes, storage_path,"
                    " source) VALUES (:h, 'invoice', 'application/pdf', 1024, :p, 'generated')"
                    " RETURNING id"
                ),
                {"h": uuid.uuid4().hex * 2, "p": f"documents/{uuid.uuid4().hex}"},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO parsed_documents (document_id, parser_version, text, structure,"
                    " coordinates, bank_tokens) VALUES (:d, :v, :t, '{}', '{}', CAST(:b AS jsonb))"
                ),
                {
                    "d": document,
                    "v": PARSER_VERSION,
                    "t": invoice.text(self.vendor_name),
                    "b": json.dumps([{"token": TOKEN, "bank_fp": BANK_FP, "last4": "4471"}]),
                },
            )
            invoice_id: uuid.UUID = conn.execute(
                text(
                    "INSERT INTO invoices (dataset_id, document_id, received_at)"
                    " VALUES (:d, :doc, now()) RETURNING id"
                ),
                {"d": self.dataset, "doc": document},
            ).scalar_one()
        return invoice_id


def make_scenario(engine: Engine) -> Scenario:
    tag = uuid.uuid4().hex[:6]
    vendor_name = f"Halden Industrial Supply {tag}"
    with engine.begin() as conn:

        def one(sql: str, **params: Any) -> uuid.UUID:
            value: uuid.UUID = conn.execute(text(sql), params).scalar_one()
            return value

        def user(role: str, limit: int | None = None) -> uuid.UUID:
            return one(
                "INSERT INTO users (username, display_name, password_hash, role, approval_limit)"
                " VALUES (:u, 'T', 'x', CAST(:r AS user_role), :l) RETURNING id",
                u=f"{role}-{uuid.uuid4().hex[:8]}",
                r=role,
                l=limit,
            )

        dataset = one(
            "INSERT INTO datasets (name, version, split) VALUES (:n, '1', 'dev') RETURNING id",
            n=f"agent-{tag}",
        )
        vendor = one(
            "INSERT INTO vendors (dataset_id, vendor_code, name, address, tax_rate,"
            " is_approved) VALUES (:d, 'V-0001', :n, '1 Quay St', 0.0825, true)"
            " RETURNING id",
            d=dataset,
            n=vendor_name,
        )
        one(
            "INSERT INTO vendor_bank_accounts (vendor_id, bank_fp, last4, valid_from)"
            " VALUES (:v, :fp, '4471', '2026-01-01') RETURNING id",
            v=vendor,
            fp=BANK_FP,
        )
        po_number = f"PO-{tag}"
        po = one(
            "INSERT INTO purchase_orders (dataset_id, po_number, vendor_id, approval_level,"
            " issued_at) VALUES (:d, :n, :v, 1, '2026-09-01') RETURNING id",
            d=dataset,
            n=po_number,
            v=vendor,
        )
        line = one(
            "INSERT INTO po_lines (po_id, line_no, sku, description, quantity, unit_price)"
            " VALUES (:p, 1, 'HB-8', 'Hex bolts M8', 100, 0.42) RETURNING id",
            p=po,
        )
        receipt = one(
            "INSERT INTO receipts (dataset_id, receipt_number, po_id, received_at)"
            " VALUES (:d, :n, :p, '2026-10-01') RETURNING id",
            d=dataset,
            n=f"GR-{tag}",
            p=po,
        )
        one(
            "INSERT INTO receipt_lines (receipt_id, po_line_id, quantity_received)"
            " VALUES (:r, :l, 100) RETURNING id",
            r=receipt,
            l=line,
        )
        analyst = user("analyst")
        approver = user("approver", limit=10000)
    return Scenario(
        engine, dataset, vendor_name, po_number, analyst, approver, StandInModel(vendor_name, {})
    )


def fts_search(engine: Engine) -> Any:
    """Full-text policy search: same passages as production, without the embedding model."""

    def search(query: str) -> list[PassageRef]:
        found = retrieve(engine, None, query, mode="fts")  # type: ignore[arg-type]
        return [PassageRef(p.chunk_id, p.section_id, p.title, p.text) for p in found]

    return search


def make_agent(
    engine: Engine,
    url: str,
    model: Any,
    *,
    mode: LlmMode = LlmMode.LIVE,
    max_llm_calls: int = 5,
    agent_class: type[Agent] = Agent,
) -> Agent:
    quota = QuotaGate(engine, model.name, model.model, 10_000, 1_000_000)
    llm = LlmClient(model, mode, quota if mode is not LlmMode.REPLAY else None, engine)
    deps = AgentDeps(engine, llm, fts_search(engine), None, Path("."), max_llm_calls)
    return agent_class.connect(deps, url)


def job(case_id: uuid.UUID, job_type: JobType = JobType.INVESTIGATE, **payload: Any) -> Job:
    return Job(uuid.uuid4(), job_type, case_id, {k: str(v) for k, v in payload.items()}, 1, 5)
