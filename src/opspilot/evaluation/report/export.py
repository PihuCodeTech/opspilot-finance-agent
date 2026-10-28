"""The reproducible record of an evaluation, exported to `results/<name>/raw/` (P14).

Read under the scorer role (the only one that reads labels): the dataset manifests,
each run's configuration, every item's output and labels, the model calls, the
baseline's sandboxed actions and each OpsPilot run's audit chain head. Every record
is bank-masked (`masking.py`), and files are written as sorted JSON lines so the same
evaluation always exports the same bytes.
"""

import json
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from opspilot.evaluation.report.masking import export_safe

RAW = "raw"


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _write(path: Path, records: Sequence[Any]) -> None:
    path.write_text("".join(dumps(r) + "\n" for r in records), encoding="utf-8")


def _rows(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def export(engine: Engine, run_ids: Sequence[uuid.UUID], target: Path) -> Path:
    """Write the raw record for these runs under `target/raw`; returns that directory."""
    ids = sorted(str(r) for r in run_ids)
    raw = target / RAW
    raw.mkdir(parents=True, exist_ok=True)
    with engine.connect() as conn:
        runs = _rows(
            conn,
            "SELECT r.id, r.system, r.subset, r.repeat_index, r.provider, r.model,"
            " r.prompt_versions, r.git_sha, r.status, r.config, r.started_at, r.finished_at,"
            " d.name AS dataset,"
            " d.version AS dataset_version, d.split, v.version AS policy_version"
            " FROM eval_runs r JOIN datasets d ON d.id = r.dataset_id"
            " JOIN policy_versions v ON v.id = r.policy_version_id"
            " WHERE r.id = ANY(CAST(:ids AS uuid[])) ORDER BY r.started_at, r.id",
            ids=ids,
        )
        if len(runs) != len(ids):
            raise ValueError("unknown evaluation run")
        datasets = _rows(
            conn,
            "SELECT DISTINCT d.name, d.version, d.split, d.seed, d.generator_version,"
            " d.manifest_sha256, d.frozen_at"
            " FROM datasets d JOIN eval_runs r ON r.dataset_id = d.id"
            " WHERE r.id = ANY(CAST(:ids AS uuid[])) ORDER BY d.name, d.version",
            ids=ids,
        )
        outputs = _rows(
            conn,
            "SELECT e.eval_run_id AS run_id, e.dataset_item_id AS item_id,"
            " i.labels->>'key' AS key, e.output, e.execution_ms, e.quota_wait_ms,"
            " e.end_to_end_ms, e.tokens_in, e.tokens_out, e.error FROM eval_item_results e"
            " JOIN dataset_items i ON i.id = e.dataset_item_id"
            " WHERE e.eval_run_id = ANY(CAST(:ids AS uuid[])) ORDER BY e.eval_run_id, key",
            ids=ids,
        )
        labels = _rows(
            conn,
            "SELECT DISTINCT i.id AS item_id, i.labels->>'key' AS key, i.labels"
            " FROM dataset_items i JOIN eval_item_results e ON e.dataset_item_id = i.id"
            " WHERE e.eval_run_id = ANY(CAST(:ids AS uuid[])) ORDER BY key",
            ids=ids,
        )
        calls = _rows(
            conn,
            "SELECT coalesce(l.eval_run_id, c.eval_run_id) AS run_id, l.purpose, l.provider,"
            " l.model, l.request_sha256, l.request, l.response, l.status, l.tokens_in,"
            " l.tokens_out, l.latency_ms, l.quota_wait_ms, l.created_at FROM llm_calls l"
            " LEFT JOIN investigation_runs ir ON ir.id = l.run_id"
            " LEFT JOIN cases c ON c.id = ir.case_id"
            " WHERE coalesce(l.eval_run_id, c.eval_run_id) = ANY(CAST(:ids AS uuid[]))"
            " ORDER BY run_id, l.created_at, l.request_sha256",
            ids=ids,
        )
        actions = _rows(
            conn,
            "SELECT b.eval_run_id AS run_id, i.labels->>'key' AS key, b.action, b.args"
            " FROM baseline_actions b JOIN dataset_items i ON i.id = b.dataset_item_id"
            " WHERE b.eval_run_id = ANY(CAST(:ids AS uuid[])) ORDER BY run_id, key, b.created_at",
            ids=ids,
        )
        heads = _rows(
            conn,
            "SELECT DISTINCT ON (chain_id) chain_id, seq, encode(hash, 'hex') AS hash"
            " FROM audit_events WHERE chain_id = ANY(CAST(:chains AS text[]))"
            " ORDER BY chain_id, seq DESC",
            chains=[f"eval:{i}" for i in ids],
        )
        safe = [
            export_safe(conn, records)
            for records in (runs, datasets, outputs, labels, calls, actions, heads)
        ]
    for name, records in zip(
        ("runs", "datasets", "outputs", "labels", "llm_calls", "baseline_actions", "chain_heads"),
        safe,
        strict=True,
    ):
        _write(raw / f"{name}.jsonl", records)
    return raw
