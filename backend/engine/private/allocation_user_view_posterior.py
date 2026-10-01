"""
backend/engine/private/allocation_user_view_posterior.py
========================================================
Bayesian expected-return posterior of a Phase 20A user-view set (Phase 20B): a Black-Litterman-LIKE linear-Gaussian
conditioning of the base expected-return prior on explicit user views. It is NOT the canonical 1992 Black-Litterman
market-equilibrium model: Sentinax still has no market-equilibrium prior, so there is no tau, no risk aversion, no market
weights and no historical-covariance input.

Model (m prior expected-return vector, U prior expected-return uncertainty covariance, P view matrix, q targets, Omega the
diagonal view-noise covariance of Phase 20A, all consumed ONLY through one `UserReturnViewSet`):

    S = P U P' + Omega                 innovation covariance (full U, one symmetric calculation path)
    d = q - P m                        view innovation (zero is valid: a view still carries information)
    m_post = m + U P' S^-1 d           posterior expected returns
    U_post = U - U P' S^-1 P U         posterior expected-return uncertainty

`U_post` is the posterior epistemic covariance of the expected-return ESTIMATES; it is not the historical return
covariance, not a future realized-return covariance and not a portfolio covariance, and nothing is added to it.

Solve, never invert: this module never forms S^-1, U^-1 or Omega^-1 and has no inverse, pseudo-inverse, determinant or
adjugate. S is factored by a deterministic LDL-transposed decomposition (unit lower L, no row reordering) in the 50-digit
Decimal context and the systems S y = d and S Z = P U are solved by forward, diagonal and back substitution.

Strict positive definiteness of S: every LDL pivot must be > 0, otherwise the build fails closed with
`user-view posterior system is singular or non-positive-definite` (no jitter, epsilon diagonal, dropped view, lowered
confidence, pseudo-inverse or least-squares). This can legitimately happen with several exact (confidence 1, zero noise)
views that are linearly dependent, e.g. A, B and A-B all exact: individually valid but redundant noiseless equations. The
canonical policy is to fail closed. Algebraic dependence of P alone is not the criterion: dependent views with enough
noise make S strictly positive definite and are accepted.

Posterior support and geometry:
    - Every posterior expected simple return must satisfy >= -1 (simple-return support); otherwise
      `user-view posterior expected return violates simple-return support` (no clipping, rescaling or confidence change).
    - U_post must be symmetric (each entry i <= j is computed once and mirrored) and positive semidefinite, checked by a
      deterministic LDL-style test: a negative pivot is invalid; a zero pivot requires the remaining cross terms to be
      exactly zero. Unlike the prior, U_post may be singular (a full-confidence view removes all uncertainty along its
      direction). The PSD test is exact and does not tolerate rounding noise: a posterior whose exact-zero direction is
      polluted by 50-digit rounding noise below zero is rejected rather than repaired (no epsilon, eigenvalue clipping or
      nearest-PSD). Exact zeros are normalized to Decimal 0 (never signed zero).
    - For one isolated view with s = p U p' and omega = ((1 - c) / c) s: p m_post = p m + c (q - p m) and
      p U_post p' = (1 - c) s. The posterior uncertainty is independent of the targets q; targets move only the mean.

Architectural Invariants:
    - Pure domain module: standard library plus `allocation_user_views` only (no allocation, optimizer, numpy, scipy,
      historical return or covariance access, persistence, clock or randomness; no float arithmetic).
    - ONE fresh 50-digit ROUND_HALF_EVEN context with maximum exponent range per canonical calculation, passed explicitly;
      the ambient context is never read or mutated. Only `decimal.Overflow` is translated.
    - `BayesianExpectedReturnPosterior` retains its view set by identity (the prior and the views are never copied or
      mutated) and recomputes the canonical posterior through the same private helper as the builder.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from backend.engine.private.allocation_user_views import (
    ExpectedReturnPrior,
    UserReturnView,
    UserReturnViewSet,
    _ERR_RANGE as _VIEWS_RANGE_ERROR,
)

_ERR_SOURCE_TYPE = "source must be an exact UserReturnViewSet instance"
_ERR_ARGUMENT_TYPE = "view_set must be an exact UserReturnViewSet instance"
_ERR_RETURNS_TYPE = "expected_returns must be a tuple of exact Decimal instances"
_ERR_RETURNS_SHAPE = "expected_returns must match the prior dimension exactly"
_ERR_MATRIX_TYPE = "uncertainty_covariance must be a tuple of tuples of exact Decimal instances"
_ERR_MATRIX_SHAPE = "uncertainty_covariance must be a square matrix matching the prior dimension"
_ERR_FINITE = "posterior values must be finite"
_ERR_SYMMETRY = "uncertainty_covariance must be exactly symmetric"
_ERR_MATCH = "posterior must match the canonical Bayesian update exactly"
_ERR_SINGULAR = "user-view posterior system is singular or non-positive-definite"
_ERR_SUPPORT = "user-view posterior expected return violates simple-return support"
_ERR_PSD = "user-view posterior uncertainty is not positive semidefinite"
_ERR_RANGE = "user-view posterior analytics exceeds supported Decimal range"

_Matrix = list[list[Decimal]]


def _analytics_context() -> decimal.Context:
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


def _clean(value: Decimal) -> Decimal:
    """Exact zero (including signed zero) becomes plain Decimal 0."""
    return Decimal(0) if value.is_zero() else value


def _factor_spd(matrix: _Matrix, ctx: decimal.Context) -> tuple[_Matrix, list[Decimal]]:
    """A = L D L' with unit lower L; every pivot must be > 0 (strictly positive definite), otherwise fail closed."""
    size = len(matrix)
    lower: _Matrix = [[Decimal(1) if i == j else Decimal(0) for j in range(size)] for i in range(size)]
    diagonal: list[Decimal] = []
    for k in range(size):
        weighted = Decimal(0)
        for j in range(k):
            weighted = ctx.add(weighted, ctx.multiply(ctx.multiply(lower[k][j], lower[k][j]), diagonal[j]))
        pivot = ctx.subtract(matrix[k][k], weighted)
        if pivot <= 0:
            raise ValueError(_ERR_SINGULAR)
        diagonal.append(pivot)
        for i in range(k + 1, size):
            cross = Decimal(0)
            for j in range(k):
                cross = ctx.add(cross, ctx.multiply(ctx.multiply(lower[i][j], lower[k][j]), diagonal[j]))
            lower[i][k] = ctx.divide(ctx.subtract(matrix[i][k], cross), pivot)
    return (lower, diagonal)


