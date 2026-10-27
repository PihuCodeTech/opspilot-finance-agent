"""The investigation graph (`06-architecture.md` §3) and its job handlers.

Each node reads what it needs from Postgres by ID and writes insert-only,
idempotent rows. A node re-run after a crash finds its own output and skips
the work, so a resumed run never duplicates rows and never repeats a finished
LLM step. The graph state holds only IDs and small values; the database is the
record.

LLM calls are capped at `MAX_LLM_CALLS` per run. Optional steps (line matching,
investigation, retries) yield to the explanation, which falls back to a
template when its budget is spent.
"""

import json
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, NotRequired, TypedDict

import psycopg
import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, Engine, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url

from opspilot.actions.execute import execute
from opspilot.agent.deps import AgentDeps, PassageRef, budget_left, prompt, prompt_versions
from opspilot.agent.explain import (
    CheckView,
    ExplainOutput,
    FindingView,
    check_key,
    prompt_safe,
    template,
    validate,
)
from opspilot.agent.records import VendorNotIdentifiedError, find_vendor, resolve_records
from opspilot.agent.tools import Scope, ToolBox
from opspilot.cases import service
from opspilot.cases.state import load_policy_config
from opspilot.db.models import (
    Case,
    CheckResult,
    Decision,
    ExecutedAction,
    Extraction,
    Finding,
    FindingCheckResult,
    FindingCitation,
    InvestigationRun,
    Invoice,
    LineMatch,
    ParsedDocument,
    PolicyChunk,
    Proposal,
)
from opspilot.domain.enums import (
    ActionType,
    CaseStatus,
    CheckStatus,
    DecisionKind,
    MatchMethod,
    ProposalSource,
    RunKind,
    RunStatus,
)
from opspilot.extraction.mapping import (
    SCHEMA_VERSION,
    InvoiceExtraction,
    facts_from,
    ground,
)
from opspilot.extraction.preprocess import (
    PARSER_VERSION,
    UnsupportedDocumentError,
    preprocess_document,
)
from opspilot.jobs.queue import Job
from opspilot.jobs.worker import PauseJob
from opspilot.llm.client import LlmResult
from opspilot.llm.errors import LlmError, QuotaExhaustedError, StructuredOutputError
from opspilot.llm.types import FunctionResult, LlmRequest, Message
from opspilot.policy.allowlist import FindingState, allowed_actions
from opspilot.policy.document import FINDING_WORDS
from opspilot.policy.schema import PolicyConfig
from opspilot.truthlens.checks import run_all
from opspilot.truthlens.findings import SEVERITY, derive_findings
from opspilot.truthlens.matching import match_lines, similarity
from opspilot.truthlens.types import CheckOutcome, ErpContext, InvoiceFacts

log = structlog.get_logger()

EXPLAIN_ATTEMPTS = 2  # the first try and one retry with the errors fed back


class RunFailure(RuntimeError):  # noqa: N818  # a run outcome, recorded as T5
    """A run that can't finish (unidentified vendor, unusable reading, LLM unavailable)."""


class AgentState(TypedDict):
    case_id: str
    run_id: str
    kind: NotRequired[str]
    parsed_document_id: NotRequired[str]
    calls: NotRequired[int]
    tokens_in: NotRequired[int]
    tokens_out: NotRequired[int]
    quota_wait_ms: NotRequired[int]
    passages: NotRequired[dict[str, str]]  # section ID -> policy chunk ID
    note: NotRequired[str]
    explanation: NotRequired[dict[str, Any] | None]
    explain_attempts: NotRequired[int]
    errors: NotRequired[list[str]]
    fallback: NotRequired[bool]
    outcome: NotRequired[str]
    decision_id: NotRequired[str]


class _Match(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_line: int
    po_line_no: int | None
    justification: str


class MatchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matches: list[_Match]


def _usage(state: AgentState, calls: tuple[LlmResult, ...] | list[LlmResult]) -> dict[str, int]:
    return {
        "calls": state.get("calls", 0) + len(calls),
        "tokens_in": state.get("tokens_in", 0) + sum(c.response.tokens_in for c in calls),
        "tokens_out": state.get("tokens_out", 0) + sum(c.response.tokens_out for c in calls),
        "quota_wait_ms": state.get("quota_wait_ms", 0) + sum(c.quota_wait_ms for c in calls),
    }


def _uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)


