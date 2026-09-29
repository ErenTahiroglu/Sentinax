"""
backend/engine/private/macro/us_treasury_curve.py
=================================================
Exact U.S. Treasury yield-curve slope evidence (Phase 17D). Pure calculation over Phase 17C histories.

Scope: the signed spreads `10Y - 2Y` and `10Y - 3M` in percentage points, per effective date, and nothing else.
No normalization, score, regime, inversion flag, probability or portfolio action.

Architectural Invariants:
    - Inputs are exact tuples of exact `MacroStateInputFact` (one history per tenor, bound to its canonical key).
      No I/O, no clock, no registry search, no aliasing.
    - Every fact must carry the caller's `mode` and an `as_of` equal to the caller's; histories must be strictly
      increasing by `effective_date` (validated, never repaired).
    - Alignment is the UNION of actual effective dates. A tenor without a row on a date contributes `None` (not a
      synthetic fact, not a forward fill, not the nearest date). An explicit UNAVAILABLE fact is retained as-is and
      yields no slope; it is distinct from a missing row.
    - Each slope needs only its own two usable numeric values. Component facts are retained by identity.
    - Subtraction is exact and independent of the ambient Decimal context: it is done on integer coefficients at a
      common exponent and rebuilt from (sign, digits, exponent). No float, rounding or quantization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from backend.engine.private.domain import AsOfMode
from backend.engine.private.macro.state_inputs import MacroStateInputFact

_KEY_10Y = "US_TREASURY_PAR_10Y"
_KEY_2Y = "US_TREASURY_PAR_2Y"
_KEY_3M = "US_TREASURY_PAR_3M"

_ERR_MODE_TYPE = "mode must be an exact AsOfMode instance"
_ERR_AS_OF_TYPE = "as_of must be an exact timezone-aware datetime instance"
_ERR_MEMBER = "every history member must be an exact MacroStateInputFact"


@dataclass(frozen=True)
class USTreasuryCurveSlopePoint:
    """Cross-tenor evidence for one effective date. Slopes are signed percentage-point spreads (evidence only)."""
    effective_date: date
    mode: AsOfMode
    as_of: datetime
    ten_year: MacroStateInputFact | None
    two_year: MacroStateInputFact | None
    three_month: MacroStateInputFact | None
    slope_10y_2y: Decimal | None
    slope_10y_3m: Decimal | None


def _exact_subtract(minuend: Decimal, subtrahend: Decimal) -> Decimal:
    """`minuend - subtrahend` exactly, whatever the ambient Decimal context."""
    a_sign, a_digits, a_exp = minuend.as_tuple()
    b_sign, b_digits, b_exp = subtrahend.as_tuple()
    exponent = min(a_exp, b_exp)
    a_int = int("".join(map(str, a_digits))) * 10 ** (a_exp - exponent)
    b_int = int("".join(map(str, b_digits))) * 10 ** (b_exp - exponent)
    difference = (-a_int if a_sign else a_int) - (-b_int if b_sign else b_int)
    digits = tuple(int(c) for c in str(abs(difference)))
    return Decimal((1 if difference < 0 else 0, digits, exponent))


def _slope(long_leg: MacroStateInputFact | None, short_leg: MacroStateInputFact | None) -> Decimal | None:
    if long_leg is None or short_leg is None or long_leg.value is None or short_leg.value is None:
        return None
    return _exact_subtract(long_leg.value, short_leg.value)


def _validate_history(
    name: str, history: object, key: str, mode: AsOfMode, as_of: datetime,
) -> dict[date, MacroStateInputFact]:
    if type(history) is not tuple:
        raise TypeError(f"{name} history must be an exact tuple")
    by_date: dict[date, MacroStateInputFact] = {}
    previous: date | None = None
    for fact in history:
        if type(fact) is not MacroStateInputFact:
            raise TypeError(_ERR_MEMBER)
        if fact.canonical_key != key:
            raise ValueError(f"{name} history canonical_key must be {key}")
        if fact.mode is not mode:
            raise ValueError(f"{name} history fact mode does not match the requested mode")
        if fact.as_of != as_of:
            raise ValueError(f"{name} history fact as_of does not match the requested as_of")
        if previous is not None and fact.effective_date <= previous:
            raise ValueError(f"{name} history must have strictly increasing effective_date")
        previous = fact.effective_date
        by_date[fact.effective_date] = fact
    return by_date


def build_us_treasury_curve_slope_history(
    *,
    ten_year_history: tuple[MacroStateInputFact, ...],
    two_year_history: tuple[MacroStateInputFact, ...],
    three_month_history: tuple[MacroStateInputFact, ...],
    mode: AsOfMode,
    as_of: datetime,
) -> tuple[USTreasuryCurveSlopePoint, ...]:
    if type(mode) is not AsOfMode:
        raise TypeError(_ERR_MODE_TYPE)
    if type(as_of) is not datetime or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise TypeError(_ERR_AS_OF_TYPE)

    ten = _validate_history("ten_year", ten_year_history, _KEY_10Y, mode, as_of)
    two = _validate_history("two_year", two_year_history, _KEY_2Y, mode, as_of)
    three = _validate_history("three_month", three_month_history, _KEY_3M, mode, as_of)

    points: list[USTreasuryCurveSlopePoint] = []
    for day in sorted(ten.keys() | two.keys() | three.keys()):
        ten_fact, two_fact, three_fact = ten.get(day), two.get(day), three.get(day)
        points.append(USTreasuryCurveSlopePoint(
            effective_date=day,
            mode=mode,
            as_of=as_of,
            ten_year=ten_fact,
            two_year=two_fact,
            three_month=three_fact,
            slope_10y_2y=_slope(ten_fact, two_fact),
            slope_10y_3m=_slope(ten_fact, three_fact),
        ))
    return tuple(points)
