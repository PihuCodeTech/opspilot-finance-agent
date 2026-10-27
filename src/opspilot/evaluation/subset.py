"""The pre-declared stratified subset of the final evaluation set (`04-prd.md` D6).

Step 12 decides, from measured quota, whether to run the full final set or
this subset. Both are fixed before any tuning: the subset is drawn once, with
a fixed seed, and published as a list of item keys. It is never re-drawn.
"""

import random
from collections import defaultdict
from typing import Any

SUBSET_SEED = 5005
SUBSET_SIZE = 150


def stratum(labels: dict[str, Any]) -> str:
    """Group by the item's first exception type; clean invoices form their own stratum."""
    if labels["is_clean"]:
        return "clean"
    return str(sorted(e["type"] for e in labels["exceptions"])[0])


def stratified_subset(
    labels_by_key: dict[str, dict[str, Any]], size: int = SUBSET_SIZE, seed: int = SUBSET_SEED
) -> list[str]:
    """Proportional allocation per stratum (largest remainder), then a seeded draw."""
    strata: dict[str, list[str]] = defaultdict(list)
    for key in sorted(labels_by_key):
        strata[stratum(labels_by_key[key])].append(key)
    total = len(labels_by_key)
    if size > total:
        raise ValueError("subset is larger than the set")
    exact = {name: len(keys) * size / total for name, keys in strata.items()}
    quota = {name: int(value) for name, value in exact.items()}
    by_remainder = sorted(strata, key=lambda name: (-(exact[name] - quota[name]), name))
    for name in by_remainder[: size - sum(quota.values())]:
        quota[name] += 1
    rng = random.Random(seed)  # noqa: S311  # sampling, not security
    chosen: list[str] = []
    for name in sorted(strata):
        chosen += rng.sample(strata[name], quota[name])
    return sorted(chosen)