# ---------------------------------------------------------------- reading the run


@dataclass(frozen=True)
class RunContext:
    run: dict[str, Any]
    dataset_id: uuid.UUID
    document_id: uuid.UUID
    policy: PolicyConfig


def run_context(conn: Connection, run_id: uuid.UUID) -> RunContext:
    row = conn.execute(
        select(InvestigationRun.__table__, Invoice.dataset_id, Invoice.document_id)
        .join(Case, Case.id == InvestigationRun.case_id)
        .join(Invoice, Invoice.id == Case.invoice_id)
        .where(InvestigationRun.id == run_id)
    ).one()
    run = dict(row._mapping)
    return RunContext(
        run, row.dataset_id, row.document_id, load_policy_config(conn, run["policy_version_id"])
    )


def _document_for_run(ctx: RunContext) -> uuid.UUID:
    """A recheck may read a clearer copy instead of the original document."""
    inputs = ctx.run.get("recheck_inputs") or {}
    replacement = inputs.get("replacement_document_id")
    return _uuid(replacement) if replacement else ctx.document_id


def facts_and_records(conn: Connection, run_id: uuid.UUID) -> tuple[InvoiceFacts, ErpContext]:
    """Rebuild TruthLens input for a run from its stored extraction and line matches."""
    ctx = run_context(conn, run_id)
    extraction = conn.execute(select(Extraction).where(Extraction.run_id == run_id)).one()
    data, confidence = extraction.data, extraction.field_confidence
    vendor = find_vendor(conn, ctx.dataset_id, data["vendor_name"])
    vendor_ok = str(confidence.get("vendor_name")) == "1"
    facts = facts_from(data, confidence, extraction.bank_fp, vendor_ok)
    po_number = _po_override(ctx) or data["po_number"]
    records = resolve_records(conn, ctx.dataset_id, vendor, po_number)
    po_line_no = {ln.id: ln.line_no for ln in records.po.lines} if records.po else {}
    mapping = {
        m.invoice_line_no: po_line_no.get(m.po_line_id) if m.po_line_id else None
        for m in conn.execute(select(LineMatch).where(LineMatch.run_id == run_id)).all()
    }
    if _po_override(ctx):
        facts = replace(facts, po_number=po_number)
    return facts, replace(records, line_mapping=mapping)


def _po_override(ctx: RunContext) -> str | None:
    inputs = ctx.run.get("recheck_inputs") or {}
    value = inputs.get("po_number")
    return str(value) if value else None


def stored_outcomes(conn: Connection, run_id: uuid.UUID) -> list[tuple[CheckOutcome, uuid.UUID]]:
    rows = conn.execute(
        select(CheckResult).where(CheckResult.run_id == run_id).order_by(CheckResult.created_at)
    ).all()
    return [
        (
            CheckOutcome(
                r.check_code,
                r.subject_key,
                r.status,
                r.compared,
                r.tolerance,
                tuple(r.evidence_refs),
                r.inconclusive_reason,
            ),
            r.id,
        )
        for r in rows
    ]


# ---------------------------------------------------------------- the graph


