"""Structured JSON logging with redaction (named logs.py to avoid shadowing stdlib logging).

Secrets, cookies and tokens are dropped by key, and long digit runs (possible
bank or card numbers) are masked to their last four digits in every value.
"""

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

_REDACTED_KEYS = frozenset(
    {
        "password",
        "secret",
        "token",
        "cookie",
        "authorization",
        "csrf",
        "csrf_token",
        "api_key",
        "session_secret",
        "bank_hmac_key",
        "gemini_api_key",
        "account_number",
        "verified_account_number",
    }
)
# A whole UUID is tried first so its dash-joined digit groups are never read as one run.
_UUID = r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
_LONG_DIGITS = re.compile(rf"{_UUID}|\d[\d \-]{{6,}}\d")
# ISO dates and times are not account numbers and must stay readable in logs.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def mask_digits(text: str) -> str:
    """Replace runs of 8+ digits (spaces/dashes allowed) with •••• + last 4 digits.

    UUIDs (row and run IDs) and ISO dates are identifiers, not account numbers, and are kept.
    """

    def _mask(match: re.Match[str]) -> str:
        if "-" in match.group(0) and re.fullmatch(_UUID, match.group(0)):
            return match.group(0)
        if _ISO_DATE.fullmatch(match.group(0)):
            return match.group(0)
        digits = re.sub(r"\D", "", match.group(0))
        return f"••••{digits[-4:]}" if len(digits) >= 8 else match.group(0)  # noqa: PLR2004

    return _LONG_DIGITS.sub(_mask, text)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return mask_digits(value)
    if isinstance(value, dict):
        return {k: "[redacted]" if _is_sensitive(k) else _scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(v) for v in value]
    return value


def _is_sensitive(key: object) -> bool:
    return isinstance(key, str) and key.lower() in _REDACTED_KEYS


def redact(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        event_dict[key] = "[redacted]" if _is_sensitive(key) else _scrub(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            redact,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )
