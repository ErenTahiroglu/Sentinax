"""
backend/engine/private/allocation_cvar_optimizer.py
===================================================
Long-only, fully invested minimum historical CVaR optimizer (Phase 19B).

Two numerical domains are kept strictly apart:

    - Economic authority: the exact Decimal Phase 19A contracts (aligned historical scenarios, exact weights, the
      Rockafellar-Uryasev objective and its exact empirical minimum).
    - Numerical search engine: SciPy `linprog` with HiGHS dual simplex in binary64.

The solver output is a CANDIDATE only; it is never the economic authority by itself. The public weights, threshold and
CVaR are reconstructed and validated through Phase 19A: the raw solver `x`, solver zeta, slacks and the solver objective
are not stored and are not the economic authority (the raw solver objective is used only as a post-validation
cross-check).

This module is intentionally NOT in the static-guard PURE_MANIFEST: it crosses from exact Decimal into a binary64
solver. Phase 19A stays pure.

Linear program (continuous, no integrality), variables in this exact order, N assets and T scenarios:

    x = [w_0 .. w_(N-1), zeta, u_0 .. u_(T-1)]                    dimension N + 1 + T

    minimize   zeta + 1 / [T (1 - alpha)] * sum_t u_t               (w_i cost 0; no return term, regularizer or tie-break)
    subject to u_t >= L_t(w) - zeta,  L_t(w) = -sum_i r_it w_i      ->  -sum_i r_it w_i - zeta - u_t <= 0
               sum_i w_i = 1                                        (the only equality)
    bounds     0 <= w_i <= 1,   zeta free,   u_t >= 0

Solver configuration (fixed, non-configurable): method="highs-ds", presolve True, primal and dual feasibility tolerances
1e-9, simplex_dual_edge_weight_strategy "steepest-devex", maxiter 100000, no time limit (acceptance never depends on
machine speed). These are NUMERICAL solver tolerances and resource caps, not financial thresholds, risk tolerances,
allocation preferences or confidence levels. SciPy is pinned (1.18.0) because the constructor canonically re-runs the
solver.

Binary64 boundary: Decimal -> binary64 only through `_decimal_to_solver64` (decimal string, no built-in float, no ambient
Decimal arithmetic). Every coefficient must be finite. Every NONZERO return coefficient and the generated objective
coefficient must also satisfy 1e-9 <= |value| <= 1e15 after conversion (exact zero is valid zero); otherwise the input is
refused. This conservative support envelope is not an economic filter: nothing is rounded up, zeroed or clipped. The exact
Phase 19A domain is therefore a superset of the supported optimizer domain (an extreme confidence level can be evaluated
exactly but not optimized).

Raw candidate validation and exact reconstruction:
    - Accept only status 0 / success with a finite, correctly sized x and finite objective.
    - A raw weight in [-1e-9, 0] becomes exact Decimal 0 (signed zero included); in [1, 1 + 1e-9] exact Decimal 1;
      anything outside [-1e-9, 1 + 1e-9] fails post-validation. Other weights use Decimal(str(binary64)).
    - The exact sum is closed with the Phase 18B context-free coefficient helpers in a zero-allowing closure: zeros stay
      zero and the residual goes to the largest weight (lowest canonical index on ties). The residual |1 - sum| must be
      <= 1e-8 beforehand; a materially infeasible candidate is never force-closed.
    - The closed weights pass the Phase 19A `CVarPortfolioWeights` constructor, the scenarios are rebuilt exactly (solver
      slacks are never used as losses) and the public threshold and CVaR come from Phase 19A. The exact CVaR must agree
      with the solver objective within 1e-8, otherwise the candidate is rejected.

Non-uniqueness: the returned weights are one solver-selected optimal candidate. The model does not claim the weight vector
is mathematically unique (identical assets, plateaus); no secondary objective, epsilon perturbation or economic
tie-break is added. Constructor self-validation is canonical implementation identity under the pinned solver, not a
claim that other optimal portfolios are economically inferior.

Out of scope: expected return, return targets, turnover, fees, taxes, holdings, user views, short selling, leverage,
Monte Carlo and any covariance-based method.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import numpy as np
from scipy.optimize import linprog

from backend.engine.private.allocation_benchmarks import _aligned, _exact_decimal, _sums_to_exactly_one
from backend.engine.private.allocation_cvar import (
    AllocationMonth,
    AllocationReturnPanel,
    CVarPortfolioWeights,
    _ERR_RANGE as _CVAR_RANGE_ERROR,
    _analytics_context,
    _validate_confidence,
    build_cvar_scenario_series,
    calculate_historical_portfolio_cvar,
)

_ERR_BINARY64 = "minimum CVaR solver input exceeds supported binary64 range"
_ERR_SOLVER = "minimum CVaR solver did not return an optimal solution"
_ERR_POST = "minimum CVaR solver candidate failed exact post-validation"
_ERR_PANEL_TYPE = "return_panel must be an exact AllocationReturnPanel instance"
_ERR_SOURCE_TYPE = "source must be an exact AllocationReturnPanel instance"
_ERR_WEIGHTS_TYPE = "weights must be a tuple of exact Decimal instances"
_ERR_RESULT_TYPE = "threshold and conditional_value_at_risk must be exact Decimal instances"
_ERR_MATCH = "minimum CVaR result must match the canonical optimization exactly"

# Numerical reconciliation tolerances (not financial thresholds).
_SOLVER_WEIGHT_LOWER = Decimal("-1E-9")  # -tolerance, written out: unary minus on a Decimal would use the ambient context
_SOLVER_WEIGHT_UPPER = Decimal("1.000000001")  # 1 + 1E-9, written out so no ambient arithmetic is needed
_SOLVER_RESIDUAL_LIMIT_DIGITS = 8  # |1 - sum(weights)| <= 1E-8 before exact closure
_SOLVER_OBJECTIVE_TOLERANCE = Decimal("1E-8")

# Conservative binary64 support envelope for nonzero solver coefficients (not an economic filter).
_ENVELOPE_MINIMUM = 1e-9
_ENVELOPE_MAXIMUM = 1e15

_SOLVER_OPTIONS = {
    "presolve": True,
    "primal_feasibility_tolerance": 1e-9,
    "dual_feasibility_tolerance": 1e-9,
    "simplex_dual_edge_weight_strategy": "steepest-devex",
    "maxiter": 100000,
    "disp": False,
}


def _decimal_to_solver64(value: Decimal) -> np.float64:
    """The single Decimal -> binary64 solver-boundary conversion (via the decimal string)."""
    return np.float64(str(value))


def _checked_coefficient(value: Decimal) -> np.float64:
    converted = _decimal_to_solver64(value)
    if not np.isfinite(converted):
        raise ValueError(_ERR_BINARY64)
    if value != 0:  # exact mathematical zero is valid zero; a nonzero value must survive the envelope unchanged
        magnitude = abs(converted)
        if not (_ENVELOPE_MINIMUM <= magnitude <= _ENVELOPE_MAXIMUM):
            raise ValueError(_ERR_BINARY64)
    return converted


@dataclass(eq=False)
class _LinearProgram:
    c: np.ndarray
    a_ub: np.ndarray
    b_ub: np.ndarray
    a_eq: np.ndarray
    b_eq: np.ndarray
    bounds: list[tuple[int | None, int | None]]


def _build_linear_program(panel: AllocationReturnPanel, confidence_level: Decimal) -> _LinearProgram:
    """Rockafellar-Uryasev LP over [w_0..w_(N-1), zeta, u_0..u_(T-1)] for the historical scenarios of the panel."""
    assets = panel.instrument_count
    scenarios = panel.observation_count
    size = assets + 1 + scenarios
    ctx = _analytics_context()
    try:
        scale = ctx.divide(Decimal(1), ctx.multiply(Decimal(scenarios), ctx.subtract(Decimal(1), confidence_level)))
    except decimal.Overflow:
        raise ValueError(_ERR_BINARY64) from None
    objective_coefficient = _checked_coefficient(scale)

    c = np.zeros(size, dtype=np.float64)
    c[assets] = 1.0
    c[assets + 1:] = objective_coefficient

    a_ub = np.zeros((scenarios, size), dtype=np.float64)
    for scenario in range(scenarios):
        for asset, series in enumerate(panel.series):
            negated = series.points[scenario].simple_return.copy_negate()
            a_ub[scenario, asset] = _checked_coefficient(Decimal(0) if negated.is_zero() else negated)
        a_ub[scenario, assets] = -1.0
        a_ub[scenario, assets + 1 + scenario] = -1.0
    b_ub = np.zeros(scenarios, dtype=np.float64)

    a_eq = np.zeros((1, size), dtype=np.float64)
    a_eq[0, :assets] = 1.0
    b_eq = np.array([1.0], dtype=np.float64)

    bounds: list[tuple[int | None, int | None]] = [(0, 1)] * assets + [(None, None)] + [(0, None)] * scenarios
    return _LinearProgram(c=c, a_ub=a_ub, b_ub=b_ub, a_eq=a_eq, b_eq=b_eq, bounds=bounds)


def _solve(program: _LinearProgram) -> object:
    return linprog(
        c=program.c,
        A_ub=program.a_ub,
        b_ub=program.b_ub,
        A_eq=program.a_eq,
        b_eq=program.b_eq,
        bounds=program.bounds,
        method="highs-ds",
        options=dict(_SOLVER_OPTIONS),
    )


def _candidate(result: object, assets: int, scenarios: int) -> tuple[list[np.float64], Decimal]:
    """Raw weights and solver objective of an OPTIMAL solver result; anything else fails closed (no partial weights)."""
    try:
        optimal = result.success is True and result.status == 0 and result.x is not None and result.fun is not None  # type: ignore[attr-defined]
        if not optimal:
            raise ValueError(_ERR_SOLVER)
        x = np.asarray(result.x, dtype=np.float64)  # type: ignore[attr-defined]
        objective = np.float64(result.fun)  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        raise ValueError(_ERR_SOLVER) from None
    if x.ndim != 1 or x.shape[0] != assets + 1 + scenarios or not bool(np.all(np.isfinite(x))) or not bool(np.isfinite(objective)):
        raise ValueError(_ERR_SOLVER)
    return (list(x[:assets]), Decimal(str(objective)))


def _canonical_raw_weight(raw: np.float64) -> Decimal:
    value = Decimal(str(raw))
    if value < _SOLVER_WEIGHT_LOWER or value > _SOLVER_WEIGHT_UPPER:
        raise ValueError(_ERR_POST)
    if value <= 0:
        return Decimal(0)  # includes signed zero and the [-1E-9, 0] reconciliation band
    if value >= 1:
        return Decimal(1)
    return value


def _close_weights(preliminary: list[Decimal]) -> tuple[Decimal, ...]:
    """Zero-allowing exact closure: the residual 1 - sum goes to the largest weight (lowest index on ties)."""
    try:
        coefficients, one, common = _aligned(tuple(preliminary))
    except ValueError:
        raise ValueError(_ERR_POST) from None
    residual = one - sum(coefficients)
    if abs(residual) * 10**_SOLVER_RESIDUAL_LIMIT_DIGITS > 10 ** (0 - common):
        raise ValueError(_ERR_POST)  # beyond 1E-8: a materially infeasible candidate is never force-closed
    closed = list(preliminary)
    if residual != 0:
        target = 0
        for index, weight in enumerate(preliminary):
            if weight > preliminary[target]:  # strict: ties keep the lowest canonical index
                target = index
        value = coefficients[target] + residual
        if value < 0:
            raise ValueError(_ERR_POST)
        closed[target] = _exact_decimal(value, common)
    if any(weight < 0 or weight > 1 for weight in closed):
        raise ValueError(_ERR_POST)
    result = tuple(closed)
    try:
        exact = _sums_to_exactly_one(result)
    except ValueError:
        raise ValueError(_ERR_POST) from None
    if not exact:
        raise ValueError(_ERR_POST)
    return result


def _optimize(panel: AllocationReturnPanel, confidence_level: Decimal) -> tuple[tuple[Decimal, ...], Decimal, Decimal]:
    """Single canonical optimization shared by the builder and the constructor verification."""
    program = _build_linear_program(panel, confidence_level)
    raw_weights, solver_objective = _candidate(_solve(program), panel.instrument_count, panel.observation_count)
    closed = _close_weights([_canonical_raw_weight(raw) for raw in raw_weights])
    try:
        portfolio = CVarPortfolioWeights(source=panel, weights=closed)
    except ValueError as error:
        if str(error) == _CVAR_RANGE_ERROR:
            raise
        raise ValueError(_ERR_POST) from None
    exact = calculate_historical_portfolio_cvar(
        scenarios=build_cvar_scenario_series(portfolio=portfolio),
        confidence_level=confidence_level,
    )
    ctx = _analytics_context()
    try:
        difference = ctx.abs(ctx.subtract(exact.conditional_value_at_risk, solver_objective))
    except decimal.Overflow:
        raise ValueError(_ERR_POST) from None
    if difference > _SOLVER_OBJECTIVE_TOLERANCE:
        raise ValueError(_ERR_POST)
    return (closed, exact.threshold, exact.conditional_value_at_risk)


@dataclass(frozen=True)
class MinimumCVarOptimizationResult:
    """One solver-selected optimal long-only candidate, reconstructed and validated through exact Phase 19A."""
    source: AllocationReturnPanel
    confidence_level: Decimal
    weights: tuple[Decimal, ...]
    threshold: Decimal
    conditional_value_at_risk: Decimal

    def __post_init__(self) -> None:
        if type(self.source) is not AllocationReturnPanel:
            raise TypeError(_ERR_SOURCE_TYPE)
        alpha = _validate_confidence(self.confidence_level)
        if type(self.weights) is not tuple or any(type(w) is not Decimal for w in self.weights):
            raise TypeError(_ERR_WEIGHTS_TYPE)
        if type(self.threshold) is not Decimal or type(self.conditional_value_at_risk) is not Decimal:
            raise TypeError(_ERR_RESULT_TYPE)
        if (self.weights, self.threshold, self.conditional_value_at_risk) != _optimize(self.source, alpha):
            raise ValueError(_ERR_MATCH)

    @property
    def instrument_ids(self) -> tuple[UUID, ...]:
        return self.source.instrument_ids

    @property
    def periods(self) -> tuple[AllocationMonth, ...]:
        return self.source.periods

    @property
    def scenario_count(self) -> int:
        return self.source.observation_count

    @property
    def dimension(self) -> int:
        return self.source.instrument_count


def optimize_minimum_cvar_portfolio(
    *,
    return_panel: AllocationReturnPanel,
    confidence_level: Decimal,
) -> MinimumCVarOptimizationResult:
    """Minimum historical CVaR over long-only, fully invested weights of a Phase 18A return panel."""
    if type(return_panel) is not AllocationReturnPanel:
        raise TypeError(_ERR_PANEL_TYPE)
    alpha = _validate_confidence(confidence_level)
    weights, threshold, value = _optimize(return_panel, alpha)
    return MinimumCVarOptimizationResult(
        source=return_panel,
        confidence_level=confidence_level,
        weights=weights,
        threshold=threshold,
        conditional_value_at_risk=value,
    )
