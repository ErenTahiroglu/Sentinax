"""
backend/engine/private/fund_drawdown.py
=======================================
Deterministic TEFAS maximum drawdown + recovery diagnostics for the Private Investment Decision Engine
(Phase 16C).

Architectural Invariants:
    - Pure domain module. Zero network, filesystem, database, ambient clock, UUID generation, randomness, hashing,
      float arithmetic, pandas/numpy/scipy, persistence, or provider/resolver calls. Phase 16A
      (`TefasFundPriceSeries`) remains the sole price authority; raw observations, snapshots, legacy analyzers,
      and frontend math are never consumed.
    - Requires a complete authoritative source (exact type, no gaps, at least two points). Missing or insufficient
      data fails closed; it never yields a zero drawdown. A genuinely flat or rising complete series is an OBSERVED
      zero drawdown. SOURCE_AS_OF series are all gaps and are rejected. Source temporal semantics are inherited.
    - `max_drawdown` is a non-negative LOSS MAGNITUDE in [0, 1]: `1 - P_t / running_peak` (0.20 means a 20% loss),
      never a negative number. Arithmetic uses an explicit fresh `decimal.Context` (50 significant digits,
      ROUND_HALF_EVEN, Emin/Emax = MIN_EMIN/MAX_EMAX), never the ambient context, no quantization. An extremely
      small positive trough may analytically round to exactly 1 without any zero price.
    - Running peak moves only on a STRICTLY higher price (earliest peak date on equal prices); the maximum episode
      updates only on a STRICTLY deeper drawdown (earliest maximum wins ties).
    - Recovery is the first source point strictly after the trough with `price >= peak_price`; if none exists,
      `recovery_date is None` (no fabricated recovery at the analysis end). A zero drawdown reports the first point
      date as peak, trough, and recovery. Durations are CALENDAR days (no trading-day inference); unrecovered
      recovery/underwater durations are None.
    - `TefasFundDrawdown` retains the source by identity and its constructor recomputes the canonical result from
      that source through the same private helper as the builder, rejecting any forged field.
    - No rolling returns, volatility, downside deviation, Sortino, Calmar, VaR/CVaR, Sharpe, rankings, or scores.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from backend.engine.private.fund_price_series import TefasFundPriceSeries

# Analytical (not monetary) precision: significant digits for every drawdown division/subtraction.
_DRAWDOWN_DECIMAL_PRECISION = 50

_ERR_SOURCE_TYPE = "source must be an exact TefasFundPriceSeries instance"
_ERR_INCOMPLETE = "TEFAS fund drawdown requires a complete price series"
_ERR_TOO_FEW = "TEFAS fund drawdown requires at least two price points"
_ERR_MATCH = "drawdown result must match the canonical calculation from source"


def _drawdown_context() -> decimal.Context:
    """A fresh explicit analytics context; independent of, and never mutating, the ambient Decimal context."""
    return decimal.Context(
        prec=_DRAWDOWN_DECIMAL_PRECISION,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
    )


def _require_complete_source(source: object) -> None:
    if type(source) is not TefasFundPriceSeries:
        raise TypeError(_ERR_SOURCE_TYPE)
    if len(source.gaps) != 0:
        raise ValueError(_ERR_INCOMPLETE)
    if len(source.points) < 2:
        raise ValueError(_ERR_TOO_FEW)


def _canonical_drawdown(source: TefasFundPriceSeries) -> tuple[Decimal, date, date, date | None]:
    """Single canonical calculation shared by the builder and the constructor verification."""
    context = _drawdown_context()
    points = source.points

    peak_price = points[0].unit_price
    peak_date = points[0].trade_date
    max_drawdown = Decimal(0)
    max_peak_price = peak_price
    max_peak_date = peak_date
    max_trough_date = peak_date

    for point in points[1:]:
        if point.unit_price > peak_price:
            peak_price = point.unit_price
            peak_date = point.trade_date
        drawdown = context.subtract(Decimal(1), context.divide(point.unit_price, peak_price))
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_peak_price = peak_price
            max_peak_date = peak_date
            max_trough_date = point.trade_date

    if max_drawdown == 0:
        first = points[0].trade_date
        return Decimal(0), first, first, first

    recovery_date = None
    for point in points:
        if point.trade_date > max_trough_date and point.unit_price >= max_peak_price:
            recovery_date = point.trade_date
            break
    return max_drawdown, max_peak_date, max_trough_date, recovery_date


@dataclass(frozen=True)
class TefasFundDrawdown:
    """Maximum drawdown (loss magnitude) with peak, trough, and recovery identity over a complete price series."""
    source: TefasFundPriceSeries
    max_drawdown: Decimal
    peak_date: date
    trough_date: date
    recovery_date: date | None

    def __post_init__(self) -> None:
        _require_complete_source(self.source)
        if type(self.max_drawdown) is not Decimal:
            raise TypeError("max_drawdown must be an exact Decimal instance")
        if not self.max_drawdown.is_finite() or not (Decimal(0) <= self.max_drawdown <= Decimal(1)):
            raise ValueError("max_drawdown must be a finite Decimal between 0 and 1")
        if type(self.peak_date) is not date:
            raise TypeError("peak_date must be an exact date instance")
        if type(self.trough_date) is not date:
            raise TypeError("trough_date must be an exact date instance")
        if self.recovery_date is not None and type(self.recovery_date) is not date:
            raise TypeError("recovery_date must be None or an exact date instance")
        if self.peak_date > self.trough_date:
            raise ValueError("peak_date must not be after trough_date")
        if self.recovery_date is not None and self.recovery_date < self.trough_date:
            raise ValueError("recovery_date must not be before trough_date")

        canonical = _canonical_drawdown(self.source)
        if (self.max_drawdown, self.peak_date, self.trough_date, self.recovery_date) != canonical:
            raise ValueError(_ERR_MATCH)

    @property
    def decline_days(self) -> int:
        return (self.trough_date - self.peak_date).days

    @property
    def recovery_days(self) -> int | None:
        if self.recovery_date is None:
            return None
        return (self.recovery_date - self.trough_date).days

    @property
    def underwater_days(self) -> int | None:
        if self.recovery_date is None:
            return None
        return (self.recovery_date - self.peak_date).days


def calculate_tefas_fund_drawdown(*, price_series: TefasFundPriceSeries) -> TefasFundDrawdown:
    """
    Calculate the maximum drawdown and recovery diagnostics of a complete authoritative price series.
    """
    _require_complete_source(price_series)
    max_drawdown, peak_date, trough_date, recovery_date = _canonical_drawdown(price_series)
    return TefasFundDrawdown(
        source=price_series,
        max_drawdown=max_drawdown,
        peak_date=peak_date,
        trough_date=trough_date,
        recovery_date=recovery_date,
    )
