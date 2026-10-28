"""Phases 1 and 2 of an evaluation (`06-architecture.md` §4, step 7): preprocess, then infer.

Inference runs under the app role, which can't read labels (a column grant), and reads
dataset items without them. Each item's result is one `eval_item_results` row, written
once: a run stopped at the quota limit resumes from the next unfinished item, and an
OpsPilot case interrupted mid-graph continues from its checkpoint, so a resumed run gives
the same results as an uninterrupted one. Nothing here reads labels or scores anything.
"""

import datetime as dt
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, insert, select, update

from opspilot.agent.deps import MAX_LLM_CALLS, prompt_versions
from opspilot.agent.graph import Agent
from opspilot.agent.runtime import git_sha
from opspilot.agent.tools import MAX_TOOL_CALLS
from opspilot.cases import service
from opspilot.db.models import (
    Case,
    Dataset,
    DatasetItem,
    EvalItemResult,
    EvalRun,
    InvestigationRun,
    Invoice,
    ParsedDocument,
)
from opspilot.domain.enums import DatasetSplit, EvalRunStatus, EvalSystem, JobType
from opspilot.evaluation.baseline.agent import Baseline
from opspilot.evaluation.baseline.agent import prompt_versions as baseline_prompts
from opspilot.evaluation.runner.outputs import (
    CallRecord,
    SystemOutput,
    call_record,
    opspilot_output,
)
from opspilot.jobs.queue import Job
from opspilot.jobs.worker import PauseJob
from opspilot.llm.client import LlmClient, assert_single_provider
from opspilot.llm.errors import QuotaExhaustedError
from opspilot.policy.retrieve import active_version_id

FROZEN_PROMPTS = Path(__file__).parents[1] / "frozen_prompts.json"


class EvaluationError(RuntimeError):
    pass


def all_prompt_versions() -> dict[str, str]:
    return prompt_versions() | baseline_prompts()


