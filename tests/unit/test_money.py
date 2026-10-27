from decimal import Decimal

import pytest

from opspilot.domain.money import (
    Money,
    format_money,
    format_percent,
    percent_change,
    round_money,
    round_quantity,
    round_unit_price,
    to_decimal,
)


def test_rounding_is_half_up_not_bankers() -> None:
    assert round_money("2.345") == Decimal("2.35")
    assert round_money("2.355") == Decimal("2.36")
    assert round_money("-2.345") == Decimal("-2.35")
    assert round_unit_price("1.23455") == Decimal("1.2346")
    assert round_quantity("0.0005") == Decimal("0.001")


def test_floats_are_refused() -> None:
    with pytest.raises(TypeError):
        to_decimal(0.1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money.of(True)


@pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity"])
def test_non_numbers_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        to_decimal(bad)


def test_money_arithmetic_stays_at_cents() -> None:
    total = Money.of("12.50").times("3") + Money.of("0.015")
    assert total.amount == Decimal("37.52")
    assert (Money.of("1.00") - Money.of("1.50")).amount == Decimal("-0.50")
    assert Money.of("10") > Money.of("9.99")


def test_line_total_rounds_once() -> None:
    # 3 × 1.235 = 3.705 → 3.71 (half-up)
    assert Money.of("1.235").amount == Decimal("1.24")  # money itself is at cents
    assert Money.of("3").times("1.235").amount == Decimal("3.71")


def test_percent_change_matches_the_prd_formula() -> None:
    assert percent_change("11.22", "11.00") == Decimal("2.00")
    assert percent_change("11.2211", "11.00") == Decimal("2.01")
    assert percent_change("10.78", "11.00") == Decimal("-2.00")
    with pytest.raises(ZeroDivisionError):
        percent_change("1", "0")


def test_formatting() -> None:
    assert format_money(Money.of("14212.5")) == "$14,212.50"
    assert format_money(Money.of("1.5"), signed=True) == "+$1.50"
    assert format_money(Money.of("-1.5")) == "−$1.50"
    assert format_money(Money.zero()) == "$0.00"
    assert format_percent("2.125") == "+2.13%"
    assert format_percent("-2.13") == "−2.13%"
    assert format_percent("0") == "0.00%"
