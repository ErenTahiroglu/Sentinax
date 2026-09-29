"""
backend/engine/private/fund_volatility.py
=========================================
Canonical month-end monthly returns and historical monthly / annualized fund volatility for the Private
Investment Decision Engine (Phase 16K).

The methodology requires volatility as a risk diagnostic but defines neither sampling frequency, the sample vs
population denominator, nor the annualization convention. This module establishes the explicit Sentinax Phase 16K
engineering convention (it is NOT a formula already defined by the methodology report):

    - Sampling: the LAST authoritative price observation of each calendar month (no averaging, first observation,
      or nearest-day inference). Day of month need not match between months.
    - Return: adjacent calendar-month simple return, through the Phase 16B `_simple_return` (sole return
      arithmetic authority; its range errors propagate unchanged).
    - Volatility: sample standard deviation of monthly returns, denominator N - 1.
    - Annualization: `sqrt(sample_variance * 12)`, computed directly from the variance so rounding occurs in one
      defined arithmetic path. No 252/365 day-count annualization.

Architectural Invariants:
    - Pure domain module. Zero network, filesystem, database, ambient clock, randomness, hashing, float
      arithmetic, pandas/numpy/scipy, persistence, or provider/resolver calls. Phase 16A (`TefasFundPriceSeries`)
      is the sole price authority and is retained by identity. The sample is NOT the overlapping Phase 16D rolling
      windows; this module never imports the rolling or annualized-return modules.
    - Requires a complete source (no explicit gaps) and calendar-month continuity between the first and last
      represented month; nothing is bridged or interpolated. One represented month is valid and yields no monthly
      returns. Source temporal semantics (CURRENT_REPORTED / SYSTEM_AS_OF; SOURCE_AS_OF is gap-only and therefore
      rejected) are inherited, not reinterpreted.
    - Fewer than two monthly returns means volatility is unavailable (None / None), never zero. This is
      mathematical availability, not a statistical sufficiency policy.
    - Decimal arithmetic uses an explicit fresh 50-digit ROUND_HALF_EVEN context with maximum exponent range,
      never the ambient context. Only `decimal.Overflow` is translated (static range error).
    - Constructors recompute canonical points / volatility through the same private helpers as the builders and
      reject forged values.
    - sqrt(12) scaling is a conventional descriptive scaling, not a forecast of realized volatility and not a proof
      of iid returns or zero serial correlation.
    - No Sharpe/Sortino, excess return, risk-free rate, MAR, covariance, stress, bootstrap, ranking, or score.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from backend.engine.private.fund_price_series import TefasFundPricePoint, TefasFundPriceSeries
from backend.engine.private.fund_return_series import _simple_return

_ERR_SOURCE_TYPE = "source must be an exact TefasFundPriceSeries instance"
_ERR_INCOMPLETE = "TEFAS fund volatility requires a complete price series"
_ERR_CONTINUITY = "TEFAS fund volatility requires continuous monthly price coverage"
_ERR_POINTS_TYPE = "points must be a tuple of exact TefasFundMonthlyReturnPoint instances"
_ERR_POINTS_MATCH = "points must match the canonical month-end monthly returns exactly"
_ERR_MONTHLY_TYPE = "source must be an exact TefasFundMonthlyReturnSeries instance"
_ERR_VALUE_TYPE = "volatility values must be exact Decimal instances or None"
_ERR_PAIR = "monthly_volatility and annualized_volatility must both be None or both be Decimal"
_ERR_VALUE_RANGE = "volatility values must be finite and non-negative"
_ERR_VOL_MATCH = "historical volatility must match the canonical sample calculation exactly"
_ERR_RANGE = "TEFAS fund volatility exceeds supported Decimal analytics range"

_MONTHS_PER_YEAR = Decimal(12)


def _volatility_context() -> decimal.Context:
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


def _month_key(day: date) -> tuple[int, int]:
    return (day.year, day.month)


def _month_index(key: tuple[int, int]) -> int:
    return key[0] * 12 + (key[1] - 1)


@dataclass(frozen=True)
class TefasFundMonthlyReturnPoint:
    """One simple return between two adjacent calendar-month-end representatives (decimal fraction)."""
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
        if not self.simple_return.is_finite() or not self.simple_return >= Decimal(-1):
            raise ValueError("simple_return must be a finite Decimal greater than or equal to -1")


def _require_complete_source(source: object) -> None:
    if type(source) is not TefasFundPriceSeries:
        raise TypeError(_ERR_SOURCE_TYPE)
    if len(source.gaps) != 0:
        raise ValueError(_ERR_INCOMPLETE)


def _canonical_points(source: TefasFundPriceSeries) -> tuple[TefasFundMonthlyReturnPoint, ...]:
    """Single canonical month-end sampling shared by the builder and the constructor verification."""
    last_by_month: dict[tuple[int, int], TefasFundPricePoint] = {}
    for point in source.points:  # canonical ascending order: later points in a month override earlier ones
        last_by_month[_month_key(point.trade_date)] = point
    months = sorted(last_by_month)
    if not months:
        return ()
    if len(months) != _month_index(months[-1]) - _month_index(months[0]) + 1:
        raise ValueError(_ERR_CONTINUITY)
    representatives = [last_by_month[m] for m in months]
    return tuple(
        TefasFundMonthlyReturnPoint(
            start_date=start.trade_date,
            end_date=end.trade_date,
            simple_return=_simple_return(start.unit_price, end.unit_price),
        )
        for start, end in zip(representatives, representatives[1:])
    )


@dataclass(frozen=True)
class TefasFundMonthlyReturnSeries:
    """Adjacent calendar-month-end simple returns, retaining the authoritative price source."""
    source: TefasFundPriceSeries
    points: tuple[TefasFundMonthlyReturnPoint, ...]

    def __post_init__(self) -> None:
        _require_complete_source(self.source)
        if type(self.points) is not tuple or any(type(p) is not TefasFundMonthlyReturnPoint for p in self.points):
            raise TypeError(_ERR_POINTS_TYPE)
        if self.points != _canonical_points(self.source):
            raise ValueError(_ERR_POINTS_MATCH)

    @property
    def is_available(self) -> bool:
        return len(self.points) > 0

    @property
    def observation_count(self) -> int:
        return len(self.points)


def build_tefas_fund_monthly_return_series(
    *,
    price_series: TefasFundPriceSeries,
) -> TefasFundMonthlyReturnSeries:
    """Build canonical month-end monthly returns from a complete, monthly-continuous authoritative price series."""
    _require_complete_source(price_series)
    return TefasFundMonthlyReturnSeries(source=price_series, points=_canonical_points(price_series))


def _canonical_volatility(
    monthly_returns: TefasFundMonthlyReturnSeries,
) -> tuple[Decimal, Decimal] | tuple[None, None]:
    """Single canonical sample-volatility calculation shared by the builder and the constructor verification."""
    returns = [p.simple_return for p in monthly_returns.points]
    count = len(returns)
    if count < 2:
        return (None, None)
    ctx = _volatility_context()
    try:
        total = Decimal(0)
        for value in returns:
            total = ctx.add(total, value)
        mean = ctx.divide(total, Decimal(count))
        squares = Decimal(0)
        for value in returns:
            deviation = ctx.subtract(value, mean)
            squares = ctx.add(squares, ctx.multiply(deviation, deviation))
        variance = ctx.divide(squares, Decimal(count - 1))
        monthly = ctx.sqrt(variance)
        annualized = ctx.sqrt(ctx.multiply(variance, _MONTHS_PER_YEAR))
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None
    return (monthly, annualized)


@dataclass(frozen=True)
class TefasFundHistoricalVolatility:
    """Sample monthly volatility and sqrt(12)-annualized volatility of month-end monthly returns."""
    source: TefasFundMonthlyReturnSeries
    monthly_volatility: Decimal | None
    annualized_volatility: Decimal | None

    def __post_init__(self) -> None:
        if type(self.source) is not TefasFundMonthlyReturnSeries:
            raise TypeError(_ERR_MONTHLY_TYPE)
        values = (self.monthly_volatility, self.annualized_volatility)
        if any(v is not None and type(v) is not Decimal for v in values):
            raise TypeError(_ERR_VALUE_TYPE)
        if (self.monthly_volatility is None) != (self.annualized_volatility is None):
            raise ValueError(_ERR_PAIR)
        if self.monthly_volatility is not None:
            if any(not v.is_finite() or not v >= Decimal(0) for v in values):  # type: ignore[union-attr]
                raise ValueError(_ERR_VALUE_RANGE)
        if values != _canonical_volatility(self.source):
            raise ValueError(_ERR_VOL_MATCH)

    @property
    def is_available(self) -> bool:
        return self.monthly_volatility is not None


def calculate_tefas_fund_historical_volatility(
    *,
    monthly_returns: TefasFundMonthlyReturnSeries,
) -> TefasFundHistoricalVolatility:
    """Sample (N - 1) monthly volatility and sqrt(sample_variance * 12) annualized volatility."""
    if type(monthly_returns) is not TefasFundMonthlyReturnSeries:
        raise TypeError(_ERR_MONTHLY_TYPE)
    monthly, annualized = _canonical_volatility(monthly_returns)
    return TefasFundHistoricalVolatility(
        source=monthly_returns,
        monthly_volatility=monthly,
        annualized_volatility=annualized,
    )
