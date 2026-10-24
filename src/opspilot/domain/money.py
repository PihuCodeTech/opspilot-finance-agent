"""Money, unit prices and quantities as exact decimals (never float).

Precision follows `04-prd.md` D8 and `07-design.md` §5: money 2 dp, unit
prices up to 4 dp, quantities up to 3 dp, all rounded half-up. Single
currency (USD) in the MVP.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CURRENCY = "USD"
_CENT = Decimal("0.01")
_UNIT_PRICE_STEP = Decimal("0.0001")
_QUANTITY_STEP = Decimal("0.001")
_PERCENT_STEP = Decimal("0.01")
_HUNDRED = Decimal(100)

DecimalLike = Decimal | int | str


def to_decimal(value: DecimalLike) -> Decimal:
    """Convert exactly; floats are refused so binary rounding can't creep in."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise TypeError(f"expected Decimal, int or str, got {type(value).__name__}")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"not a decimal number: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return result


def round_money(value: DecimalLike) -> Decimal:
    return to_decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def round_unit_price(value: DecimalLike) -> Decimal:
    return to_decimal(value).quantize(_UNIT_PRICE_STEP, rounding=ROUND_HALF_UP)


def round_quantity(value: DecimalLike) -> Decimal:
    return to_decimal(value).quantize(_QUANTITY_STEP, rounding=ROUND_HALF_UP)


def round_percent(value: DecimalLike) -> Decimal:
    return to_decimal(value).quantize(_PERCENT_STEP, rounding=ROUND_HALF_UP)


def percent_change(new: DecimalLike, base: DecimalLike) -> Decimal:
    """(new − base) ÷ base × 100, rounded to 2 dp (the PRD's price variance formula)."""
    base_d = to_decimal(base)
    if base_d == 0:
        raise ZeroDivisionError("percent change from a zero base is undefined")
    return round_percent((to_decimal(new) - base_d) / base_d * _HUNDRED)


@dataclass(frozen=True, order=True)
class Money:
    """An amount in USD, always held at 2 dp."""

    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", round_money(self.amount))

    @classmethod
    def of(cls, value: DecimalLike) -> Money:
        return cls(to_decimal(value))

    @classmethod
    def zero(cls) -> Money:
        return cls(Decimal(0))

    def __add__(self, other: Money) -> Money:
        return Money(self.amount + other.amount)

    def __sub__(self, other: Money) -> Money:
        return Money(self.amount - other.amount)

    def __neg__(self) -> Money:
        return Money(-self.amount)

    def times(self, factor: DecimalLike) -> Money:
        """Multiply (e.g. quantity × unit price), rounding the result half-up to cents."""
        return Money(self.amount * to_decimal(factor))

    def __str__(self) -> str:
        return format_money(self)


def format_money(value: Money, *, signed: bool = False) -> str:
    """`$1,234.50`; with `signed`, `+$1.50` / `−$1.50` (U+2212 minus, as in the UI)."""
    amount = value.amount
    body = f"${abs(amount):,.2f}"
    if amount < 0:
        return f"−{body}"
    return f"+{body}" if signed else body


def format_percent(value: DecimalLike, *, signed: bool = True) -> str:
    pct = round_percent(value)
    body = f"{abs(pct):.2f}%"
    if pct < 0:
        return f"−{body}"
    return f"+{body}" if signed and pct > 0 else body
