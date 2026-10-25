"""`opspilot` command line.

Subcommands are registered now so the interface is stable; each phase fills in
its own (see `.workflow/09-tasks.md`). Unimplemented commands exit with code 2.
"""

import argparse
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType

import structlog
from sqlalchemy import Engine

from opspilot.audit.verify import verify as verify_audit
from opspilot.config import get_settings
from opspilot.db.roles import RoleProvisioningError, provision_roles
from opspilot.db.seed import seed
from opspilot.db.session import (
    get_engine,
    get_owner_engine,
    get_scorer_engine,
    get_verifier_engine,
)
from opspilot.domain.enums import JobType
from opspilot.evaluation.archive import archive_bundle
from opspilot.evaluation.datasets import DatasetError, freeze_dataset, load_bundle
from opspilot.evaluation.generator.build import DEFAULT_SPECS, SplitSpec, generate
from opspilot.evaluation.generator.bundle import (
    BundleError,
    labels_document,
    read_bundle,
    verify_bundle,
    write_bundle,
)
from opspilot.evaluation.subset import SUBSET_SEED, SUBSET_SIZE, stratified_subset
from opspilot.extraction.bank_tokens import PossibleAccountLeakError
from opspilot.extraction.preprocess import (
    PARSER_VERSION,
    Preprocessor,
    UnsupportedDocumentError,
    pending_documents,
    preprocess_document,
)
from opspilot.jobs.queue import Job
from opspilot.jobs.worker import Worker, cleanup
from opspilot.llm.errors import LlmError
from opspilot.llm.smoke import run_smoke
from opspilot.logs import configure_logging
from opspilot.policy.document import render_policy
from opspilot.policy.loader import DEFAULT_POLICY_PATH, load_policy
from opspilot.policy.retrieval_eval import evaluate as evaluate_retrieval
from opspilot.policy.retrieve import Embedder, active_version_id, index_policy, retrieve

log = structlog.get_logger()

_PLANNED: dict[str, tuple[str, str]] = {
    "eval": ("Run, score or rescore an evaluation", "P14"),
    "backup": ("Back up the database and documents volume", "P15+"),
    "restore": ("Restore a backup", "P15+"),
}


def _not_implemented(name: str, phase: str) -> Callable[[argparse.Namespace], int]:
    def run(_args: argparse.Namespace) -> int:
        sys.stderr.write(f"opspilot {name}: not implemented yet (planned for {phase}).\n")
        return 2

    return run


def _cleanup_handler(engine: Engine, _job: Job) -> None:
    cleanup(engine, retention_days=get_settings().checkpoint_retention_days)


