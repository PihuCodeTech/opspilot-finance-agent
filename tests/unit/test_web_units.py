"""Web building blocks without a database: limits, cursors, masking, uploads, highlights."""

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from opspilot.cases.errors import ValidationFailedError
from opspilot.cases.queries import _apply_mask, _fingerprints, decode_cursor, encode_cursor
from opspilot.web import errors
from opspilot.web.middleware import BodySizeLimitMiddleware
from opspilot.web.pages import Box, _pixels, find_boxes, find_redactions
from opspilot.web.ratelimit import Limit, RateLimitedError, RateLimiter
from opspilot.web.schemas import Approve, RecordResolution
from opspilot.web.uploads import JPEG, PDF, PNG, UnsupportedMediaTypeError, sniff

FP, OTHER = "3f9a1c2b7d10", "0123456789ab"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_rate_limiter_counts_per_key_and_reports_the_wait() -> None:
    clock = Clock()
    limiter = RateLimiter(clock)
    limit = Limit(2, 60)
    limiter.hit((("a",), limit))
    limiter.hit((("a",), limit))
    limiter.hit((("b",), limit))  # another key is unaffected
    with pytest.raises(RateLimitedError) as refused:
        limiter.hit((("a",), limit))
    assert refused.value.retry_after == 60
    clock.now += 30
    with pytest.raises(RateLimitedError) as again:
        limiter.hit((("a",), limit))
    assert again.value.retry_after == 30  # refusals don't extend the wait
    clock.now += 31
    limiter.hit((("a",), limit))


def test_a_refused_attempt_counts_against_no_key() -> None:
    limiter = RateLimiter(Clock())
    tight, loose = Limit(1, 60), Limit(5, 60)
    limiter.hit((("tight",), tight), (("loose",), loose))
    for _ in range(3):
        with pytest.raises(RateLimitedError):
            limiter.hit((("tight",), tight), (("loose",), loose))
    for _ in range(4):
        limiter.hit((("loose",), loose))  # still 4 left: refusals weren't counted


def test_cursors_round_trip_and_reject_garbage() -> None:
    at = dt.datetime(2026, 10, 26, 12, 0, tzinfo=dt.UTC)
    ident = uuid.uuid4()
    assert decode_cursor(encode_cursor(at, ident), 2) == [at.isoformat(), str(ident)]
    for bad in ("%%%", encode_cursor("only-one"), "ÿ"):
        with pytest.raises(ValidationFailedError):
            decode_cursor(bad, 2)


def test_every_fingerprint_shape_is_masked() -> None:
    value = {
        "compared": {"invoice_bank_fp": FP, "vendor_master_bank_fp": OTHER},
        "evidence_refs": [
            {"entity": "invoice", "field": "bank_account", "value": f"BANK_ACCT[fp:{FP}]"},
            {"entity": "vendor_bank_account", "field": "bank_fp", "value": OTHER},
            {"entity": "invoice", "field": "total", "value": "108.25"},
        ],
        "note": f"paid to BANK_ACCT[fp:{FP}] last time",
    }
    found: set[str] = set()
    _fingerprints(value, found)
    assert found == {FP, OTHER}
    masked = _apply_mask(value, {FP: "4471"})
    text = str(masked)
    assert FP not in text and OTHER not in text
    assert masked["compared"] == {"invoice_bank_fp": "••••4471", "vendor_master_bank_fp": "••••"}
    assert masked["evidence_refs"][0]["value"] == "BANK_ACCT[••••4471]"
    assert masked["evidence_refs"][2]["value"] == "108.25"  # other values untouched


def test_types_come_from_the_bytes() -> None:
    assert sniff(b"%PDF-1.7 ...") == PDF
    assert sniff(b"\x89PNG\r\n\x1a\n...") == PNG
    assert sniff(b"\xff\xd8\xff\xe0...") == JPEG
    for spoof in (b"GIF89a", b"<html>", b"MZ\x90", b" %PDF-1.7"):
        with pytest.raises(UnsupportedMediaTypeError):
            sniff(spoof)


