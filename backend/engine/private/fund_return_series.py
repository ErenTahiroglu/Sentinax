"""
backend/engine/private/fund_return_series.py
============================================
Deterministic TEFAS fund simple-return series for the Private Investment Decision Engine (Phase 16B).

Architectural Invariants:
    - Pure domain module. Zero network, filesystem, database, ambient clock, UUID generation, randomness, hashing,
      float arithmetic, pandas/numpy/scipy, persistence, or provider calls. It never calls the market-data resolver:
      Phase 16A (`TefasFundPriceSeries`) remains the sole price-series authority.
    - Returns derive ONLY from a complete `TefasFundPriceSeries` (exact type; no gaps; at least two points). Any gap
      fails closed: gaps are never bridged (D1 selected, D2 gap, D3 selected never yields a D1 -> D3 return) and
      missing is never zero. A SOURCE_AS_OF series is all gaps, so it is rejected. Source temporal semantics
      (CURRENT_REPORTED / SYSTEM_AS_OF / SOURCE_AS_OF) are inherited and not re-validated.
    - Simple return for adjacent authoritative prices P0 -> P1 is `P1 / P0 - 1`, stored as a decimal fraction
      (0.10 = +10%). Actual start/end dates are kept; there is no annualization, rescaling by elapsed days,
      calendar inference, or log return.
    - Arithmetic uses an explicit fresh `decimal.Context` (50 significant digits, ROUND_HALF_EVEN) for every division
      and subtraction; it never reads or mutates the ambient `decimal.getcontext()`. The 50-digit precision is an
      analytical contract, NOT money rounding, currency precision, database NUMERIC precision, or display precision.
      Repeating returns (for example 3 -> 4) are deterministic 50-digit-context approximations, not exact Decimals.
    - `TefasFundReturnSeries` retains the source series by identity and its constructor is self-validating: every
      return point must equal the canonical return recomputed from the adjacent source prices, so manual forgeries
      (wrong dates, wrong value, missing/extra/reordered points, bridging points) are rejected. Return points do
      not copy prices, currency, resolution keys, confidence, or timestamps: provenance lives in the source.
    - No minimum-sample policy, rolling/mean returns, volatility, downside deviation, drawdown, VaR/CVaR,
      Sharpe/Sortino/Calmar, capture ratios, rankings, scores, or recommendations.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from backend.engine.private.fund_price_series import TefasFundPriceSeries

# Analytical (not monetary) precision: significant digits for every return division/subtraction.
_RETURN_DECIMAL_PRECISION = 50

_ERR_SOURCE_TYPE = "source must be an exact TefasFundPriceSeries instance"
_ERR_INCOMPLETE = "TEFAS fund return series requires a complete price series"
_ERR_TOO_FEW = "TEFAS fund return series requires at least two price points"
_ERR_POINTS_TYPE = "points must be a tuple of exact TefasFundReturnPoint instances"
_ERR_POINTS_MATCH = "points must match consecutive source price points exactly"


def _return_context() -> decimal.Context:
    """A fresh explicit analytics context; independent of, and never mutating, the ambient Decimal context."""
    return decimal.Context(
        prec=_RETURN_DECIMAL_PRECISION,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
    )


def _simple_return(start_price: Decimal, end_price: Decimal) -> Decimal:
    context = _return_context()
    return context.subtract(context.divide(end_price, start_price), Decimal(1))


@dataclass(frozen=True)
class TefasFundReturnPoint:
    """One simple return between two adjacent authoritative price points (decimal fraction)."""
    start_date: date
    end_date: date
    simple_return: Decimal

    def __post_init__(self) -> None:
        if type(self.start_date) is not date:
            raise TypeError("start_date must be an exact date instance")
        if type(self.end_date) is not date:
            raise TypeError("end_date must be an exact date instance")
        if not self.start_date < self.end_date:
            raise ValueError("end_date must be after start_date")
        if type(self.simple_return) is not Decimal:
            raise TypeError("simple_return must be an exact Decimal instance")
        if not self.simple_return.is_finite() or not self.simple_return > Decimal(-1):
            raise ValueError("simple_return must be a finite Decimal greater than -1")


def _require_complete_source(source: object) -> None:
    if type(source) is not TefasFundPriceSeries:
        raise TypeError(_ERR_SOURCE_TYPE)
    if len(source.gaps) != 0:
        raise ValueError(_ERR_INCOMPLETE)
    if len(source.points) < 2:
        raise ValueError(_ERR_TOO_FEW)


@dataclass(frozen=True)
class TefasFundReturnSeries:
    """Simple returns over a complete authoritative price series, retaining that source by identity."""
    source: TefasFundPriceSeries
    points: tuple[TefasFundReturnPoint, ...]

    def __post_init__(self) -> None:
        _require_complete_source(self.source)
        if type(self.points) is not tuple or any(type(p) is not TefasFundReturnPoint for p in self.points):
            raise TypeError(_ERR_POINTS_TYPE)

        prices = self.source.points
        if len(self.points) != len(prices) - 1:
            raise ValueError(_ERR_POINTS_MATCH)
        for index, point in enumerate(self.points):
            start, end = prices[index], prices[index + 1]
            if (
                point.start_date != start.trade_date
                or point.end_date != end.trade_date
                or point.simple_return != _simple_return(start.unit_price, end.unit_price)
            ):
                raise ValueError(_ERR_POINTS_MATCH)


def build_tefas_fund_return_series(*, price_series: TefasFundPriceSeries) -> TefasFundReturnSeries:
    """
    Derive one simple return per adjacent pair of a complete authoritative price series.
    """
    _require_complete_source(price_series)
    prices = price_series.points
    points = tuple(
        TefasFundReturnPoint(
            start_date=prices[index].trade_date,
            end_date=prices[index + 1].trade_date,
            simple_return=_simple_return(prices[index].unit_price, prices[index + 1].unit_price),
        )
        for index in range(len(prices) - 1)
    )
    return TefasFundReturnSeries(source=price_series, points=points)
