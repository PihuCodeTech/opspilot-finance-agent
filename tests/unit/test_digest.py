import datetime as dt
import uuid
from decimal import Decimal

import pytest

from opspilot.domain.digest import canonical_json, sha256_hex
from opspilot.domain.enums import ActionType
from opspilot.domain.money import Money


def test_key_order_and_whitespace_do_not_matter() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'
    assert sha256_hex({"a": 1, "b": 2}) == sha256_hex({"b": 2, "a": 1})


def test_values_have_one_spelling() -> None:
    when = dt.datetime(2026, 10, 24, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    ident = uuid.UUID("12345678-1234-5678-1234-567812345678")
    out = canonical_json(
        {
            "d": Decimal("1.50"),
            "m": Money.of("2"),
            "e": ActionType.PLACE_ON_HOLD,
            "t": when,
            "u": ident,
        }
    )
    assert out == (
        b'{"d":"1.50","e":"place_on_hold","m":"2.00",'
        b'"t":"2026-10-24T12:30:00+00:00","u":"12345678-1234-5678-1234-567812345678"}'
    )


def test_sets_are_sorted() -> None:
    assert canonical_json(frozenset({"b", "a", "c"})) == b'["a","b","c"]'


@pytest.mark.parametrize(
    "bad",
    [1.5, dt.datetime(2026, 1, 1), object()],  # float, naive datetime, unknown type
)
def test_ambiguous_values_are_refused(bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json(bad)