def _worker(_args: argparse.Namespace) -> int:
    """Claim and run jobs until SIGTERM/SIGINT; the agent's job types arrive in P10."""
    stop = threading.Event()

    def _handle(signum: int, _frame: FrameType | None) -> None:
        log.info("worker.stopping", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    worker = Worker(get_engine(), {JobType.CLEANUP: _cleanup_handler})
    log.info("worker.started", job_types=sorted(worker.handlers))
    worker.run(stop)
    return 0


def _cleanup(_args: argparse.Namespace) -> int:
    report = cleanup(get_engine(), retention_days=get_settings().checkpoint_retention_days)
    sys.stdout.write(
        f"held cases released: {report.released}; checkpoint threads deleted:"
        f" {report.threads_deleted}\n"
    )
    return 0


def _policy_build(args: argparse.Namespace) -> int:
    """Validate the policy configuration and render the policy document (indexing is P7)."""
    policy = load_policy(Path(args.config))
    document = render_policy(policy)
    Path(args.out).write_text(document.text, encoding="utf-8")
    sys.stdout.write(
        f"policy {document.version}\n"
        f"config sha256   {document.config_sha256}\n"
        f"document sha256 {document.document_sha256}\n"
        f"written to {args.out}\n"
    )
    return 0


def _provision_roles(_args: argparse.Namespace) -> int:
    try:
        roles = provision_roles(get_settings())
    except RoleProvisioningError as exc:
        sys.stderr.write(f"opspilot db provision-roles: {exc}\n")
        return 1
    sys.stdout.write(f"login enabled for: {', '.join(roles)}\n")
    return 0


def _seed(_args: argparse.Namespace) -> int:
    settings = get_settings()
    password = settings.demo_password.get_secret_value() if settings.demo_password else None
    result = seed(get_owner_engine(), load_policy(), password)
    lines = [
        f"policy {result.policy_version}: "
        f"{'created' if result.policy_created else 'already present'}, active, "
        f"{result.chunk_count} sections",
        f"users created: {', '.join(result.created_users) or 'none (all exist)'}",
    ]
    if result.password:
        lines.append(f"demo password (shown once, local/preview only): {result.password}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _generate(args: argparse.Namespace) -> int:
    base = DEFAULT_SPECS[args.split]
    spec = SplitSpec(
        base.split,
        args.seed if args.seed is not None else base.seed,
        args.size if args.size is not None else base.size,
        base.clean_ratio,
        base.multi_ratio,
        base.heldout,
        base.adversarial,
    )
    world = generate(spec, load_policy().config)
    target = write_bundle(world, Path(args.out))
    sys.stdout.write(f"{len(world.items)} invoices written to {target}\n")
    return 0


def _dataset(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle)
    try:
        if args.dataset_command == "verify":
            problems = verify_bundle(bundle)
            for problem in problems:
                sys.stdout.write(f"{problem}\n")
            sys.stdout.write("ok\n" if not problems else f"{len(problems)} problem(s)\n")
            return 0 if not problems else 1
        if args.dataset_command == "subset":
            keys = stratified_subset(labels_document(read_bundle(bundle)), args.size, args.seed)
            sys.stdout.write("\n".join(keys) + "\n")
            return 0
        if args.dataset_command == "archive":
            target, digest = archive_bundle(bundle, Path(args.out))
            sys.stdout.write(f"{target}\nsha256 {digest}\n")
            return 0
        settings = get_settings()
        if args.dataset_command == "load":
            result = load_bundle(
                get_scorer_engine(),
                bundle,
                Path(settings.document_store_path),
                settings.bank_hmac_key.get_secret_value(),
            )
            sys.stdout.write(f"loaded {result.name} {result.version}: {result.items} invoices\n")
        else:
            frozen_at = freeze_dataset(get_scorer_engine(), bundle)
            sys.stdout.write(f"frozen at {frozen_at.isoformat()}\n")
    except (BundleError, DatasetError) as exc:
        sys.stderr.write(f"opspilot dataset {args.dataset_command}: {exc}\n")
        return 1
    return 0


def _preprocess(args: argparse.Namespace) -> int:
    """Shared non-LLM preprocessing for a dataset (runs as the app role; resumable)."""
    settings = get_settings()
    engine = get_engine()
    preprocessor = Preprocessor(
        settings.bank_hmac_key.get_secret_value(), settings.docling_artifacts_path
    )
    pending = pending_documents(engine, args.dataset, args.version)
    if args.limit:
        pending = pending[: args.limit]
    sys.stdout.write(f"{PARSER_VERSION}: {len(pending)} document(s) to parse\n")
    failures = 0
    for index, document_id in enumerate(pending, start=1):
        try:
            preprocess_document(
                engine, preprocessor, document_id, Path(settings.document_store_path)
            )
        except (UnsupportedDocumentError, PossibleAccountLeakError) as exc:
            failures += 1
            log.error("preprocess.failed", document_id=str(document_id), reason=str(exc))
        if index % 25 == 0:
            sys.stdout.write(f"  {index}/{len(pending)}\n")
    sys.stdout.write(f"done; {failures} failure(s)\n")
    return 1 if failures else 0


def _llm_smoke(args: argparse.Namespace) -> int:
    """One live structured call with synthetic text; records it as a test fixture."""
    try:
        result, used_today = run_smoke(get_settings(), Path(args.fixtures))
    except LlmError as exc:
        sys.stderr.write(f"opspilot llm smoke: {type(exc).__name__}: {exc}\n")
        return 1
    call = result.calls[-1]
    sys.stdout.write(
        f"answer: {result.value.model_dump()}\n"
        f"valid on first try: {result.first_try_valid}; calls: {len(result.calls)}\n"
        f"tokens in/out: {call.response.tokens_in}/{call.response.tokens_out}; "
        f"latency {call.latency_ms} ms; quota wait {call.quota_wait_ms} ms\n"
        f"requests used today (this provider/model): {used_today}\n"
        f"fixture: {args.fixtures}/{call.request_sha256}.json\n"
    )
    return 0


def _policy_index(_args: argparse.Namespace) -> int:
    engine = get_owner_engine()
    count = index_policy(engine, Embedder(), active_version_id(engine))
    sys.stdout.write(f"embedded {count} section(s) of the active policy\n")
    return 0


def _policy_search(args: argparse.Namespace) -> int:
    passages = retrieve(get_engine(), Embedder(), args.question, mode=args.mode)
    if not passages:
        sys.stdout.write("No applicable policy passage found.\n")
    for p in passages:
        sys.stdout.write(
            f"§{p.section_id} {p.title}  (fts {p.fts_rank}, vector {p.vector_rank}, "
            f"similarity {p.similarity:.2f})\n"
        )
    return 0


def _policy_eval(_args: argparse.Namespace) -> int:
    sys.stdout.write(
        "| mode | recall@5 | MRR | hit rate | unrelated → nothing |\n|---|---|---|---|---|\n"
    )
    for score in evaluate_retrieval(get_engine(), Embedder()):
        sys.stdout.write(
            f"| {score.mode} | {score.recall_at_5:.2f} | {score.mrr:.2f} | {score.hit_rate:.2f} "
            f"({score.answerable} q) | {score.no_answer_correct:.2f} "
            f"({score.unanswerable} q) |\n"
        )
    return 0


type Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


def _add_policy(sub: Subparsers) -> None:
    policy = sub.add_parser("policy", help="Policy configuration, document and search")
    policy_sub = policy.add_subparsers(dest="policy_command", required=True)
    build = policy_sub.add_parser(
        "build", help="Validate the configuration and render the document"
    )
    build.add_argument("--config", default=str(DEFAULT_POLICY_PATH), help="policy TOML file")
    build.add_argument("--out", default="policy.md", help="where to write the Markdown document")
    build.set_defaults(handler=_policy_build)
    index = policy_sub.add_parser("index", help="Embed the active policy's sections (owner)")
    index.set_defaults(handler=_policy_index)
    search = policy_sub.add_parser("search", help="Search the active policy")
    search.add_argument("question")
    search.add_argument("--mode", choices=["hybrid", "fts", "vector"], default="hybrid")
    search.set_defaults(handler=_policy_search)
    evaluate = policy_sub.add_parser("evaluate-retrieval", help="Recall@5 per retrieval mode")
    evaluate.set_defaults(handler=_policy_eval)


def _add_admin(sub: Subparsers) -> None:
    db = sub.add_parser("db", help="Database administration (runs as the owner)")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    provision = db_sub.add_parser(
        "provision-roles", help="Enable login for runtime roles using passwords from .env"
    )
    provision.set_defaults(handler=_provision_roles)
    seed_cmd = sub.add_parser("seed", help="Create demo users and activate the policy version")
    seed_cmd.set_defaults(handler=_seed)


def _add_data(sub: Subparsers) -> None:
    gen = sub.add_parser("generate", help="Generate a synthetic dataset bundle (renders PDFs)")
    gen.add_argument("--split", choices=sorted(DEFAULT_SPECS), required=True)
    gen.add_argument("--seed", type=int, help="override the split's fixed seed")
    gen.add_argument("--size", type=int, help="override the split's size")
    gen.add_argument("--out", default="data/datasets", help="directory for the bundle")
    gen.set_defaults(handler=_generate)
    dataset = sub.add_parser("dataset", help="Verify, load or freeze a dataset bundle")
    dataset_sub = dataset.add_subparsers(dest="dataset_command", required=True)
    for name, text in (
        ("verify", "Check every file against the manifest"),
        ("load", "Load into the database (scorer role)"),
        ("freeze", "Freeze a loaded dataset (irreversible)"),
    ):
        cmd = dataset_sub.add_parser(name, help=text)
        cmd.add_argument("bundle", help="path to the bundle directory")
        cmd.set_defaults(handler=_dataset)
    subset = dataset_sub.add_parser("subset", help="Print the pre-declared stratified subset keys")
    subset.add_argument("bundle", help="path to the bundle directory")
    subset.add_argument("--size", type=int, default=SUBSET_SIZE)
    subset.add_argument("--seed", type=int, default=SUBSET_SEED)
    subset.set_defaults(handler=_dataset)
    archive = dataset_sub.add_parser("archive", help="Write a reproducible .tar.gz and its SHA-256")
    archive.add_argument("bundle", help="path to the bundle directory")
    archive.add_argument("--out", default="data/releases")
    archive.set_defaults(handler=_dataset)
    pre = sub.add_parser("preprocess", help="Parse a dataset's documents (Docling, no LLM)")
    pre.add_argument("--dataset", required=True, help="dataset name, e.g. dev")
    pre.add_argument("--version", required=True, help="dataset version, e.g. g1-s1001")
    pre.add_argument("--limit", type=int, help="parse at most this many (for a quick check)")
    pre.set_defaults(handler=_preprocess)


def _audit_verify(_args: argparse.Namespace) -> int:
    """Exit 0 when every chain and digest checks out, 1 on any mismatch."""
    engine = get_verifier_engine()
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        report = verify_audit(conn)
        conn.rollback()
    lines = [
        f"chain {c.chain_id}: {c.events} events, head seq {c.head_seq} {c.head_hash.hex()}"
        f" [{'ok' if c.error is None else 'BROKEN at ' + c.error}]"
        for c in report.chains
    ] or ["no audit events yet"]
    lines += [f"MISMATCH {m}" for m in report.mismatches]
    lines.append(f"digests checked: {report.digests_checked}; mismatches: {len(report.mismatches)}")
    lines.append("audit verify: " + ("OK" if report.ok else "FAILED"))
    sys.stdout.write("\n".join(lines) + "\n")
    return 0 if report.ok else 1


def _add_audit(sub: Subparsers) -> None:
    audit = sub.add_parser("audit", help="Audit chain and digest verification")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    verify_cmd = audit_sub.add_parser(
        "verify", help="Recompute every chain and digest as the read-only verifier role"
    )
    verify_cmd.set_defaults(handler=_audit_verify)


def _add_llm(sub: Subparsers) -> None:
    llm = sub.add_parser("llm", help="LLM utilities")
    llm_sub = llm.add_subparsers(dest="llm_command", required=True)
    smoke = llm_sub.add_parser("smoke", help="One live call with synthetic text (uses quota)")
    smoke.add_argument("--fixtures", default="tests/fixtures/llm")
    smoke.set_defaults(handler=_llm_smoke)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opspilot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("worker", help="Run the background worker")
    worker.set_defaults(handler=_worker)
    cleanup_cmd = sub.add_parser(
        "cleanup", help="Release held cases that can now be approved; delete old checkpoints"
    )
    cleanup_cmd.set_defaults(handler=_cleanup)
    for add in (_add_policy, _add_admin, _add_data, _add_llm, _add_audit):
        add(sub)
    for name, (help_text, phase) in _PLANNED.items():
        cmd = sub.add_parser(name, help=f"{help_text} ({phase})")
        cmd.set_defaults(handler=_not_implemented(name, phase))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings().log_level)
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