def _solve_ldl(lower: _Matrix, diagonal: list[Decimal], rhs: list[Decimal], ctx: decimal.Context) -> list[Decimal]:
    """Solve (L D L') x = rhs by forward, diagonal and back substitution (no inverse, no row reordering)."""
    size = len(diagonal)
    forward: list[Decimal] = []
    for i in range(size):
        total = rhs[i]
        for j in range(i):
            total = ctx.subtract(total, ctx.multiply(lower[i][j], forward[j]))
        forward.append(total)
    scaled = [ctx.divide(forward[i], diagonal[i]) for i in range(size)]
    solution = [Decimal(0)] * size
    for i in range(size - 1, -1, -1):
        total = scaled[i]
        for j in range(i + 1, size):
            total = ctx.subtract(total, ctx.multiply(lower[j][i], solution[j]))
        solution[i] = total
    return solution


def _solve_ldl_matrix(lower: _Matrix, diagonal: list[Decimal], columns: list[list[Decimal]], ctx: decimal.Context) -> _Matrix:
    """Solve for several right-hand-side columns; one solution list per column."""
    return [_solve_ldl(lower, diagonal, column, ctx) for column in columns]


def _require_psd(matrix: _Matrix, ctx: decimal.Context) -> None:
    """Positive semidefiniteness by LDL: no negative pivot; a zero pivot needs exactly zero remaining cross terms."""
    size = len(matrix)
    lower: _Matrix = [[Decimal(0)] * size for _ in range(size)]
    pivots: list[Decimal] = []
    for k in range(size):
        weighted = Decimal(0)
        for j in range(k):
            weighted = ctx.add(weighted, ctx.multiply(ctx.multiply(lower[k][j], lower[k][j]), pivots[j]))
        pivot = ctx.subtract(matrix[k][k], weighted)
        if pivot < 0:
            raise ValueError(_ERR_PSD)
        pivots.append(pivot)
        for i in range(k + 1, size):
            cross = Decimal(0)
            for j in range(k):
                cross = ctx.add(cross, ctx.multiply(ctx.multiply(lower[i][j], lower[k][j]), pivots[j]))
            remainder = ctx.subtract(matrix[i][k], cross)
            if pivot == 0:
                if remainder != 0:
                    raise ValueError(_ERR_PSD)
            else:
                lower[i][k] = ctx.divide(remainder, pivot)


