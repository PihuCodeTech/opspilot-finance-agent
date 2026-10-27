import datetime as dt
import uuid

import pytest

from opspilot.audit.chain import ZERO_HASH, AuditPayloadError, event_hash
from opspilot.audit.digests import proposal_digest, resolution_content, resolution_digest

OCCURRED = dt.datetime(2026, 10, 25, 9, 30, tzinfo=dt.UTC)


def content(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "chain_id": "live",
        "seq": 1,
        "occurred_at": OCCURRED,
        "actor_id": None,
        "case_id": None,
        "event_type": "case.created",
        "payload": {"note": "x"},
    }
    return base | changes


def test_hash_covers_previous_hash_and_every_field() -> None:
    first = event_hash(ZERO_HASH, content())
    assert first == event_hash(ZERO_HASH, content())
    assert event_hash(first, content()) != first
    for change in (
        {"seq": 2},
        {"chain_id": "eval:x"},
        {"occurred_at": OCCURRED + dt.timedelta(microseconds=1)},
        {"actor_id": uuid.UUID(int=1)},
        {"event_type": "case.closed"},
        {"payload": {"note": "y"}},
    ):
        assert event_hash(ZERO_HASH, content(**change)) != first, change


def test_timezone_does_not_change_the_hash() -> None:
    cest = OCCURRED.astimezone(dt.timezone(dt.timedelta(hours=2)))
    assert event_hash(ZERO_HASH, content(occurred_at=cest)) == event_hash(ZERO_HASH, content())


def test_proposal_digest_ignores_insertion_time_and_own_digest() -> None:
    row = {"id": uuid.UUID(int=7), "action": "place_on_hold", "reason": None}
    stamped = row | {"created_at": OCCURRED, "digest": b"\x01" * 32}
    assert proposal_digest(stamped) == proposal_digest(row)
    assert proposal_digest(row | {"action": "reject_invoice"}) != proposal_digest(row)


def test_resolution_digest_does_not_depend_on_row_order() -> None:
    evidence = [{"id": uuid.UUID(int=i), "erp_id": None} for i in range(3)]
    forward = resolution_content({"id": uuid.UUID(int=9)}, evidence, [], [])
    backward = resolution_content({"id": uuid.UUID(int=9)}, evidence[::-1], [], [])
    assert resolution_digest(forward) == resolution_digest(backward)


def test_json_floats_are_digested_as_decimals() -> None:
    row = {"id": uuid.UUID(int=1), "confidence": {"total": 0.95}}
    assert proposal_digest(row)  # canonical JSON alone refuses floats


def test_payload_with_account_like_digits_is_refused() -> None:
    from opspilot.audit.chain import append_event  # noqa: PLC0415

    with pytest.raises(AuditPayloadError):
        append_event(None, chain_id="live", event_type="x", payload={"note": "GB 1234567890"})  # type: ignore[arg-type]
