"""
backend/engine/private/allocation_risk_parity.py
================================================
True Equal Risk Contribution (ERC / risk parity) benchmark over the Phase 18A sample covariance matrix
(Phase 18C).

For long-only weights w (w_i > 0, sum w_i = 1) with portfolio variance V = w' Sigma w > 0, the relative risk
contribution is

    RRC_i = w_i (Sigma w)_i / V,        ERC target: RRC_i = 1 / N for every asset.

This is NOT Inverse Volatility. It consumes the full covariance matrix (diagonal AND off-diagonal), so two sources with
identical variances but different covariance can yield different ERC weights. For a diagonal matrix (and for two
assets) the ERC solution coincides with Inverse Volatility. Sample means are never read; no expected return, leverage,
custom risk budget, constraint, HRP or CVaR logic exists here. Equal budgets only: b_i = 1 / N.

Solver (deterministic cyclical coordinate descent on the standard risk-budgeting formulation):
    - Find a positive vector x with x_i (Sigma x)_i = b_i, b_i = 1 / N. The weights are w_i = x_i / sum(x).
    - Initialization: the Phase 18B Inverse Volatility weights w0, sigma0 = sqrt(w0' Sigma w0) (must be > 0),
      x_i = w0_i / sigma0. No randomness.
    - Coordinate update, sequentially for i = 0 .. N-1 within one cycle (never in parallel): with a = Sigma_ii,
      c = sum_{j != i} Sigma_ij x_j, b = 1 / N, solve a x_i^2 + c x_i - b = 0 for the positive root, using the
      cancellation-free branch d = sqrt(c^2 + 4ab): x_i = 2b / (c + d) if c >= 0 else (d - c) / (2a).
    - Convergence is tested on the initialization first (cycles = 0 is valid) and then only after each COMPLETE cycle:
      max_i |RRC_i(x) - 1/N| <= _ERC_RELATIVE_TOLERANCE. RRC is invariant to positive scaling of x.
    - `_ERC_RELATIVE_TOLERANCE` (1E-24) is a NUMERICAL solver tolerance. It is not economic significance, data
      accuracy, forecast confidence or an allocation threshold. `_ERC_MAX_CYCLES` (10000) is a deterministic
      computational resource cap, not a financial threshold. Both are numerical-solver settings only.

Unavailability (never repaired, never fabricated):
    - ZERO_VARIANCE: any Sigma_ii == 0. No epsilon floor, dropped asset, zero weight or inverse-volatility fallback.
    - DEGENERATE_RISK_GEOMETRY: a positive-weight portfolio has w' Sigma w <= 0 (e.g. two perfectly anti-correlated
      equal-volatility assets), so relative risk contributions are undefined.
    - NON_CONVERGENCE: the cycle cap was reached, or the STORED (exactly closed) weights miss the tolerance.
      No partial weights are returned.
    A genuine Decimal analytical overflow / resource-range failure is a distinct static ValueError, never
    NON_CONVERGENCE.

Exact stored-weight closure:
    - x / sum(x) is computed in the 50-digit analytics context and closed to an exact Decimal sum of 1 by the already
      reviewed Phase 18B coefficient closure (`_close_to_one`, `_sums_to_exactly_one`). This is intentional
      package-internal reuse; the algorithm is not duplicated and Phase 18B is not modified. The risk contributions
      are then recomputed from the STORED weights and must meet the tolerance.

Architectural Invariants:
    - Pure domain module: standard library plus the two Phase 18 allocation modules only. Zero network, filesystem,
      database, ambient clock, randomness, float arithmetic, numpy/pandas/scipy, persistence, provider or resolver
      calls.
    - Each canonical solve builds ONE fresh 50-digit ROUND_HALF_EVEN context with maximum exponent range and passes it
      explicitly; the ambient context is never read or mutated and no module-global mutable Context exists.
    - `EqualRiskContributionResult` retains its source by identity and recomputes the complete canonical solver result
      (weights, cycles, reason) through the same private helper as the builder, rejecting forged values.
    - Risk diagnostics are derived, non-stored properties. RRC values are never clipped or made absolute.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from uuid import UUID

from backend.engine.private.allocation_benchmarks import (
    _close_to_one,
    _sums_to_exactly_one,
    build_inverse_volatility_benchmark,
)
from backend.engine.private.allocation_matrix import AllocationCovarianceMatrix

_ERC_RELATIVE_TOLERANCE = Decimal("1E-24")
_ERC_MAX_CYCLES = 10000

_ERR_COVARIANCE_TYPE = "covariance must be an exact AllocationCovarianceMatrix instance"
_ERR_SOURCE_TYPE = "source must be an exact AllocationCovarianceMatrix instance"
_ERR_WEIGHTS_TYPE = "weights must be None or a tuple of exact Decimal instances"
_ERR_REASON_TYPE = "unavailable_reason must be None or an exact EqualRiskContributionUnavailableReason instance"
_ERR_CYCLES_TYPE = "cycles must be an exact int instance"
_ERR_CYCLES_RANGE = "cycles must be between 0 and the resource cap"
_ERR_PAIRING = "weights and unavailable_reason must be exactly one available weights tuple or one unavailable reason"
_ERR_SHAPE = "weights must match the covariance dimension exactly"
_ERR_WEIGHT_RANGE = "weights must be finite Decimals with 0 < weight <= 1"
_ERR_WEIGHT_SUM = "weights must sum to exactly 1"
_ERR_MATCH = "equal risk contribution result must match the canonical solve exactly"
_ERR_RANGE = "equal risk contribution exceeds supported Decimal analytics range"


class EqualRiskContributionUnavailableReason(Enum):
    ZERO_VARIANCE = "zero_variance"
    DEGENERATE_RISK_GEOMETRY = "degenerate_risk_geometry"
    NON_CONVERGENCE = "non_convergence"


def _analytics_context() -> decimal.Context:
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


def _risk_state(
    rows: tuple[tuple[Decimal, ...], ...],
    vector: tuple[Decimal, ...] | list[Decimal],
    ctx: decimal.Context,
) -> tuple[list[Decimal], Decimal]:
    """Marginal vector Sigma v and quadratic form v' Sigma v from the FULL covariance matrix."""
    marginal: list[Decimal] = []
    for row in rows:
        total = Decimal(0)
        for entry, value in zip(row, vector):
            total = ctx.add(total, ctx.multiply(entry, value))
        marginal.append(total)
    variance = Decimal(0)
    for value, marginal_value in zip(vector, marginal):
        variance = ctx.add(variance, ctx.multiply(value, marginal_value))
    return (marginal, variance)


