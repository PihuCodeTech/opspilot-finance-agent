"""Intervals and percentiles for the report (`04-prd.md` §10: every rate has a 95% CI).

Rates use the Wilson score interval (sound at 0 and 1 and for small samples). The
re-weighted precision has no closed form, so it uses a percentile bootstrap over
invoices with a fixed seed, which keeps every report byte-for-byte reproducible.
"""

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

Z95 = 1.959963984540054
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 14


@dataclass(frozen=True)
class Rate:
    hits: float
    total: float
    low: float | None
    high: float | None

    @property
    def value(self) -> float | None:
        return self.hits / self.total if self.total else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": _round(self.value),
            "hits": _round(self.hits),
            "n": _round(self.total),
            "ci95": [_round(self.low), _round(self.high)],
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def wilson(hits: int, total: int) -> Rate:
    if total == 0:
        return Rate(0, 0, None, None)
    p = hits / total
    centre = (p + Z95**2 / (2 * total)) / (1 + Z95**2 / total)
    half = (Z95 / (1 + Z95**2 / total)) * math.sqrt(p * (1 - p) / total + Z95**2 / (4 * total**2))
    return Rate(hits, total, max(0.0, centre - half), min(1.0, centre + half))


def bootstrap(items: Sequence[Any], ratio: Callable[[Sequence[Any]], tuple[float, float]]) -> Rate:
    """A ratio statistic over items with a percentile bootstrap interval."""
    hits, total = ratio(items)
    if not items or total == 0:
        return Rate(hits, total, None, None)
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311  # sampling, not security
    values = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        h, t = ratio([items[rng.randrange(len(items))] for _ in items])
        if t:
            values.append(h / t)
    values.sort()
    return Rate(hits, total, values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1])


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100)."""
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)])


def overlap(a: Rate, b: Rate) -> bool | None:
    if None in (a.low, a.high, b.low, b.high):
        return None
    return not (a.high < b.low or b.high < a.low)  # type: ignore[operator]
