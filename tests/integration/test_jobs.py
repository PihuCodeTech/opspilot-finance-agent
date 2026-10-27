"""The job queue and worker loop against Postgres (committed rows; each test cleans up)."""

import datetime as dt
import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from opspilot.audit.verify import VerifyReport
from opspilot.domain.enums import JobStatus, JobType
from opspilot.jobs.queue import Job, backoff, claim, enqueue, fail
from opspilot.jobs.worker import PauseJob, Worker, cleanup

pytestmark = pytest.mark.integration

CLEANUP = frozenset({JobType.CLEANUP})


@pytest.fixture
def engine(owner_engine: Engine) -> Iterator[Engine]:
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs"))
    yield owner_engine
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs"))


def add(engine: Engine, key: str | None = None, **kwargs: object) -> uuid.UUID | None:
    with engine.begin() as conn:
        return enqueue(conn, JobType.CLEANUP, idempotency_key=key, **kwargs)  # type: ignore[arg-type]


def row(engine: Engine, job_id: uuid.UUID | None) -> dict[str, object]:
    assert job_id is not None
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT status::text, attempts, run_after > now() AS later, last_error"
                    " FROM jobs WHERE id = :i"
                ),
                {"i": job_id},
            )
            .mappings()
            .one()
        )


def test_enqueue_is_idempotent_by_key(engine: Engine) -> None:
    first = add(engine, "cleanup:2026-10-25")
    assert first is not None
    assert add(engine, "cleanup:2026-10-25") is None
    assert add(engine) is not None and add(engine) is not None  # no key: always new


def test_only_due_jobs_of_handled_types_are_claimed(engine: Engine) -> None:
    add(engine, run_after=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
    with engine.begin() as conn:
        enqueue(conn, JobType.RESUME, payload={})
    assert claim(engine, CLEANUP) is None
    due = add(engine)
    job = claim(engine, CLEANUP)
    assert job is not None and job.id == due and job.attempts == 1
    assert claim(engine, CLEANUP) is None  # leased


def test_concurrent_claims_never_share_a_job(engine: Engine) -> None:
    ids = {add(engine) for _ in range(8)}
    claimed: list[uuid.UUID] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def take() -> None:
        barrier.wait()
        while (job := claim(engine, CLEANUP)) is not None:
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=take) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(map(str, claimed)) == sorted(map(str, ids))  # each exactly once


def test_a_crashed_workers_job_is_reclaimed_after_its_lease(engine: Engine) -> None:
    job_id = add(engine)
    crashed = claim(engine, CLEANUP, lease=dt.timedelta(seconds=-1))  # lease already over
    assert crashed is not None and crashed.id == job_id
    again = claim(engine, CLEANUP)
    assert again is not None and again.id == job_id and again.attempts == 2


def test_failures_back_off_then_fail_for_good(engine: Engine) -> None:
    job_id = add(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET max_attempts = 2 WHERE id = :i"), {"i": job_id})
    job = claim(engine, CLEANUP)
    assert job is not None
    with engine.begin() as conn:
        assert fail(conn, job, "boom") is JobStatus.QUEUED
    state = row(engine, job_id)
    assert state["status"] == "queued" and state["later"] and state["last_error"] == "boom"
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET run_after = now() WHERE id = :i"), {"i": job_id})
    job = claim(engine, CLEANUP)
    assert job is not None and job.attempts == 2
    with engine.begin() as conn:
        assert fail(conn, job, "boom again") is JobStatus.FAILED
    assert row(engine, job_id)["status"] == "failed"
    assert backoff(1) < backoff(2) < backoff(3) and backoff(50) == dt.timedelta(hours=1)


def test_worker_runs_pauses_and_records_failures(engine: Engine) -> None:
    seen: list[Job] = []

    def ok(_engine: Engine, job: Job) -> None:
        seen.append(job)

    done = add(engine)
    assert Worker(engine, {JobType.CLEANUP: ok}).run_once() is not None
    assert row(engine, done)["status"] == "done" and len(seen) == 1

    def boom(_engine: Engine, _job: Job) -> None:
        raise RuntimeError("provider unavailable")

    failed = add(engine)
    Worker(engine, {JobType.CLEANUP: boom}).run_once()
    state = row(engine, failed)
    assert (
        state["status"] == "queued" and state["last_error"] == "RuntimeError: provider unavailable"
    )
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE id = :i"), {"i": failed})

    def quota(_engine: Engine, _job: Job) -> None:
        raise PauseJob(dt.datetime.now(dt.UTC) + dt.timedelta(hours=2), "daily quota reached")

    paused = add(engine)
    Worker(engine, {JobType.CLEANUP: quota}).run_once()
    state = row(engine, paused)
    assert (state["status"], state["attempts"], state["later"]) == ("queued", 0, True)


def test_a_job_that_kept_crashing_is_not_run_again(engine: Engine) -> None:
    job_id = add(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET max_attempts = 1 WHERE id = :i"), {"i": job_id})
    assert claim(engine, CLEANUP, lease=dt.timedelta(seconds=-1)) is not None  # crashed
    ran: list[Job] = []
    Worker(engine, {JobType.CLEANUP: lambda _e, j: ran.append(j)}).run_once()
    assert not ran and row(engine, job_id)["status"] == "failed"


def test_cleanup_is_idempotent(engine: Engine) -> None:
    deleted: list[str] = []
    first = cleanup(engine, retention_days=30, delete_thread=deleted.append)
    second = cleanup(engine, retention_days=30, delete_thread=deleted.append)
    assert second.released == 0  # anything releasable went the first time
    assert first.threads_deleted == second.threads_deleted  # deleting again is harmless


def test_cleanup_runs_the_audit_check_and_never_hides_a_failure(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    ok = cleanup(engine, retention_days=30, verify_audit=VerifyReport)
    assert ok.verified is True
    broken = VerifyReport(mismatches=["run x: evidence does not match its digest"])
    failed = cleanup(engine, retention_days=30, verify_audit=lambda: broken)
    assert failed.verified is False
    assert "audit.verify_failed" in capsys.readouterr().out

    def unavailable() -> VerifyReport:
        raise RuntimeError("DATABASE_URL_VERIFIER is not set")

    skipped = cleanup(engine, retention_days=30, verify_audit=unavailable)
    assert skipped.verified is None  # not run is not a pass
    assert "audit.verify_not_run" in capsys.readouterr().out
