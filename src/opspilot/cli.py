"""`opspilot` command line.

Subcommands are registered now so the interface is stable; each phase fills in
its own (see `.workflow/09-tasks.md`). Unimplemented commands exit with code 2.
"""

import argparse
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from types import FrameType

import structlog

from opspilot.config import get_settings
from opspilot.logs import configure_logging

log = structlog.get_logger()

_PLANNED: dict[str, tuple[str, str]] = {
    "seed": ("Create demo users, the active policy version and the demo dataset", "P2"),
    "generate": ("Generate a synthetic dataset split", "P3"),
    "dataset": ("Freeze, fetch or verify datasets", "P3"),
    "preprocess": ("Run shared non-LLM preprocessing for a dataset", "P4"),
    "policy": ("Build and index the policy document from the configuration", "P1/P7"),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opspilot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("worker", help="Run the background worker")
    worker.set_defaults(handler=_worker)
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
