"""Scoring a set of runs into `results/<name>/`, and rescoring it later (P14).

Everything is built in a temporary folder, scanned for bank details, and only then moved
into place: a scan that finds anything blocks the release and nothing is published
(owner, 2026-10-26). Rescoring rebuilds the report from the raw record alone, with no
database and no API key, and gives the same bytes.
"""

import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from opspilot.evaluation.report.export import RAW, export
from opspilot.evaluation.report.masking import known_fingerprints, scan
from opspilot.evaluation.report.render import build


class ReleaseBlockedError(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            f"{len(problems)} possible bank detail(s) in the export; nothing was published"
        )
        self.problems = problems


def _swap(built: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    built.rename(target)


def publish(
    engine: Engine, run_ids: Sequence[uuid.UUID], results: Path, name: str
) -> dict[str, Any]:
    """Export, score and scan under the scorer role; publish to `results/<name>` if clean."""
    target = results / name
    building = results / f".building-{name}"
    shutil.rmtree(building, ignore_errors=True)
    try:
        export(engine, run_ids, building)
        report = build(building, name)
        with engine.connect() as conn:
            fingerprints = known_fingerprints(conn)
        problems = scan(building, fingerprints)
        if problems:
            raise ReleaseBlockedError(problems)
        _swap(building, target)
    finally:
        shutil.rmtree(building, ignore_errors=True)
    return report


def rescore(target: Path) -> dict[str, Any]:
    """Rebuild `report.*` and `items.csv` from `target/raw` (no database, no model)."""
    building = target.parent / f".rescoring-{target.name}"
    shutil.rmtree(building, ignore_errors=True)
    try:
        shutil.copytree(target / RAW, building / RAW)
        report = build(building, target.name)
        problems = scan(building, set())  # tokens and number shapes; fingerprints need the DB
        if problems:
            raise ReleaseBlockedError(problems)
        _swap(building, target)
    finally:
        shutil.rmtree(building, ignore_errors=True)
    return report
