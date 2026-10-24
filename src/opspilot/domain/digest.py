"""Canonical JSON and SHA-256 digests.

One encoding is used for audit hashes and for evidence, proposal and
resolution digests (`06-database.md` §5): sorted keys, no whitespace, UTF-8,
decimals and other non-JSON values as strings.
"""

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import Enum
from typing import Any

from opspilot.domain.money import Money


def _normalise(value: Any) -> Any:  # noqa: PLR0911, PLR0912  # one branch per supported type
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        raise TypeError("floats are not allowed in canonical JSON; use Decimal")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Money):
        return format(value.amount, "f")
    if isinstance(value, Enum):
        return _normalise(value.value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use UTC-aware values")
        return value.astimezone(dt.UTC).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, set | frozenset):
        # Sets have no order; sort by each element's canonical form so digests are stable.
        items = [_normalise(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True, separators=(",", ":")))
    if isinstance(value, Mapping):
        return {str(k): _normalise(v) for k, v in value.items()}
    if isinstance(value, Sequence):
        return [_normalise(v) for v in value]
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _normalise(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_digest(value: Any) -> bytes:
    return hashlib.sha256(canonical_json(value)).digest()


def sha256_hex(value: Any) -> str:
    return sha256_digest(value).hex()
