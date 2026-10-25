"""Replace bank account numbers in document text with fingerprint tokens.

Two detectors, merged:
- anchored: the value after a bank label ("Bank account:", "Remit to", "IBAN", …),
  in plain, spaced, dashed or IBAN form;
- generic: any run of 10+ digits written with optional single spaces between
  groups, wherever it appears.

Invoice and PO numbers (letters and dashes), dates and money amounts (commas
and decimal points) are left alone. After replacement, any remaining run of
10+ digits makes preprocessing fail: a possible leak is never stored
(`rules.md` §2).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from opspilot.domain.bank import BankFingerprint, fingerprint, normalise_account

_LABEL = (
    r"(?:bank\s*(?:account|details)?|account(?:\s*(?:no\.?|number|for\s+payment))?"
    r"|iban|remit\s*to|pay\s*to\s*account|settlement\s*account)"
)
_VALUE = r"(?P<value>[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?|\d[\d \-]{6,32}\d)"
ANCHORED = re.compile(rf"\b{_LABEL}\b[^\n\d:A-Z]*:?\s*{_VALUE}", re.IGNORECASE)
# Not glued to letters, digits or separators: digits inside identifiers and hashes aren't numbers.
GENERIC = re.compile(
    r"(?<![\w.,\-/])\d{2,6}(?: \d{2,6}){1,7}(?![\w.,\-/])|(?<![\w.,\-/])\d{10,34}(?![\w.,\-/])"
)
RESIDUAL = re.compile(r"\d(?:[ \-]?\d){9,}")
# Our own tokens carry hex fingerprints, which can contain long digit runs by chance.
_TOKEN = re.compile(r"BANK_ACCT\[fp:[0-9a-f]{12}\]")
_MIN_DIGITS = 10
_MIN_ANCHORED_DIGITS = 8


class PossibleAccountLeakError(RuntimeError):
    """Raised when text still holds a long digit run after tokenisation."""


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    raw: str


def _digits(text: str) -> int:
    return sum(ch.isdigit() for ch in text)


def find_account_numbers(text: str) -> list[Span]:
    spans: list[Span] = []
    for match in ANCHORED.finditer(text):
        value = match.group("value").strip(" -")
        if _digits(value) >= _MIN_ANCHORED_DIGITS:
            start = match.start("value")
            spans.append(Span(start, start + len(value), value))
    for match in GENERIC.finditer(text):
        if _digits(match.group(0)) >= _MIN_DIGITS:
            spans.append(Span(match.start(), match.end(), match.group(0)))
    merged: list[Span] = []
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if merged and span.start < merged[-1].end:
            continue  # overlapping detections of the same number
        merged.append(span)
    return merged


@dataclass
class Tokeniser:
    """Tokenises every string it is given with one key, remembering the tokens it made."""

    key: str

    def __post_init__(self) -> None:
        self.found: dict[str, BankFingerprint] = {}

    def text(self, value: str) -> str:
        out, last = [], 0
        for span in find_account_numbers(value):
            fp = fingerprint(span.raw, self.key)
            self.found[normalise_account(span.raw)] = fp
            out.append(value[last : span.start])
            out.append(fp.token())
            last = span.end
        out.append(value[last:])
        result = "".join(out)
        if RESIDUAL.search(_TOKEN.sub("BANK_ACCT", result)):
            raise PossibleAccountLeakError("a long digit run remains after tokenisation")
        return result

    def structure(self, value: Any) -> Any:
        """Tokenise every string inside nested dicts and lists (e.g. Docling's JSON)."""
        return _walk(value, self.text)

    def tokens(self) -> list[dict[str, str]]:
        return sorted(
            (
                {"token": fp.token(), "bank_fp": fp.bank_fp, "last4": fp.last4}
                for fp in self.found.values()
            ),
            key=lambda t: t["token"],
        )


def _walk(value: Any, apply: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return apply(value)
    if isinstance(value, dict):
        return {k: _walk(v, apply) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, apply) for v in value]
    return value
