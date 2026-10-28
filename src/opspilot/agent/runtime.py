"""Building the production agent, and running a dataset slice through it.

The worker and the CLI build the agent the same way: the app database role,
the configured LLM client (replay unless the owner switches to live), hybrid
policy retrieval with the local embedding model, and Docling preprocessing.

`run_slice` drives the graph item by item for an evaluation run, and reports
per invoice its LLM calls, tokens, quota wait and time. That report is the P10
record of a live dev slice. It never reads labels; scoring is P14.
"""

import datetime as dt
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import Engine, insert, select, text, update

from opspilot.agent.deps import AgentDeps, PassageRef, prompt_versions
from opspilot.agent.graph import Agent
from opspilot.cases import service
from opspilot.config import LlmMode, Settings
from opspilot.db.models import Case, Dataset, EvalRun, InvestigationRun, Invoice
from opspilot.domain.enums import EvalRunStatus, EvalSystem, JobType
from opspilot.extraction.preprocess import Preprocessor
from opspilot.jobs.queue import Job
from opspilot.llm.factory import build_client
from opspilot.policy.retrieve import Embedder, active_version_id, retrieve

# Record mode also writes each live reply here, so tests can replay it offline.
RECORDINGS = Path("tests/fixtures/llm")


def checkpoint_saver(database_url: str) -> PostgresSaver:
    """A checkpointer alone (no models loaded), for `opspilot cleanup`."""
    return Agent.saver(database_url)


def build_agent(settings: Settings, engine: Engine) -> Agent:
    embedder = Embedder()

    def search(query: str) -> list[PassageRef]:
        return [
            PassageRef(p.chunk_id, p.section_id, p.title, p.text)
            for p in retrieve(engine, embedder, query)
        ]

    deps = AgentDeps(
        engine=engine,
        llm=build_client(settings, RECORDINGS if settings.llm_mode is LlmMode.RECORD else None),
        policy_search=search,
        preprocessor=Preprocessor(
            settings.bank_hmac_key.get_secret_value(), settings.docling_artifacts_path
        ),
        document_store=Path(settings.document_store_path),
    )
    return Agent.connect(deps, settings.database_url.get_secret_value())


@dataclass(frozen=True)
class ItemReport:
    invoice_id: uuid.UUID
    status: str
    llm_calls: int
    tokens_in: int
    tokens_out: int
    quota_wait_ms: int
    seconds: float
    failure: str | None


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607  # fixed command, no input
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def run_slice(
    agent: Agent, dataset_name: str, dataset_version: str, limit: int
) -> tuple[uuid.UUID, list[ItemReport]]:
    """Investigate the first `limit` invoices of a dataset as one evaluation run."""
    engine = agent.deps.engine
    with engine.begin() as conn:
        dataset = conn.execute(
            select(Dataset.id, Dataset.split).where(
                Dataset.name == dataset_name, Dataset.version == dataset_version
            )
        ).one()
        if dataset.split == "final":
            raise ValueError("the final set is held out until the final evaluation (P14)")
        eval_run_id = uuid.uuid4()
        conn.execute(
            insert(EvalRun).values(
                id=eval_run_id,
                system=EvalSystem.OPSPILOT,
                dataset_id=dataset.id,
                subset=f"first-{limit}",
                provider=agent.deps.llm.provider.name,
                model=agent.deps.llm.provider.model,
                prompt_versions=prompt_versions(),
                policy_version_id=active_version_id(engine),
                git_sha=git_sha(),
                status=EvalRunStatus.RUNNING,
            )
        )
        invoices = (
            conn.execute(
                select(Invoice.id)
                .where(Invoice.dataset_id == dataset.id)
                .order_by(Invoice.received_at, Invoice.id)
                .limit(limit)
            )
            .scalars()
            .all()
        )
    reports = []
    for invoice_id in invoices:
        with engine.begin() as conn:
            case_id = service.create_eval_case(conn, eval_run_id, invoice_id)
        started = time.monotonic()
        agent.investigate_job(engine, Job(uuid.uuid4(), JobType.INVESTIGATE, case_id, {}, 1, 1))
        reports.append(_report(engine, invoice_id, case_id, time.monotonic() - started))
    with engine.begin() as conn:
        conn.execute(
            update(EvalRun)
            .where(EvalRun.id == eval_run_id)
            .values(status=EvalRunStatus.COMPLETED, finished_at=dt.datetime.now(dt.UTC))
        )
    return eval_run_id, reports


def _report(
    engine: Engine, invoice_id: uuid.UUID, case_id: uuid.UUID, seconds: float
) -> ItemReport:
    with engine.connect() as conn:
        status = conn.execute(select(Case.status).where(Case.id == case_id)).scalar_one()
        run = conn.execute(
            select(InvestigationRun)
            .where(InvestigationRun.case_id == case_id)
            .order_by(InvestigationRun.attempt.desc())
            .limit(1)
        ).one()
        calls = conn.execute(
            text("SELECT count(*) FROM llm_calls WHERE run_id = :r"), {"r": run.id}
        ).scalar_one()
    return ItemReport(
        invoice_id,
        str(status),
        max(int(calls), run.llm_call_count),
        run.tokens_in,
        run.tokens_out,
        run.quota_wait_ms or 0,
        round(seconds, 2),
        run.failure_reason,
    )