def test_highlights_match_numbers_by_value_and_text_by_substring() -> None:
    coordinates = {
        "table_cells": [
            {"text": "1,185.00", "page": 1, "bbox": [10, 700, 60, 690], "origin": "BOTTOMLEFT"},
            {"text": "1,185.00", "page": 2, "bbox": [10, 700, 60, 690], "origin": "BOTTOMLEFT"},
        ],
        "items": [
            {
                "text": "Purchase order PO-26-010013",
                "page": 1,
                "bbox": [1, 2, 3, 4],
                "origin": "TOPLEFT",
            },
            {"text": "Qty 1", "page": 1, "bbox": [5, 6, 7, 8], "origin": "TOPLEFT"},
        ],
    }
    boxes = find_boxes(coordinates, ["1185.00", "PO-26-010013", "A"], page=1)
    assert [b.bbox for b in boxes] == [(10, 700, 60, 690), (1, 2, 3, 4)]
    assert find_boxes(coordinates, ["1185.01"], page=1) == []


def test_bottom_left_boxes_flip_to_image_coordinates() -> None:
    box = Box(1, (100, 700, 160, 680), "BOTTOMLEFT")
    left, top, right, bottom = _pixels(box, height_pt=792, scale=2)
    assert (left, right) == (198, 322)
    assert (top, bottom) == (2 * 92 - 2, 2 * 112 + 2)  # 792-700 and 792-680, padded


def test_forms_carry_lists_and_structured_values() -> None:
    rid = uuid.uuid4()
    approve = Approve.model_validate(
        Approve.from_form(
            FormData(
                [
                    ("csrf_token", "t"),
                    ("expected_version", "3"),
                    ("accepted_resolution", f"{rid}:{'a' * 64}"),
                    ("note", ""),
                ]
            )
        )
    )
    assert approve.expected_version == 3 and approve.note is None
    assert approve.accepted_resolutions[0].resolution_id == rid
    doc, ledger = uuid.uuid4(), uuid.uuid4()
    resolution = RecordResolution.model_validate(
        RecordResolution.from_form(
            FormData(
                [
                    ("csrf_token", "t"),
                    ("expected_version", "2"),
                    ("resolution_type", "not_duplicate"),
                    ("summary", "different goods"),
                    ("evidence", f"supporting:{doc}"),
                    ("evidence", f"erp:ledger_invoice:{ledger}"),
                    ("structured.distinguishing_fact", "different_goods"),
                ]
            )
        )
    )
    assert [e.supporting_document_id for e in resolution.evidence] == [doc, None]
    assert resolution.evidence[1].erp_entity == "ledger_invoice"
    assert resolution.structured == {"distinguishing_fact": "different_goods"}


def test_json_money_is_a_string_never_a_float() -> None:
    encoded = errors.to_json(
        {
            "amount": Decimal("1234.50"),
            "at": dt.datetime(2026, 10, 26, 12, tzinfo=dt.UTC),
            "digest": b"\x01\xff",
        }
    )
    assert encoded == {"amount": "1234.50", "at": "2026-10-26T12:00:00Z", "digest": "01ff"}


def _echo_app(limit: int) -> TestClient:
    app = FastAPI()

    @app.post("/api/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    errors.install(app)
    return TestClient(BodySizeLimitMiddleware(app, max_bytes=limit))


def test_oversized_bodies_are_refused_before_parsing() -> None:
    client = _echo_app(100)
    assert client.post("/api/echo", content=b"x" * 100).json() == {"size": 100}
    declared = client.post("/api/echo", content=b"x" * 101)
    assert declared.status_code == 413
    assert declared.json()["error"]["code"] == "file_too_large"

    def chunks() -> Iterator[bytes]:
        yield b"x" * 60
        yield b"x" * 60

    streamed = client.post("/api/echo", content=chunks())  # no Content-Length
    assert streamed.status_code == 413


def test_bank_numbers_are_redacted_wherever_the_parse_saw_one() -> None:
    coordinates = {
        "items": [
            {
                "text": f"Pay to account: BANK_ACCT[fp:{FP}]",
                "page": 1,
                "bbox": [1, 2, 3, 4],
                "origin": "TOPLEFT",
            },
            {"text": "Total due 108.25", "page": 1, "bbox": [5, 6, 7, 8], "origin": "TOPLEFT"},
            {"text": f"BANK_ACCT[fp:{FP}]", "page": 2, "bbox": [9, 9, 9, 9], "origin": "TOPLEFT"},
        ]
    }
    assert [b.bbox for b in find_redactions(coordinates, 1)] == [(1, 2, 3, 4)]
