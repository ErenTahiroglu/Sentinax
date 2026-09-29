"""
backend/engine/private/fund_annualized_returns.py
=================================================
Annualized TEFAS rolling returns and the Calmar diagnostic for the Private Investment Decision Engine
(Phase 16F).

Architectural Invariants:
    - Pure domain module. Zero network, filesystem, database, ambient clock, UUID generation, randomness, hashing,
      float arithmetic, `math`/`pow`, pandas/numpy/scipy, persistence, or provider/resolver calls.
    - Annualization consumes ONLY an exact Phase 16D `TefasFundRollingReturnSeries` (retained by identity). For a
      cumulative window return R over exactly H calendar months (`Horizon.months`, never day counts, 365, or
      365.25): `annualized = (1 + R) ** (12 / H) - 1`. The only supported exponents are 1 (12M, exact identity:
      the annualized value IS the cumulative value), 1/3 (36M), and 1/5 (60M); no generic irrational-power engine.
    - Roots use deterministic Decimal Newton iteration (`_nth_root`, degree 3 or 5 only) in a fixed 60-digit guard
      context from a magnitude-derived starting point at or above the root, stopping as soon as an iteration stops
      decreasing (equal or non-decreasing), with a hard cap of 256 iterations (static error on non-convergence);
      the root is rounded ONCE into the canonical 50-digit ROUND_HALF_EVEN context (Emin/Emax = MIN_EMIN/MAX_EMAX).
      Contexts are always fresh and explicit, never the ambient context. R = -1 gives a zero base and -1.
    - Only `decimal.Overflow` is translated to a static analytics-range ValueError; unrelated errors propagate.
    - `TefasFundAnnualizedRollingReturnSeries` maps rolling points one-to-one (same dates, order, and count; an
      empty source gives a valid empty series) and its constructor recomputes them through the same private helper
      as the builder.
    - Calmar = MOST RECENT annualized rolling return / `TefasFundDrawdown.max_drawdown`, no averaging and no
      absolute value (so it can be positive, zero, or negative). The annualized series and the drawdown must share
      the SAME authoritative `TefasFundPriceSeries` object (identity, not equality). Empty annualized history or a
      zero max drawdown gives `None` (never Infinity, a sentinel, or 0). Calmar is a historical diagnostic only,
      not a risk limit, signal, score, or ranking.
    - No Sortino/Sharpe (Sortino's numerator needs an explicit return/MAR decision), category authority, peer
      logic, persistence, rankings, or scores.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from backend.engine.private.domain import Horizon
from backend.engine.private.fund_drawdown import TefasFundDrawdown
from backend.engine.private.fund_rolling_returns import TefasFundRollingReturnSeries

# Analytical (not monetary) precisions: canonical result digits, and a fixed root-iteration guard precision.
_ANNUALIZATION_DECIMAL_PRECISION = 50
_ROOT_GUARD_DECIMAL_PRECISION = 60
_MAX_ROOT_ITERATIONS = 256

_ERR_SOURCE_TYPE = "source must be an exact TefasFundRollingReturnSeries instance"
_ERR_POINTS_TYPE = "points must be a tuple of exact TefasFundAnnualizedReturnPoint instances"
_ERR_POINTS_MATCH = "points must match the canonical annualization of source exactly"
_ERR_RANGE = "TEFAS annualized return exceeds supported Decimal analytics range"
_ERR_CONVERGE = "TEFAS annualized return root calculation did not converge"
_ERR_ROOT_DEGREE = "root degree must be 3 or 5"
_ERR_ROOT_VALUE = "root base must be a finite non-negative Decimal"
_ERR_CALMAR_SOURCE_TYPE = "annualized_source must be an exact TefasFundAnnualizedRollingReturnSeries instance"
_ERR_CALMAR_DRAWDOWN_TYPE = "drawdown must be an exact TefasFundDrawdown instance"
_ERR_CALMAR_SAME_SOURCE = "Calmar inputs must share the same authoritative price source"
_ERR_CALMAR_TYPE = "calmar_ratio must be None or an exact Decimal instance"
_ERR_CALMAR_VALUE = "calmar_ratio must be a finite Decimal"
_ERR_CALMAR_MATCH = "Calmar ratio must match the canonical calculation from its inputs"
_ERR_CALMAR_RANGE = "TEFAS Calmar ratio exceeds supported Decimal analytics range"


def _make_context(precision: int) -> decimal.Context:
    return decimal.Context(
        prec=precision,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
    )


def _annualization_context() -> decimal.Context:
    """Fresh canonical 50-digit analytics context (never the ambient context)."""
    return _make_context(_ANNUALIZATION_DECIMAL_PRECISION)


def _root_guard_context() -> decimal.Context:
    """Fresh fixed 60-digit guard context for root iterations (never scaled with input magnitude)."""
    return _make_context(_ROOT_GUARD_DECIMAL_PRECISION)


def _nth_root(value: Decimal, n: int) -> Decimal:
    """
    Deterministic Decimal n-th root for n in {3, 5} and value >= 0, rounded once into the canonical context.
    """
    if type(n) is not int or n not in (3, 5):
        raise ValueError(_ERR_ROOT_DEGREE)
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise ValueError(_ERR_ROOT_VALUE)
    if value == 0:
        return Decimal(0)

    guard = _root_guard_context()
    # Start at or above the root: 10 ** ceil((adjusted + 1) / n) >= value ** (1 / n).
    start_exponent = -((-(value.adjusted() + 1)) // n)
    current = Decimal((0, (1,), start_exponent))
    for _ in range(_MAX_ROOT_ITERATIONS):
        power = guard.power(current, n - 1)
        following = guard.divide(
            guard.add(guard.multiply(Decimal(n - 1), current), guard.divide(value, power)),
            Decimal(n),
        )
        if following >= current:  # monotone-decreasing Newton sequence has reached the root
            return _annualization_context().plus(current)
        current = following
    raise ValueError(_ERR_CONVERGE)


def _annualize(cumulative_return: Decimal, horizon: Horizon) -> Decimal:
    """(1 + R) ** (12 / H) - 1 for the supported horizons."""
    if horizon is Horizon.ALLOCATION_12M:
        return cumulative_return  # exponent 1: exact identity
    degree = 3 if horizon is Horizon.STRATEGIC_3Y else 5
    try:
        context = _annualization_context()
        base = context.add(Decimal(1), cumulative_return)
        return context.subtract(_nth_root(base, degree), Decimal(1))
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None


@dataclass(frozen=True)
class TefasFundAnnualizedReturnPoint:
    """One annualized window return (decimal fraction) matching one rolling window's dates."""
    start_date: date
    end_date: date
    annualized_return: Decimal

    def __post_init__(self) -> None:
        if type(self.start_date) is not date:
            raise TypeError("start_date must be an exact date instance")
        if type(self.end_date) is not date:
            raise TypeError("end_date must be an exact date instance")
        if not self.start_date < self.end_date:
            raise ValueError("end_date must be after start_date")
        if type(self.annualized_return) is not Decimal:
            raise TypeError("annualized_return must be an exact Decimal instance")
        if not self.annualized_return.is_finite() or not self.annualized_return >= Decimal(-1):
            raise ValueError("annualized_return must be a finite Decimal greater than or equal to -1")


