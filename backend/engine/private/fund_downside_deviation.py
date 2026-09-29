"""
backend/engine/private/fund_downside_deviation.py
=================================================
Deterministic rolling TEFAS downside deviation for the Private Investment Decision Engine (Phase 16E).

Formula (N = ALL valid rolling windows, not only downside windows):
    DD = sqrt( (1 / N) * sum( min(r_t - MAR, 0) ** 2 ) )

Architectural Invariants:
    - Pure domain module. Zero network, filesystem, database, ambient clock, UUID generation, randomness, hashing,
      float arithmetic, pandas/numpy/scipy, persistence, or provider/resolver calls.
    - Consumes ONLY an exact Phase 16D `TefasFundRollingReturnSeries` (never raw prices, observations, a
      `TefasFundReturnSeries`, or bare Decimal tuples), retained by identity; price provenance, PIT context,
      horizon, monthly continuity, and canonical return arithmetic all live in that source.
    - `minimum_acceptable_return` (MAR) is MANDATORY, explicit, and has NO default (no hidden zero). It is a
      cumulative hurdle over the SAME horizon as the rolling windows (12M windows -> a 12M cumulative hurdle,
      and so on): there is no unit conversion, annualization, or MAR inference from goals, inflation, risk
      profile, benchmarks, or yields. Exact `Decimal`, finite, `>= -1`.
    - Windows above the MAR contribute 0 to the numerator but stay in the denominator; there is no sample
      correction (no N-1, no Bessel).
    - Availability: an empty source (insufficient history) yields `downside_deviation = None` (unavailable, NOT zero
      downside risk). A non-empty source with no shortfall yields an OBSERVED exact `Decimal("0")`.
      `shortfall_count` counts strict `r_t < MAR` (a return equal to the MAR is not a shortfall).
    - All arithmetic uses an explicit fresh `decimal.Context` (50 significant digits, ROUND_HALF_EVEN,
      Emin/Emax = MIN_EMIN/MAX_EMAX), never the ambient context, no quantization. A result beyond even that range
      fails closed with a static ValueError (only `decimal.Overflow` is translated).
    - `TefasFundDownsideDeviation` is self-validating: its constructor recomputes the canonical value from the
      source and MAR through the same private helper as the builder and rejects any forged value.
    - Descriptive diagnostic only: Phase 16D windows overlap heavily, so the observations are NOT statistically
      independent and no standard error, confidence interval, or significance is implied or corrected.
    - No Sortino/Sharpe/Calmar, mean or excess return, CVaR, annualization, peer logic, rankings, or scores.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal

from backend.engine.private.fund_rolling_returns import TefasFundRollingReturnSeries

# Analytical (not monetary) precision: significant digits for the downside-deviation arithmetic.
_DOWNSIDE_DECIMAL_PRECISION = 50

_ERR_SOURCE_TYPE = "source must be an exact TefasFundRollingReturnSeries instance"
_ERR_MAR_TYPE = "minimum_acceptable_return must be an exact Decimal instance"
_ERR_MAR_VALUE = "minimum_acceptable_return must be a finite Decimal greater than or equal to -1"
_ERR_DD_TYPE = "downside_deviation must be None or an exact Decimal instance"
_ERR_DD_VALUE = "downside_deviation must be a finite Decimal greater than or equal to 0"
_ERR_MATCH = "downside deviation result must match the canonical calculation from source"
_ERR_RANGE = "TEFAS downside deviation exceeds supported Decimal analytics range"


def _downside_context() -> decimal.Context:
    """A fresh explicit analytics context; independent of, and never mutating, the ambient Decimal context."""
    return decimal.Context(
        prec=_DOWNSIDE_DECIMAL_PRECISION,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
    )


def _require_source(source: object) -> None:
    if type(source) is not TefasFundRollingReturnSeries:
        raise TypeError(_ERR_SOURCE_TYPE)


def _require_mar(mar: object) -> None:
    if type(mar) is not Decimal:
        raise TypeError(_ERR_MAR_TYPE)
    if not mar.is_finite() or not mar >= Decimal(-1):
        raise ValueError(_ERR_MAR_VALUE)


def _canonical_downside_deviation(
    source: TefasFundRollingReturnSeries, mar: Decimal
) -> Decimal | None:
    """Single canonical calculation shared by the builder and the constructor verification."""
    points = source.points
    if len(points) == 0:
        return None  # insufficient history: unavailable, never zero risk

    context = _downside_context()
    try:
        total = Decimal(0)
        for point in points:
            delta = context.subtract(point.simple_return, mar)
            if delta < 0:
                total = context.add(total, context.multiply(delta, delta))
        mean_squared_shortfall = context.divide(total, Decimal(len(points)))
        return context.sqrt(mean_squared_shortfall)
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None


@dataclass(frozen=True)
class TefasFundDownsideDeviation:
    """Downside deviation of rolling returns against an explicit same-horizon minimum acceptable return."""
    source: TefasFundRollingReturnSeries
    minimum_acceptable_return: Decimal
    downside_deviation: Decimal | None

    def __post_init__(self) -> None:
        _require_source(self.source)
        _require_mar(self.minimum_acceptable_return)
        if self.downside_deviation is not None:
            if type(self.downside_deviation) is not Decimal:
                raise TypeError(_ERR_DD_TYPE)
            if not self.downside_deviation.is_finite() or not self.downside_deviation >= Decimal(0):
                raise ValueError(_ERR_DD_VALUE)

        canonical = _canonical_downside_deviation(self.source, self.minimum_acceptable_return)
        if canonical is None:
            if self.downside_deviation is not None:
                raise ValueError(_ERR_MATCH)
        elif self.downside_deviation is None or self.downside_deviation != canonical:
            raise ValueError(_ERR_MATCH)

    @property
    def is_available(self) -> bool:
        return self.downside_deviation is not None

    @property
    def observation_count(self) -> int:
        return len(self.source.points)

    @property
    def shortfall_count(self) -> int:
        return sum(1 for point in self.source.points if point.simple_return < self.minimum_acceptable_return)


def calculate_tefas_fund_downside_deviation(
    *,
    rolling_series: TefasFundRollingReturnSeries,
    minimum_acceptable_return: Decimal,
) -> TefasFundDownsideDeviation:
    """
    Calculate downside deviation of a Phase 16D rolling series against an explicit same-horizon MAR.
    """
    _require_source(rolling_series)
    _require_mar(minimum_acceptable_return)
    return TefasFundDownsideDeviation(
        source=rolling_series,
        minimum_acceptable_return=minimum_acceptable_return,
        downside_deviation=_canonical_downside_deviation(rolling_series, minimum_acceptable_return),
    )
