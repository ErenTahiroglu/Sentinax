"""
backend/engine/private/allocation_cvar.py
=========================================
Exact historical portfolio scenarios and the Rockafellar-Uryasev CVaR objective for GIVEN long-only weights
(Phase 19A).

This module is the deterministic mathematical authority that a later numerical optimizer (Phase 19B) must satisfy. It
does NOT choose weights, contains no optimizer and no solver.

Methodology:
    - Source: a Phase 18A `AllocationReturnPanel`. CVaR is scenario/tail based; covariance cannot reconstruct the
      historical loss distribution, so neither the covariance matrix nor the sample means are ever used.
    - Each aligned calendar month is one equally weighted historical scenario, in exactly the panel period order (no
      sorting, bootstrap, resampling or recency weighting).
    - Scenario return R_t(w) = sum_i w_i r_it (no compounding, annualization or geometric mean).
      Loss convention: L_t(w) = -R_t(w). A profitable scenario therefore has a negative loss; the loss is never floored
      at zero. A negative zero is canonicalized to plain 0.
    - Rockafellar-Uryasev objective for T scenarios:

          F_alpha(zeta) = zeta + 1 / (T (1 - alpha)) * sum_t max(L_t - zeta, 0)

      `evaluate_cvar_objective` evaluates a caller-specified zeta. The 0 in max(L_t - zeta, 0) is the mathematical
      positive part, not missing-data substitution.
    - The empirical CVaR is min_zeta F_alpha(zeta). F is convex and piecewise linear with breakpoints at the scenario
      losses, so the minimum is attained at one of them: every unique scenario loss (ascending) is evaluated and the
      minimum objective value is the economic authority. The reported `threshold` is one canonical minimizer; if several
      thresholds attain the same minimum the smallest threshold is selected. This is deliberately NOT the Phase 16J
      nearest-rank VaR / mean-of-tail rule and does not reuse that module.
    - Weights are long-only (0 <= w_i <= 1) with a mathematically exact sum of 1. Zero weights are valid: no
      instrument is dropped, nothing is renormalized and the source stays full-dimensional (corner solutions of a later
      optimizer). Because every simple return is >= -1, every portfolio loss is <= 1 and the CVaR value is <= 1. There is
      no lower bound: a portfolio whose every scenario is profitable has a negative CVaR.
    - Not included: no optimizer or LP, no expected-return or turnover constraint, no transaction cost, no user views,
      no bootstrap / Monte Carlo / synthetic stress scenarios.

Architectural Invariants:
    - Pure domain module: standard library plus `allocation_matrix` and the Phase 18B exact-sum helper only. No network,
      filesystem, database, ambient clock, randomness, float arithmetic, numpy/pandas/scipy, persistence, provider or
      resolver calls.
    - Analytical sums, products and divisions use ONE fresh 50-digit ROUND_HALF_EVEN context with maximum exponent range
      passed explicitly; the ambient context is never read or mutated and no module-global mutable Context exists. The
      weight-sum check reuses the context-free Phase 18B coefficient arithmetic. Only `decimal.Overflow` is translated.
    - Every result retains its source by identity and recomputes its canonical value through the same private helper as
      the builder, rejecting forged values.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from backend.engine.private.allocation_benchmarks import _sums_to_exactly_one
from backend.engine.private.allocation_matrix import AllocationMonth, AllocationReturnPanel

_ERR_PANEL_TYPE = "source must be an exact AllocationReturnPanel instance"
_ERR_WEIGHTS_TYPE = "weights must be a tuple of exact Decimal instances"
_ERR_WEIGHTS_SHAPE = "weights must match the panel instrument count exactly"
_ERR_WEIGHTS_RANGE = "weights must be finite non-negative Decimals not greater than 1"
_ERR_WEIGHTS_SUM = "weights must sum to exactly 1"
_ERR_PERIOD_TYPE = "period must be an exact AllocationMonth instance"
_ERR_POINT_TYPE = "portfolio_return and loss must be exact Decimal instances"
_ERR_POINT_VALUE = "portfolio_return and loss must be finite and non-negative-zero canonical"
_ERR_LOSS_SIGN = "loss must equal the negated portfolio_return"
_ERR_PORTFOLIO_TYPE = "source must be an exact CVarPortfolioWeights instance"
_ERR_POINTS_TYPE = "points must be a tuple of exact CVarScenarioPoint instances"
_ERR_POINTS_COUNT = "points must contain exactly one scenario per panel period"
_ERR_POINTS_PERIODS = "points must follow the panel period sequence exactly"
_ERR_POINTS_MATCH = "points must match the canonical portfolio scenarios exactly"
_ERR_CONFIDENCE_TYPE = "confidence_level must be an exact Decimal instance"
_ERR_CONFIDENCE_RANGE = "confidence_level must be finite with 0 < confidence_level < 1"
_ERR_THRESHOLD_TYPE = "threshold must be an exact Decimal instance"
_ERR_THRESHOLD_RANGE = "threshold must be finite"
_ERR_SCENARIOS_TYPE = "scenarios must be an exact CVarScenarioSeries instance"
_ERR_RESULT_TYPE = "threshold and conditional_value_at_risk must be exact Decimal instances"
_ERR_RESULT_RANGE = "threshold and conditional_value_at_risk must be finite and the CVaR value must not exceed 1"
_ERR_RESULT_MATCH = "CVaR result must match the canonical Rockafellar-Uryasev minimum exactly"
_ERR_RANGE = "portfolio CVaR exceeds supported Decimal analytics range"


def _analytics_context() -> decimal.Context:
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


def _validate_confidence(value: object) -> Decimal:
    if type(value) is not Decimal:
        raise TypeError(_ERR_CONFIDENCE_TYPE)
    if not value.is_finite() or not (value > Decimal(0) and value < Decimal(1)):
        raise ValueError(_ERR_CONFIDENCE_RANGE)
    return value


@dataclass(frozen=True)
class CVarPortfolioWeights:
    """Exact long-only weights (zero allowed) over a Phase 18A return panel; weights[i] <-> panel instrument i."""
    source: AllocationReturnPanel
    weights: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if type(self.source) is not AllocationReturnPanel:
            raise TypeError(_ERR_PANEL_TYPE)
        if type(self.weights) is not tuple or any(type(w) is not Decimal for w in self.weights):
            raise TypeError(_ERR_WEIGHTS_TYPE)
        if len(self.weights) != self.source.instrument_count:
            raise ValueError(_ERR_WEIGHTS_SHAPE)
        if any(not w.is_finite() or w.is_signed() or w > Decimal(1) for w in self.weights):
            raise ValueError(_ERR_WEIGHTS_RANGE)
        try:
            exact = _sums_to_exactly_one(self.weights)
        except ValueError:
            raise ValueError(_ERR_RANGE) from None
        if not exact:
            raise ValueError(_ERR_WEIGHTS_SUM)


@dataclass(frozen=True)
class CVarScenarioPoint:
    """One equally weighted historical scenario: portfolio return and loss L = -return."""
    period: AllocationMonth
    portfolio_return: Decimal
    loss: Decimal

    def __post_init__(self) -> None:
        if type(self.period) is not AllocationMonth:
            raise TypeError(_ERR_PERIOD_TYPE)
        if type(self.portfolio_return) is not Decimal or type(self.loss) is not Decimal:
            raise TypeError(_ERR_POINT_TYPE)
        for value in (self.portfolio_return, self.loss):
            if not value.is_finite() or (value.is_zero() and value.is_signed()):
                raise ValueError(_ERR_POINT_VALUE)
        if self.loss != self.portfolio_return.copy_negate():
            raise ValueError(_ERR_LOSS_SIGN)


def _canonical_points(portfolio: CVarPortfolioWeights) -> tuple[CVarScenarioPoint, ...]:
    """Single canonical scenario calculation shared by the builder and the constructor verification."""
    ctx = _analytics_context()
    panel = portfolio.source
    points: list[CVarScenarioPoint] = []
    try:
        for index, period in enumerate(panel.periods):
            total = Decimal(0)
            for weight, series in zip(portfolio.weights, panel.series):
                total = ctx.add(total, ctx.multiply(weight, series.points[index].simple_return))
            portfolio_return = Decimal(0) if total.is_zero() else total
            loss = portfolio_return.copy_negate()  # exact and context-free
            points.append(
                CVarScenarioPoint(period=period, portfolio_return=portfolio_return, loss=Decimal(0) if loss.is_zero() else loss)
            )
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None
    return tuple(points)


@dataclass(frozen=True)
class CVarScenarioSeries:
    """Historical portfolio scenarios in the exact panel period order, retaining the weights source by identity."""
    source: CVarPortfolioWeights
    points: tuple[CVarScenarioPoint, ...]

    def __post_init__(self) -> None:
        if type(self.source) is not CVarPortfolioWeights:
            raise TypeError(_ERR_PORTFOLIO_TYPE)
        if type(self.points) is not tuple or any(type(p) is not CVarScenarioPoint for p in self.points):
            raise TypeError(_ERR_POINTS_TYPE)
        panel = self.source.source
        if len(self.points) != panel.observation_count:
            raise ValueError(_ERR_POINTS_COUNT)
        if tuple(p.period for p in self.points) != panel.periods:
            raise ValueError(_ERR_POINTS_PERIODS)
        if self.points != _canonical_points(self.source):
            raise ValueError(_ERR_POINTS_MATCH)


def build_cvar_scenario_series(
    *,
    portfolio: CVarPortfolioWeights,
) -> CVarScenarioSeries:
    """Historical scenarios R_t = sum_i w_i r_it and losses L_t = -R_t for the given weights."""
    if type(portfolio) is not CVarPortfolioWeights:
        raise TypeError(_ERR_PORTFOLIO_TYPE)
    return CVarScenarioSeries(source=portfolio, points=_canonical_points(portfolio))


def _objective(losses: list[Decimal], confidence_level: Decimal, threshold: Decimal, ctx: decimal.Context) -> Decimal:
    """F_alpha(zeta) = zeta + sum_t max(L_t - zeta, 0) / (T (1 - alpha))."""
    excess_total = Decimal(0)
    for loss in losses:
        excess = ctx.subtract(loss, threshold)
        if excess > 0:  # positive part: mathematical max(., 0), not missing-data substitution
            excess_total = ctx.add(excess_total, excess)
    scale = ctx.multiply(Decimal(len(losses)), ctx.subtract(Decimal(1), confidence_level))
    return ctx.add(threshold, ctx.divide(excess_total, scale))


def evaluate_cvar_objective(
    *,
    scenarios: CVarScenarioSeries,
    confidence_level: Decimal,
    threshold: Decimal,
) -> Decimal:
    """Rockafellar-Uryasev objective F_alpha(zeta) at a caller-specified finite threshold zeta (may be negative)."""
    if type(scenarios) is not CVarScenarioSeries:
        raise TypeError(_ERR_SCENARIOS_TYPE)
    alpha = _validate_confidence(confidence_level)
    if type(threshold) is not Decimal:
        raise TypeError(_ERR_THRESHOLD_TYPE)
    if not threshold.is_finite():
        raise ValueError(_ERR_THRESHOLD_RANGE)
    try:
        return _objective([p.loss for p in scenarios.points], alpha, threshold, _analytics_context())
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None


def _canonical_minimum(scenarios: CVarScenarioSeries, confidence_level: Decimal) -> tuple[Decimal, Decimal]:
    """Minimum of F_alpha over the unique scenario losses; ties select the smallest threshold."""
    ctx = _analytics_context()
    losses = [p.loss for p in scenarios.points]
    candidates: list[Decimal] = []
    for loss in sorted(losses):
        if not candidates or loss != candidates[-1]:
            candidates.append(loss)
    best_threshold = candidates[0]
    try:
        best_value = _objective(losses, confidence_level, best_threshold, ctx)
        for candidate in candidates[1:]:  # ascending: a later candidate must be strictly better to replace
            value = _objective(losses, confidence_level, candidate, ctx)
            if value < best_value:
                best_threshold, best_value = candidate, value
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None
    return (best_threshold, best_value)


@dataclass(frozen=True)
class HistoricalPortfolioCVar:
    """Empirical CVaR = min_zeta F_alpha(zeta) of a given portfolio, with one canonical minimizing threshold."""
    source: CVarScenarioSeries
    confidence_level: Decimal
    threshold: Decimal
    conditional_value_at_risk: Decimal

    def __post_init__(self) -> None:
        if type(self.source) is not CVarScenarioSeries:
            raise TypeError(_ERR_SCENARIOS_TYPE)
        alpha = _validate_confidence(self.confidence_level)
        if type(self.threshold) is not Decimal or type(self.conditional_value_at_risk) is not Decimal:
            raise TypeError(_ERR_RESULT_TYPE)
        if (
            not self.threshold.is_finite()
            or not self.conditional_value_at_risk.is_finite()
            or self.conditional_value_at_risk > Decimal(1)
        ):
            raise ValueError(_ERR_RESULT_RANGE)
        if (self.threshold, self.conditional_value_at_risk) != _canonical_minimum(self.source, alpha):
            raise ValueError(_ERR_RESULT_MATCH)

    @property
    def instrument_ids(self) -> tuple[UUID, ...]:
        return self.source.source.source.instrument_ids

    @property
    def periods(self) -> tuple[AllocationMonth, ...]:
        return self.source.source.source.periods

    @property
    def scenario_count(self) -> int:
        return len(self.source.points)


def calculate_historical_portfolio_cvar(
    *,
    scenarios: CVarScenarioSeries,
    confidence_level: Decimal,
) -> HistoricalPortfolioCVar:
    """Exact empirical CVaR of the given portfolio scenarios; the minimum objective value is the authority."""
    if type(scenarios) is not CVarScenarioSeries:
        raise TypeError(_ERR_SCENARIOS_TYPE)
    alpha = _validate_confidence(confidence_level)
    threshold, value = _canonical_minimum(scenarios, alpha)
    return HistoricalPortfolioCVar(
        source=scenarios,
        confidence_level=confidence_level,
        threshold=threshold,
        conditional_value_at_risk=value,
    )
