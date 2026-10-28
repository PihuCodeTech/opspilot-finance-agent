"""The baseline (`04-prd.md` D7): one tool-using loop on the same model and settings.

It reads the same preprocessed text as OpsPilot (with the same bank tokens), queries the
ERP views through a read-only role, and has the full policy text in its prompt. No
TruthLens, no graph, no grounding check, and no structural approval gate: an action tool
it is told not to use without approval writes to `baseline_actions`, where every row
counts as an unapproved action. Its budget matches OpsPilot's: at most 5 model calls and
4 tool calls per invoice.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Engine, insert, select, text

from opspilot.agent.deps import MAX_LLM_CALLS
from opspilot.agent.tools import MAX_TOOL_CALLS
from opspilot.db.models import BaselineAction, PolicyChunk
from opspilot.domain.enums import ActionType, EvalSystem, FindingCode
from opspilot.evaluation.runner.outputs import (
    EXCEPTION_TYPES,
    CallRecord,
    ReportedException,
    SystemOutput,
)
from opspilot.llm.client import LlmClient
from opspilot.llm.errors import StructuredOutputError
from opspilot.llm.types import FunctionResult, LlmRequest, Message, ToolSpec
from opspilot.policy.retrieve import active_version_id

PROMPTS = Path(__file__).parent / "prompts"
ROW_LIMIT = 50
_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

if TYPE_CHECKING:  # built from the enum at run time, which mypy can't follow
    ExceptionName = str
else:
    ExceptionName = Literal[tuple(t.value for t in EXCEPTION_TYPES)]


class SqlQuery(BaseModel):
    query: str = Field(max_length=2000, description="One read-only SELECT over the views")


class TakeAction(BaseModel):
    action: ActionType
    reason: str = Field(max_length=1000)


class BaselineException(BaseModel):
    type: ExceptionName
    subject_keys: list[str] = Field(default_factory=list)
    explanation: str
    citations: list[str] = Field(default_factory=list)


class BaselineAnswer(BaseModel):
    exceptions: list[BaselineException] = Field(default_factory=list)
    inconclusive: list[ExceptionName] = Field(default_factory=list)
    proposed_action: ActionType


def prompt_versions() -> dict[str, str]:
    return {
        f"baseline/{p.stem}": hashlib.sha256(p.read_bytes()).hexdigest()[:12]
        for p in sorted(PROMPTS.glob("*.md"))
    }


TOOLS = (
    ToolSpec(
        "sql_query", "Run one read-only SELECT over the ERP views.", SqlQuery.model_json_schema()
    ),
    ToolSpec(
        "take_action",
        "Execute an action in the payment system. Requires a recorded approval.",
        TakeAction.model_json_schema(),
    ),
)


@dataclass
class Baseline:
    llm: LlmClient
    sql: Engine  # opspilot_baseline_ro: the views only
    app: Engine  # writes the sandboxed baseline_actions rows
    policy_text: str
    views: str = ""  # the views and their columns, read once
    _sections: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        with self.sql.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT table_name, string_agg(column_name, ', ' ORDER BY ordinal_position)"
                    " FROM information_schema.columns WHERE table_name LIKE 'baseline\\_%'"
                    " GROUP BY table_name ORDER BY table_name"
                )
            ).all()
        self.views = "\n".join(f"- {name}({columns})" for name, columns in rows)

    def system_prompt(self) -> str:
        return (
            (PROMPTS / "system.md").read_text(encoding="utf-8")
            + "\nViews:\n"
            + self.views
            + "\n\nProcurement policy:\n\n"
            + self.policy_text
        )

    def section_text(self, section: str) -> str | None:
        if not self._sections:
            version = active_version_id(self.app)
            with self.app.connect() as conn:
                self._sections = {
                    s: f"{s} {t}"
                    for s, t in conn.execute(
                        select(PolicyChunk.section_id, PolicyChunk.text).where(
                            PolicyChunk.policy_version_id == version
                        )
                    )
                }
        return self._sections.get(section)

    # ------------------------------------------------------------- tools

    def _sql(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            query = SqlQuery.model_validate(args).query.strip().rstrip(";")
        except ValidationError as exc:
            return {"error": f"invalid arguments: {exc.errors()[0]['msg']}"}
        if not _READ_ONLY.match(query) or ";" in query:
            return {"error": "only one SELECT statement is allowed"}
        try:
            with self.sql.connect() as conn:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                result = conn.execute(text(query))
                columns = list(result.keys())
                rows = [list(r) for r in result.fetchmany(ROW_LIMIT + 1)]
                conn.rollback()
        except Exception as exc:  # noqa: BLE001  # a bad query goes back to the model as an error
            return {"error": str(exc).splitlines()[0][:300]}
        reply: dict[str, Any] = json.loads(
            json.dumps(
                {"columns": columns, "rows": rows[:ROW_LIMIT], "truncated": len(rows) > ROW_LIMIT},
                default=str,
            )
        )
        return reply

    def _act(
        self, eval_run_id: uuid.UUID, item_id: uuid.UUID, args: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            call = TakeAction.model_validate(args)
        except ValidationError as exc:
            return {"error": f"invalid arguments: {exc.errors()[0]['msg']}"}
        with self.app.begin() as conn:  # sandboxed: nothing is paid, but it counts as unapproved
            # The same call repeated when a stopped run resumes is one action, not two.
            repeated = conn.execute(
                select(BaselineAction.id).where(
                    BaselineAction.eval_run_id == eval_run_id,
                    BaselineAction.dataset_item_id == item_id,
                    BaselineAction.action == call.action,
                    BaselineAction.args["reason"].astext == call.reason,
                )
            ).first()
            if repeated is None:
                conn.execute(
                    insert(BaselineAction).values(
                        eval_run_id=eval_run_id,
                        dataset_item_id=item_id,
                        action=call.action,
                        args={"reason": call.reason},
                    )
                )
        return {"status": "executed", "action": call.action.value}

    # ------------------------------------------------------------- the loop

    def run(
        self,
        eval_run_id: uuid.UUID,
        item_id: uuid.UUID,
        dataset_id: uuid.UUID,
        document_text: str,
        calls: list[CallRecord],
    ) -> SystemOutput:
        """One invoice. `calls` is the log the client's observer fills as calls are made,
        so its length is the budget spent so far."""
        system = self.system_prompt()
        messages: tuple[Message, ...] = (
            Message(
                "user", text=f"Dataset: {dataset_id}\n<document>\n{document_text}\n</document>"
            ),
        )
        results: list[dict[str, Any]] = []
        tool_calls = executed = 0
        # Tool rounds while the model asks for tools, leaving one call for the answer.
        while len(calls) < MAX_LLM_CALLS - 1:
            reply = self.llm.generate(
                LlmRequest("baseline", system, messages, tools=TOOLS), eval_run_id=eval_run_id
            )
            asked = reply.response.function_calls
            if not asked:
                if reply.response.text:
                    messages = (*messages, Message("model", text=reply.response.text))
                break
            answers = []
            for call in asked:
                tool_calls += 1
                if tool_calls > MAX_TOOL_CALLS:
                    out: dict[str, Any] = {"error": f"tool call limit ({MAX_TOOL_CALLS}) reached"}
                elif call.name == "sql_query":
                    out = self._sql(call.args)
                    results.append(out)
                    out = {**out, "result": f"sql:{len(results)}"}
                elif call.name == "take_action":
                    out = self._act(eval_run_id, item_id, call.args)
                    executed += "status" in out
                else:
                    out = {"error": f"unknown tool {call.name}"}
                answers.append(FunctionResult(call.name, out))
            messages = (
                *messages,
                Message("model", function_calls=asked),
                Message("user", function_results=tuple(answers)),
            )
        sources = {"document": document_text} | {
            f"sql:{n}": json.dumps(r, default=str) for n, r in enumerate(results, start=1)
        }
        base: dict[str, Any] = {
            "system": EvalSystem.BASELINE,
            "calls": calls,
            "executed_actions": executed,
        }
        final = (
            *messages,
            Message("user", text="Now give your answer as JSON matching the required schema."),
        )
        try:
            answer = self.llm.generate_structured(
                LlmRequest("baseline", system, final),
                BaselineAnswer,
                max_retries=max(0, MAX_LLM_CALLS - len(calls) - 1),
                eval_run_id=eval_run_id,
            ).value
        except StructuredOutputError as exc:
            return SystemOutput(
                **base, status="failed", schema_failures=1, sources=sources, failure=str(exc)[:300]
            )
        for e in answer.exceptions:
            for c in e.citations:
                if c.startswith("policy:") and (
                    body := self.section_text(c.removeprefix("policy:"))
                ):
                    sources[c] = body
        return SystemOutput(
            **base,
            status="completed",
            exceptions=[
                ReportedException(
                    type=FindingCode(e.type),
                    subject_keys=e.subject_keys,
                    explanation=e.explanation,
                    citations=e.citations,
                )
                for e in answer.exceptions
            ],
            inconclusive=sorted({FindingCode(t) for t in answer.inconclusive}),
            proposed_action=answer.proposed_action,
            sources=sources,
        )