class Agent:
    def __init__(self, deps: AgentDeps, checkpointer: PostgresSaver) -> None:
        self.deps = deps
        self.checkpointer = checkpointer
        self.graph = self._build().compile(checkpointer=checkpointer)

    @staticmethod
    def saver(database_url: str) -> PostgresSaver:
        """The checkpointer on its own connection (autocommit, as the library requires)."""
        url = make_url(database_url).set(drivername="postgresql")
        conn = psycopg.connect(
            url.render_as_string(hide_password=False),
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        )
        return PostgresSaver(conn)

    @classmethod
    def connect(cls, deps: AgentDeps, database_url: str) -> "Agent":
        return cls(deps, cls.saver(database_url))

    # ------------------------------------------------------------- nodes

    def _llm(
        self, state: AgentState, request: LlmRequest, model: type[BaseModel], retries: int
    ) -> tuple[Any, dict[str, int]]:
        result = self.deps.llm.generate_structured(
            request, model, max_retries=retries, run_id=_uuid(state["run_id"])
        )
        return result.value, _usage(state, result.calls)

    def load_case(self, state: AgentState) -> dict[str, Any]:
        with self.deps.engine.connect() as conn:
            run = conn.execute(
                select(InvestigationRun.kind, InvestigationRun.status).where(
                    InvestigationRun.id == _uuid(state["run_id"])
                )
            ).one()
        return {"kind": str(run.kind)}

    def extract(self, state: AgentState) -> dict[str, Any]:
        run_id = _uuid(state["run_id"])
        with self.deps.engine.connect() as conn:
            ctx = run_context(conn, run_id)
        document_id = _document_for_run(ctx)
        parsed = self._parsed(document_id)
        updates: dict[str, Any] = {"parsed_document_id": str(parsed.id)}
        with self.deps.engine.connect() as conn:
            done = conn.execute(select(Extraction.id).where(Extraction.run_id == run_id)).first()
        if done is not None:
            return updates
        request = LlmRequest(
            "extract",
            prompt("extract"),
            (Message("user", text=f"<document>\n{parsed.text}\n</document>"),),
        )
        retries = 1 if budget_left(state.get("calls", 0) + 1, self.deps, optional=True) else 0
        try:
            value, usage = self._llm(state, request, InvoiceExtraction, retries)
        except StructuredOutputError as exc:
            raise RunFailure("extraction_invalid: the reading didn't match the schema") from exc
        grounded = ground(value, parsed.text, parsed.bank_tokens)
        with self.deps.engine.begin() as conn:
            conn.execute(
                insert(Extraction)
                .values(
                    run_id=run_id,
                    schema_version=SCHEMA_VERSION,
                    data=grounded.data,
                    field_confidence={k: str(v) for k, v in grounded.confidence.items()},
                    coordinates={},
                    bank_fp=grounded.bank_fp,
                    bank_last4=grounded.bank_last4,
                )
                .on_conflict_do_nothing(index_elements=["run_id"])
            )
        return updates | usage

    def _parsed(self, document_id: uuid.UUID) -> Any:  # a parsed_documents row
        """The shared non-LLM parse (the baseline reads the same row); created if missing."""
        query = (
            select(ParsedDocument)
            .where(ParsedDocument.document_id == document_id)
            .order_by(
                (ParsedDocument.parser_version == PARSER_VERSION).desc(),
                ParsedDocument.created_at.desc(),
            )
            .limit(1)
        )
        with self.deps.engine.connect() as conn:
            parsed = conn.execute(query).first()
        if parsed is None:
            if self.deps.preprocessor is None:
                raise RunFailure("document_not_parsed")
            try:
                preprocess_document(
                    self.deps.engine, self.deps.preprocessor, document_id, self.deps.document_store
                )
            except UnsupportedDocumentError as exc:
                # A scanned image: no text without OCR (P15), so nothing to read and nothing
                # that may go to a model. The case fails with the reason (T5).
                raise RunFailure(f"scanned_image_unsupported: {exc}") from exc
            with self.deps.engine.connect() as conn:
                parsed = conn.execute(query).one()
        return parsed

    def resolve_records(self, state: AgentState) -> dict[str, Any]:
        """Fail early, with a clear reason, if the vendor read can't be found in the master."""
        with self.deps.engine.connect() as conn:
            ctx = run_context(conn, _uuid(state["run_id"]))
            name = conn.execute(
                select(Extraction.data["vendor_name"].astext).where(
                    Extraction.run_id == _uuid(state["run_id"])
                )
            ).scalar_one()
            try:
                find_vendor(conn, ctx.dataset_id, name)
            except VendorNotIdentifiedError as exc:
                raise RunFailure(f"vendor_not_identified: {exc}") from exc
        return {}

    def match_lines(self, state: AgentState) -> dict[str, Any]:
        run_id = _uuid(state["run_id"])
        with self.deps.engine.connect() as conn:
            if conn.execute(select(LineMatch.id).where(LineMatch.run_id == run_id)).first():
                return {}
            facts, records = facts_and_records(conn, run_id)
        po = records.po if records.po and records.po.vendor_code == records.vendor.code else None
        matches = match_lines(facts.lines, po.lines if po else ())
        updates: dict[str, Any] = {}
        decided: dict[int, tuple[int | None, MatchMethod, Any, str | None]] = {
            m.invoice_line: (m.po_line_no, m.method, m.similarity, None) for m in matches
        }
        unmatched = [m.invoice_line for m in matches if m.po_line_no is None]
        used = {m.po_line_no for m in matches if m.po_line_no is not None}
        available = [pl for pl in (po.lines if po else ()) if pl.line_no not in used]
        if unmatched and available and budget_left(state.get("calls", 0), self.deps, optional=True):
            by_index = {ln.index: ln for ln in facts.lines}
            payload = {
                "unmatched_lines": [
                    {"line": i, "description": by_index[i].description, "sku": by_index[i].sku}
                    for i in unmatched
                ],
                "available_po_lines": [
                    {"line_no": pl.line_no, "description": pl.description, "sku": pl.sku}
                    for pl in available
                ],
            }
            request = LlmRequest(
                "match", prompt("match"), (Message("user", text=json.dumps(payload)),)
            )
            try:
                value, updates = self._llm(state, request, MatchOutput, 0)
            except StructuredOutputError:  # an unusable reply leaves the lines unmatched
                value = MatchOutput(matches=[])
                updates = {"calls": state.get("calls", 0) + 1}
            free = {pl.line_no: pl for pl in available}
            for proposal in value.matches:
                target = free.get(proposal.po_line_no) if proposal.po_line_no else None
                if proposal.invoice_line in unmatched and target is not None:
                    score = similarity(
                        by_index[proposal.invoice_line].description, target.description
                    )
                    decided[proposal.invoice_line] = (
                        target.line_no,
                        MatchMethod.LLM,
                        score,
                        proposal.justification[:500],
                    )
                    del free[target.line_no]
        line_id = {pl.line_no: pl.id for pl in (po.lines if po else ())}
        with self.deps.engine.begin() as conn:
            for index, (po_line_no, method, score, why) in decided.items():
                conn.execute(
                    insert(LineMatch)
                    .values(
                        run_id=run_id,
                        invoice_line_no=index,
                        po_line_id=line_id.get(po_line_no) if po_line_no else None,
                        method=method,
                        similarity=score,
                        justification=why,
                    )
                    .on_conflict_do_nothing(index_elements=["run_id", "invoice_line_no"])
                )
        return updates

    def run_checks(self, state: AgentState) -> dict[str, Any]:
        run_id = _uuid(state["run_id"])
        with self.deps.engine.begin() as conn:
            if conn.execute(select(CheckResult.id).where(CheckResult.run_id == run_id)).first():
                return {}
            facts, records = facts_and_records(conn, run_id)
            policy = run_context(conn, run_id).policy
            for outcome in run_all(facts, records, policy):
                conn.execute(
                    insert(CheckResult)
                    .values(
                        run_id=run_id,
                        check_code=outcome.check_code,
                        subject_key=outcome.subject_key,
                        status=outcome.status,
                        compared=outcome.compared,
                        tolerance=outcome.tolerance,
                        evidence_refs=list(outcome.evidence_refs),
                        inconclusive_reason=outcome.inconclusive_reason,
                    )
                    .on_conflict_do_nothing(index_elements=["run_id", "check_code", "subject_key"])
                )
        return {}

    def investigate(self, state: AgentState) -> dict[str, Any]:
        run_id = _uuid(state["run_id"])
        with self.deps.engine.connect() as conn:
            outcomes = [o for o, _ in stored_outcomes(conn, run_id) if o.status != CheckStatus.PASS]
            if not outcomes or not budget_left(state.get("calls", 0), self.deps, optional=True):
                return {}
            _facts, records = facts_and_records(conn, run_id)
            box = ToolBox(
                conn,
                Scope(
                    run_context(conn, run_id).dataset_id,
                    records.vendor.id,  # type: ignore[arg-type]
                    records.po.id if records.po else None,
                ),
                self.deps.policy_search,
            )
            payload = [
                {
                    "check": check_key(o.check_code, o.subject_key),
                    "status": o.status,
                    "compared": prompt_safe(o.compared),
                    "reason": o.inconclusive_reason,
                }
                for o in outcomes
            ]
            messages: tuple[Message, ...] = (
                Message(
                    "user",
                    text="Checks that failed or were inconclusive:\n"
                    + json.dumps(payload, default=str),
                ),
            )
            usage: dict[str, int] = {}
            note = ""
            current = dict(state)
            for round_no in range(2):
                tools = box.specs() if round_no == 0 else ()
                request = LlmRequest("investigate", prompt("investigate"), messages, tools=tools)
                result = self.deps.llm.generate(request, run_id=run_id)
                usage = _usage(current, [result])  # type: ignore[arg-type]
                current.update(usage)
                note = (result.response.text or "").strip()
                calls = result.response.function_calls
                if not calls:
                    break
                outputs = tuple(box.run(c.name, c.args) for c in calls)
                messages = (
                    *messages,
                    Message("model", function_calls=calls),
                    Message(
                        "user",
                        function_results=tuple(
                            FunctionResult(c.name, out)
                            for c, out in zip(calls, outputs, strict=True)
                        ),
                    ),
                )
                if not budget_left(usage["calls"], self.deps, optional=True):
                    break
        passages = dict(state.get("passages", {}))
        passages.update({s: str(p.id) for s, p in box.passages.items()})
        return usage | {"note": note[:1500], "passages": passages}

    def _explain_inputs(
        self, conn: Connection, state: AgentState
    ) -> tuple[
        list[FindingView],
        dict[str, CheckView],
        dict[str, PassageRef],
        PolicyConfig,
        dict[Any, list[str]],
    ]:
        run_id = _uuid(state["run_id"])
        stored = stored_outcomes(conn, run_id)
        outcomes = [o for o, _ in stored]
        checks = {
            check_key(o.check_code, o.subject_key): CheckView(
                check_key(o.check_code, o.subject_key),
                str(o.status),
                o.compared,
                o.tolerance,
                o.subject_key,
            )
            for o in outcomes
        }
        findings = [
            FindingView(
                f"{d.code}@{d.subject_key}",
                d.code,
                d.subject_key,
                tuple(
                    check_key(outcomes[i].check_code, outcomes[i].subject_key)
                    for i in d.outcome_indexes
                ),
            )
            for d in derive_findings(outcomes)
        ]
        passages: dict[str, PassageRef] = {}
        by_code: dict[Any, list[str]] = {}
        for finding in findings:
            found = self.deps.policy_search(FINDING_WORDS[finding.code])[:2]
            by_code[finding.code] = [p.section_id for p in found]
            passages.update({p.section_id: p for p in found})
        chunk_ids = [_uuid(c) for c in state.get("passages", {}).values()]
        for chunk in conn.execute(select(PolicyChunk).where(PolicyChunk.id.in_(chunk_ids))).all():
            passages[chunk.section_id] = PassageRef(
                chunk.id, chunk.section_id, chunk.title, chunk.text
            )
        return findings, checks, passages, run_context(conn, run_id).policy, by_code

    def _allowed(self, policy: PolicyConfig, findings: list[FindingView]) -> frozenset[Any]:
        return allowed_actions(
            policy, ProposalSource.AGENT, [FindingState(f.code) for f in findings]
        )

    def explain_and_propose(self, state: AgentState) -> dict[str, Any]:
        attempts = state.get("explain_attempts", 0) + 1
        if not budget_left(state.get("calls", 0), self.deps, optional=False):
            return {
                "explanation": None,
                "explain_attempts": attempts,
                "errors": ["no LLM calls left"],
            }
        with self.deps.engine.connect() as conn:
            findings, checks, passages, policy, _ = self._explain_inputs(conn, state)
        allowed = self._allowed(policy, findings)
        payload = {
            "findings": [{"finding": f.key, "checks": list(f.checks)} for f in findings],
            "check_results": {
                k: {
                    "status": c.status,
                    "compared": prompt_safe(c.compared),
                    "tolerance": c.tolerance,
                }
                for k, c in checks.items()
            },
            "passages": [
                {"section": p.section_id, "title": p.title, "text": p.text}
                for p in sorted(passages.values(), key=lambda p: p.section_id)
            ],
            "allowed_actions": sorted(a.value for a in allowed),
            "investigation_note": state.get("note", ""),
        }
        messages: tuple[Message, ...] = (Message("user", text=json.dumps(payload, default=str)),)
        if state.get("explanation") is not None and state.get("errors"):
            messages = (
                *messages,
                Message("model", text=json.dumps(state["explanation"])),
                Message(
                    "user",
                    text="Fix these problems and reply again:\n- "
                    + "\n- ".join(state.get("errors", [])),
                ),
            )
        request = LlmRequest("explain", prompt("explain"), messages)
        try:
            value, usage = self._llm(state, request, ExplainOutput, 0)
        except StructuredOutputError:
            usage = {"calls": state.get("calls", 0) + 1}
            return usage | {
                "explanation": None,
                "explain_attempts": attempts,
                "errors": ["the reply did not match the required JSON schema"],
            }
        return usage | {"explanation": value.model_dump(), "explain_attempts": attempts}

    def validate(self, state: AgentState) -> dict[str, Any]:
        if state.get("explanation") is None:
            return {"errors": state.get("errors") or ["no explanation"]}
        with self.deps.engine.connect() as conn:
            findings, checks, passages, policy, _ = self._explain_inputs(conn, state)
        output = ExplainOutput.model_validate(state["explanation"])
        return {
            "errors": validate(output, findings, checks, passages, self._allowed(policy, findings))
        }

    def _after_validate(self, state: AgentState) -> str:
        if not state.get("errors"):
            return "persist_findings"
        retry = state.get("explain_attempts", 0) < EXPLAIN_ATTEMPTS and budget_left(
            state.get("calls", 0), self.deps, optional=False
        )
        return "explain_and_propose" if retry else "fallback"

    def fallback(self, state: AgentState) -> dict[str, Any]:
        with self.deps.engine.connect() as conn:
            findings, checks, _passages, policy, by_code = self._explain_inputs(conn, state)
        output = template(findings, checks, by_code, self._allowed(policy, findings))
        return {"explanation": output.model_dump(), "fallback": True, "errors": []}

    def persist_findings(self, state: AgentState) -> dict[str, Any]:
        run_id = _uuid(state["run_id"])
        with self.deps.engine.begin() as conn:
            status = conn.execute(
                select(InvestigationRun.status).where(InvestigationRun.id == run_id)
            ).scalar_one()
            if status != RunStatus.RUNNING:
                return {}  # finished before a crash; nothing to redo
            findings, _checks, passages, _policy, _ = self._explain_inputs(conn, state)
            output = ExplainOutput.model_validate(state["explanation"])
            notes = {n.finding: n for n in output.findings}
            check_ids = {
                check_key(o.check_code, o.subject_key): cid
                for o, cid in stored_outcomes(conn, run_id)
            }
            case_id = _uuid(state["case_id"])
            for finding in findings:
                note = notes[finding.key]
                finding_id = (
                    conn.execute(
                        insert(Finding)
                        .values(
                            run_id=run_id,
                            case_id=case_id,
                            exception_type=finding.code,
                            subject_key=finding.subject,
                            severity=SEVERITY[finding.code],
                            explanation=note.explanation,
                            grounding_status="fallback" if state.get("fallback") else "ok",
                        )
                        .on_conflict_do_nothing(
                            index_elements=["run_id", "exception_type", "subject_key"]
                        )
                        .returning(Finding.id)
                    ).scalar_one_or_none()
                    or conn.execute(
                        select(Finding.id).where(
                            Finding.run_id == run_id,
                            Finding.exception_type == finding.code,
                            Finding.subject_key == finding.subject,
                        )
                    ).scalar_one()
                )
                for key in {*finding.checks, *note.check_results}:
                    if key in check_ids:
                        conn.execute(
                            insert(FindingCheckResult)
                            .values(finding_id=finding_id, check_result_id=check_ids[key])
                            .on_conflict_do_nothing()
                        )
                for section in note.policy_sections:
                    if section in passages:
                        conn.execute(
                            insert(FindingCitation)
                            .values(
                                finding_id=finding_id,
                                policy_chunk_id=passages[section].id,
                                quoted_text=passages[section].text[:500],
                            )
                            .on_conflict_do_nothing()
                        )
            proposed = conn.execute(
                select(Proposal.id).where(
                    Proposal.run_id == run_id, Proposal.source == ProposalSource.AGENT
                )
            ).first()
            if proposed is None:
                service.record_agent_proposal(conn, run_id, ActionType(output.proposed_action))
            self._record_usage(conn, state)
            service.run_succeeded(conn, run_id, _uuid(state["parsed_document_id"]))
        return {}

    def _record_usage(self, conn: Connection, state: AgentState) -> None:
        conn.execute(
            update(InvestigationRun)
            .where(InvestigationRun.id == _uuid(state["run_id"]))
            .values(
                provider=self.deps.llm.provider.name,
                model=self.deps.llm.provider.model,
                prompt_versions=prompt_versions(),
                llm_call_count=state.get("calls", 0),
                tokens_in=state.get("tokens_in", 0),
                tokens_out=state.get("tokens_out", 0),
                quota_wait_ms=state.get("quota_wait_ms", 0),
                execution_ms=conn.execute(
                    text(
                        "SELECT (extract(epoch FROM clock_timestamp() - started_at) * 1000)::int"
                        " FROM investigation_runs WHERE id = :r"
                    ),
                    {"r": _uuid(state["run_id"])},
                ).scalar_one(),
            )
        )

    def _recorded_outcome(self, state: AgentState) -> tuple[str, str | None] | None:
        """What the database says happened; the resume payload is never trusted."""
        case_id, run_id = _uuid(state["case_id"]), _uuid(state["run_id"])
        with self.deps.engine.connect() as conn:
            latest_run = conn.execute(
                select(InvestigationRun.id)
                .where(InvestigationRun.case_id == case_id)
                .order_by(InvestigationRun.attempt.desc())
                .limit(1)
            ).scalar_one()
            if latest_run != run_id:
                return ("superseded", None)
            status = conn.execute(select(Case.status).where(Case.id == case_id)).scalar_one()
            decision = conn.execute(
                select(Decision.id, Decision.kind)
                .where(Decision.case_id == case_id)
                .order_by(Decision.case_version.desc(), Decision.created_at.desc())
                .limit(1)
            ).first()
            if decision is None:
                return None
            if status == CaseStatus.APPROVED and decision.kind == DecisionKind.APPROVE:
                return ("execute", str(decision.id))
            if status == CaseStatus.CLOSED:
                return ("end", str(decision.id))  # dismissed, or already executed
        return None

    def await_decision(self, state: AgentState) -> dict[str, Any]:
        outcome = self._recorded_outcome(state)
        if outcome is None:
            interrupt({"case_id": state["case_id"], "run_id": state["run_id"]})
            outcome = self._recorded_outcome(state)
        if outcome is None:
            return {"outcome": "wait"}
        return {"outcome": outcome[0], "decision_id": outcome[1] or ""}

    def execute_action(self, state: AgentState) -> dict[str, Any]:
        decision_id = _uuid(state["decision_id"])
        with self.deps.engine.begin() as conn:
            done = conn.execute(
                select(ExecutedAction.id).where(ExecutedAction.decision_id == decision_id)
            ).first()
            if done is None:  # never twice, even if a crash repeats this node
                execute(conn, _uuid(state["case_id"]), decision_id)
        return {}

    # ------------------------------------------------------------- wiring

    def _build(self) -> StateGraph:  # type: ignore[type-arg]
        g: StateGraph = StateGraph(AgentState)  # type: ignore[type-arg]
        nodes: dict[str, Any] = {
            "load_case": self.load_case,
            "extract": self.extract,
            "resolve_records": self.resolve_records,
            "match_lines": self.match_lines,
            "run_checks": self.run_checks,
            "investigate": self.investigate,
            "explain_and_propose": self.explain_and_propose,
            "validate": self.validate,
            "fallback": self.fallback,
            "persist_findings": self.persist_findings,
            "await_decision": self.await_decision,
            "execute_action": self.execute_action,
        }
        for name, node in nodes.items():
            g.add_node(name, node)
        g.add_conditional_edges(
            START,
            lambda s: "await_decision" if s.get("kind") == RunKind.REOPEN else "load_case",
            ["load_case", "await_decision"],
        )
        for a, b in (
            ("load_case", "extract"),
            ("extract", "resolve_records"),
            ("resolve_records", "match_lines"),
            ("match_lines", "run_checks"),
            ("run_checks", "investigate"),
            ("investigate", "explain_and_propose"),
            ("explain_and_propose", "validate"),
            ("fallback", "persist_findings"),
            ("persist_findings", "await_decision"),
            ("execute_action", END),
        ):
            g.add_edge(a, b)
        g.add_conditional_edges(
            "validate",
            self._after_validate,
            ["persist_findings", "explain_and_propose", "fallback"],
        )
        g.add_conditional_edges(
            "await_decision",
            lambda s: {"execute": "execute_action", "wait": "await_decision"}.get(
                s.get("outcome", ""), END
            ),
            ["execute_action", "await_decision", END],
        )
        return g

    # ------------------------------------------------------------- jobs

    def _config(self, thread_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread_id}}

    def is_paused(self, thread_id: str) -> bool:
        snapshot = self.graph.get_state(self._config(thread_id))
        return any(t.interrupts for t in snapshot.tasks)

    def _run_thread(self, run: dict[str, Any]) -> None:
        config = self._config(run["thread_id"])
        snapshot = self.graph.get_state(config)
        if any(t.interrupts for t in snapshot.tasks):
            return  # already waiting for a decision
        if snapshot.next:  # a crash left work pending: continue from the last checkpoint
            self.graph.invoke(None, config, durability="sync")
        elif not snapshot.values:
            start: AgentState = {
                "case_id": str(run["case_id"]),
                "run_id": str(run["id"]),
                "kind": str(run["kind"]),
            }
            self.graph.invoke(start, config, durability="sync")

    def investigate_job(self, _engine: Engine, job: Job) -> None:
        """T1/T3/T15/T17 jobs: start (or continue) the run's graph on its own thread."""
        with self.deps.engine.begin() as conn:
            run_id = job.payload.get("run_id")
            if run_id is None:  # T1: the first investigation starts here (T2)
                status = conn.execute(
                    select(Case.status).where(Case.id == job.case_id)
                ).scalar_one()
                if status == CaseStatus.NEW:
                    run_id = str(service.start_investigation(conn, job.case_id))  # type: ignore[arg-type]
                else:  # T2 already committed before a crash
                    run_id = str(
                        conn.execute(
                            select(InvestigationRun.id)
                            .where(InvestigationRun.case_id == job.case_id)
                            .order_by(InvestigationRun.attempt.desc())
                            .limit(1)
                        ).scalar_one()
                    )
            run = dict(
                conn.execute(
                    select(InvestigationRun.__table__).where(InvestigationRun.id == _uuid(run_id))
                )
                .one()
                ._mapping
            )
        if run["kind"] == RunKind.RECHECK:
            self._end_superseded(run)
        started = time.monotonic()
        try:
            self._run_thread(run)
        except QuotaExhaustedError as exc:
            raise PauseJob(exc.reset_at, "LLM daily quota reached") from exc
        except (RunFailure, LlmError, VendorNotIdentifiedError) as exc:
            self._fail(run, f"{type(exc).__name__}: {exc}")
        log.info("agent.run", run_id=str(run["id"]), seconds=round(time.monotonic() - started, 2))

    def _end_superseded(self, run: dict[str, Any]) -> None:
        with self.deps.engine.connect() as conn:
            older = (
                conn.execute(
                    select(InvestigationRun.thread_id).where(
                        InvestigationRun.case_id == run["case_id"],
                        InvestigationRun.attempt < run["attempt"],
                    )
                )
                .scalars()
                .all()
            )
        for thread_id in older:
            if self.is_paused(thread_id):
                self.graph.invoke(
                    Command(resume={"superseded_by": str(run["id"])}),
                    self._config(thread_id),
                    durability="sync",
                )

    def _fail(self, run: dict[str, Any], reason: str) -> None:
        with self.deps.engine.begin() as conn:
            status = conn.execute(
                select(InvestigationRun.status).where(InvestigationRun.id == run["id"])
            ).scalar_one()
            if status == RunStatus.RUNNING:
                service.run_failed(conn, run["id"], reason)
        log.warning("agent.run_failed", run_id=str(run["id"]), reason=reason[:200])

    def resume_job(self, _engine: Engine, job: Job) -> None:
        """After approve (T12) or dismiss (T8): wake the paused graph; it re-reads the decision."""
        with self.deps.engine.connect() as conn:
            thread_id = conn.execute(
                select(InvestigationRun.thread_id).where(
                    InvestigationRun.id == _uuid(job.payload["run_id"])
                )
            ).scalar_one()
        snapshot = self.graph.get_state(self._config(thread_id))
        if not any(t.interrupts for t in snapshot.tasks):
            if snapshot.values and not snapshot.next:
                return  # the graph already finished: a repeated resume does nothing
            raise RuntimeError("the run's graph isn't waiting yet; retry later")
        self.graph.invoke(
            Command(resume={"decision_id": job.payload.get("decision_id")}),
            self._config(thread_id),
            durability="sync",
        )

    def delete_thread(self, thread_id: str) -> None:
        self.checkpointer.delete_thread(thread_id)