def _prior_returns(view_set: UserReturnViewSet) -> list[Decimal]:
    return list(view_set.source.expected_returns)


def _prior_uncertainty(view_set: UserReturnViewSet) -> _Matrix:
    return [list(row) for row in view_set.source.uncertainty_covariance]


def _noise_variances(view_set: UserReturnViewSet) -> list[Decimal]:
    """Omega diagonal from the Phase 20A mapping (its range error is re-expressed as the posterior range error)."""
    try:
        return list(view_set.view_noise_variances)
    except ValueError as error:
        if str(error) == _VIEWS_RANGE_ERROR:
            raise ValueError(_ERR_RANGE) from None
        raise


def _projected_prior_returns(view_set: UserReturnViewSet, ctx: decimal.Context) -> list[Decimal]:
    prior = _prior_returns(view_set)
    result: list[Decimal] = []
    for loadings in view_set.view_matrix:
        total = Decimal(0)
        for loading, value in zip(loadings, prior):
            total = ctx.add(total, ctx.multiply(loading, value))
        result.append(_clean(total))
    return result


def _innovations(view_set: UserReturnViewSet, ctx: decimal.Context) -> list[Decimal]:
    projected = _projected_prior_returns(view_set, ctx)
    return [_clean(ctx.subtract(target, value)) for target, value in zip(view_set.view_targets, projected)]


def _gain(view_set: UserReturnViewSet, ctx: decimal.Context) -> _Matrix:
    """W = U P' (N x K)."""
    uncertainty = _prior_uncertainty(view_set)
    rows = view_set.view_matrix
    gain: _Matrix = []
    for i in range(len(uncertainty)):
        line: list[Decimal] = []
        for loadings in rows:
            total = Decimal(0)
            for j, loading in enumerate(loadings):
                total = ctx.add(total, ctx.multiply(uncertainty[i][j], loading))
            line.append(total)
        gain.append(line)
    return gain


def _innovation_matrix(view_set: UserReturnViewSet, ctx: decimal.Context) -> _Matrix:
    """S = P U P' + Omega; each entry k <= l is computed once and mirrored (exact symmetry)."""
    rows = view_set.view_matrix
    count = len(rows)
    gain = _gain(view_set, ctx)  # W = U P'
    noise = _noise_variances(view_set)
    matrix: _Matrix = [[Decimal(0)] * count for _ in range(count)]
    for k in range(count):
        for l in range(k, count):
            total = Decimal(0)
            for i, loading in enumerate(rows[k]):
                total = ctx.add(total, ctx.multiply(loading, gain[i][l]))
            if k == l:
                total = ctx.add(total, noise[k])
            matrix[k][l] = total
            matrix[l][k] = total
    return matrix


def _matvec_rows(rows: tuple[tuple[Decimal, ...], ...], vector: list[Decimal], ctx: decimal.Context) -> list[Decimal]:
    result: list[Decimal] = []
    for loadings in rows:
        total = Decimal(0)
        for loading, value in zip(loadings, vector):
            total = ctx.add(total, ctx.multiply(loading, value))
        result.append(_clean(total))
    return result


def _canonical_posterior(view_set: UserReturnViewSet) -> tuple[tuple[Decimal, ...], tuple[tuple[Decimal, ...], ...]]:
    """Single canonical Bayesian update shared by the builder and the constructor verification."""
    ctx = _analytics_context()
    try:
        prior = _prior_returns(view_set)
        uncertainty = _prior_uncertainty(view_set)
        size, count = len(prior), len(view_set.views)
        innovation = _innovations(view_set, ctx)
        lower, diagonal = _factor_spd(_innovation_matrix(view_set, ctx), ctx)  # S = L D L'
        gain = _gain(view_set, ctx)                                            # W = U P'
        y = _solve_ldl(lower, diagonal, innovation, ctx)                       # S y = d
        means: list[Decimal] = []
        for i in range(size):
            shift = Decimal(0)
            for k in range(count):
                shift = ctx.add(shift, ctx.multiply(gain[i][k], y[k]))
            means.append(_clean(ctx.add(prior[i], shift)))
        if any(value < Decimal(-1) for value in means):
            raise ValueError(_ERR_SUPPORT)
        # S Z = P U: column j of P U is row j of W (U is symmetric)
        z_columns = _solve_ldl_matrix(lower, diagonal, [gain[j] for j in range(size)], ctx)
        posterior_uncertainty: _Matrix = [[Decimal(0)] * size for _ in range(size)]
        for i in range(size):
            for j in range(i, size):
                correction = Decimal(0)
                for k in range(count):
                    correction = ctx.add(correction, ctx.multiply(gain[i][k], z_columns[j][k]))
                value = _clean(ctx.subtract(uncertainty[i][j], correction))
                posterior_uncertainty[i][j] = value
                posterior_uncertainty[j][i] = value
        _require_psd(posterior_uncertainty, ctx)
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None
    return (tuple(means), tuple(tuple(row) for row in posterior_uncertainty))


