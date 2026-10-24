"""Compare the code with the canonical tables in `.workflow/04-prd.md` §2–§3.

`.workflow/` is local and never committed, so this runs on the owner's machine
and is skipped in CI. The other unit tests carry a transcription of the same
tables, so CI still guards the values.
"""

import re
from pathlib import Path

import pytest

from opspilot.domain.enums import ActionType, ActorRole, CaseStatus, FindingCode, ResolutionType
from opspilot.domain.transitions import TRANSITIONS
from opspilot.policy.loader import load_policy

PRD = Path(__file__).resolve().parents[2] / ".workflow" / "04-prd.md"
pytestmark = pytest.mark.skipif(not PRD.exists(), reason="local .workflow/ docs not present")


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end, text.index(start))]


def _rows(block: str) -> list[list[str]]:
    rows = []
    for line in block.splitlines():
        if line.startswith("|") and not set(line) <= set("|- "):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows[1:]  # drop the header row


def _ticks(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def _prd() -> str:
    return PRD.read_text(encoding="utf-8")


def test_transition_table_matches_the_prd() -> None:
    block = _section(_prd(), "| # | From | Event | To | Actor |", "Anything else returns 409")
    documented = {}
    for cells in _rows(block):
        tid, source, _event, target, actor = cells[:5]
        src = _ticks(source)
        role = (
            ActorRole.APPROVER
            if "approver" in actor
            else ActorRole.ANALYST
            if "analyst" in actor
            else ActorRole.SYSTEM
        )
        documented[tid] = (CaseStatus(src[0]) if src else None, CaseStatus(_ticks(target)[0]), role)
    assert documented == {t.id: (t.source, t.target, t.actor) for t in TRANSITIONS}


def test_allowlists_match_the_prd() -> None:
    block = _section(_prd(), "| Case findings (unresolved) |", "- **Storage and validation:**")
    rows = _rows(block)
    categories = ["clean", "inconclusive", "restricted", "other", "all_resolved"]
    assert len(rows) == len(categories)
    lists = load_policy().config.allowlists
    for category, (_label, agent_cell, analyst_cell) in zip(categories, rows, strict=True):
        agent_doc = {ActionType(a) for a in _ticks(agent_cell)}
        analyst_doc = {ActionType(a) for a in _ticks(analyst_cell)}
        if category == "all_resolved":
            assert not agent_doc  # the agent proposes before resolutions exist ("—")
        else:
            assert agent_doc == set(getattr(lists.agent, category)), category
        assert analyst_doc == set(getattr(lists.analyst, category)), category


def test_resolution_rules_match_the_prd() -> None:
    block = _section(_prd(), "| Finding | Resolution type |", "- **Approval of payment:**")
    documented: set[tuple[FindingCode, ResolutionType]] = set()
    for finding_cell, type_cell, *_ in _rows(block):
        types = [t for t in _ticks(type_cell) if t in ResolutionType._value2member_map_]
        for code in _ticks(finding_cell):
            if code in FindingCode._value2member_map_:
                documented |= {(FindingCode(code), ResolutionType(t)) for t in types}
    configured = {(r.finding, r.type) for r in load_policy().config.resolutions.rules}
    assert documented == configured
