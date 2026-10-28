"""`opspilot eval …`: the three phases as commands (`06-architecture.md` §4, step 7).

    eval preprocess     phase 1: the shared parse for every item (no model)
    eval subset         declare a stratified subset (scorer role; writes item IDs only)
    eval freeze-prompts record the prompts tuned on dev; the final set needs them
    eval run            phase 2: one system over a dataset (app role; no labels)
    eval score          phase 3: export, score and publish to results/ (scorer role)
    eval rescore        rebuild a published report from its raw files (no DB, no key)
    eval plan           the final-set call budget, measured from runs already made

Evaluation runs spend quota when live and are never part of the test suite.
"""

import argparse
import math
import sys
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from opspilot.agent.runtime import build_agent
from opspilot.config import get_settings
from opspilot.db.models import EvalItemResult
from opspilot.db.session import get_baseline_engine, get_engine, get_scorer_engine
from opspilot.domain.enums import EvalSystem
from opspilot.evaluation.baseline.agent import Baseline
from opspilot.evaluation.report.publish import ReleaseBlockedError, publish, rescore
from opspilot.evaluation.runner import inference
from opspilot.evaluation.runner.inference import EvaluationError, Plan, Subset, Systems
from opspilot.evaluation.scorer.subsets import declare_subset
from opspilot.evaluation.subset import SUBSET_SEED, SUBSET_SIZE
from opspilot.extraction.preprocess import Preprocessor, preprocess_document
from opspilot.llm.factory import build_client
from opspilot.policy.document import render_policy
from opspilot.policy.loader import load_policy

