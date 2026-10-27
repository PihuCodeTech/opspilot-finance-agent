"""In-process sliding-window limits for logins and uploads (`06-api.md` §8).

One app instance in the MVP, so memory is enough; a restart forgets the
windows, which only ever makes the limit more lenient for a moment.
"""

import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass

from opspilot.cases.errors import ServiceError


class RateLimitedError(ServiceError):
    status = 429
    code = "rate_limited"

    def __init__(self, retry_after: int) -> None:
        super().__init__("Too many requests. Try again shortly.")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Limit:
    count: int
    seconds: int


MINUTE = 60
DAY = 24 * 60 * 60
LOGIN_PER_USER_AND_IP = Limit(5, MINUTE)
LOGIN_PER_IP = Limit(20, MINUTE)
UPLOADS_PER_MINUTE = Limit(10, MINUTE)
UPLOADS_PER_DAY = Limit(100, DAY)


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: defaultdict[tuple[str, ...], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, *checks: tuple[tuple[str, ...], Limit]) -> None:
        """Count one attempt against every key, or raise if any key is at its limit.

        Nothing is counted when one check refuses, so a refused attempt never
        extends the wait.
        """
        now = self._clock()
        with self._lock:
            waits = []
            for key, limit in checks:
                hits = self._hits[key]
                while hits and hits[0] <= now - limit.seconds:
                    hits.popleft()
                if len(hits) >= limit.count:
                    waits.append(hits[0] + limit.seconds - now)
            if waits:
                raise RateLimitedError(max(1, math.ceil(max(waits))))
            for key, _limit in checks:
                self._hits[key].append(now)