def _require_rolling_source(source: object) -> None:
    if type(source) is not TefasFundRollingReturnSeries:
        raise TypeError(_ERR_SOURCE_TYPE)


def _canonical_annualized_points(
    source: TefasFundRollingReturnSeries,
) -> tuple[TefasFundAnnualizedReturnPoint, ...]:
    """Single canonical annualization shared by the builder and the constructor verification."""
    return tuple(
        TefasFundAnnualizedReturnPoint(
            start_date=point.start_date,
            end_date=point.end_date,
            annualized_return=_annualize(point.simple_return, source.horizon),
        )
        for point in source.points
    )


@dataclass(frozen=True)
class TefasFundAnnualizedRollingReturnSeries:
    """Annualized rolling returns, retaining the exact Phase 16D rolling source."""
    source: TefasFundRollingReturnSeries
    points: tuple[TefasFundAnnualizedReturnPoint, ...]

    def __post_init__(self) -> None:
        _require_rolling_source(self.source)
        if type(self.points) is not tuple or any(
            type(p) is not TefasFundAnnualizedReturnPoint for p in self.points
        ):
            raise TypeError(_ERR_POINTS_TYPE)
        if self.points != _canonical_annualized_points(self.source):
            raise ValueError(_ERR_POINTS_MATCH)

    @property
    def is_available(self) -> bool:
        return len(self.points) > 0