def _relative_contributions(
    vector: tuple[Decimal, ...] | list[Decimal],
    marginal: list[Decimal],
    variance: Decimal,
    ctx: decimal.Context,
) -> list[Decimal]:
    return [ctx.divide(ctx.multiply(value, marginal_value), variance) for value, marginal_value in zip(vector, marginal)]


def _max_error(contributions: list[Decimal], ctx: decimal.Context) -> Decimal:
    target = ctx.divide(Decimal(1), Decimal(len(contributions)))
    worst = Decimal(0)
    for contribution in contributions:
        error = ctx.abs(ctx.subtract(contribution, target))
        if error > worst:
            worst = error
    return worst


def _coordinate_update(a: Decimal, c: Decimal, b: Decimal, ctx: decimal.Context) -> Decimal:
    """Positive root of a x^2 + c x - b = 0 without subtractive cancellation (a > 0, b > 0)."""
    d = ctx.sqrt(ctx.add(ctx.multiply(c, c), ctx.multiply(ctx.multiply(Decimal(4), a), b)))
    if c >= 0:
        return ctx.divide(ctx.multiply(Decimal(2), b), ctx.add(c, d))
    return ctx.divide(ctx.subtract(d, c), ctx.multiply(Decimal(2), a))


def _canonical_solve(source: AllocationCovarianceMatrix) -> tuple[
    tuple[Decimal, ...] | None, int, EqualRiskContributionUnavailableReason | None
]:
    """Single canonical ERC solve shared by the builder and the constructor verification."""
    rows = source.covariance
    size = source.dimension
    if any(rows[i][i] == 0 for i in range(size)):
        return (None, 0, EqualRiskContributionUnavailableReason.ZERO_VARIANCE)

    ctx = _analytics_context()
    try:
        initial = build_inverse_volatility_benchmark(covariance=source)
    except ValueError:
        raise ValueError(_ERR_RANGE) from None
    start_weights = initial.weights
    if start_weights is None:  # unreachable once every diagonal entry is positive; fail closed rather than guess
        return (None, 0, EqualRiskContributionUnavailableReason.ZERO_VARIANCE)

    degenerate = (None, 0, EqualRiskContributionUnavailableReason.DEGENERATE_RISK_GEOMETRY)
    try:
        _, start_variance = _risk_state(rows, start_weights, ctx)
        if start_variance <= 0:
            return degenerate
        start_volatility = ctx.sqrt(start_variance)
        x = [ctx.divide(weight, start_volatility) for weight in start_weights]
        share = ctx.divide(Decimal(1), Decimal(size))
        cycles = 0
        while True:
            marginal, variance = _risk_state(rows, x, ctx)
            if variance <= 0:
                return (None, cycles, EqualRiskContributionUnavailableReason.DEGENERATE_RISK_GEOMETRY)
            if _max_error(_relative_contributions(x, marginal, variance, ctx), ctx) <= _ERC_RELATIVE_TOLERANCE:
                break
            if cycles >= _ERC_MAX_CYCLES:
                return (None, cycles, EqualRiskContributionUnavailableReason.NON_CONVERGENCE)
            for i in range(size):  # sequential coordinate updates; never in parallel
                cross = Decimal(0)
                for j in range(size):
                    if j != i:
                        cross = ctx.add(cross, ctx.multiply(rows[i][j], x[j]))
                x[i] = _coordinate_update(rows[i][i], cross, share, ctx)
            cycles += 1
        total = Decimal(0)
        for value in x:
            total = ctx.add(total, value)
        preliminary = [ctx.divide(value, total) for value in x]
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None

    try:
        stored = _close_to_one(preliminary)
    except ValueError:
        raise ValueError(_ERR_RANGE) from None

    try:
        marginal, variance = _risk_state(rows, stored, ctx)
        if variance <= 0 or _max_error(_relative_contributions(stored, marginal, variance, ctx), ctx) > _ERC_RELATIVE_TOLERANCE:
            return (None, cycles, EqualRiskContributionUnavailableReason.NON_CONVERGENCE)
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None
    return (stored, cycles, None)