@dataclass(frozen=True)
class BayesianExpectedReturnPosterior:
    """Posterior expected returns and expected-return uncertainty of a user-view set; the prior is never altered."""
    source: UserReturnViewSet
    expected_returns: tuple[Decimal, ...]
    uncertainty_covariance: tuple[tuple[Decimal, ...], ...]

    def __post_init__(self) -> None:
        if type(self.source) is not UserReturnViewSet:
            raise TypeError(_ERR_SOURCE_TYPE)
        size = self.source.source.dimension
        if type(self.expected_returns) is not tuple or any(type(v) is not Decimal for v in self.expected_returns):
            raise TypeError(_ERR_RETURNS_TYPE)
        matrix = self.uncertainty_covariance
        if type(matrix) is not tuple or any(
            type(row) is not tuple or any(type(v) is not Decimal for v in row) for row in matrix
        ):
            raise TypeError(_ERR_MATRIX_TYPE)
        if len(self.expected_returns) != size:
            raise ValueError(_ERR_RETURNS_SHAPE)
        if len(matrix) != size or any(len(row) != size for row in matrix):
            raise ValueError(_ERR_MATRIX_SHAPE)
        if any(not v.is_finite() for v in self.expected_returns) or any(not v.is_finite() for row in matrix for v in row):
            raise ValueError(_ERR_FINITE)
        if any(v < Decimal(-1) for v in self.expected_returns):
            raise ValueError(_ERR_SUPPORT)
        if any(matrix[i][j] != matrix[j][i] for i in range(size) for j in range(i + 1, size)):
            raise ValueError(_ERR_SYMMETRY)
        try:
            _require_psd([list(row) for row in matrix], _analytics_context())
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None
        if (self.expected_returns, self.uncertainty_covariance) != _canonical_posterior(self.source):
            raise ValueError(_ERR_MATCH)

    @property
    def instrument_ids(self) -> tuple[UUID, ...]:
        return self.source.source.instrument_ids

    @property
    def dimension(self) -> int:
        return self.source.source.dimension

    @property
    def prior(self) -> ExpectedReturnPrior:
        return self.source.source

    @property
    def views(self) -> tuple[UserReturnView, ...]:
        return self.source.views

    @property
    def projected_prior_returns(self) -> tuple[Decimal, ...]:
        """P m in canonical view order."""
        try:
            return tuple(_projected_prior_returns(self.source, _analytics_context()))
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None

    @property
    def view_innovations(self) -> tuple[Decimal, ...]:
        """d = q - P m in canonical view order (zero is valid)."""
        try:
            return tuple(_innovations(self.source, _analytics_context()))
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None

    @property
    def innovation_covariance(self) -> tuple[tuple[Decimal, ...], ...]:
        """S = P U P' + Omega."""
        try:
            return tuple(tuple(row) for row in _innovation_matrix(self.source, _analytics_context()))
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None

    @property
    def posterior_view_returns(self) -> tuple[Decimal, ...]:
        """P m_post in canonical view order (noisy views need not equal their targets)."""
        try:
            return tuple(_matvec_rows(self.source.view_matrix, list(self.expected_returns), _analytics_context()))
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None

    @property
    def posterior_shift(self) -> tuple[Decimal, ...]:
        """m_post - m: the user-view overlay; the prior itself is never mutated."""
        ctx = _analytics_context()
        try:
            return tuple(_clean(ctx.subtract(post, prior)) for post, prior in zip(self.expected_returns, self.source.source.expected_returns))
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None


def build_bayesian_expected_return_posterior(
    *,
    view_set: UserReturnViewSet,
) -> BayesianExpectedReturnPosterior:
    """Black-Litterman-LIKE Bayesian posterior of a Phase 20A view set (solve-not-invert, strictly PD innovation system)."""
    if type(view_set) is not UserReturnViewSet:
        raise TypeError(_ERR_ARGUMENT_TYPE)
    means, posterior_uncertainty = _canonical_posterior(view_set)
    return BayesianExpectedReturnPosterior(source=view_set, expected_returns=means, uncertainty_covariance=posterior_uncertainty)
