"""
backend/engine/private/fund_rolling_returns.py
==============================================
Month-end rolling 12M / 36M / 60M cumulative TEFAS returns for the Private Investment Decision Engine
(Phase 16D).

Architectural Invariants:
    - Pure domain module. Zero network, filesystem, database, ambient clock, UUID generation, randomness, hashing,
      float arithmetic, pandas/numpy/scipy/dateutil, persistence, or provider/resolver calls. Phase 16A
      (`TefasFundPriceSeries`) remains the sole price authority and is retained by identity.
    - Requires a complete authoritative source (exact type, no explicit gaps, at least two points). A source with
      explicit gaps fails before monthly processing; a SOURCE_AS_OF series (all gaps) cannot reach this module.
      Source temporal semantics are inherited, not reinterpreted.
    - Cadence is MONTHLY. The representative observation of a calendar month is the LAST authoritative source point
      in that month (no averaging, first-of-month, nearest-date, or interpolation).
    - Continuity: from the month of the first source point through the month of the last, every calendar month must
      contain at least one source point, otherwise the series fails closed (no silent bridging of unknown months).
      This is NOT daily or trading-day completeness and involves no holiday inference.
    - A window compares the end month's representative with the representative of the month exactly
      `horizon.months` earlier (pure calendar-month identity; no 365-day, timedelta, or anniversary logic, so
      February and leap years match naturally). End months whose start month precedes the first represented month
      produce no window: insufficient history yields an EMPTY, VALID series, never an error or a fabricated window.
      For M continuous months and a horizon of H months the window count is max(M - H, 0).
    - Only `Horizon.ALLOCATION_12M`, `Horizon.STRATEGIC_3Y`, and `Horizon.STRATEGIC_5Y` are accepted.
    - Return arithmetic has ONE authority: the Phase 16B `_simple_return` (explicit 50-digit ROUND_HALF_EVEN
      context, maximum exponent range, static analytics-range error). Nothing is duplicated here; its errors
      propagate unchanged. Stored returns are cumulative window returns (`>= -1`), not annualized.
    - `TefasFundRollingReturnSeries` retains the source by identity and its constructor recomputes the canonical
      windows through the same private helper as the builder, rejecting any forged point.
    - No mean/median rolling return, win rate, peer percentile, persistence, dispersion, ranking, score,
      annualization/CAGR, volatility, downside deviation, VaR/CVaR, Sharpe/Sortino/Calmar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from backend.engine.private.domain import Horizon
from backend.engine.private.fund_price_series import TefasFundPricePoint, TefasFundPriceSeries
from backend.engine.private.fund_return_series import _simple_return

_ALLOWED_HORIZONS = (Horizon.ALLOCATION_12M, Horizon.STRATEGIC_3Y, Horizon.STRATEGIC_5Y)

_ERR_SOURCE_TYPE = "source must be an exact TefasFundPriceSeries instance"
_ERR_INCOMPLETE = "TEFAS rolling returns require a complete price series"
_ERR_TOO_FEW = "TEFAS rolling returns require at least two price points"
_ERR_CONTINUITY = "TEFAS rolling returns require continuous monthly price coverage"
_ERR_HORIZON_TYPE = "horizon must be an exact Horizon instance"
_ERR_HORIZON_VALUE = "horizon must be ALLOCATION_12M, STRATEGIC_3Y or STRATEGIC_5Y"
_ERR_POINTS_TYPE = "points must be a tuple of exact TefasFundRollingReturnPoint instances"
_ERR_POINTS_MATCH = "points must match the canonical monthly rolling windows exactly"


def _month_key(day: date) -> tuple[int, int]:
    return (day.year, day.month)


def _subtract_months(key: tuple[int, int], months: int) -> tuple[int, int]:
    """Subtract an exact integer number of calendar months from a (year, month) key."""
    year, month = key
    index = year * 12 + (month - 1) - months
    return (index // 12, index % 12 + 1)


def _month_index(key: tuple[int, int]) -> int:
    return key[0] * 12 + (key[1] - 1)


@dataclass(frozen=True)
class TefasFundRollingReturnPoint:
    """One cumulative month-end window return (decimal fraction) between two source observations."""
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
    if len(source.points) < 2:
        raise ValueError(_ERR_TOO_FEW)


def _require_supported_horizon(horizon: object) -> None:
    if type(horizon) is not Horizon:
        raise TypeError(_ERR_HORIZON_TYPE)
    if horizon not in _ALLOWED_HORIZONS:
        raise ValueError(_ERR_HORIZON_VALUE)


def _canonical_windows(
    source: TefasFundPriceSeries, horizon: Horizon
) -> tuple[TefasFundRollingReturnPoint, ...]:
    """Single canonical monthly-window calculation shared by the builder and the constructor verification."""
    last_by_month: dict[tuple[int, int], TefasFundPricePoint] = {}
    for point in source.points:  # canonical ascending order: later points in a month override earlier ones
        last_by_month[_month_key(point.trade_date)] = point
    months = sorted(last_by_month)

    if len(months) != _month_index(months[-1]) - _month_index(months[0]) + 1:
        raise ValueError(_ERR_CONTINUITY)

    windows = []
    for end_key in months:
        start_key = _subtract_months(end_key, horizon.months)
        if start_key < months[0]:
            continue  # insufficient history for this end month: no window, not an error
        start, end = last_by_month[start_key], last_by_month[end_key]
        windows.append(
            TefasFundRollingReturnPoint(
                start_date=start.trade_date,
                end_date=end.trade_date,
                simple_return=_simple_return(start.unit_price, end.unit_price),
            )
        )
    return tuple(windows)


@dataclass(frozen=True)
class TefasFundRollingReturnSeries:
    """Month-end rolling cumulative returns for one horizon, retaining the authoritative price source."""
    source: TefasFundPriceSeries
    horizon: Horizon
    points: tuple[TefasFundRollingReturnPoint, ...]

    def __post_init__(self) -> None:
        _require_complete_source(self.source)
        _require_supported_horizon(self.horizon)
        if type(self.points) is not tuple or any(
            type(p) is not TefasFundRollingReturnPoint for p in self.points
        ):
            raise TypeError(_ERR_POINTS_TYPE)
        if self.points != _canonical_windows(self.source, self.horizon):
            raise ValueError(_ERR_POINTS_MATCH)

    @property
    def is_available(self) -> bool:
        return len(self.points) > 0

    @property
    def window_count(self) -> int:
        return len(self.points)


def build_tefas_fund_rolling_return_series(
    *,
    price_series: TefasFundPriceSeries,
    horizon: Horizon,
) -> TefasFundRollingReturnSeries:
    """
    Build month-end rolling cumulative returns for a complete, monthly-continuous authoritative price series.
    """
    _require_complete_source(price_series)
    _require_supported_horizon(horizon)
    return TefasFundRollingReturnSeries(
        source=price_series,
        horizon=horizon,
        points=_canonical_windows(price_series, horizon),
    )
