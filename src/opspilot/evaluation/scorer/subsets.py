"""Declaring a subset before it is run (`04-prd.md` §10: repeats on ~150 invoices).

The scorer draws the stratified subset from labels (`evaluation/subset.py`) and writes
only item IDs and keys, so inference can run it without ever seeing a label. The file
is published under `results/subsets/` and its digest is stored on every run that uses it.
"""

import json
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from opspilot.evaluation.subset import stratified_subset


def declare_subset(
    engine: Engine, dataset: str, version: str, *, size: int, seed: int, out: Path
) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT i.id, i.labels FROM dataset_items i JOIN datasets d ON d.id = i.dataset_id"
                " WHERE d.name = :n AND d.version = :v"
            ),
            {"n": dataset, "v": version},
        ).all()
    by_key = {r.labels["key"]: r for r in rows}
    keys = stratified_subset({k: r.labels for k, r in by_key.items()}, size, seed)
    subset = {
        "name": f"{dataset}-{version}-stratified-{size}-s{seed}",
        "dataset": dataset,
        "version": version,
        "size": size,
        "seed": seed,
        "keys": keys,
        "item_ids": [str(by_key[k].id) for k in keys],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(subset, indent=2) + "\n", encoding="utf-8")
    return subset
