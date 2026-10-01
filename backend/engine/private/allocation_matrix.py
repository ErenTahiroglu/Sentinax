"""
backend/engine/private/allocation_matrix.py
===========================================
Generic aligned monthly return panel and exact Decimal sample covariance foundation for the Private Investment
Decision Engine (Phase 18A).

This module is the deterministic mathematical input layer for later allocation benchmarks. It produces a canonical
multi-instrument monthly return panel, sample arithmetic means and the sample covariance matrix. It produces NO
portfolio weights.

Authority limit:
    - This module is NOT a market-data resolver. It validates mathematical structure only. Caller-provided returns
      are never authoritative merely because they satisfy these checks: source resolution, point-in-time semantics
      and instrument identity are resolved upstream. Adapters from authoritative market-data series into this
      generic contract are out of scope here.

Architectural Invariants:
    - Pure domain module, standard library only (dataclasses, decimal, uuid). Zero network, filesystem, database,
      ambient clock, randomness, float arithmetic, numpy/pandas/scipy/statistics, persistence, provider or resolver
      calls. It is source-neutral: it never imports fund, market-data, provider or deprecated optimizer modules.
    - Monthly alignment uses an explicit economic calendar-month identity (`AllocationMonth`), never a trading date.
      Every instrument must carry exactly the same consecutive month sequence. Nothing is filled, interpolated,
      intersected, sorted inside a constructor or otherwise repaired; constructors validate only. The builder may
      canonicalize instrument order (ascending UUID string) without touching any return value, month or point.
    - Decimal arithmetic uses an explicit fresh 50-digit ROUND_HALF_EVEN context with maximum exponent range, never
      the ambient context. Only `decimal.Overflow` is translated (one static range error).
    - Sample mean is the descriptive arithmetic mean of observed returns; it is not an expected-return forecast.
      Covariance uses the sample denominator N - 1 (N >= 2). No weighting, shrinkage or estimator repair.
    - A constant return series has variance 0, a valid observed value; later layers decide whether it is admissible.
      Negative covariance is valid and is never clipped, made absolute, projected or repaired.
    - `AllocationCovarianceMatrix` retains its panel by identity and recomputes the canonical result through the same
      private helper as the builder, rejecting forged values.
    - No correlation, weights, optimizer, expected return, ranking or recommendation surface exists here.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

_ERR_YEAR_TYPE = "year must be an exact int instance"
_ERR_MONTH_TYPE = "month must be an exact int instance"
_ERR_YEAR_RANGE = "year must be greater than or equal to 1"
_ERR_MONTH_RANGE = "month must be between 1 and 12"
_ERR_PERIOD_TYPE = "period must be an exact AllocationMonth instance"
_ERR_RETURN_TYPE = "simple_return must be an exact Decimal instance"
_ERR_RETURN_RANGE = "simple_return must be a finite Decimal greater than or equal to -1"
_ERR_INSTRUMENT_TYPE = "instrument_id must be an exact UUID instance"
_ERR_POINTS_TYPE = "points must be a tuple of exact AllocationMonthlyReturnPoint instances"
_ERR_POINTS_COUNT = "an instrument return series requires at least 2 monthly return observations"
_ERR_POINTS_ORDER = "months must be strictly increasing without duplicates"
_ERR_POINTS_GAP = "months must be consecutive without gaps"
_ERR_SERIES_TYPE = "series must be a tuple of exact AllocationInstrumentReturnSeries instances"
_ERR_SERIES_EMPTY = "a return panel requires at least 1 instrument"
_ERR_DUPLICATE = "instrument_id values must be unique within a return panel"
_ERR_ORDER = "series must be in canonical ascending UUID string order"
_ERR_ALIGNMENT = "all instruments must carry exactly the same monthly periods"
_ERR_PANEL_TYPE = "return_panel must be an exact AllocationReturnPanel instance"
_ERR_SOURCE_TYPE = "source must be an exact AllocationReturnPanel instance"
_ERR_MEANS_TYPE = "sample_means must be a tuple of exact Decimal instances"
_ERR_MATRIX_TYPE = "covariance must be a tuple of tuples of exact Decimal instances"
_ERR_SHAPE = "sample_means and covariance must match the panel dimension exactly"
_ERR_MATCH = "sample means and covariance must match the canonical sample calculation exactly"
_ERR_RANGE = "allocation covariance exceeds supported Decimal analytics range"


def _analytics_context() -> decimal.Context:
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


@dataclass(frozen=True)
class AllocationMonth:
    """Economic calendar month identity (not a trading date, publication date or timestamp)."""
    year: int
    month: int

    def __post_init__(self) -> None:
        if type(self.year) is not int:
            raise TypeError(_ERR_YEAR_TYPE)
        if type(self.month) is not int:
            raise TypeError(_ERR_MONTH_TYPE)
        if self.year < 1:
            raise ValueError(_ERR_YEAR_RANGE)
        if not 1 <= self.month <= 12:
            raise ValueError(_ERR_MONTH_RANGE)

    @property
    def month_index(self) -> int:
        """Overflow-safe integer ordinal used only for strict ordering and consecutive-month validation."""
        return self.year * 12 + (self.month - 1)


@dataclass(frozen=True)
class AllocationMonthlyReturnPoint:
    """One observed simple return (decimal fraction) for one calendar month."""
    period: AllocationMonth
    simple_return: Decimal

    def __post_init__(self) -> None:
        if type(self.period) is not AllocationMonth:
            raise TypeError(_ERR_PERIOD_TYPE)
        if type(self.simple_return) is not Decimal:
            raise TypeError(_ERR_RETURN_TYPE)
        if not self.simple_return.is_finite() or not self.simple_return >= Decimal(-1):
            raise ValueError(_ERR_RETURN_RANGE)


@dataclass(frozen=True)
class AllocationInstrumentReturnSeries:
    """Consecutive monthly return observations for one instrument (at least 2; validation only, never repair)."""
    instrument_id: UUID
    points: tuple[AllocationMonthlyReturnPoint, ...]

    def __post_init__(self) -> None:
        if type(self.instrument_id) is not UUID:
            raise TypeError(_ERR_INSTRUMENT_TYPE)
        if type(self.points) is not tuple or any(type(p) is not AllocationMonthlyReturnPoint for p in self.points):
            raise TypeError(_ERR_POINTS_TYPE)
        if len(self.points) < 2:
            raise ValueError(_ERR_POINTS_COUNT)
        for earlier, later in zip(self.points, self.points[1:]):
            step = later.period.month_index - earlier.period.month_index
            if step <= 0:
                raise ValueError(_ERR_POINTS_ORDER)
            if step != 1:
                raise ValueError(_ERR_POINTS_GAP)


def _periods(series: AllocationInstrumentReturnSeries) -> tuple[AllocationMonth, ...]:
    return tuple(p.period for p in series.points)


@dataclass(frozen=True)
class AllocationReturnPanel:
    """Instruments in canonical UUID-string order sharing exactly the same consecutive monthly periods."""
    series: tuple[AllocationInstrumentReturnSeries, ...]

    def __post_init__(self) -> None:
        if type(self.series) is not tuple or any(type(s) is not AllocationInstrumentReturnSeries for s in self.series):
            raise TypeError(_ERR_SERIES_TYPE)
        if not self.series:
            raise ValueError(_ERR_SERIES_EMPTY)
        keys = [str(s.instrument_id) for s in self.series]
        if len(set(keys)) != len(keys):
            raise ValueError(_ERR_DUPLICATE)
        if keys != sorted(keys):
            raise ValueError(_ERR_ORDER)
        reference = _periods(self.series[0])
        if any(_periods(s) != reference for s in self.series[1:]):
            raise ValueError(_ERR_ALIGNMENT)

    @property
    def instrument_ids(self) -> tuple[UUID, ...]:
        return tuple(s.instrument_id for s in self.series)

    @property
    def periods(self) -> tuple[AllocationMonth, ...]:
        return _periods(self.series[0])

    @property
    def instrument_count(self) -> int:
        return len(self.series)

    @property
    def observation_count(self) -> int:
        return len(self.series[0].points)


def build_allocation_return_panel(
    *,
    series: tuple[AllocationInstrumentReturnSeries, ...],
) -> AllocationReturnPanel:
    """Canonicalize instrument order (ascending UUID string); returns, months and points are never altered."""
    if type(series) is not tuple or any(type(s) is not AllocationInstrumentReturnSeries for s in series):
        raise TypeError(_ERR_SERIES_TYPE)
    return AllocationReturnPanel(series=tuple(sorted(series, key=lambda s: str(s.instrument_id))))


def _canonical_covariance(
    panel: AllocationReturnPanel,
) -> tuple[tuple[Decimal, ...], tuple[tuple[Decimal, ...], ...]]:
    """Single canonical sample-mean / sample-covariance calculation shared by the builder and the constructor."""
    ctx = _analytics_context()
    count = panel.observation_count
    size = panel.instrument_count
    try:
        means: list[Decimal] = []
        deviations: list[list[Decimal]] = []
        for series in panel.series:
            total = Decimal(0)
            for point in series.points:
                total = ctx.add(total, point.simple_return)
            mean = ctx.divide(total, Decimal(count))
            means.append(mean)
            deviations.append([ctx.subtract(point.simple_return, mean) for point in series.points])
        cells: list[list[Decimal | None]] = [[None] * size for _ in range(size)]
        for i in range(size):
            for j in range(i, size):
                products = Decimal(0)
                for dev_i, dev_j in zip(deviations[i], deviations[j]):
                    products = ctx.add(products, ctx.multiply(dev_i, dev_j))
                value = ctx.divide(products, Decimal(count - 1))
                cells[i][j] = value
                cells[j][i] = value  # one calculation path: exact symmetry by construction
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None
    matrix = tuple(tuple(row) for row in cells)  # type: ignore[arg-type]
    return (tuple(means), matrix)


@dataclass(frozen=True)
class AllocationCovarianceMatrix:
    """Descriptive sample means and sample (N - 1) covariance of an aligned panel, retaining the panel by identity."""
    source: AllocationReturnPanel
    sample_means: tuple[Decimal, ...]
    covariance: tuple[tuple[Decimal, ...], ...]

    def __post_init__(self) -> None:
        if type(self.source) is not AllocationReturnPanel:
            raise TypeError(_ERR_SOURCE_TYPE)
        if type(self.sample_means) is not tuple or any(type(v) is not Decimal for v in self.sample_means):
            raise TypeError(_ERR_MEANS_TYPE)
        if type(self.covariance) is not tuple or any(
            type(row) is not tuple or any(type(v) is not Decimal for v in row) for row in self.covariance
        ):
            raise TypeError(_ERR_MATRIX_TYPE)
        size = self.source.instrument_count
        if len(self.sample_means) != size or len(self.covariance) != size or any(len(r) != size for r in self.covariance):
            raise ValueError(_ERR_SHAPE)
        means, matrix = _canonical_covariance(self.source)
        if self.sample_means != means or self.covariance != matrix:
            raise ValueError(_ERR_MATCH)

    @property
    def instrument_ids(self) -> tuple[UUID, ...]:
        return self.source.instrument_ids

    @property
    def observation_count(self) -> int:
        return self.source.observation_count

    @property
    def dimension(self) -> int:
        return self.source.instrument_count


def build_allocation_covariance_matrix(
    *,
    return_panel: AllocationReturnPanel,
) -> AllocationCovarianceMatrix:
    """Sample means and sample (N - 1) covariance of an aligned return panel."""
    if type(return_panel) is not AllocationReturnPanel:
        raise TypeError(_ERR_PANEL_TYPE)
    means, matrix = _canonical_covariance(return_panel)
    return AllocationCovarianceMatrix(source=return_panel, sample_means=means, covariance=matrix)
