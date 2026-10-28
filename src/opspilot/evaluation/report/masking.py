"""Bank-number masking for everything written to `results/` (owner, 2026-10-26: mandatory).

Exports go through the same masking as the API (`mask_bank`: fingerprints and tokens
become `••••` + last 4), then any account-like number left in free text is masked with
the preprocessing detectors. `scan` is the release gate: it reads back every file and
reports anything that could still be an account number, a fingerprint or a bank token.
A non-empty scan blocks the release.

Digit groups split across a line break are joined first ("1234 5678\\n9012 3456" is one
number), and long hex hashes and UUIDs are set aside so their digits aren't read as one.
"""

import re
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from opspilot.cases.queries import mask_bank
from opspilot.extraction.bank_tokens import RESIDUAL, find_account_numbers

MASK = "••••"
_SPLIT_DIGITS = re.compile(r"(?<=\d)[ \t]*\r?\n[ \t]*(?=\d)")
_IDENTIFIERS = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    r"|\b[0-9a-f]{32,}\b"
)
_TOKEN = re.compile(r"BANK_ACCT\[fp:[0-9a-f]{12}\]")


def mask_text(value: str) -> str:
    joined = _SPLIT_DIGITS.sub(" ", value)
    out, last = [], 0
    for span in find_account_numbers(joined):
        digits = re.sub(r"\D", "", span.raw)
        out += [joined[last : span.start], MASK + digits[-4:]]
        last = span.end
    return "".join(out) + joined[last:]


def _walk(value: Any) -> Any:
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: _walk(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_walk(v) for v in value]
    return value


def export_safe(conn: Connection, value: Any) -> Any:
    """Fingerprints and tokens first (they need the last 4 from the database), then free text."""
    return _walk(mask_bank(conn, value))


def known_fingerprints(conn: Connection) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT bank_fp FROM vendor_bank_accounts UNION SELECT bank_fp FROM extractions"
            " WHERE bank_fp IS NOT NULL UNION SELECT verified_bank_fp FROM bank_verifications"
            " UNION SELECT t->>'bank_fp' FROM parsed_documents, jsonb_array_elements(bank_tokens) t"
        )
    ).scalars()
    return {fp for fp in rows if fp}


def leaks(content: str, fingerprints: set[str]) -> list[str]:
    """What in this text could still identify a bank account."""
    found = []
    if _TOKEN.search(content):
        found.append("a BANK_ACCT fingerprint token")
    found += [f"fingerprint {fp[:4]}…" for fp in sorted(fingerprints) if fp in content]
    plain = _IDENTIFIERS.sub("#", _SPLIT_DIGITS.sub(" ", content))
    if RESIDUAL.search(plain):
        found.append("a run of 10+ digits")
    found += [
        f"an account number ending {re.sub(r'\D', '', s.raw)[-4:]}"
        for s in find_account_numbers(plain)
    ]
    return found


def scan(directory: Path, fingerprints: set[str]) -> list[str]:
    """Every problem in every file under `directory`, as `path: problem`."""
    problems = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        content = path.read_text(encoding="utf-8", errors="replace")
        problems += [
            f"{path.relative_to(directory)}: {problem}" for problem in leaks(content, fingerprints)
        ]
    return problems
