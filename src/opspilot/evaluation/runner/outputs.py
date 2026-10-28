"""The common output both systems are scored on (`04-prd.md` §6.6, D7).

Each system's result for one invoice becomes a `SystemOutput`: the exceptions it
reports (type, subjects, explanation and the sources the explanation cites), what
it couldn't decide, the action it proposes, and a log of every model call it made.
The scorer reads nothing else, so both systems are scored the same way.

`sources` holds the text each citation points at: for OpsPilot the cited check
results and policy passages, for the baseline the SQL results, the document and
policy sections it cites. Grounding is then judged identically for both: every
number in an explanation must appear in its cited sources.
"""

import json
import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Connection, select

from opspilot.db.models import (
    Case,
    CheckResult,
    ExecutedAction,
    Finding,
    FindingCheckResult,
    FindingCitation,
    InvestigationRun,
    PolicyChunk,
    Proposal,
)
from opspilot.domain.enums import ActionType, CheckStatus, EvalSystem, FindingCode
from opspilot.llm.client import SCHEMA_FEEDBACK, LlmResult
from opspilot.llm.types import LlmRequest
from opspilot.truthlens.findings import EXCEPTION_FOR

# The nine exception types the labels use; uncertainty and forensics aren't exceptions.
EXCEPTION_TYPES = tuple(EXCEPTION_FOR.values())


class ReportedException(BaseModel):
    type: FindingCode
    subject_keys: list[str] = Field(default_factory=list)
    explanation: str = ""
    citations: list[str] = Field(default_factory=list)  # keys into SystemOutput.sources


class CallRecord(BaseModel):
    """One model call, as the system made it (replayed or live)."""

    purpose: str
    request_sha256: str
    schema_retry: bool  # this call sent an invalid structured reply back
    structured: bool
    function_calls: int
    invalid_tool_results: int  # tool calls answered with an error (invalid arguments)
    tokens_in: int
    tokens_out: int
    quota_wait_ms: int
    latency_ms: int
    replayed: bool


class SystemOutput(BaseModel):
    system: EvalSystem
    status: str  # how the run ended: needs_review, investigation_failed, completed, failed
    exceptions: list[ReportedException] = Field(default_factory=list)
    # Exception types the system couldn't decide (an inconclusive deciding check).
    inconclusive: list[FindingCode] = Field(default_factory=list)
    extraction_uncertain: bool = False
    proposed_action: ActionType | None = None
    sources: dict[str, str] = Field(default_factory=dict)
    calls: list[CallRecord] = Field(default_factory=list)
    # Schema failures that ended the run (no valid output after the retries).
    schema_failures: int = 0
    executed_actions: int = 0  # consequential actions that ran (OpsPilot: should be 0)
    failure: str | None = None


def call_record(request: LlmRequest, result: LlmResult) -> CallRecord:
    last = request.messages[-1] if request.messages else None
    invalid = sum(
        1
        for r in (last.function_results if last else ())
        if isinstance(r.response, dict) and "error" in r.response
    )
    return CallRecord(
        purpose=request.purpose,
        request_sha256=result.request_sha256,
        schema_retry=bool(last and last.text and last.text.startswith(SCHEMA_FEEDBACK)),
        structured=request.response_schema is not None,
        function_calls=len(result.response.function_calls),
        invalid_tool_results=invalid,
        tokens_in=result.response.tokens_in,
        tokens_out=result.response.tokens_out,
        quota_wait_ms=result.quota_wait_ms,
        latency_ms=result.latency_ms,
        replayed=result.replayed,
    )


def check_source(check: Any) -> str:
    """The same evidence text the agent's grounding check uses (`agent/explain.py`)."""
    return json.dumps([check.compared, check.tolerance, check.subject_key], default=str)


def opspilot_output(conn: Connection, case_id: uuid.UUID, calls: list[CallRecord]) -> SystemOutput:
    """Read what OpsPilot concluded for one evaluation case from what it wrote."""
    case = conn.execute(select(Case).where(Case.id == case_id)).scalar_one()
    run = conn.execute(
        select(InvestigationRun)
        .where(InvestigationRun.case_id == case_id)
        .order_by(InvestigationRun.attempt.desc())
        .limit(1)
    ).scalar_one_or_none()
    executed = len(
        conn.execute(select(ExecutedAction.id).where(ExecutedAction.case_id == case_id)).all()
    )
    base: dict[str, Any] = {
        "system": EvalSystem.OPSPILOT,
        "status": str(case.status),
        "calls": calls,
        "executed_actions": executed,
        "schema_failures": int(
            bool(run and run.failure_reason and "StructuredOutputError" in run.failure_reason)
        ),
        "failure": run.failure_reason if run else "no run",
    }
    if run is None or case.evidence_run_id is None:
        return SystemOutput(**base)
    run_id = case.evidence_run_id
    checks = conn.execute(select(CheckResult).where(CheckResult.run_id == run_id)).scalars().all()
    by_id = {c.id: c for c in checks}
    sources: dict[str, str] = {}
    exceptions = []
    findings = conn.execute(
        select(Finding)
        .where(Finding.run_id == run_id)
        .order_by(Finding.exception_type, Finding.subject_key)
    ).scalars()
    uncertain = False
    for finding in findings:
        if finding.exception_type == FindingCode.EXTRACTION_UNCERTAIN:
            uncertain = True
            continue
        if finding.exception_type not in EXCEPTION_TYPES:
            continue  # forensics signals are information, not exceptions
        citations = []
        for (check_id,) in conn.execute(
            select(FindingCheckResult.check_result_id).where(
                FindingCheckResult.finding_id == finding.id
            )
        ):
            check = by_id[check_id]
            key = f"check:{check.check_code}@{check.subject_key}"
            sources[key] = check_source(check)
            citations.append(key)
        for section, text in conn.execute(
            select(PolicyChunk.section_id, PolicyChunk.text)
            .join(FindingCitation, FindingCitation.policy_chunk_id == PolicyChunk.id)
            .where(FindingCitation.finding_id == finding.id)
        ):
            key = f"policy:{section}"
            sources[key] = f"{section} {text}"
            citations.append(key)
        exceptions.append(
            ReportedException(
                type=finding.exception_type,
                subject_keys=[finding.subject_key],
                explanation=finding.explanation,
                citations=sorted(set(citations)),
            )
        )
    inconclusive = sorted(
        {EXCEPTION_FOR[c.check_code] for c in checks if c.status == CheckStatus.INCONCLUSIVE}
    )
    action = conn.execute(
        select(Proposal.action)
        .where(Proposal.case_id == case_id, Proposal.run_id == run_id)
        .order_by(Proposal.created_at.desc())
        .limit(1)
    ).scalar()
    return SystemOutput(
        **base | {"failure": run.failure_reason},
        exceptions=exceptions,
        inconclusive=inconclusive,
        extraction_uncertain=uncertain,
        proposed_action=action,
        sources=sources,
    )