def freeze_prompts(path: Path = FROZEN_PROMPTS) -> dict[str, str]:
    """Record the prompts tuned on the development set; the final set runs only with these."""
    versions = all_prompt_versions()
    path.write_text(json.dumps(versions, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return versions


def _require_frozen_prompts(path: Path) -> None:
    if not path.exists():
        raise EvaluationError("prompts aren't frozen: run `opspilot eval freeze-prompts` first")
    frozen = json.loads(path.read_text(encoding="utf-8"))
    if frozen != all_prompt_versions():
        changed = sorted(
            k
            for k in {*frozen, *all_prompt_versions()}
            if frozen.get(k) != all_prompt_versions().get(k)
        )
        raise EvaluationError(f"prompts changed since they were frozen: {changed}")


@dataclass(frozen=True)
class Subset:
    """A pre-declared list of items (chosen by the scorer from labels; inference sees IDs only)."""

    name: str
    item_ids: tuple[uuid.UUID, ...]
    sha256: str

    @classmethod
    def read(cls, path: Path) -> "Subset":
        raw = path.read_bytes()
        data = json.loads(raw)
        return cls(
            data["name"],
            tuple(uuid.UUID(i) for i in data["item_ids"]),
            hashlib.sha256(raw).hexdigest(),
        )


@dataclass(frozen=True)
class Plan:
    system: EvalSystem
    dataset: str
    version: str
    llm_mode: str
    subset: Subset | None = None
    repeat_index: int = 0
    final: bool = False  # the frozen final set: allowed only with frozen prompts
    frozen_prompts: Path = FROZEN_PROMPTS


@dataclass
class Progress:
    eval_run_id: uuid.UUID
    done: int = 0
    total: int = 0
    stopped_until: dt.datetime | None = None  # set when the quota stopped the run
    errors: list[str] = field(default_factory=list)


def _dataset(engine: Engine, plan: Plan) -> Any:
    with engine.connect() as conn:
        dataset = conn.execute(
            select(Dataset.id, Dataset.split, Dataset.frozen_at).where(
                Dataset.name == plan.dataset, Dataset.version == plan.version
            )
        ).one_or_none()
    if dataset is None:
        raise EvaluationError(f"no dataset {plan.dataset} {plan.version}")
    if dataset.split == DatasetSplit.FINAL:
        if not plan.final:
            raise EvaluationError(
                "the final set is held out: pass --final for the final evaluation"
            )
        if dataset.frozen_at is None:
            raise EvaluationError("the final set must be frozen before it is evaluated")
        _require_frozen_prompts(plan.frozen_prompts)
    return dataset


def items(engine: Engine, dataset_id: uuid.UUID, subset: Subset | None) -> list[Any]:
    """The dataset's invoices in a fixed order, without labels (the app role can't read them)."""
    query = (
        select(DatasetItem.id, DatasetItem.invoice_id, Invoice.document_id)
        .join(Invoice, Invoice.id == DatasetItem.invoice_id)
        .where(DatasetItem.dataset_id == dataset_id)
        .order_by(Invoice.received_at, Invoice.id)
    )
    if subset is not None:
        query = query.where(DatasetItem.id.in_(subset.item_ids))
    with engine.connect() as conn:
        rows = conn.execute(query).all()
    if subset is not None and len(rows) != len(subset.item_ids):
        raise EvaluationError(f"subset {subset.name} names items outside this dataset")
    return list(rows)


def preprocess(
    engine: Engine, parse: Callable[[uuid.UUID], bool], dataset: str, version: str
) -> tuple[int, int]:
    """Phase 1: the shared non-LLM parse for every item, so both systems read identical text."""
    with engine.connect() as conn:
        dataset_id = conn.execute(
            select(Dataset.id).where(Dataset.name == dataset, Dataset.version == version)
        ).scalar_one()
    rows = items(engine, dataset_id, None)
    created = sum(1 for row in rows if parse(row.document_id))
    return created, len(rows)


def start_run(
    engine: Engine, llm: LlmClient, plan: Plan, resume: uuid.UUID | None = None
) -> uuid.UUID:
    dataset = _dataset(engine, plan)
    with engine.begin() as conn:
        if resume is not None:
            run = conn.execute(select(EvalRun).where(EvalRun.id == resume)).scalar_one()
            if (run.system, run.dataset_id) != (plan.system, dataset.id):
                raise EvaluationError("that run is for another system or dataset")
            if run.config.get("llm_mode") != plan.llm_mode or run.model != llm.provider.model:
                raise EvaluationError(
                    "a run resumes with the same LLM mode and model it started with"
                )
            conn.execute(
                update(EvalRun).where(EvalRun.id == resume).values(status=EvalRunStatus.RUNNING)
            )
            return resume
        run_id = uuid.uuid4()
        conn.execute(
            insert(EvalRun).values(
                id=run_id,
                system=plan.system,
                dataset_id=dataset.id,
                subset=plan.subset.name if plan.subset else None,
                repeat_index=plan.repeat_index,
                provider=llm.provider.name,
                model=llm.provider.model,
                prompt_versions=all_prompt_versions(),
                policy_version_id=active_version_id(engine),
                git_sha=git_sha(),
                status=EvalRunStatus.RUNNING,
                config={
                    "llm_mode": plan.llm_mode,
                    "max_llm_calls": MAX_LLM_CALLS,
                    "max_tool_calls": MAX_TOOL_CALLS,
                    "subset_sha256": plan.subset.sha256 if plan.subset else None,
                    "final": plan.final,
                },
            )
        )
    return run_id


@dataclass
class Systems:
    """The two systems, sharing one observed LLM client (same model and settings)."""

    llm: LlmClient
    agent: Agent | None = None
    baseline: Baseline | None = None
    calls: list[CallRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.llm.observer = lambda request, result: self.calls.append(call_record(request, result))


def _opspilot_item(
    systems: Systems, engine: Engine, eval_run_id: uuid.UUID, row: Any
) -> tuple[SystemOutput, int, int]:
    if systems.agent is None:
        raise EvaluationError("the OpsPilot agent isn't set up for this run")
    with engine.begin() as conn:
        case_id = conn.execute(
            select(Case.id).where(
                Case.eval_run_id == eval_run_id, Case.invoice_id == row.invoice_id
            )
        ).scalar()
        if case_id is None:  # a resumed item continues its case and thread
            case_id = service.create_eval_case(conn, eval_run_id, row.invoice_id)
    systems.agent.investigate_job(engine, Job(uuid.uuid4(), JobType.INVESTIGATE, case_id, {}, 1, 1))
    with engine.connect() as conn:
        output = opspilot_output(conn, case_id, list(systems.calls))
        run = conn.execute(
            select(InvestigationRun.execution_ms, InvestigationRun.quota_wait_ms, Case.created_at)
            .join(Case, Case.id == InvestigationRun.case_id)
            .where(InvestigationRun.case_id == case_id)
            .order_by(InvestigationRun.attempt.desc())
            .limit(1)
        ).one()
    end_to_end = int((dt.datetime.now(dt.UTC) - run.created_at).total_seconds() * 1000)
    return output, run.execution_ms or 0, end_to_end


def _baseline_item(
    systems: Systems, engine: Engine, eval_run_id: uuid.UUID, dataset_id: uuid.UUID, row: Any
) -> tuple[SystemOutput, int, int]:
    if systems.baseline is None:
        raise EvaluationError("the baseline isn't set up for this run")
    with engine.connect() as conn:
        text = conn.execute(
            select(ParsedDocument.text)
            .where(ParsedDocument.document_id == row.document_id)
            .order_by(ParsedDocument.created_at.desc())
            .limit(1)
        ).scalar()
    if text is None:
        raise EvaluationError("an item has no parsed text: run `opspilot eval preprocess` first")
    started = time.monotonic()
    output = systems.baseline.run(eval_run_id, row.id, dataset_id, text, systems.calls)
    wall = int((time.monotonic() - started) * 1000)
    waited = sum(c.quota_wait_ms for c in output.calls)
    return output, max(0, wall - waited), wall


def run(
    engine: Engine,
    systems: Systems,
    plan: Plan,
    eval_run_id: uuid.UUID,
    on_item: Callable[[Progress], None] | None = None,
) -> Progress:
    """Phase 2: run one system over the plan's items, skipping those already done."""
    dataset = _dataset(engine, plan)
    rows = items(engine, dataset.id, plan.subset)
    with engine.connect() as conn:
        done = set(
            conn.execute(
                select(EvalItemResult.dataset_item_id).where(
                    EvalItemResult.eval_run_id == eval_run_id
                )
            ).scalars()
        )
    progress = Progress(eval_run_id, len(done), len(rows))
    for row in rows:
        if row.id in done:
            continue
        systems.calls.clear()
        try:
            if plan.system is EvalSystem.OPSPILOT:
                output, execution_ms, end_to_end_ms = _opspilot_item(
                    systems, engine, eval_run_id, row
                )
            else:
                output, execution_ms, end_to_end_ms = _baseline_item(
                    systems, engine, eval_run_id, dataset.id, row
                )
        except (PauseJob, QuotaExhaustedError) as exc:
            # Stop at the quota limit; the item is redone (or continued) on resume.
            until = exc.until if isinstance(exc, PauseJob) else exc.reset_at
            _set_status(engine, eval_run_id, EvalRunStatus.STOPPED)
            progress.stopped_until = until
            return progress
        if plan.llm_mode != "replay":
            assert_single_provider(
                engine, eval_run_id, systems.llm.provider.name, systems.llm.provider.model
            )
        with engine.begin() as conn:
            case_id = conn.execute(
                select(Case.id).where(
                    Case.eval_run_id == eval_run_id, Case.invoice_id == row.invoice_id
                )
            ).scalar()
            conn.execute(
                insert(EvalItemResult).values(
                    eval_run_id=eval_run_id,
                    dataset_item_id=row.id,
                    case_id=case_id,
                    output=output.model_dump(mode="json"),
                    execution_ms=execution_ms,
                    quota_wait_ms=sum(c.quota_wait_ms for c in output.calls),
                    end_to_end_ms=end_to_end_ms,
                    tokens_in=sum(c.tokens_in for c in output.calls),
                    tokens_out=sum(c.tokens_out for c in output.calls),
                    error=output.failure
                    if output.status in {"failed", "investigation_failed"}
                    else None,
                )
            )
        progress.done += 1
        if on_item:
            on_item(progress)
    _set_status(engine, eval_run_id, EvalRunStatus.COMPLETED, finished=True)
    return progress


def _set_status(
    engine: Engine, eval_run_id: uuid.UUID, status: EvalRunStatus, finished: bool = False
) -> None:
    values: dict[str, Any] = {"status": status}
    if finished:
        values["finished_at"] = dt.datetime.now(dt.UTC)
    with engine.begin() as conn:
        conn.execute(update(EvalRun).where(EvalRun.id == eval_run_id).values(**values))


def runs_for(engine: Engine, ids: Sequence[uuid.UUID]) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(select(EvalRun).where(EvalRun.id.in_(ids))).scalars())