RESULTS = Path("results")


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _preprocess(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = get_engine()
    parser = Preprocessor(
        settings.bank_hmac_key.get_secret_value(), settings.docling_artifacts_path
    )
    store = Path(settings.document_store_path)
    created, total = inference.preprocess(
        engine,
        lambda document_id: preprocess_document(engine, parser, document_id, store),
        args.dataset,
        args.version,
    )
    _out(f"{created} parsed now, {total - created} already parsed, {total} items")
    return 0


def _subset(args: argparse.Namespace) -> int:
    out = Path(
        args.out
        or RESULTS / "subsets" / f"{args.dataset}-{args.version}-{args.size}-s{args.seed}.json"
    )
    subset = declare_subset(
        get_scorer_engine(), args.dataset, args.version, size=args.size, seed=args.seed, out=out
    )
    _out(f"{subset['name']}: {len(subset['item_ids'])} items written to {out}")
    return 0


def _freeze(_args: argparse.Namespace) -> int:
    for name, digest in sorted(inference.freeze_prompts().items()):
        _out(f"{name:<24} {digest}")
    _out(f"frozen in {inference.FROZEN_PROMPTS}")
    return 0


def _systems(system: EvalSystem) -> Systems:
    settings = get_settings()
    engine = get_engine()
    if system is EvalSystem.OPSPILOT:
        agent = build_agent(settings, engine)
        return Systems(agent.deps.llm, agent=agent)
    llm = build_client(settings)
    return Systems(
        llm,
        baseline=Baseline(llm, get_baseline_engine(), engine, render_policy(load_policy()).text),
    )


def _run(args: argparse.Namespace) -> int:
    from opspilot.cli import LiveRunNotAuthorizedError, _refuse, require_live_authorization  # noqa: PLC0415  # the CLI imports this module

    settings = get_settings()
    try:
        require_live_authorization(settings.llm_mode, args.allow_live, "eval run")
    except LiveRunNotAuthorizedError as exc:
        return _refuse(exc)
    plan = Plan(
        EvalSystem(args.system),
        args.dataset,
        args.version,
        settings.llm_mode.value,
        Subset.read(Path(args.subset)) if args.subset else None,
        args.repeat,
        args.final,
    )
    engine = get_engine()
    try:
        systems = _systems(plan.system)
        run_id = inference.start_run(
            engine, systems.llm, plan, uuid.UUID(args.resume) if args.resume else None
        )
        _out(f"eval run {run_id} ({plan.system}, {plan.llm_mode}, {systems.llm.provider.model})")

        def show(p: inference.Progress) -> None:
            if p.done % 10 == 0 or p.done == p.total:
                _out(f"  {p.done}/{p.total}")

        progress = inference.run(engine, systems, plan, run_id, show)
    except EvaluationError as exc:
        sys.stderr.write(f"opspilot eval run: {exc}\n")
        return 1
    if progress.stopped_until:
        _out(
            f"stopped at the AI quota after {progress.done}/{progress.total} items; resumes after"
            f" {progress.stopped_until:%Y-%m-%d %H:%M UTC}:\n"
            f"  opspilot eval run --system {plan.system} --dataset {plan.dataset}"
            f" --version {plan.version}"
            f"{' --subset ' + args.subset if args.subset else ''}{' --final' if plan.final else ''}"
            f" --repeat {plan.repeat_index} --resume {run_id} --allow-live"
        )
        return 3  # stopped, resumable
    _out(f"completed {progress.done}/{progress.total} items")
    return 0


def _score(args: argparse.Namespace) -> int:
    try:
        report = publish(
            get_scorer_engine(), [uuid.UUID(r) for r in args.runs], Path(args.results), args.name
        )
    except ReleaseBlockedError as exc:
        sys.stderr.write(
            f"release blocked: {exc}\n" + "".join(f"  {p}\n" for p in exc.problems[:50])
        )
        return 4
    _out(f"published {Path(args.results) / args.name} ({len(report['runs'])} runs)")
    return 0


def _rescore(args: argparse.Namespace) -> int:
    target = Path(args.target)
    if not (target / "raw").is_dir():
        target = Path(args.results) / args.target
    try:
        rescore(target)
    except ReleaseBlockedError as exc:
        sys.stderr.write(
            f"release blocked: {exc}\n" + "".join(f"  {p}\n" for p in exc.problems[:50])
        )
        return 4
    _out(f"rescored {target}")
    return 0


def _plan(args: argparse.Namespace) -> int:
    """Model calls a final-set run needs, from the calls per invoice measured so far."""
    engine = get_engine()
    measured: dict[str, list[int]] = {}
    with engine.connect() as conn:
        for run_id in args.runs:
            outputs = conn.execute(
                select(EvalItemResult.output).where(EvalItemResult.eval_run_id == uuid.UUID(run_id))
            ).scalars()
            for output in outputs:
                measured.setdefault(str(output["system"]), []).append(len(output.get("calls", [])))
    if not measured:
        sys.stderr.write("opspilot eval plan: no items in those runs yet\n")
        return 1
    rows: list[Any] = []
    for system, calls in sorted(measured.items()):
        mean = sum(calls) / len(calls)
        full = math.ceil(mean * args.items)
        repeats = math.ceil(mean * args.subset_size * args.repeats)
        rows.append((system, mean, max(calls), full, repeats))
    total = sum(r[3] + r[4] for r in rows)
    _out(f"final set: {args.items} invoices; subset: {args.subset_size} × {args.repeats} repeats")
    for system, mean, most, full, repeats in rows:
        _out(
            f"  {system:<9} {mean:.2f} calls/invoice (max {most}):"
            f" full run {full}, repeats {repeats}"
        )
    _out(
        f"  total {total} calls ≈ {math.ceil(total / args.per_day)} days"
        f" at {args.per_day} requests/day"
    )
    subset_only = sum(math.ceil(r[1] * args.subset_size * (1 + args.repeats)) for r in rows)
    _out(
        f"  subset only (no full run): {subset_only} calls"
        f" ≈ {math.ceil(subset_only / args.per_day)} days"
    )
    return 0


def add_eval(sub: Any, live_help: str) -> None:
    ev = sub.add_parser("eval", help="Run, score or rescore an evaluation (P14)")
    ev_sub = ev.add_subparsers(dest="eval_command", required=True)

    def dataset(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--dataset", required=True, help="dataset name, e.g. dev")
        cmd.add_argument("--version", required=True, help="dataset version, e.g. g1-s1001")

    pre = ev_sub.add_parser("preprocess", help="Phase 1: parse every item's document (no model)")
    dataset(pre)
    pre.set_defaults(handler=_preprocess)

    subset = ev_sub.add_parser("subset", help="Declare a stratified subset (scorer role)")
    dataset(subset)
    subset.add_argument("--size", type=int, default=SUBSET_SIZE)
    subset.add_argument("--seed", type=int, default=SUBSET_SEED)
    subset.add_argument("--out")
    subset.set_defaults(handler=_subset)

    freeze = ev_sub.add_parser(
        "freeze-prompts", help="Freeze both systems' prompts (after dev tuning)"
    )
    freeze.set_defaults(handler=_freeze)

    run = ev_sub.add_parser(
        "run", help="Phase 2: run one system over a dataset (uses quota when live)"
    )
    run.add_argument("--system", required=True, choices=[s.value for s in EvalSystem])
    dataset(run)
    run.add_argument("--subset", help="a subset file from `eval subset`")
    run.add_argument("--repeat", type=int, default=0, help="repeat index (0 for the first run)")
    run.add_argument(
        "--final", action="store_true", help="the frozen final set (needs frozen prompts)"
    )
    run.add_argument("--resume", help="continue a run stopped at the quota limit")
    run.add_argument("--allow-live", action="store_true", help=live_help)
    run.set_defaults(handler=_run)

    score = ev_sub.add_parser("score", help="Phase 3: export, score and publish runs (scorer role)")
    score.add_argument("--runs", nargs="+", required=True)
    score.add_argument("--name", required=True, help="folder under results/, e.g. dev-g1-s1001")
    score.add_argument("--results", default=str(RESULTS))
    score.set_defaults(handler=_score)

    re_cmd = ev_sub.add_parser("rescore", help="Rebuild a report from its raw files (no API key)")
    re_cmd.add_argument("target", help="results/<name> or <name>")
    re_cmd.add_argument("--results", default=str(RESULTS))
    re_cmd.set_defaults(handler=_rescore)

    plan = ev_sub.add_parser("plan", help="Final-set call budget from measured runs")
    plan.add_argument("--runs", nargs="+", required=True)
    plan.add_argument("--items", type=int, default=600)
    plan.add_argument("--subset-size", type=int, default=SUBSET_SIZE)
    plan.add_argument("--repeats", type=int, default=2)
    plan.add_argument("--per-day", type=int, default=get_settings().llm_rpd)
    plan.set_defaults(handler=_plan)
