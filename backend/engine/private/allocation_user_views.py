"""
backend/engine/private/allocation_user_views.py
===============================================
Base expected-return prior, absolute / relative user views and the explicit confidence -> view-uncertainty mapping
(Phase 20A). Black-Litterman-LIKE Bayesian foundation: this module defines the contracts only; there is no posterior yet.

Methodological status: Sentinax Phase 20 is Black-Litterman-LIKE Bayesian view integration (prior mean, prior
uncertainty, linear views, view uncertainty, Bayesian posterior). It is not a claim that every input is the original 1992
Black-Litterman equilibrium model. The repository has no market equilibrium, CAPM-implied or reverse-optimized returns,
no market portfolio and no risk aversion, so none is invented: there is no tau, no risk-aversion or market-weight
parameter, and `AllocationCovarianceMatrix.sample_means` is never reinterpreted as equilibrium returns. A future genuine
equilibrium prior could populate the same contract without changing the user-view overlay.

Two layers that never mutate one another:
    - `ExpectedReturnPrior`: the BASE model. Explicit caller-supplied expected returns m (simple returns, >= -1) and an
      expected-return UNCERTAINTY covariance U. U is the epistemic covariance of the expected-return estimate; it is NOT
      the historical return covariance (NOT the historical return covariance, and not silently tau * Sigma). It must be
      exactly symmetric and strictly positive definite, checked by a deterministic LDL-transposed decomposition in Decimal
      (no numpy, no eigenvalue clipping, no jitter, no epsilon diagonal, no pseudo-inverse). The prior is bound to an
      `AllocationReturnPanel` for instrument identity, canonical order and dimension ONLY; nothing is inferred from the
      panel's returns.
    - `UserReturnView` / `UserReturnViewSet`: the USER overlay. A view is a linear statement E[p . R] = q.
        ABSOLUTE: exactly one loading, exactly 1 (E[R_i] = q), q >= -1.
        RELATIVE: positive leg summing exactly to +1 and negative leg summing exactly to -1 (context-free exact
        coefficient arithmetic), at least one loading on each side, first nonzero loading positive (one economic view, one
        canonical representation); q is a return spread with no lower bound.
      Confidence is the user's explicitly supplied confidence in 0 < c <= 1. It is not a model probability, forecast
      accuracy, p-value, LLM confidence or risk tolerance and is never inferred. Zero confidence is invalid (do not supply
      the view); full confidence c = 1 means an exact, noiseless view and is never silently weakened.
      A set rejects views whose loadings duplicate one another (no double counting) and requires the canonical order
      (ABSOLUTE before RELATIVE, then numeric lexicographic loadings); the builder sorts the original view instances without
      copying or altering them, so the economics never depend on input order.

Sentinax canonical confidence-to-view-uncertainty mapping (Gaussian, a defined mapping, not an empirical probability
calibration and not the Idzorek algorithm):

    s_k     = p_k U p_k'                    projected prior uncertainty (full U, never a diagonal shortcut)
    omega_k = ((1 - c_k) / c_k) * s_k       higher confidence -> lower view-noise variance; c = 1 -> 0

View errors are assumed conditionally independent, so the view-noise covariance Omega is diagonal. Correlated view errors
are deferred. For one isolated view the mapping lets confidence control how strongly a later Bayesian update responds
relative to the prior uncertainty along that view direction. The posterior itself is deferred (no posterior here), as is
any matrix inversion or linear solve; only the scalar quadratic form p U p' is computed.

Architectural Invariants:
    - Pure domain module: standard library plus `allocation_matrix` and the Phase 18B exact-sum helper only. No numpy,
      scipy, network, filesystem, database, ambient clock, randomness, float arithmetic or persistence. No allocation,
      benchmark, optimizer, ranking or recommendation.
    - ONE fresh 50-digit ROUND_HALF_EVEN context with maximum exponent range per calculation, passed explicitly; the ambient
      context is never read or mutated and no module-global mutable Context exists. Only `decimal.Overflow` is translated.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from uuid import UUID

from backend.engine.private.allocation_benchmarks import _sums_to_exactly_one
from backend.engine.private.allocation_matrix import AllocationReturnPanel

_ERR_SOURCE_TYPE = "source must be an exact AllocationReturnPanel instance"
_ERR_RETURNS_TYPE = "expected_returns must be a tuple of exact Decimal instances"
_ERR_RETURNS_SHAPE = "expected_returns must match the panel instrument count exactly"
_ERR_RETURNS_RANGE = "expected_returns must be finite Decimals greater than or equal to -1"
_ERR_MATRIX_TYPE = "uncertainty_covariance must be a tuple of tuples of exact Decimal instances"
_ERR_MATRIX_SHAPE = "uncertainty_covariance must be a square matrix matching the panel instrument count"
_ERR_MATRIX_FINITE = "uncertainty_covariance entries must be finite"
_ERR_MATRIX_SYMMETRY = "uncertainty_covariance must be exactly symmetric"
_ERR_MATRIX_PD = "uncertainty_covariance must be strictly positive definite"
_ERR_KIND_TYPE = "kind must be an exact UserReturnViewKind instance"
_ERR_LOADINGS_TYPE = "loadings must be a tuple of exact Decimal instances"
_ERR_LOADINGS_EMPTY = "loadings must contain at least one entry"
_ERR_LOADINGS_FINITE = "loadings must be finite"
_ERR_LOADINGS_ZERO = "loadings must not be all zero"
_ERR_TARGET_TYPE = "target_return must be an exact Decimal instance"
_ERR_TARGET_FINITE = "target_return must be finite"
_ERR_CONFIDENCE_TYPE = "confidence must be an exact Decimal instance"
_ERR_CONFIDENCE_RANGE = "confidence must satisfy 0 < confidence <= 1"
_ERR_ABSOLUTE_LOADING = "an absolute view requires exactly one nonzero loading equal to 1"
_ERR_ABSOLUTE_TARGET = "an absolute view target_return must be greater than or equal to -1"
_ERR_RELATIVE_LEGS = "a relative view requires at least one positive and one negative loading"
_ERR_RELATIVE_SUMS = "a relative view requires a positive leg summing to exactly 1 and a negative leg summing to exactly -1"
_ERR_RELATIVE_ORIENTATION = "a relative view requires its first nonzero loading to be positive"
_ERR_PRIOR_TYPE = "source must be an exact ExpectedReturnPrior instance"
_ERR_PRIOR_ARGUMENT = "prior must be an exact ExpectedReturnPrior instance"
_ERR_VIEWS_TYPE = "views must be a tuple of exact UserReturnView instances"
_ERR_VIEWS_EMPTY = "a view set requires at least one view"
_ERR_VIEW_DIMENSION = "every view must have exactly one loading per prior instrument"
_ERR_VIEW_DUPLICATE = "views must not contain duplicate loadings"
_ERR_VIEW_ORDER = "views must be in canonical order"
_ERR_GEOMETRY = "user-view projected prior uncertainty must be positive"
_ERR_RANGE = "user-view analytics exceeds supported Decimal range"


def _analytics_context() -> decimal.Context:
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


def _require_positive_definite(matrix: tuple[tuple[Decimal, ...], ...], ctx: decimal.Context) -> None:
    """Strict positive definiteness through a sequential LDL-transposed decomposition: every pivot must be > 0."""
    size = len(matrix)
    lower = [[Decimal(0)] * size for _ in range(size)]
    pivots: list[Decimal] = []
    for k in range(size):
        weighted = Decimal(0)
        for j in range(k):
            weighted = ctx.add(weighted, ctx.multiply(ctx.multiply(lower[k][j], lower[k][j]), pivots[j]))
        pivot = ctx.subtract(matrix[k][k], weighted)
        if pivot <= 0:
            raise ValueError(_ERR_MATRIX_PD)
        pivots.append(pivot)
        for i in range(k + 1, size):
            cross = Decimal(0)
            for j in range(k):
                cross = ctx.add(cross, ctx.multiply(ctx.multiply(lower[i][j], lower[k][j]), pivots[j]))
            lower[i][k] = ctx.divide(ctx.subtract(matrix[i][k], cross), pivot)


@dataclass(frozen=True)
class ExpectedReturnPrior:
    """Explicit base expected-return prior m and expected-return uncertainty covariance U (NOT return covariance)."""
    source: AllocationReturnPanel
    expected_returns: tuple[Decimal, ...]
    uncertainty_covariance: tuple[tuple[Decimal, ...], ...]

    def __post_init__(self) -> None:
        if type(self.source) is not AllocationReturnPanel:
            raise TypeError(_ERR_SOURCE_TYPE)
        size = self.source.instrument_count
        if type(self.expected_returns) is not tuple or any(type(v) is not Decimal for v in self.expected_returns):
            raise TypeError(_ERR_RETURNS_TYPE)
        if len(self.expected_returns) != size:
            raise ValueError(_ERR_RETURNS_SHAPE)
        if any(not v.is_finite() or v < Decimal(-1) for v in self.expected_returns):
            raise ValueError(_ERR_RETURNS_RANGE)
        matrix = self.uncertainty_covariance
        if type(matrix) is not tuple or any(
            type(row) is not tuple or any(type(v) is not Decimal for v in row) for row in matrix
        ):
            raise TypeError(_ERR_MATRIX_TYPE)
        if len(matrix) != size or any(len(row) != size for row in matrix):
            raise ValueError(_ERR_MATRIX_SHAPE)
        if any(not v.is_finite() for row in matrix for v in row):
            raise ValueError(_ERR_MATRIX_FINITE)
        if any(matrix[i][j] != matrix[j][i] for i in range(size) for j in range(i + 1, size)):
            raise ValueError(_ERR_MATRIX_SYMMETRY)
        try:
            _require_positive_definite(matrix, _analytics_context())
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None

    @property
    def instrument_ids(self) -> tuple[UUID, ...]:
        return self.source.instrument_ids

    @property
    def dimension(self) -> int:
        return self.source.instrument_count


class UserReturnViewKind(Enum):
    ABSOLUTE = "absolute"
    RELATIVE = "relative"


@dataclass(frozen=True)
class UserReturnView:
    """One linear return view E[loadings . R] = target_return with the user's explicit confidence (source-neutral)."""
    kind: UserReturnViewKind
    loadings: tuple[Decimal, ...]
    target_return: Decimal
    confidence: Decimal

    def __post_init__(self) -> None:
        if type(self.kind) is not UserReturnViewKind:
            raise TypeError(_ERR_KIND_TYPE)
        if type(self.loadings) is not tuple or any(type(v) is not Decimal for v in self.loadings):
            raise TypeError(_ERR_LOADINGS_TYPE)
        if not self.loadings:
            raise ValueError(_ERR_LOADINGS_EMPTY)
        if any(not v.is_finite() for v in self.loadings):
            raise ValueError(_ERR_LOADINGS_FINITE)
        if type(self.target_return) is not Decimal:
            raise TypeError(_ERR_TARGET_TYPE)
        if not self.target_return.is_finite():
            raise ValueError(_ERR_TARGET_FINITE)
        if type(self.confidence) is not Decimal:
            raise TypeError(_ERR_CONFIDENCE_TYPE)
        if not self.confidence.is_finite() or not (self.confidence > Decimal(0) and self.confidence <= Decimal(1)):
            raise ValueError(_ERR_CONFIDENCE_RANGE)
        nonzero = [v for v in self.loadings if v != 0]
        if not nonzero:
            raise ValueError(_ERR_LOADINGS_ZERO)
        if self.kind is UserReturnViewKind.ABSOLUTE:
            if len(nonzero) != 1 or nonzero[0] != 1:
                raise ValueError(_ERR_ABSOLUTE_LOADING)
            if self.target_return < Decimal(-1):
                raise ValueError(_ERR_ABSOLUTE_TARGET)
            return
        positives = [v for v in nonzero if v > 0]
        negatives = [v for v in nonzero if v < 0]
        if not positives or not negatives:
            raise ValueError(_ERR_RELATIVE_LEGS)
        try:
            exact = _sums_to_exactly_one(tuple(positives)) and _sums_to_exactly_one(tuple(v.copy_negate() for v in negatives))
        except ValueError:
            raise ValueError(_ERR_RANGE) from None
        if not exact:
            raise ValueError(_ERR_RELATIVE_SUMS)
        if nonzero[0] < 0:
            raise ValueError(_ERR_RELATIVE_ORIENTATION)


