"""
backend/engine/private/allocation_benchmarks.py
===============================================
Equal Weight and Inverse Volatility allocation benchmarks over the Phase 18A sample covariance authority
(Phase 18B).

Both are closed-form, long-only, descriptive portfolio-construction benchmarks. They are NOT recommendations,
optimizer outputs, suitability decisions, tactical tilts or trade instructions.

Methodology:
    - Equal Weight:       w_i = 1 / N. Always mathematically available for any valid covariance source.
    - Inverse Volatility: sigma_i = sqrt(Sigma_ii), raw_i = 1 / sigma_i, w_i = raw_i / sum(raw_j).

Inverse Volatility is NOT Equal-Risk-Contribution / risk parity. It depends on the covariance diagonal only and
deliberately ignores off-diagonal covariance, correlation, marginal portfolio risk and risk contribution (later
checkpoints). Sample means are never read: these are risk/structure benchmarks, not expected-return strategies.
No annualization is applied (all assets share the monthly frequency and a common scaling cancels in the
normalization).

Zero volatility:
    - Phase 18A admits variance 0 as an observed value. If ANY diagonal variance is 0, 1 / sigma is undefined and
      Inverse Volatility is UNAVAILABLE (weights None, reason ZERO_VOLATILITY). No epsilon, floor, dropped asset,
      100% allocation, zero weight or carried-forward volatility is substituted: undefined is never zero.

Exact-sum residual closure:
    - Preliminary weights are computed with a fresh 50-digit ROUND_HALF_EVEN context. Because quantities such as
      1/3 have no finite Decimal form, the residual 1 - sum(preliminary) is computed in exact arithmetic and added to
      exactly one preliminary weight: the largest one, the lowest canonical source index on ties (instrument order
      is the canonical UUID-string order of Phase 18A, hence input-order independent). The result sums to exactly 1.
      This residual closure is numerical representation closure only; it is NOT an economic preference or an
      allocation signal. Each closed weight must be finite with 0 < w <= 1; otherwise the calculation fails closed
      with a static analytical-range error (no clamping).

Architectural Invariants:
    - Pure domain module: standard library plus `allocation_matrix` only. Zero network, filesystem, database,
      ambient clock, randomness, float arithmetic, numpy/pandas/scipy, persistence, provider or resolver calls.
    - Decimal arithmetic never touches the ambient context. Each calculation builds fresh contexts (a 50-digit
      analytics context and an exact closure context that traps Inexact); no module-global mutable Context exists.
    - `weights[i]` corresponds to `source.instrument_ids[i]`; instrument ids are not duplicated as stored fields.
    - `AllocationBenchmarkResult` retains its covariance source by identity and recomputes the canonical result
      through the same private helper as the builders, rejecting forged values.
    - No portfolio volatility, optimizer, correlation, ERC, HRP, CVaR, expected return, ranking or recommendation.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from uuid import UUID

from backend.engine.private.allocation_matrix import AllocationCovarianceMatrix

_ERR_SOURCE_TYPE = "source must be an exact AllocationCovarianceMatrix instance"
_ERR_COVARIANCE_TYPE = "covariance must be an exact AllocationCovarianceMatrix instance"
_ERR_METHOD_TYPE = "method must be an exact AllocationBenchmarkMethod instance"
_ERR_WEIGHTS_TYPE = "weights must be None or a tuple of exact Decimal instances"
_ERR_REASON_TYPE = "unavailable_reason must be None or an exact AllocationBenchmarkUnavailableReason instance"
_ERR_PAIRING = "weights and unavailable_reason must be exactly one available weights tuple or one unavailable reason"
_ERR_SHAPE = "weights must match the covariance dimension exactly"
_ERR_WEIGHT_RANGE = "weights must be finite Decimals with 0 < weight <= 1"
_ERR_WEIGHT_SUM = "weights must sum to exactly 1"
_ERR_MATCH = "benchmark must match the canonical calculation exactly"
_ERR_RANGE = "allocation benchmark exceeds supported Decimal analytics range"

_CLOSURE_PRECISION = 1000


class AllocationBenchmarkMethod(Enum):
    EQUAL_WEIGHT = "equal_weight"
    INVERSE_VOLATILITY = "inverse_volatility"


class AllocationBenchmarkUnavailableReason(Enum):
    ZERO_VOLATILITY = "zero_volatility"


def _analytics_context() -> decimal.Context:
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


def _closure_context() -> decimal.Context:
    """Fresh exact-arithmetic context: any inexact closure step is a deterministic range failure, never rounded."""
    return decimal.Context(
        prec=_CLOSURE_PRECISION,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
        traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow, decimal.Inexact],
    )


def _exact_total(weights: tuple[Decimal, ...]) -> Decimal:
    closure = _closure_context()
    try:
        total = Decimal(0)
        for weight in weights:
            total = closure.add(total, weight)
    except (decimal.Overflow, decimal.Inexact):
        raise ValueError(_ERR_RANGE) from None
    return total


def _close_to_one(preliminary: list[Decimal]) -> tuple[Decimal, ...]:
    """Apply the residual 1 - sum(preliminary) to the largest preliminary weight (lowest index on ties)."""
    closure = _closure_context()
    try:
        residual = closure.subtract(Decimal(1), _exact_total(tuple(preliminary)))
        target = 0
        for index, weight in enumerate(preliminary):
            if weight > preliminary[target]:  # strict: ties keep the lowest canonical index
                target = index
        closed = list(preliminary)
        closed[target] = closure.add(preliminary[target], residual)
    except (decimal.Overflow, decimal.Inexact):
        raise ValueError(_ERR_RANGE) from None
    if any(not w.is_finite() or not (w > Decimal(0) and w <= Decimal(1)) for w in closed):
        raise ValueError(_ERR_RANGE)
    result = tuple(closed)
    if _exact_total(result) != Decimal(1):
        raise ValueError(_ERR_RANGE)
    return result


def _equal_weight_preliminary(size: int, ctx: decimal.Context) -> list[Decimal]:
    share = ctx.divide(Decimal(1), Decimal(size))
    return [share for _ in range(size)]


def _inverse_volatility_preliminary(
    covariance: AllocationCovarianceMatrix,
    ctx: decimal.Context,
) -> list[Decimal] | None:
    variances = [covariance.covariance[i][i] for i in range(covariance.dimension)]  # diagonal only
    if any(variance == 0 for variance in variances):
        return None
    raw = [ctx.divide(Decimal(1), ctx.sqrt(variance)) for variance in variances]
    total = Decimal(0)
    for value in raw:
        total = ctx.add(total, value)
    return [ctx.divide(value, total) for value in raw]


def _canonical_benchmark(
    source: AllocationCovarianceMatrix,
    method: AllocationBenchmarkMethod,
) -> tuple[tuple[Decimal, ...], None] | tuple[None, AllocationBenchmarkUnavailableReason]:
    """Single canonical benchmark calculation shared by the builders and the constructor verification."""
    ctx = _analytics_context()
    try:
        if method is AllocationBenchmarkMethod.EQUAL_WEIGHT:
            preliminary: list[Decimal] | None = _equal_weight_preliminary(source.dimension, ctx)
        else:
            preliminary = _inverse_volatility_preliminary(source, ctx)
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None
    if preliminary is None:
        return (None, AllocationBenchmarkUnavailableReason.ZERO_VOLATILITY)
    return (_close_to_one(preliminary), None)


@dataclass(frozen=True)
class AllocationBenchmarkResult:
    """Long-only benchmark weights (aligned to source.instrument_ids) or an explicit unavailability reason."""
    source: AllocationCovarianceMatrix
    method: AllocationBenchmarkMethod
    weights: tuple[Decimal, ...] | None
    unavailable_reason: AllocationBenchmarkUnavailableReason | None

    def __post_init__(self) -> None:
        if type(self.source) is not AllocationCovarianceMatrix:
            raise TypeError(_ERR_SOURCE_TYPE)
        if type(self.method) is not AllocationBenchmarkMethod:
            raise TypeError(_ERR_METHOD_TYPE)
        if self.weights is not None and (
            type(self.weights) is not tuple or any(type(w) is not Decimal for w in self.weights)
        ):
            raise TypeError(_ERR_WEIGHTS_TYPE)
        if self.unavailable_reason is not None and type(self.unavailable_reason) is not AllocationBenchmarkUnavailableReason:
            raise TypeError(_ERR_REASON_TYPE)
        if (self.weights is None) == (self.unavailable_reason is None):
            raise ValueError(_ERR_PAIRING)
        if self.weights is not None:
            if len(self.weights) != self.source.dimension:
                raise ValueError(_ERR_SHAPE)
            if any(not w.is_finite() or not (w > Decimal(0) and w <= Decimal(1)) for w in self.weights):
                raise ValueError(_ERR_WEIGHT_RANGE)
            if _exact_total(self.weights) != Decimal(1):
                raise ValueError(_ERR_WEIGHT_SUM)
        if (self.weights, self.unavailable_reason) != _canonical_benchmark(self.source, self.method):
            raise ValueError(_ERR_MATCH)

    @property
    def instrument_ids(self) -> tuple[UUID, ...]:
        return self.source.instrument_ids

    @property
    def dimension(self) -> int:
        return self.source.dimension

    @property
    def is_available(self) -> bool:
        return self.weights is not None


def _build(covariance: AllocationCovarianceMatrix, method: AllocationBenchmarkMethod) -> AllocationBenchmarkResult:
    if type(covariance) is not AllocationCovarianceMatrix:
        raise TypeError(_ERR_COVARIANCE_TYPE)
    weights, reason = _canonical_benchmark(covariance, method)
    return AllocationBenchmarkResult(source=covariance, method=method, weights=weights, unavailable_reason=reason)


def build_equal_weight_benchmark(
    *,
    covariance: AllocationCovarianceMatrix,
) -> AllocationBenchmarkResult:
    """Equal Weight benchmark: 1 / N per asset, closed to exactly 1."""
    return _build(covariance, AllocationBenchmarkMethod.EQUAL_WEIGHT)


def build_inverse_volatility_benchmark(
    *,
    covariance: AllocationCovarianceMatrix,
) -> AllocationBenchmarkResult:
    """Inverse Volatility benchmark from the covariance diagonal only; unavailable when any variance is 0."""
    return _build(covariance, AllocationBenchmarkMethod.INVERSE_VOLATILITY)