def annualize_tefas_fund_rolling_returns(
    *, rolling_series: TefasFundRollingReturnSeries
) -> TefasFundAnnualizedRollingReturnSeries:
    """
    Annualize every window of a Phase 16D rolling series using calendar-horizon (12 / H) exponents.
    """
    _require_rolling_source(rolling_series)
    return TefasFundAnnualizedRollingReturnSeries(
        source=rolling_series,
        points=_canonical_annualized_points(rolling_series),
    )


def _require_calmar_inputs(annualized_source: object, drawdown: object) -> None:
    if type(annualized_source) is not TefasFundAnnualizedRollingReturnSeries:
        raise TypeError(_ERR_CALMAR_SOURCE_TYPE)
    if type(drawdown) is not TefasFundDrawdown:
        raise TypeError(_ERR_CALMAR_DRAWDOWN_TYPE)
    if annualized_source.source.source is not drawdown.source:
        raise ValueError(_ERR_CALMAR_SAME_SOURCE)


def _canonical_calmar(
    annualized_source: TefasFundAnnualizedRollingReturnSeries, drawdown: TefasFundDrawdown
) -> Decimal | None:
    """Single canonical Calmar calculation shared by the builder and the constructor verification."""
    if len(annualized_source.points) == 0 or drawdown.max_drawdown == 0:
        return None
    try:
        return _annualization_context().divide(
            annualized_source.points[-1].annualized_return, drawdown.max_drawdown
        )
    except decimal.Overflow:
        raise ValueError(_ERR_CALMAR_RANGE) from None


@dataclass(frozen=True)
class TefasFundCalmar:
    """Calmar diagnostic: latest annualized rolling return divided by max drawdown (same price source)."""
    annualized_source: TefasFundAnnualizedRollingReturnSeries
    drawdown: TefasFundDrawdown
    calmar_ratio: Decimal | None

    def __post_init__(self) -> None:
        _require_calmar_inputs(self.annualized_source, self.drawdown)
        if self.calmar_ratio is not None:
            if type(self.calmar_ratio) is not Decimal:
                raise TypeError(_ERR_CALMAR_TYPE)
            if not self.calmar_ratio.is_finite():
                raise ValueError(_ERR_CALMAR_VALUE)

        canonical = _canonical_calmar(self.annualized_source, self.drawdown)
        if canonical is None:
            if self.calmar_ratio is not None:
                raise ValueError(_ERR_CALMAR_MATCH)
        elif self.calmar_ratio is None or self.calmar_ratio != canonical:
            raise ValueError(_ERR_CALMAR_MATCH)


def calculate_tefas_fund_calmar(
    *,
    annualized_series: TefasFundAnnualizedRollingReturnSeries,
    drawdown: TefasFundDrawdown,
) -> TefasFundCalmar:
    """
    Calculate the historical Calmar diagnostic from an annualized rolling series and its drawdown.
    """
    _require_calmar_inputs(annualized_series, drawdown)
    return TefasFundCalmar(
        annualized_source=annualized_series,
        drawdown=drawdown,
        calmar_ratio=_canonical_calmar(annualized_series, drawdown),
    )
