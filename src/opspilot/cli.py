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

from opspilot.config import get_settings
from opspilot.logs import configure_logging
from opspilot.policy.document import render_policy
from opspilot.policy.loader import DEFAULT_POLICY_PATH, load_policy

log = structlog.get_logger()

_PLANNED: dict[str, tuple[str, str]] = {
    "seed": ("Create demo users, the active policy version and the demo dataset", "P2"),
    "generate": ("Generate a synthetic dataset split", "P3"),
    "dataset": ("Freeze, fetch or verify datasets", "P3"),
    "preprocess": ("Run shared non-LLM preprocessing for a dataset", "P4"),
    "audit": ("Verify audit chains and evidence digests", "P8"),
    "cleanup": ("Checkpoint retention and held-case re-check", "P9"),
    "eval": ("Run, score or rescore an evaluation", "P14"),
    "backup": ("Back up the database and documents volume", "P15+"),
    "restore": ("Restore a backup", "P15+"),
}

_HEARTBEAT_SECONDS = 30.0


def _not_implemented(name: str, phase: str) -> Callable[[argparse.Namespace], int]:
    def run(_args: argparse.Namespace) -> int:
        sys.stderr.write(f"opspilot {name}: not implemented yet (planned for {phase}).\n")
        return 2

    return run


def _worker(_args: argparse.Namespace) -> int:
    """Idle worker loop until P9 adds the job queue; stops cleanly on SIGTERM/SIGINT."""
    stop = threading.Event()

    def _handle(signum: int, _frame: FrameType | None) -> None:
        log.info("worker.stopping", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    log.info("worker.started", jobs="none until P9")
    while not stop.wait(_HEARTBEAT_SECONDS):
        log.info("worker.heartbeat")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opspilot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("worker", help="Run the background worker")
    worker.set_defaults(handler=_worker)
    policy = sub.add_parser("policy", help="Policy configuration and document")
    policy_sub = policy.add_subparsers(dest="policy_command", required=True)
    build = policy_sub.add_parser(
        "build", help="Validate the configuration and render the document"
    )
    build.add_argument("--config", default=str(DEFAULT_POLICY_PATH), help="policy TOML file")
    build.add_argument("--out", default="policy.md", help="where to write the Markdown document")
    build.set_defaults(handler=_policy_build)
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