def _view_key(view: UserReturnView) -> tuple[int, tuple[Decimal, ...]]:
    return (0 if view.kind is UserReturnViewKind.ABSOLUTE else 1, view.loadings)


def _projected_uncertainty(
    matrix: tuple[tuple[Decimal, ...], ...],
    loadings: tuple[Decimal, ...],
    ctx: decimal.Context,
) -> Decimal:
    """s = p U p' with the FULL prior uncertainty covariance."""
    total = Decimal(0)
    for i, row in enumerate(matrix):
        for j, entry in enumerate(row):
            total = ctx.add(total, ctx.multiply(ctx.multiply(loadings[i], entry), loadings[j]))
    if total <= 0:
        raise ValueError(_ERR_GEOMETRY)
    return total


@dataclass(frozen=True)
class UserReturnViewSet:
    """Canonically ordered, non-duplicate user views bound to one expected-return prior."""
    source: ExpectedReturnPrior
    views: tuple[UserReturnView, ...]

    def __post_init__(self) -> None:
        if type(self.source) is not ExpectedReturnPrior:
            raise TypeError(_ERR_PRIOR_TYPE)
        if type(self.views) is not tuple or any(type(v) is not UserReturnView for v in self.views):
            raise TypeError(_ERR_VIEWS_TYPE)
        if not self.views:
            raise ValueError(_ERR_VIEWS_EMPTY)
        if any(len(v.loadings) != self.source.dimension for v in self.views):
            raise ValueError(_ERR_VIEW_DIMENSION)
        for index, view in enumerate(self.views):
            if any(view.loadings == earlier.loadings for earlier in self.views[:index]):
                raise ValueError(_ERR_VIEW_DUPLICATE)
        keys = [_view_key(v) for v in self.views]
        if any(not (keys[i] < keys[i + 1]) for i in range(len(keys) - 1)):
            raise ValueError(_ERR_VIEW_ORDER)

    @property
    def view_matrix(self) -> tuple[tuple[Decimal, ...], ...]:
        return tuple(view.loadings for view in self.views)

    @property
    def view_targets(self) -> tuple[Decimal, ...]:
        return tuple(view.target_return for view in self.views)

    @property
    def view_confidences(self) -> tuple[Decimal, ...]:
        return tuple(view.confidence for view in self.views)

    @property
    def projected_prior_uncertainties(self) -> tuple[Decimal, ...]:
        ctx = _analytics_context()
        try:
            return tuple(_projected_uncertainty(self.source.uncertainty_covariance, v.loadings, ctx) for v in self.views)
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None

    @property
    def view_noise_variances(self) -> tuple[Decimal, ...]:
        """omega_k = ((1 - c_k) / c_k) * s_k: Sentinax canonical confidence-to-view-uncertainty mapping."""
        ctx = _analytics_context()
        try:
            projected = tuple(_projected_uncertainty(self.source.uncertainty_covariance, v.loadings, ctx) for v in self.views)
            return tuple(
                ctx.multiply(ctx.divide(ctx.subtract(Decimal(1), view.confidence), view.confidence), s)
                for view, s in zip(self.views, projected)
            )
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None

    @property
    def view_noise_covariance(self) -> tuple[tuple[Decimal, ...], ...]:
        """Omega: diagonal (conditionally independent view errors)."""
        variances = self.view_noise_variances
        size = len(variances)
        return tuple(tuple(variances[i] if i == j else Decimal(0) for j in range(size)) for i in range(size))


def build_user_return_view_set(
    *,
    prior: ExpectedReturnPrior,
    views: tuple[UserReturnView, ...],
) -> UserReturnViewSet:
    """Canonically order the ORIGINAL view instances (never copied or altered) over one prior."""
    if type(prior) is not ExpectedReturnPrior:
        raise TypeError(_ERR_PRIOR_ARGUMENT)
    if type(views) is not tuple or any(type(v) is not UserReturnView for v in views):
        raise TypeError(_ERR_VIEWS_TYPE)
    return UserReturnViewSet(source=prior, views=tuple(sorted(views, key=_view_key)))
