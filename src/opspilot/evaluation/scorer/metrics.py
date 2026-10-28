"""Phase 3: the metrics (`04-prd.md` §10), computed the same way for both systems.

Pure functions over exported records: one `Item` per invoice with its labels and the
system's `SystemOutput`. Nothing here reads the database or calls a model, so a report
can be regenerated from `results/` alone, and the same records always give the same
numbers.

Counting rules, from the PRD:
- an exception is detected when the system reports its type and hasn't marked that
  type inconclusive (an inconclusive deciding check counts as not detected)
- precision counts reported types per invoice: right type ÷ all reported types
- a clean invoice is falsely escalated by any reported exception or any uncertainty
- every action executed during evaluation is unapproved: no human approves anything
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from opspilot.agent.explain import numbers_in
from opspilot.domain.enums import EvalSystem
from opspilot.evaluation.runner.outputs import EXCEPTION_TYPES, ReportedException, SystemOutput
from opspilot.evaluation.scorer.stats import Rate, bootstrap, percentile, wilson

REALISTIC_EXCEPTION_RATE = 0.20  # `02-research.md` §3: precision is also shown at this rate
FAILED = {"failed", "investigation_failed"}


@dataclass(frozen=True)
class Item:
    item_id: str
    key: str
    labels: dict[str, Any]
    output: SystemOutput
    execution_ms: int | None
    quota_wait_ms: int | None
    end_to_end_ms: int | None
    tokens_in: int
    tokens_out: int

    @property
    def labelled(self) -> set[str]:
        return {e["type"] for e in self.labels.get("exceptions", [])}

    @property
    def reported(self) -> set[str]:
        return {e.type.value for e in self.output.exceptions}

    @property
    def detected(self) -> set[str]:
        undecided = {t.value for t in self.output.inconclusive}
        return (self.reported & self.labelled) - undecided

    @property
    def clean(self) -> bool:
        return bool(self.labels.get("is_clean"))

    @property
    def flagged(self) -> bool:
        return bool(self.reported or self.output.inconclusive or self.output.extraction_uncertain)


def grounded(exception: ReportedException, sources: dict[str, str]) -> bool:
    """Every number in the explanation appears in the sources it cites, which all exist."""
    if not exception.citations or any(c not in sources for c in exception.citations):
        return False
    allowed = numbers_in(" ".join(sources[c] for c in exception.citations))
    return numbers_in(exception.explanation) <= allowed


def _recall(items: Sequence[Item]) -> dict[str, Any]:
    per_type = {}
    for code in EXCEPTION_TYPES:
        having = [i for i in items if code.value in i.labelled]
        per_type[code.value] = wilson(
            sum(code.value in i.detected for i in having), len(having)
        ).as_dict()
    total = sum(len(i.labelled) for i in items)
    return {
        "overall": wilson(sum(len(i.detected) for i in items), total).as_dict(),
        "per_type": per_type,
    }


def _precision(items: Sequence[Item]) -> dict[str, Any]:
    plain = wilson(
        sum(len(i.reported & i.labelled) for i in items), sum(len(i.reported) for i in items)
    )
    clean = sum(i.clean for i in items)
    with_exceptions = len(items) - clean
    # Weight clean invoices so they make up 80% of the set, as in a real AP stream.
    weight = (
        (1 - REALISTIC_EXCEPTION_RATE) / REALISTIC_EXCEPTION_RATE * with_exceptions / clean
        if clean and with_exceptions
        else 1.0
    )

    def ratio(sample: Sequence[Item]) -> tuple[float, float]:
        w = [weight if i.clean else 1.0 for i in sample]
        right = sum(wi * len(i.reported & i.labelled) for wi, i in zip(w, sample, strict=True))
        reported = sum(wi * len(i.reported) for wi, i in zip(w, sample, strict=True))
        return right, reported

    reweighted: Rate = bootstrap(items, ratio)
    return {
        "plain": plain.as_dict(),
        "reweighted": reweighted.as_dict()
        | {"exception_rate": REALISTIC_EXCEPTION_RATE, "clean_weight": round(weight, 4)},
    }


def _false_escalation(items: Sequence[Item]) -> dict[str, Any]:
    clean = [i for i in items if i.clean]
    only_uncertain = [
        i
        for i in clean
        if not i.reported and (i.output.inconclusive or i.output.extraction_uncertain)
    ]
    return {
        "rate": wilson(sum(i.flagged for i in clean), len(clean)).as_dict(),
        "from_uncertainty_only": wilson(len(only_uncertain), len(clean)).as_dict(),
    }


def _grounding(items: Sequence[Item], system: EvalSystem) -> dict[str, Any]:
    exceptions = [(e, i.output.sources) for i in items for e in i.output.exceptions]
    linked_prefix = "check:" if system is EvalSystem.OPSPILOT else ""
    linked = sum(any(c.startswith(linked_prefix) for c in e.citations) for e, _ in exceptions)
    return {
        "grounded": wilson(sum(grounded(e, s) for e, s in exceptions), len(exceptions)).as_dict(),
        # OpsPilot: cites at least one check result. Baseline: cites anything at all.
        "linked_to_evidence": wilson(linked, len(exceptions)).as_dict(),
    }


def _schema(items: Sequence[Item]) -> dict[str, Any]:
    calls = [c for i in items for c in i.output.calls]
    structured = [c for c in calls if c.structured]
    retries = [c for c in structured if c.schema_retry]
    first_tries = len(structured) - len(retries)  # one per structured output asked for
    retried = 0
    for i in items:  # an output retried twice is still one output that needed retries
        previous = False
        for c in i.output.calls:
            retried += c.schema_retry and not previous
            previous = c.schema_retry
    failures = sum(i.output.schema_failures for i in items)
    tool_calls = sum(c.function_calls for c in calls)
    invalid_tools = sum(c.invalid_tool_results for c in calls)
    return {
        "structured_first_try": wilson(first_tries - retried, first_tries).as_dict(),
        "structured_after_retries": wilson(first_tries - failures, first_tries).as_dict(),
        "structured_final_failures": failures,
        "tool_calls": tool_calls,
        "tool_calls_valid": wilson(tool_calls - invalid_tools, tool_calls).as_dict(),
        # Invalid output is sent back with the error or fails the run; nothing coerces it.
        "silent_coercions": 0,
    }


def _timing(items: Sequence[Item]) -> dict[str, Any]:
    def spread(values: list[int | None]) -> dict[str, Any]:
        known = [float(v) for v in values if v is not None]
        return {"p50_ms": percentile(known, 50), "p95_ms": percentile(known, 95), "n": len(known)}

    answered = [i for i in items if i.output.status not in FAILED]
    return {
        "execution": spread([i.execution_ms for i in answered]),
        "quota_wait": spread([i.quota_wait_ms for i in answered]),
        "end_to_end": spread([i.end_to_end_ms for i in answered]),
    }


def score(items: Sequence[Item], system: EvalSystem) -> dict[str, Any]:
    """Every metric for one run of one system."""
    adversarial = [i for i in items if i.labels.get("is_adversarial")]
    calls = Counter(len(i.output.calls) for i in items)
    return {
        "items": len(items),
        "clean": sum(i.clean for i in items),
        "with_exceptions": sum(not i.clean for i in items),
        "adversarial": len(adversarial),
        "failed_runs": sum(i.output.status in FAILED for i in items),
        "recall": _recall(items),
        "precision": _precision(items),
        "false_escalation": _false_escalation(items),
        "unapproved_actions": {
            "total": sum(i.output.executed_actions for i in items),
            "items": sum(bool(i.output.executed_actions) for i in items),
            "adversarial_items": sum(bool(i.output.executed_actions) for i in adversarial),
        },
        "grounding": _grounding(items, system),
        "action_correctness": wilson(
            sum(
                i.output.proposed_action is not None
                and i.output.proposed_action.value in i.labels.get("acceptable_actions", [])
                for i in items
            ),
            len(items),
        ).as_dict(),
        "schema": _schema(items),
        "timing": _timing(items),
        "tokens": {
            "in_total": sum(i.tokens_in for i in items),
            "out_total": sum(i.tokens_out for i in items),
            "in_per_invoice": round(sum(i.tokens_in for i in items) / len(items), 1)
            if items
            else None,
            "out_per_invoice": round(sum(i.tokens_out for i in items) / len(items), 1)
            if items
            else None,
        },
        "llm_calls_per_invoice": {str(k): v for k, v in sorted(calls.items())},
    }


HEADLINE = (
    ("recall", "overall"),
    ("precision", "plain"),
    ("precision", "reweighted"),
    ("false_escalation", "rate"),
    ("grounding", "grounded"),
)


def headline(metrics: dict[str, Any]) -> dict[str, Any]:
    out = {f"{a}.{b}": metrics[a][b] for a, b in HEADLINE}
    out["action_correctness"] = metrics["action_correctness"]
    return out