@dataclass(frozen=True)
class EqualRiskContributionResult:
    """Long-only ERC weights (aligned to source.instrument_ids) or an explicit unavailability reason."""
    source: AllocationCovarianceMatrix
    weights: tuple[Decimal, ...] | None
    cycles: int
    unavailable_reason: EqualRiskContributionUnavailableReason | None

    def __post_init__(self) -> None:
        if type(self.source) is not AllocationCovarianceMatrix:
            raise TypeError(_ERR_SOURCE_TYPE)
        if self.weights is not None and (
            type(self.weights) is not tuple or any(type(w) is not Decimal for w in self.weights)
        ):
            raise TypeError(_ERR_WEIGHTS_TYPE)
        if self.unavailable_reason is not None and type(self.unavailable_reason) is not EqualRiskContributionUnavailableReason:
            raise TypeError(_ERR_REASON_TYPE)
        if type(self.cycles) is not int:
            raise TypeError(_ERR_CYCLES_TYPE)
        if not 0 <= self.cycles <= _ERC_MAX_CYCLES:
            raise ValueError(_ERR_CYCLES_RANGE)
        if (self.weights is None) == (self.unavailable_reason is None):
            raise ValueError(_ERR_PAIRING)
        if self.weights is not None:
            if len(self.weights) != self.source.dimension:
                raise ValueError(_ERR_SHAPE)
            if any(not w.is_finite() or not (w > Decimal(0) and w <= Decimal(1)) for w in self.weights):
                raise ValueError(_ERR_WEIGHT_RANGE)
            if not _sums_to_exactly_one(self.weights):
                raise ValueError(_ERR_WEIGHT_SUM)
        if (self.weights, self.cycles, self.unavailable_reason) != _canonical_solve(self.source):
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

    def _state(self, ctx: decimal.Context) -> tuple[list[Decimal], Decimal]:
        return _risk_state(self.source.covariance, self.weights, ctx)  # type: ignore[arg-type]

    @property
    def portfolio_variance(self) -> Decimal | None:
        if self.weights is None:
            return None
        return self._state(_analytics_context())[1]

    @property
    def portfolio_volatility(self) -> Decimal | None:
        variance = self.portfolio_variance
        if variance is None:
            return None
        return _analytics_context().sqrt(variance)

    @property
    def relative_risk_contributions(self) -> tuple[Decimal, ...] | None:
        if self.weights is None:
            return None
        ctx = _analytics_context()
        marginal, variance = self._state(ctx)
        return tuple(_relative_contributions(self.weights, marginal, variance, ctx))

    @property
    def max_relative_risk_contribution_error(self) -> Decimal | None:
        contributions = self.relative_risk_contributions
        if contributions is None:
            return None
        return _max_error(list(contributions), _analytics_context())


def build_equal_risk_contribution_benchmark(
    *,
    covariance: AllocationCovarianceMatrix,
) -> EqualRiskContributionResult:
    """Equal Risk Contribution benchmark from the full sample covariance matrix (long-only, unlevered)."""
    if type(covariance) is not AllocationCovarianceMatrix:
        raise TypeError(_ERR_COVARIANCE_TYPE)
    weights, cycles, reason = _canonical_solve(covariance)
    return EqualRiskContributionResult(source=covariance, weights=weights, cycles=cycles, unavailable_reason=reason)
