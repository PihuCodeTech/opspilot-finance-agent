"""Explain-and-propose: the model's output schema, its validation, and the template fallback.

Findings come from TruthLens. The model only explains them and picks an action
from the agent allowlist. `validate` checks three things:
- every number in an explanation appears in the evidence it cites
- every cited check and passage exists
- the action is allowed

One retry gets the errors back. After that a deterministic template is used,
flagged `fallback` (`06-architecture.md` §3.1).

The prompt names checks and passages by stable keys (`price@line:1`, section
`3.1`) rather than row IDs, so a recorded reply replays for the same evidence
in any run.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from opspilot.agent.deps import PassageRef
from opspilot.domain.enums import ActionType, FindingCode
from opspilot.policy.document import FINDING_WORDS

_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
PREFERENCE = (
    ActionType.APPROVE_FOR_PAYMENT,
    ActionType.PLACE_ON_HOLD,
    ActionType.DRAFT_VENDOR_QUERY,
    ActionType.REJECT_INVOICE,
)


class FindingNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding: str = Field(description="The finding key, exactly as given")
    explanation: str = Field(min_length=1, max_length=600)
    check_results: list[str] = Field(description="Keys of the check results relied on")
    policy_sections: list[str] = Field(description="Section IDs of the passages relied on")


class ExplainOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[FindingNote]
    proposed_action: str = Field(description="One of the allowed actions, exactly")
    summary: str = Field(max_length=600)


@dataclass(frozen=True)
class CheckView:
    key: str  # "price@line:1"
    status: str
    compared: dict[str, Any]
    tolerance: dict[str, Any] | None
    subject: str


@dataclass(frozen=True)
class FindingView:
    key: str  # "PRICE_MISMATCH@line:1"
    code: FindingCode
    subject: str
    checks: tuple[str, ...]  # check keys behind it


def prompt_safe(value: Any) -> Any:
    """Compared values as the model sees them: without row IDs.

    The model never needs an ID, and a UUID's hex can hold a run of ten digits,
    which the sensitive-data guard rightly refuses as a possible account number.
    """
    if isinstance(value, dict):
        return {k: prompt_safe(v) for k, v in value.items() if not k.endswith("_id")}
    if isinstance(value, list):
        return [prompt_safe(v) for v in value]
    return value


def check_key(code: str, subject: str) -> str:
    return f"{code}@{subject}"


def _numbers(text: str) -> set[Decimal]:
    return {Decimal(m.replace(",", "")) for m in _NUMBER.findall(text)}


def evidence_numbers(checks: Iterable[CheckView], passages: Iterable[PassageRef]) -> set[Decimal]:
    found: set[Decimal] = set()
    for check in checks:
        found |= _numbers(json.dumps([check.compared, check.tolerance, check.subject]))
    for passage in passages:
        found |= _numbers(f"{passage.section_id} {passage.text}")
    return found


def validate(
    output: ExplainOutput,
    findings: list[FindingView],
    checks: Mapping[str, CheckView],
    passages: Mapping[str, PassageRef],
    allowed: frozenset[ActionType],
) -> list[str]:
    errors: list[str] = []
    expected = {f.key for f in findings}
    given = [n.finding for n in output.findings]
    if sorted(given) != sorted(expected):
        errors.append(f"explain each of these findings exactly once: {sorted(expected)}")
    for note in output.findings:
        unknown_checks = [k for k in note.check_results if k not in checks]
        unknown_sections = [s for s in note.policy_sections if s not in passages]
        if unknown_checks:
            errors.append(f"{note.finding}: unknown check results {unknown_checks}")
        if unknown_sections:
            errors.append(f"{note.finding}: unknown policy sections {unknown_sections}")
        if not [k for k in note.check_results if k in checks]:
            errors.append(f"{note.finding}: cite at least one check result")
        allowed_numbers = evidence_numbers(
            (checks[k] for k in note.check_results if k in checks),
            (passages[s] for s in note.policy_sections if s in passages),
        )
        stray = sorted(str(n) for n in _numbers(note.explanation) if n not in allowed_numbers)
        if stray:
            errors.append(f"{note.finding}: numbers not in the cited evidence: {stray}")
    if output.proposed_action not in {a.value for a in allowed}:
        errors.append(f"proposed_action must be one of {sorted(a.value for a in allowed)}")
    return errors


def default_action(allowed: frozenset[ActionType]) -> ActionType:
    return next(a for a in PREFERENCE if a in allowed)


def _scalar_parts(compared: Mapping[str, Any]) -> list[str]:
    return [
        f"{k.replace('_', ' ')} {v}"
        for k, v in compared.items()
        if isinstance(v, str | int | Decimal) and not isinstance(v, bool)
    ]


def template(
    findings: list[FindingView],
    checks: Mapping[str, CheckView],
    passages_by_code: Mapping[FindingCode, list[str]],
    allowed: frozenset[ActionType],
) -> ExplainOutput:
    """The deterministic explanation: the finding's words and the values compared."""
    notes = []
    for finding in findings:
        parts = [p for k in finding.checks for p in _scalar_parts(checks[k].compared)]
        where = "" if finding.subject == "header" else f" ({finding.subject.replace(':', ' ')})"
        notes.append(
            FindingNote(
                finding=finding.key,
                explanation=f"{FINDING_WORDS[finding.code]}{where}: " + "; ".join(parts)[:500],
                check_results=list(finding.checks),
                policy_sections=passages_by_code.get(finding.code, [])[:1],
            )
        )
    return ExplainOutput(
        findings=notes,
        proposed_action=default_action(allowed).value,
        summary="Template explanation (the model's explanation did not pass validation).",
    )
