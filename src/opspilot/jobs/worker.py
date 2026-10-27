"""The worker loop and the cleanup job.

The worker claims only job types it has a handler for, so `investigate` and
`resume` jobs wait in the queue until the agent graph (P10) registers its
handlers. A handler runs its own transactions and must be idempotent: a job
reclaimed after a crash runs again.

Cleanup (D10, T11) runs at startup and once a day. It moves held cases to
`pending_approval` once someone can approve them, then deletes old checkpoint
threads: closed cases older than the retention period, and finished evaluation
runs. Checkpoints are execution state, not the record, so nothing an auditor
needs is lost.
"""

import datetime as dt
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import structlog
from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import SQLAlchemyError

from opspilot.audit.verify import VerifyReport
from opspilot.cases.service import recheck_held_cases
from opspilot.db.models import Case, EvalRun, InvestigationRun
from opspilot.domain.enums import CaseStatus, EvalRunStatus, JobType
from opspilot.jobs.queue import LEASE, Job, claim, complete, enqueue, fail, pause_until

log = structlog.get_logger()

Handler = Callable[[Engine, Job], None]
ThreadDeleter = Callable[[str], None]


class PauseJob(Exception):  # noqa: N818  # a signal, not an error
    """Raised by a handler to wait (e.g. for the LLM quota) without spending an attempt."""

    def __init__(self, until: dt.datetime, reason: str) -> None:
        super().__init__(reason)
        self.until = until
        self.reason = reason


def no_checkpointer(_thread_id: str) -> None:
    """Until the graph exists (P10) there are no checkpoint threads to delete."""


@dataclass(frozen=True)
class CleanupReport:
    released: int  # held cases moved to pending_approval (T11)
    threads_deleted: int
    verified: bool | None = None  # the audit check: ok, failed, or None when it didn't run


def threads_to_delete(conn: Connection, retention_days: int, now: dt.datetime) -> list[str]:
    cutoff = now - dt.timedelta(days=retention_days)
    finished = (EvalRunStatus.COMPLETED, EvalRunStatus.FAILED)
    closed = (
        select(InvestigationRun.thread_id)
        .join(Case, Case.id == InvestigationRun.case_id)
        .where(
            Case.eval_run_id.is_(None),
            Case.status == CaseStatus.CLOSED,
            Case.closed_at < cutoff,
        )
    )
    evaluated = (
        select(InvestigationRun.thread_id)
        .join(Case, Case.id == InvestigationRun.case_id)
        .join(EvalRun, EvalRun.id == Case.eval_run_id)
        .where(EvalRun.status.in_(finished))
    )
    return sorted(conn.execute(closed.union(evaluated)).scalars())


def cleanup(
    engine: Engine,
    *,
    retention_days: int,
    delete_thread: ThreadDeleter = no_checkpointer,
    now: dt.datetime | None = None,
    verify_audit: Callable[[], VerifyReport] | None = None,
) -> CleanupReport:
    """Idempotent: re-running releases nothing new and deleting a missing thread is a no-op.

    Then the daily audit verification (owner follow-up, 2026-10-26). A failed check is
    logged as an error and recorded, never hidden; if the check can't run at all, case
    pages show the last result as overdue rather than as current.
    """
    with engine.begin() as conn:
        released = recheck_held_cases(conn)
        threads = threads_to_delete(conn, retention_days, now or dt.datetime.now(dt.UTC))
    for thread_id in threads:
        delete_thread(thread_id)
    verified: bool | None = None
    if verify_audit is not None:
        try:
            verified = verify_audit().ok
        except (RuntimeError, SQLAlchemyError) as exc:
            log.error("audit.verify_not_run", error=type(exc).__name__, reason=str(exc)[:200])
        else:
            if not verified:
                log.error("audit.verify_failed")
    log.info("cleanup.done", released=len(released), threads=len(threads), verified=verified)
    return CleanupReport(len(released), len(threads), verified)


def schedule_cleanup(engine: Engine, today: dt.date) -> None:
    with engine.begin() as conn:
        enqueue(conn, JobType.CLEANUP, idempotency_key=f"cleanup:{today.isoformat()}")


class Worker:
    def __init__(
        self, engine: Engine, handlers: Mapping[JobType, Handler], lease: dt.timedelta = LEASE
    ) -> None:
        self.engine = engine
        self.handlers = dict(handlers)
        self.lease = lease

    def run_once(self) -> Job | None:
        """Claim and run one due job; None when there is nothing to do."""
        job = claim(self.engine, frozenset(self.handlers), self.lease)
        if job is None:
            return None
        if job.attempts > job.max_attempts:  # reclaimed after crashing on its last attempt
            with self.engine.begin() as conn:
                fail(conn, job, "lease expired on the last attempt")
            return job
        log.info("job.started", job_id=str(job.id), type=job.type, attempt=job.attempts)
        try:
            self.handlers[job.type](self.engine, job)
        except PauseJob as pause:
            with self.engine.begin() as conn:
                pause_until(conn, job, pause.until, pause.reason)
            log.info("job.paused", job_id=str(job.id), until=pause.until.isoformat())
        except Exception as exc:  # noqa: BLE001  # recorded on the job and retried with backoff
            with self.engine.begin() as conn:
                status = fail(conn, job, f"{type(exc).__name__}: {exc}")
            log.warning("job.failed", job_id=str(job.id), error=type(exc).__name__, status=status)
        else:
            with self.engine.begin() as conn:
                complete(conn, job)
            log.info("job.done", job_id=str(job.id))
        return job

    def run(self, stop: threading.Event, poll_seconds: float = 2.0) -> None:
        scheduled: dt.date | None = None
        while not stop.is_set():
            today = dt.datetime.now(dt.UTC).date()
            if JobType.CLEANUP in self.handlers and scheduled != today:
                schedule_cleanup(self.engine, today)
                scheduled = today
            if self.run_once() is None:
                stop.wait(poll_seconds)
