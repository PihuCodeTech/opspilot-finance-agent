"""Hash-chained audit events (`06-database.md` §5).

Chains: `live` for operational activity, `eval:<eval_run_id>` per evaluation
run. Each event's hash covers the previous event's hash and the event's own
canonical content, so editing, inserting or removing any event breaks every
later link. Writers take a per-chain advisory lock inside the caller's
transaction, so the chain stays linear under concurrency and the event commits
or rolls back with the change it records.
"""

import datetime as dt
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from opspilot.domain.digest import canonical_json
from opspilot.extraction.bank_tokens import contains_account_like_digits

LIVE = "live"
ZERO_HASH = bytes(32)


def eval_chain(eval_run_id: uuid.UUID) -> str:
    return f"eval:{eval_run_id}"


class AuditPayloadError(ValueError):
    """Payloads hold masked values only; an account-like digit run is refused."""


@dataclass(frozen=True)
class AuditEvent:
    chain_id: str
    seq: int
    occurred_at: dt.datetime
    actor_id: uuid.UUID | None
    case_id: uuid.UUID | None
    event_type: str
    payload: dict[str, Any]
    prev_hash: bytes
    hash: bytes


def event_content(event: AuditEvent | dict[str, Any]) -> dict[str, Any]:
    get = event.__getitem__ if isinstance(event, dict) else lambda k: getattr(event, k)
    return {
        "chain_id": get("chain_id"),
        "seq": get("seq"),
        "occurred_at": get("occurred_at"),
        "actor_id": get("actor_id"),
        "case_id": get("case_id"),
        "event_type": get("event_type"),
        "payload": get("payload"),
    }


def event_hash(prev_hash: bytes, content: dict[str, Any]) -> bytes:
    return hashlib.sha256(prev_hash + canonical_json(content)).digest()


# Values we write ourselves in known formats: digests, IDs and timestamps are not account numbers.
_KNOWN_FORMAT = re.compile(
    r"[0-9a-f]{64}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)?)?"
)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def append_event(
    conn: Connection,
    *,
    chain_id: str,
    event_type: str,
    payload: dict[str, Any],
    actor_id: uuid.UUID | None = None,
    case_id: uuid.UUID | None = None,
) -> AuditEvent:
    """Append one event in the caller's transaction (commit together or not at all)."""
    # Stored exactly as hashed: canonical JSON round-trip (decimals as strings, no floats).
    stored = json.loads(canonical_json(payload))
    free_text = [s for s in _strings(stored) if not _KNOWN_FORMAT.fullmatch(s)]
    if any(contains_account_like_digits(s) for s in free_text):
        raise AuditPayloadError("audit payload contains an account-like digit run")
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:c, 0))"), {"c": chain_id})
    head = conn.execute(
        text("SELECT seq, hash FROM audit_events WHERE chain_id = :c ORDER BY seq DESC LIMIT 1"),
        {"c": chain_id},
    ).first()
    seq, prev = (head.seq + 1, bytes(head.hash)) if head else (1, ZERO_HASH)
    occurred_at = dt.datetime.now(dt.UTC)
    content = {
        "chain_id": chain_id,
        "seq": seq,
        "occurred_at": occurred_at,
        "actor_id": actor_id,
        "case_id": case_id,
        "event_type": event_type,
        "payload": stored,
    }
    digest = event_hash(prev, content)
    conn.execute(
        text(
            "INSERT INTO audit_events (chain_id, seq, occurred_at, actor_id, case_id, event_type,"
            " payload, prev_hash, hash) VALUES (:chain_id, :seq, :occurred_at, :actor_id, :case_id,"
            " :event_type, CAST(:payload AS jsonb), :prev, :hash)"
        ),
        {**content, "payload": json.dumps(stored), "prev": prev, "hash": digest},
    )
    return AuditEvent(
        chain_id, seq, occurred_at, actor_id, case_id, event_type, stored, prev, digest
    )
