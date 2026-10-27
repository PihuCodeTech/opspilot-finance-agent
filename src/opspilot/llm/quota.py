"""Free-tier quota enforcement (`rules.md` §2 rule 10).

Every live request first takes one slot in the current minute window and the
current day window, atomically in Postgres, so the configured limits hold
across restarts and across several workers. A full minute means waiting for
the next one (recorded as quota wait). A full day raises
`QuotaExhaustedError` with the reset time: callers pause until then; nothing
retries in a loop. The day resets at midnight Pacific time, as Google's
free-tier daily limits do.
"""

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, text

from opspilot.llm.errors import QuotaExhaustedError

PACIFIC = ZoneInfo("America/Los_Angeles")
_TAKE = text(
    """
    INSERT INTO llm_quota_windows (provider, model, window_kind, window_start, requests)
    VALUES (:provider, :model, :kind, :start, 1)
    ON CONFLICT (provider, model, window_kind, window_start)
    DO UPDATE SET requests = llm_quota_windows.requests + 1
      WHERE llm_quota_windows.requests < :limit
    RETURNING requests
    """
)


def day_window(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    local = now.astimezone(PACIFIC)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)  # zoneinfo arithmetic is wall-clock: next local midnight
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


def minute_window(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    start = now.astimezone(dt.UTC).replace(second=0, microsecond=0)
    return start, start + dt.timedelta(minutes=1)


@dataclass
class QuotaGate:
    engine: Engine
    provider: str
    model: str
    requests_per_minute: int
    requests_per_day: int
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)
    sleep: Callable[[float], None] = time.sleep

    def _take(self, conn: Connection, kind: str, start: dt.datetime, limit: int) -> bool:
        row = conn.execute(
            _TAKE,
            {
                "provider": self.provider,
                "model": self.model,
                "kind": kind,
                "start": start,
                "limit": limit,
            },
        ).first()
        return row is not None

    def acquire(self) -> int:
        """Take one request slot; returns milliseconds spent waiting for a free minute."""
        waited_ms = 0
        while True:
            now = self.clock()
            minute_start, minute_end = minute_window(now)
            day_start, day_end = day_window(now)
            with self.engine.connect() as conn:
                transaction = conn.begin()
                if not self._take(conn, "day", day_start, self.requests_per_day):
                    transaction.rollback()
                    raise QuotaExhaustedError(day_end)
                if self._take(conn, "minute", minute_start, self.requests_per_minute):
                    transaction.commit()
                    return waited_ms
                transaction.rollback()  # minute full: give back the day slot, wait, try again
            pause = max(0.05, (minute_end - now).total_seconds())
            self.sleep(pause)
            waited_ms += int(pause * 1000)

    def used_today(self) -> int:
        start, _ = day_window(self.clock())
        with self.engine.connect() as conn:
            value = conn.execute(
                text(
                    "SELECT requests FROM llm_quota_windows WHERE provider = :p AND model = :m"
                    " AND window_kind = 'day' AND window_start = :s"
                ),
                {"p": self.provider, "m": self.model, "s": start},
            ).scalar()
        return int(value or 0)
