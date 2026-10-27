"""A small Postgres job queue (`06-architecture.md` §13).

`enqueue` runs in the caller's transaction, so a job exists exactly when the
state change that asked for it commits. `claim` takes one due job with
`FOR UPDATE SKIP LOCKED` and leases it. A job whose lease ran out (the worker
crashed) is due again. Handlers must be idempotent, because a reclaimed job
runs again.
"""

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, text

from opspilot.domain.digest import canonical_json
from opspilot.domain.enums import JobStatus, JobType

LEASE = dt.timedelta(minutes=10)
BACKOFF_BASE = dt.timedelta(seconds=30)
BACKOFF_MAX = dt.timedelta(hours=1)


@dataclass(frozen=True)
class Job:
    id: uuid.UUID
    type: JobType
    case_id: uuid.UUID | None
    payload: dict[str, Any]
    attempts: int  # including this one
    max_attempts: int


def enqueue(
    conn: Connection,
    job_type: JobType,
    *,
    case_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    run_after: dt.datetime | None = None,
) -> uuid.UUID | None:
    """Returns the new job's ID, or None if a job with this idempotency key already exists."""
    return conn.execute(
        text(
            "INSERT INTO jobs (type, case_id, payload, idempotency_key, run_after)"
            " VALUES (CAST(:t AS job_type), :c, CAST(:p AS jsonb), :k, coalesce(:r, now()))"
            " ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"
        ),
        {
            "t": job_type.value,
            "c": case_id,
            "p": _json(payload or {}),
            "k": idempotency_key,
            "r": run_after,
        },
    ).scalar_one_or_none()


def _json(value: dict[str, Any]) -> str:
    return canonical_json(value).decode()  # no floats in payloads


def claim(engine: Engine, types: frozenset[JobType], lease: dt.timedelta = LEASE) -> Job | None:
    """Lease one due job of these types (queued and due, or running with an expired lease)."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1,"
                " leased_until = now() + :lease, updated_at = now()"
                " WHERE id = (SELECT id FROM jobs"
                "   WHERE type = ANY(CAST(:types AS job_type[])) AND ("
                "     (status = 'queued' AND run_after <= now())"
                "     OR (status = 'running' AND leased_until < now()))"
                "   ORDER BY run_after, created_at FOR UPDATE SKIP LOCKED LIMIT 1)"
                " RETURNING id, type, case_id, payload, attempts, max_attempts"
            ),
            {"lease": lease, "types": [t.value for t in types]},
        ).first()
    if row is None:
        return None
    return Job(row.id, JobType(row.type), row.case_id, row.payload, row.attempts, row.max_attempts)


def complete(conn: Connection, job: Job) -> None:
    conn.execute(
        text(
            "UPDATE jobs SET status = 'done', leased_until = NULL, last_error = NULL,"
            " updated_at = now() WHERE id = :id"
        ),
        {"id": job.id},
    )


def backoff(attempts: int) -> dt.timedelta:
    factor: int = 2 ** min(max(attempts - 1, 0), 16)  # capped: the maximum is reached long before
    return min(BACKOFF_BASE * factor, BACKOFF_MAX)


def fail(conn: Connection, job: Job, error: str) -> JobStatus:
    """Retry later with exponential backoff, or fail for good after `max_attempts`."""
    final = job.attempts >= job.max_attempts
    conn.execute(
        text(
            "UPDATE jobs SET status = CAST(:s AS job_status), leased_until = NULL,"
            " last_error = :e, run_after = now() + :wait, updated_at = now() WHERE id = :id"
        ),
        {
            "s": (JobStatus.FAILED if final else JobStatus.QUEUED).value,
            "e": error[:2000],
            "wait": backoff(job.attempts),
            "id": job.id,
        },
    )
    return JobStatus.FAILED if final else JobStatus.QUEUED


def pause_until(conn: Connection, job: Job, until: dt.datetime, reason: str) -> None:
    """Re-queue without spending an attempt (a provider quota pause is not a failure)."""
    conn.execute(
        text(
            "UPDATE jobs SET status = 'queued', attempts = attempts - 1, leased_until = NULL,"
            " last_error = :e, run_after = :u, updated_at = now() WHERE id = :id"
        ),
        {"e": reason, "u": until, "id": job.id},
    )
