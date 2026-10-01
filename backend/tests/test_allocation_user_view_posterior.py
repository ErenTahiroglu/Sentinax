"""
backend/tests/test_allocation_user_view_posterior.py
====================================================
Phase 20B: Bayesian expected-return posterior of a Phase 20A user-view set (Black-Litterman-LIKE linear-Gaussian update).

    S      = P U P' + Omega
    d      = q - P m
    m_post = m + U P' S^-1 d
    U_post = U - U P' S^-1 P U            (S^-1 is never formed: LDL-transposed factorization + solves in Decimal)

Expected values below come from hand calculation or an independent Fraction reference (explicit Gauss-Jordan inverse
inside the TEST only), never from production helpers.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
import itertools
import random
import re
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import allocation_user_view_posterior as module_under_test
from backend.engine.private.allocation_matrix import (
    AllocationInstrumentReturnSeries,
    AllocationMonth,
    AllocationMonthlyReturnPoint,
    build_allocation_return_panel,
)
from backend.engine.private.allocation_user_view_posterior import (
    BayesianExpectedReturnPosterior,
    build_bayesian_expected_return_posterior,
)
from backend.engine.private.allocation_user_views import (
    ExpectedReturnPrior,
    UserReturnView,
    UserReturnViewKind,
    UserReturnViewSet,
    build_user_return_view_set,
)
from backend.tests.invariants import static_guards as sg

D = Decimal
F = Fraction
UUIDS = [UUID(int=i) for i in range(1, 9)]
KIND = UserReturnViewKind
TOL = F(1, 10**40)
ERR_SINGULAR = "user-view posterior system is singular or non-positive-definite"
ERR_SUPPORT = "user-view posterior expected return violates simple-return support"
ERR_MATCH = "canonical Bayesian update"
ERR_RANGE = "user-view posterior analytics exceeds supported Decimal range"


def _panel(count: int):
    columns = [["0.01", "0.02"], ["0.00", "0.01"], ["-0.01", "0.03"], ["0.02", "-0.02"]][:count]
    series = tuple(
        AllocationInstrumentReturnSeries(
            instrument_id=UUIDS[i],
            points=tuple(AllocationMonthlyReturnPoint(period=AllocationMonth(2026, j + 1), simple_return=D(v)) for j, v in enumerate(col)),
        )
        for i, col in enumerate(columns)
    )
    return build_allocation_return_panel(series=series)


def _rows(matrix) -> tuple[tuple[Decimal, ...], ...]:
    return tuple(tuple(D(v) for v in row) for row in matrix)


def _prior(matrix, returns) -> ExpectedReturnPrior:
    return ExpectedReturnPrior(source=_panel(len(matrix)), expected_returns=tuple(D(v) for v in returns), uncertainty_covariance=_rows(matrix))


def _view(kind, loadings, target, confidence) -> UserReturnView:
    return UserReturnView(kind=kind, loadings=tuple(D(v) for v in loadings), target_return=D(target), confidence=D(confidence))


def _viewset(matrix, returns, views) -> UserReturnViewSet:
    return build_user_return_view_set(prior=_prior(matrix, returns), views=tuple(views))


def _posterior(matrix, returns, views) -> BayesianExpectedReturnPosterior:
    return build_bayesian_expected_return_posterior(view_set=_viewset(matrix, returns, views))


DIAG2 = [["4", "0"], ["0", "9"]]
DIAG3 = [["4", "0", "0"], ["0", "9", "0"], ["0", "0", "1"]]
CORR3 = [["4", "1", "0.5"], ["1", "9", "-0.5"], ["0.5", "-0.5", "1"]]
M2 = ("0.04", "0.02")
M3 = ("0.05", "0.03", "0.04")


def _inverse(matrix: list[list[Fraction]]) -> list[list[Fraction]]:
    """Gauss-Jordan inverse over Fractions: an independent test-only reference, never used by production code."""
    n = len(matrix)
    work = [list(row) + [F(int(i == j)) for j in range(n)] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot_row = next(r for r in range(col, n) if work[r][col] != 0)
        work[col], work[pivot_row] = work[pivot_row], work[col]
        pivot = work[col][col]
        work[col] = [v / pivot for v in work[col]]
        for r in range(n):
            if r != col and work[r][col] != 0:
                factor = work[r][col]
                work[r] = [a - factor * b for a, b in zip(work[r], work[col])]
    return [row[n:] for row in work]


def _reference(viewset: UserReturnViewSet):
    m = [F(v) for v in viewset.source.expected_returns]
    U = [[F(v) for v in row] for row in viewset.source.uncertainty_covariance]
    n, k = len(m), len(viewset.views)
    P = [[F(v) for v in view.loadings] for view in viewset.views]
    q = [F(view.target_return) for view in viewset.views]
    c = [F(view.confidence) for view in viewset.views]
    PU = [[sum(P[a][i] * U[i][j] for i in range(n)) for j in range(n)] for a in range(k)]
    PUP = [[sum(PU[a][j] * P[b][j] for j in range(n)) for b in range(k)] for a in range(k)]
    S = [[PUP[a][b] + (((1 - c[a]) / c[a]) * PUP[a][a] if a == b else 0) for b in range(k)] for a in range(k)]
    S_inv = _inverse(S)
    d = [q[a] - sum(P[a][i] * m[i] for i in range(n)) for a in range(k)]
    gain = [[sum(U[i][j] * P[a][j] for j in range(n)) for a in range(k)] for i in range(n)]  # U P'
    y = [sum(S_inv[a][b] * d[b] for b in range(k)) for a in range(k)]
    m_post = [m[i] + sum(gain[i][a] * y[a] for a in range(k)) for i in range(n)]
    gS = [[sum(gain[i][a] * S_inv[a][b] for a in range(k)) for b in range(k)] for i in range(n)]
    U_post = [[U[i][j] - sum(gS[i][b] * PU[b][j] for b in range(k)) for j in range(n)] for i in range(n)]
    return m_post, U_post, S, d


def _reference_joseph(viewset: UserReturnViewSet):
    """Independent exact-Fraction Joseph form: K = U P' S^-1, A = I - K P, U_post = A U A' + K Omega K'."""
    m = [F(v) for v in viewset.source.expected_returns]
    U = [[F(v) for v in row] for row in viewset.source.uncertainty_covariance]
    n, k = len(m), len(viewset.views)
    P = [[F(v) for v in view.loadings] for view in viewset.views]
    c = [F(view.confidence) for view in viewset.views]
    PU = [[sum(P[a][i] * U[i][j] for i in range(n)) for j in range(n)] for a in range(k)]
    PUP = [[sum(PU[a][j] * P[b][j] for j in range(n)) for b in range(k)] for a in range(k)]
    omega = [((1 - c[a]) / c[a]) * PUP[a][a] for a in range(k)]
    S = [[PUP[a][b] + (omega[a] if a == b else 0) for b in range(k)] for a in range(k)]
    S_inv = _inverse(S)
    UPt = [[sum(U[i][j] * P[a][j] for j in range(n)) for a in range(k)] for i in range(n)]
    K = [[sum(UPt[i][a] * S_inv[a][b] for a in range(k)) for b in range(k)] for i in range(n)]
    A = [[F(int(i == j)) - sum(K[i][a] * P[a][j] for a in range(k)) for j in range(n)] for i in range(n)]
    AU = [[sum(A[i][a] * U[a][j] for a in range(n)) for j in range(n)] for i in range(n)]
    J1 = [[sum(AU[i][b] * A[j][b] for b in range(n)) for j in range(n)] for i in range(n)]
    J2 = [[sum(K[i][a] * omega[a] * K[j][a] for a in range(k)) for j in range(n)] for i in range(n)]
    return K, A, [[J1[i][j] + J2[i][j] for j in range(n)] for i in range(n)]


def _assert_matches_reference(posterior: BayesianExpectedReturnPosterior) -> None:
    m_post, u_post, _, _ = _reference(posterior.source)
    for got, want in zip(posterior.expected_returns, m_post):
        assert abs(F(got) - want) <= TOL
    for i, row in enumerate(posterior.uncertainty_covariance):
        for j, got in enumerate(row):
            assert abs(F(got) - u_post[i][j]) <= TOL


def _is_psd_fraction(matrix, tolerance: Fraction = F(0)) -> bool:
    """Test-only PSD check: every principal minor >= -tolerance (exact Fractions, no eigen-solver)."""
    n = len(matrix)
    for size in range(1, n + 1):
        for idx in itertools.combinations(range(n), size):
            sub = [[F(matrix[i][j]) for j in idx] for i in idx]
            det = _determinant(sub)
            if det < -tolerance:
                return False
    return True


def _determinant(matrix: list[list[Fraction]]) -> Fraction:
    n = len(matrix)
    if n == 1:
        return matrix[0][0]
    total = F(0)
    for col in range(n):
        minor = [row[:col] + row[col + 1:] for row in matrix[1:]]
        total += (-1) ** col * matrix[0][col] * _determinant(minor)
    return total


# --- linear algebra ----------------------------------------------------------------------------------------------

def _ctx() -> decimal.Context:
    return module_under_test._analytics_context()


def test_spd_factorization_is_ldl_with_unit_lower_triangle() -> None:
    lower, diagonal = module_under_test._factor_spd([[D("4"), D("2")], [D("2"), D("3")]], _ctx())
    assert diagonal == [D("4"), D("2")]
    assert lower[1][0] == D("0.5") and lower[0][0] == 1 and lower[1][1] == 1 and lower[0][1] == 0
    three = module_under_test._factor_spd([list(r) for r in _rows(CORR3)], _ctx())
    assert all(p > 0 for p in three[1])
    # L D L' reproduces the matrix exactly (independent Fraction check)
    lo, dg = three
    for i in range(3):
        for j in range(3):
            assert abs(sum(F(lo[i][k]) * F(dg[k]) * F(lo[j][k]) for k in range(3)) - F(D(CORR3[i][j]))) <= TOL


@pytest.mark.parametrize("matrix", [[["1", "1"], ["1", "1"]], [["1", "2"], ["2", "1"]], [["0", "0"], ["0", "1"]], [["-1", "0"], ["0", "1"]]])
def test_spd_factorization_rejects_singular_and_indefinite_without_repair(matrix) -> None:
    with pytest.raises(ValueError, match=ERR_SINGULAR):
        module_under_test._factor_spd([[D(v) for v in row] for row in matrix], _ctx())


def test_vector_and_multi_rhs_solves_match_the_exact_solution() -> None:
    ctx = _ctx()
    lower, diagonal = module_under_test._factor_spd([[D("4"), D("2")], [D("2"), D("3")]], ctx)
    assert module_under_test._solve_ldl(lower, diagonal, [D("6"), D("5")], ctx) == [D("1"), D("1")]
    solved = module_under_test._solve_ldl_matrix(lower, diagonal, [[D("6"), D("5")], [D("4"), D("2")], [D("0"), D("0")]], ctx)
    assert solved[0] == [D("1"), D("1")] and solved[2] == [D("0"), D("0")]
    assert solved[1] == [D("1"), D("0")]
    for column, expected in (([D("2"), D("3")], (F(0), F(1))), ([D("1"), D("0")], (F(3, 8), F(-1, 4)))):
        got = module_under_test._solve_ldl(lower, diagonal, column, ctx)
        assert all(abs(F(a) - b) <= TOL for a, b in zip(got, expected))


def test_solves_never_form_an_explicit_inverse_or_reorder_rows() -> None:
    attributes = {n.attr for n in ast.walk(ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)}
    assert not attributes & {"inverse", "inv", "pinv", "solve", "linalg"}


# --- posterior: single view semantics ----------------------------------------------------------------------------

def _single(confidence: str, target="0.10"):
    return _posterior(DIAG2, M2, [_view(KIND.ABSOLUTE, ("1", "0"), target, confidence)])


@pytest.mark.parametrize("confidence,share", [("1", F(1)), ("0.5", F(1, 2)), ("0.25", F(1, 4)), ("0.75", F(3, 4))])
def test_single_view_confidence_identities(confidence: str, share: Fraction) -> None:
    posterior = _single(confidence)
    prior_projected, target, s = F(D("0.04")), F(D("0.10")), F(4)
    assert abs(F(posterior.expected_returns[0]) - (prior_projected + share * (target - prior_projected))) <= TOL  # p m_post = p m + c (q - p m)
    assert abs(F(posterior.uncertainty_covariance[0][0]) - (1 - share) * s) <= TOL                               # p U_post p' = (1 - c) s
    assert posterior.expected_returns[1] == D("0.02") and posterior.uncertainty_covariance[1][1] == 9      # nothing leaks to an uncorrelated asset
    assert posterior.uncertainty_covariance[0][1] == 0 == posterior.uncertainty_covariance[1][0]


def test_full_confidence_makes_the_view_exact_and_the_posterior_singular_psd() -> None:
    posterior = _single("1")
    assert posterior.expected_returns[0] == D("0.10") and posterior.uncertainty_covariance[0][0] == 0
    assert posterior.uncertainty_covariance == ((D(0), D(0)), (D(0), D(9)))  # singular PSD: allowed for the posterior
    assert all(not value.is_signed() for row in posterior.uncertainty_covariance for value in row)
    assert _is_psd_fraction(posterior.uncertainty_covariance)


def test_half_and_quarter_confidence_exact_values() -> None:
    half, quarter = _single("0.5"), _single("0.25")
    assert half.expected_returns[0] == D("0.07") and half.uncertainty_covariance[0][0] == 2      # midpoint of 0.04 and 0.10; half the variance
    assert quarter.expected_returns[0] == D("0.055")                                              # 25% of the innovation
    assert abs(F(quarter.uncertainty_covariance[0][0]) - 3) <= TOL                                # 75% of the variance (sqrt(12) is irrational)


def test_relative_view_single_view_identity_and_innovation() -> None:
    posterior = _posterior(DIAG3, M3, [_view(KIND.RELATIVE, ("1", "-1", "0"), "0.28", "0.5")])
    assert posterior.view_innovations == (D("0.26"),)          # q - (m_0 - m_1) = 0.28 - 0.02
    assert posterior.projected_prior_returns == (D("0.02"),)
    spread_after = F(posterior.posterior_view_returns[0])
    assert spread_after == F(D("0.02")) + F(1, 2) * F(D("0.26"))   # p m_post = p m + c d
    assert posterior.posterior_shift == (D("0.04"), D("-0.09"), D("0"))
    p_u_p = F(posterior.uncertainty_covariance[0][0]) - 2 * F(posterior.uncertainty_covariance[0][1]) + F(posterior.uncertainty_covariance[1][1])
    assert abs(p_u_p - F(13, 2)) <= TOL  # p U_post p' = (1 - c) s = 6.5


def test_zero_innovation_leaves_the_mean_but_still_reduces_uncertainty() -> None:
    posterior = _posterior(DIAG2, M2, [_view(KIND.ABSOLUTE, ("1", "0"), "0.04", "0.5")])  # q = P m
    assert posterior.view_innovations == (D("0"),)
    assert posterior.expected_returns == tuple(D(v) for v in M2)
    assert posterior.uncertainty_covariance[0][0] == 2 < 4  # a view carries information even when it confirms the prior


def test_target_changes_the_mean_but_never_the_posterior_uncertainty() -> None:
    def views(target: str):
        return [_view(KIND.ABSOLUTE, ("0", "1", "0"), target, "0.6"), _view(KIND.RELATIVE, ("1", "-1", "0"), "0.01", "0.4")]

    first = _posterior(CORR3, M3, views("0.10"))
    second = _posterior(CORR3, M3, views("0.50"))
    assert first.uncertainty_covariance == second.uncertainty_covariance
    assert first.expected_returns != second.expected_returns
    assert first.innovation_covariance == second.innovation_covariance


# --- posterior: multiple views / full covariance ----------------------------------------------------------------

ABS_B = _view(KIND.ABSOLUTE, ("0", "1", "0"), "0.06", "0.75")
REL_AB = _view(KIND.RELATIVE, ("1", "-1", "0"), "0.02", "0.5")
REL_GROUP = _view(KIND.RELATIVE, ("0.5", "0.5", "-1"), "0.01", "0.25")


def test_absolute_plus_relative_with_off_diagonal_prior_matches_the_independent_reference() -> None:
    posterior = _posterior(CORR3, M3, [ABS_B, REL_AB])
    _assert_matches_reference(posterior)
    assert posterior.source.source.expected_returns == tuple(D(v) for v in M3) and posterior.expected_returns != posterior.source.source.expected_returns


def test_three_views_full_covariance_matches_the_independent_reference() -> None:
    posterior = _posterior(CORR3, M3, [ABS_B, REL_AB, REL_GROUP])
    _assert_matches_reference(posterior)
    _, _, s_reference, d_reference = _reference(posterior.source)
    for i, row in enumerate(posterior.innovation_covariance):
        for j, got in enumerate(row):
            assert abs(F(got) - s_reference[i][j]) <= TOL
    assert all(abs(F(a) - b) <= TOL for a, b in zip(posterior.view_innovations, d_reference))


def test_absolute_view_spills_over_to_correlated_assets_only() -> None:
    correlated = _posterior([["4", "2", "0"], ["2", "9", "0"], ["0", "0", "1"]], M3, [_view(KIND.ABSOLUTE, ("1", "0", "0"), "0.15", "0.5")])
    assert correlated.posterior_shift[1] != 0 and correlated.posterior_shift[2] == 0  # asset 1 moves via U_10, asset 2 does not
    assert correlated.posterior_shift[1] == D("0.025")                                 # (U_10 / (s + omega)) * d = (2 / 8) * 0.10
    independent = _posterior(DIAG3, M3, [_view(KIND.ABSOLUTE, ("1", "0", "0"), "0.15", "0.5")])
    assert independent.posterior_shift[1] == 0


def test_same_diagonal_different_off_diagonal_prior_changes_the_unviewed_assets() -> None:
    flat = _posterior(DIAG3, M3, [_view(KIND.ABSOLUTE, ("1", "0", "0"), "0.15", "0.5")])
    linked = _posterior([["4", "1", "0.5"], ["1", "9", "0"], ["0.5", "0", "1"]], M3, [_view(KIND.ABSOLUTE, ("1", "0", "0"), "0.15", "0.5")])
    assert [DIAG3[i][i] for i in range(3)] == ["4", "9", "1"]
    assert flat.posterior_shift[1] == 0 and flat.posterior_shift[2] == 0
    assert linked.posterior_shift[1] != 0 and linked.posterior_shift[2] != 0
    assert flat.uncertainty_covariance != linked.uncertainty_covariance


def test_input_view_order_does_not_change_the_posterior() -> None:
    base = (ABS_B, REL_AB, REL_GROUP)
    reference = _posterior(CORR3, M3, base)
    for permutation in itertools.permutations(base):
        again = _posterior(CORR3, M3, permutation)
        assert again == reference
        assert again.expected_returns == reference.expected_returns and again.uncertainty_covariance == reference.uncertainty_covariance
        assert again.view_innovations == reference.view_innovations and again.innovation_covariance == reference.innovation_covariance


def test_uncertainty_never_increases_on_the_fixtures() -> None:
    for views in ([ABS_B], [ABS_B, REL_AB], [ABS_B, REL_AB, REL_GROUP]):
        posterior = _posterior(CORR3, M3, views)
        prior = posterior.source.source.uncertainty_covariance
        reduction = [[F(prior[i][j]) - F(posterior.uncertainty_covariance[i][j]) for j in range(3)] for i in range(3)]
        assert _is_psd_fraction(reduction, F(1, 10**30))  # U - U_post is PSD up to 50-digit rounding noise (fixture evidence only)


def test_posterior_uncertainty_is_symmetric_exactly() -> None:
    posterior = _posterior(CORR3, M3, [ABS_B, REL_AB, REL_GROUP])
    cov = posterior.uncertainty_covariance
    assert all(cov[i][j].as_tuple() == cov[j][i].as_tuple() for i in range(3) for j in range(3))


def test_noisy_view_posterior_is_strictly_positive_definite() -> None:
    cov = _posterior(CORR3, M3, [ABS_B, REL_AB]).uncertainty_covariance
    leading = [F(cov[0][0]), _determinant([[F(cov[i][j]) for j in range(2)] for i in range(2)]), _determinant([[F(cov[i][j]) for j in range(3)] for i in range(3)])]
    assert all(minor > 0 for minor in leading)


# --- view-system geometry ----------------------------------------------------------------------------------------

def _dependent(conf_a: str, conf_b: str, conf_ab: str):
    return [
        _view(KIND.ABSOLUTE, ("1", "0"), "0.10", conf_a),
        _view(KIND.ABSOLUTE, ("0", "1"), "0.05", conf_b),
        _view(KIND.RELATIVE, ("1", "-1"), "0.05", conf_ab),
    ]


def test_dependent_full_confidence_views_fail_closed() -> None:
    with pytest.raises(ValueError, match=ERR_SINGULAR):
        _posterior(DIAG2, M2, _dependent("1", "1", "1"))


def test_dependent_views_with_partial_noise_are_valid() -> None:
    posterior = _posterior(DIAG2, M2, _dependent("0.5", "0.5", "0.5"))
    _assert_matches_reference(posterior)
    mixed = _posterior(DIAG2, M2, _dependent("1", "1", "0.5"))  # noise on one dependent view makes S strictly PD
    _assert_matches_reference(mixed)


def test_algebraic_dependence_alone_is_not_the_rejection_criterion() -> None:
    rows = [view.loadings for view in _viewset(DIAG2, M2, _dependent("0.5", "0.5", "0.5")).views]
    assert len(rows) == 3 and len(rows[0]) == 2  # three views over two assets are necessarily linearly dependent


EXACT_PAIR = [_view(KIND.ABSOLUTE, ("0", "1", "0"), "0.06", "1"), _view(KIND.ABSOLUTE, ("1", "0", "0"), "0.07", "1")]


def test_two_independent_full_confidence_views_on_correlated_prior_are_supported() -> None:
    """Phase 20B-R1 primary regression (the former known-limitation fixture, unchanged): two exact views on a correlated prior."""
    posterior = _posterior(CORR3, M3, EXACT_PAIR)
    for view, value in zip(posterior.views, posterior.posterior_view_returns):
        assert abs(F(value) - F(view.target_return)) <= TOL  # p m_post = q for every exact view
        quadratic = sum(
            F(view.loadings[i]) * F(posterior.uncertainty_covariance[i][j]) * F(view.loadings[j]) for i in range(3) for j in range(3)
        )
        assert abs(quadratic) <= TOL  # p U_post p' = 0 along every exact direction
    viewset = posterior.source
    gram = module_under_test._exact_gram(module_under_test._posterior_factor(viewset))
    assert tuple(tuple(row) for row in gram) == posterior.uncertainty_covariance  # PSD certificate: the covariance IS the factor Gram
    _assert_matches_reference(posterior)
    assert posterior.uncertainty_covariance[2][2] != 0  # the unviewed asset keeps its (reduced) uncertainty
    assert _posterior(CORR3, M3, EXACT_PAIR[:1]).uncertainty_covariance[0][1] == 0


def test_r1_regression_exact_relative_view_next_to_a_noisy_relative_view_is_supported() -> None:
    """Permanent regression: this valid case was rejected by the finite-precision PSD test under the R1 Joseph form."""
    prior = [["1.06", "0.12", "0.03"], ["0.12", "2.71", "-0.18"], ["0.03", "-0.18", "1.14"]]
    views = [_view(KIND.RELATIVE, ("1", "0", "-1"), "0.01", "1"), _view(KIND.RELATIVE, ("1", "-1", "0"), "0.01", "0.7")]
    posterior = _posterior(prior, M3, views)
    _assert_matches_reference(posterior)
    for view, value in zip(posterior.views, posterior.posterior_view_returns):
        if view.confidence == 1:
            assert abs(F(value) - F(view.target_return)) <= TOL  # the exact view is met
            quadratic = sum(F(view.loadings[i]) * F(posterior.uncertainty_covariance[i][j]) * F(view.loadings[j]) for i in range(3) for j in range(3))
            assert abs(quadratic) <= TOL and quadratic >= 0
    gram = module_under_test._exact_gram(module_under_test._posterior_factor(posterior.source))
    assert tuple(tuple(row) for row in gram) == posterior.uncertainty_covariance


def test_all_asset_exact_views_collapse_the_uncertainty_to_the_zero_matrix_without_signed_zero() -> None:
    views = [_view(KIND.ABSOLUTE, ("1", "0", "0"), "0.07", "1"), _view(KIND.ABSOLUTE, ("0", "1", "0"), "0.06", "1"),
             _view(KIND.ABSOLUTE, ("0", "0", "1"), "0.05", "1")]
    posterior = _posterior(CORR3, M3, views)
    assert posterior.expected_returns == (D("0.07"), D("0.06"), D("0.05"))
    assert all(value == 0 and not value.is_signed() for row in posterior.uncertainty_covariance for value in row)


def test_joseph_form_is_algebraically_equivalent_to_the_gaussian_posterior_covariance() -> None:
    for views in ([ABS_B], [ABS_B, REL_AB], [ABS_B, REL_AB, REL_GROUP], EXACT_PAIR, _dependent("0.5", "0.5", "0.5")):
        matrix, returns = (DIAG2, M2) if views is not EXACT_PAIR and len(views[0].loadings) == 2 else (CORR3, M3)
        viewset = _viewset(matrix, returns, views)
        K, A, joseph = _reference_joseph(viewset)
        _, subtractive, _, _ = _reference(viewset)
        assert joseph == subtractive  # exact rational identity A U A' + K Omega K' = U - U P' S^-1 P U
        posterior = build_bayesian_expected_return_posterior(view_set=viewset)
        for i, row in enumerate(posterior.uncertainty_covariance):
            for j, got in enumerate(row):
                assert abs(F(got) - joseph[i][j]) <= TOL
        assert len(K) == viewset.source.dimension and len(A) == viewset.source.dimension


def test_covariance_is_a_square_root_joseph_factor_materialized_as_an_exact_gram_matrix() -> None:
    functions = {n.name for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)}
    assert {"_joseph_factor", "_exact_gram", "_exact_dot", "_posterior_factor"} <= functions
    assert "_require_psd" not in functions and "_joseph_covariance" not in functions  # no approximate LDL acceptance gate
    canonical = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == "_canonical_posterior")
    called = {c.func.id for c in ast.walk(canonical) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert {"_joseph_factor", "_exact_gram"} <= called and "_require_psd" not in called
    assert not {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} & {"Fraction", "fractions"}


_TOLERANCE_FRAGMENTS = ("tolerance", "epsilon", "near_zero", "ulp", "jitter", "nearest", "clip", "clamp", "repair", "projection", "symmetrize")


def test_no_tolerance_clipping_or_repair_surface_and_no_precision_escalation() -> None:
    identifiers = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    identifiers |= {n.name for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.ClassDef))} | {n.arg for n in ast.walk(_TREE) if isinstance(n, ast.arg)}
    for identifier in identifiers:
        for fragment in _TOLERANCE_FRAGMENTS:
            assert fragment not in identifier.lower(), identifier
    calls = {c.func.id for c in ast.walk(_TREE) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert not calls & {"max", "min", "abs", "round", "float", "Fraction"}  # no flooring, absolute value or averaging of pivots
    assert re.findall(r"prec\s*=\s*(\d+)", _SOURCE) == ["50"]  # no precision escalation, no second Decimal context
    assert [f.name for f in ast.walk(_TREE) if isinstance(f, ast.FunctionDef) for c in ast.walk(f)
            if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "Context"] == ["_analytics_context"]


# --- domain support ----------------------------------------------------------------------------------------------

def test_posterior_expected_return_below_minus_one_fails_closed_without_clipping() -> None:
    with pytest.raises(ValueError, match=ERR_SUPPORT):
        _posterior([["1", "0.9"], ["0.9", "1"]], ("0.0", "-0.5"), [_view(KIND.ABSOLUTE, ("1", "0"), "-1", "1")])
    boundary = _posterior([["1", "0.5"], ["0.5", "1"]], ("0.0", "-0.5"), [_view(KIND.ABSOLUTE, ("1", "0"), "-1", "1")])
    assert boundary.expected_returns[1] == D("-1")  # exactly -1 is on the support (no clipping needed, none applied)


def test_source_prior_and_views_are_retained_by_identity_and_unchanged() -> None:
    viewset = _viewset(CORR3, M3, [ABS_B, REL_AB])
    prior_returns, prior_u = viewset.source.expected_returns, viewset.source.uncertainty_covariance
    posterior = build_bayesian_expected_return_posterior(view_set=viewset)
    assert posterior.source is viewset and posterior.prior is viewset.source and posterior.views is viewset.views
    assert viewset.source.expected_returns is prior_returns and viewset.source.uncertainty_covariance is prior_u
    assert posterior.expected_returns is not prior_returns and posterior.expected_returns != prior_returns
    assert posterior.instrument_ids == viewset.source.instrument_ids and posterior.dimension == 3
    assert all(a is b for a, b in zip(posterior.views, viewset.views))


def test_derived_diagnostics_are_not_stored() -> None:
    assert [f.name for f in dataclasses.fields(BayesianExpectedReturnPosterior)] == ["source", "expected_returns", "uncertainty_covariance"]
    view = _view(KIND.ABSOLUTE, ("1", "0"), "0.10", "0.5")
    posterior = _posterior(DIAG2, M2, [view])
    assert posterior.posterior_view_returns == (posterior.expected_returns[0],)
    assert posterior.posterior_shift == (D("0.03"), D("0"))
    assert view is posterior.views[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        posterior.expected_returns = ()  # type: ignore[misc]


def test_builder_is_keyword_only_with_exact_input() -> None:
    viewset = _viewset(DIAG2, M2, [_view(KIND.ABSOLUTE, ("1", "0"), "0.10", "0.5")])
    with pytest.raises(TypeError):
        build_bayesian_expected_return_posterior(viewset)  # type: ignore[misc]
    with pytest.raises(TypeError):
        build_bayesian_expected_return_posterior(view_set=viewset, tau=D("0.05"))  # type: ignore[call-arg]
    for bad in (None, viewset.source, object(), viewset.views):
        with pytest.raises(TypeError):
            build_bayesian_expected_return_posterior(view_set=bad)  # type: ignore[arg-type]

    class _Sub(UserReturnViewSet):
        pass

    sub = object.__new__(_Sub)
    object.__setattr__(sub, "source", viewset.source)
    object.__setattr__(sub, "views", viewset.views)
    with pytest.raises(TypeError):
        build_bayesian_expected_return_posterior(view_set=sub)


# --- forge resistance --------------------------------------------------------------------------------------------

def _good():
    return _posterior(CORR3, M3, [ABS_B, REL_AB])


def _make(source, returns, covariance):
    return BayesianExpectedReturnPosterior(source=source, expected_returns=returns, uncertainty_covariance=covariance)


def test_valid_posterior_reconstructs_equal() -> None:
    good = _good()
    assert _make(good.source, good.expected_returns, good.uncertainty_covariance) == good


def test_forged_types_and_shapes_are_rejected() -> None:
    good = _good()

    class _DecSub(Decimal):
        pass

    with pytest.raises(TypeError):
        _make(good.source.source, good.expected_returns, good.uncertainty_covariance)
    with pytest.raises(TypeError):
        _make(None, good.expected_returns, good.uncertainty_covariance)
    for bad in (list(good.expected_returns), (0.1, 0.1, 0.1), (1, 0, 0), (True, False, True), ("0.1",) * 3, (_DecSub("0.1"), D(0), D(0))):
        with pytest.raises(TypeError):
            _make(good.source, bad, good.uncertainty_covariance)
    with pytest.raises(TypeError):
        _make(good.source, good.expected_returns, [list(r) for r in good.uncertainty_covariance])
    with pytest.raises(TypeError):
        _make(good.source, good.expected_returns, (list(good.uncertainty_covariance[0]),) + good.uncertainty_covariance[1:])
    with pytest.raises(TypeError):
        _make(good.source, good.expected_returns, ((1, 0, 0), (0, 1, 0), (0, 0, 1)))
    for bad in (good.expected_returns[:2], good.expected_returns + (D(0),)):
        with pytest.raises(ValueError):
            _make(good.source, bad, good.uncertainty_covariance)
    for bad in (good.uncertainty_covariance[:2], tuple(r[:2] for r in good.uncertainty_covariance)):
        with pytest.raises(ValueError):
            _make(good.source, good.expected_returns, bad)


def test_forged_values_are_rejected() -> None:
    good = _good()
    for bad in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            _make(good.source, (D(bad),) + good.expected_returns[1:], good.uncertainty_covariance)
    with pytest.raises(ValueError):
        _make(good.source, (D("-1.5"),) + good.expected_returns[1:], good.uncertainty_covariance)  # below the simple-return support
    cov = [list(r) for r in good.uncertainty_covariance]
    cov[0][1] = D("NaN")
    with pytest.raises(ValueError):
        _make(good.source, good.expected_returns, tuple(tuple(r) for r in cov))
    asym = [list(r) for r in good.uncertainty_covariance]
    asym[0][1] = asym[0][1] + D("0.001")
    with pytest.raises(ValueError):
        _make(good.source, good.expected_returns, tuple(tuple(r) for r in asym))


def test_forged_posterior_geometry_is_rejected_by_the_canonical_rebuild_alone() -> None:
    good = _good()
    negative_diagonal = ((D("-1"), D(0), D(0)), (D(0), D(1), D(0)), (D(0), D(0), D(1)))
    indefinite = ((D(1), D(2), D(0)), (D(2), D(1), D(0)), (D(0), D(0), D(1)))
    for bad in (negative_diagonal, indefinite):
        with pytest.raises(ValueError, match=ERR_MATCH):
            _make(good.source, good.expected_returns, bad)


def test_a_valid_psd_gram_matrix_that_is_not_the_canonical_posterior_is_rejected() -> None:
    good = _good()
    half_identity = tuple(tuple(D("0.5") if i == j else D(0) for j in range(3)) for i in range(3))  # a perfectly valid PSD matrix
    singular_psd = ((D(1), D(1), D(0)), (D(1), D(1), D(0)), (D(0), D(0), D(1)))
    for forged in (half_identity, singular_psd, good.source.source.uncertainty_covariance):
        with pytest.raises(ValueError, match=ERR_MATCH):
            _make(good.source, good.expected_returns, forged)


def test_otherwise_valid_alternative_posteriors_are_rejected_by_the_canonical_rebuild() -> None:
    good = _good()
    shifted = (good.expected_returns[0] + D("0.001"),) + good.expected_returns[1:]
    with pytest.raises(ValueError):
        _make(good.source, shifted, good.uncertainty_covariance)
    with pytest.raises(ValueError):  # the prior itself is a valid expected-return vector but not this posterior
        _make(good.source, good.source.source.expected_returns, good.uncertainty_covariance)
    with pytest.raises(ValueError):  # the prior covariance is valid PSD but not this posterior covariance
        _make(good.source, good.expected_returns, good.source.source.uncertainty_covariance)
    scaled = tuple(tuple(v * D("0.9") for v in row) for row in good.uncertainty_covariance)
    with pytest.raises(ValueError):
        _make(good.source, good.expected_returns, scaled)


# --- exact Gram materialization ----------------------------------------------------------------------------------

def test_exact_dot_cancellation_returns_unsigned_zero() -> None:
    for left, right in (((D("0.1"), D("0.2")), (D("0.2"), D("-0.1"))), ((D("2E-30"), D("3")), (D("3"), D("-2E-30")))):
        result = module_under_test._exact_dot(left, right)
        assert result == 0 and not result.is_signed() and result.as_tuple() == D(0).as_tuple()


def test_exact_dot_aligns_different_exponents_without_rounding() -> None:
    assert module_under_test._exact_dot((D("1E+5"), D("3E-7")), (D("2E-3"), D("5E+2"))) == D("200.00015")
    value = module_under_test._exact_dot((D("1.23456789012345678901234567890123456789012345678901234567890"), D("1E-80")), (D("3"), D("7")))
    assert F(value) == F(D("1.23456789012345678901234567890123456789012345678901234567890")) * 3 + F(D("1E-80")) * 7  # beyond 50 digits, exact
    assert module_under_test._exact_dot((D("2"), D("0")), (D("0"), D("5"))) == 0
    assert module_under_test._exact_dot((D("-1.5"), D("2")), (D("2"), D("1"))) == D("-1")


def test_exact_gram_entries_are_computed_once_and_mirrored() -> None:
    factor = [[D("0.3"), D("-1.2"), D("0.07")], [D("0.5"), D("0.25"), D("1E-40")], [D("-0.1"), D("0"), D("2")]]
    gram = module_under_test._exact_gram(factor)
    for i in range(3):
        for j in range(3):
            assert gram[i][j].as_tuple() == gram[j][i].as_tuple()
            assert F(gram[i][j]) == sum(F(factor[i][a]) * F(factor[j][a]) for a in range(3))
    assert all(gram[i][i] >= 0 for i in range(3))  # a Gram matrix has a non-negative diagonal by construction


def test_exact_gram_resource_ceiling_is_a_representation_limit_checked_before_any_big_integer() -> None:
    assert module_under_test._EXACT_GRAM_MAX_DECIMAL_PLACES == 1000
    at_ceiling = module_under_test._exact_dot((D("1E+500"), D("1E-500")), (D(1), D(1)))
    assert F(at_ceiling) == F(10) ** 500 + F(1, 10 ** 500)
    for left in ((D("1E+501"), D("1E-500")), (D("1E+999999999"), D("1E-999999999"))):  # the second would need a ~2e9-digit integer
        with pytest.raises(ValueError, match=ERR_RANGE):
            module_under_test._exact_dot(left, (D(1), D(1)))
    with pytest.raises(ValueError, match=ERR_RANGE):
        module_under_test._exact_dot((D("1E+999999999999999999"),), (D("1E+999999999999999999"),))  # exponent beyond the Decimal range


def test_exact_gram_does_not_touch_the_ambient_context() -> None:
    factor = [[D("0.3"), D("-1.2"), D("0.07")], [D("0.5"), D("0.25"), D("1E-40")]]
    baseline = module_under_test._exact_gram(factor)
    for hostile in _hostile_contexts():
        with decimal.localcontext(hostile) as ambient:
            ambient.clear_flags()
            snapshot = (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps))
            result = module_under_test._exact_gram(factor)
            assert (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps)) == snapshot
            assert not any(ambient.flags.values())
        assert [[v.as_tuple() for v in row] for row in result] == [[v.as_tuple() for v in row] for row in baseline]


def test_posterior_covariance_is_exactly_the_gram_of_its_canonical_factor() -> None:
    for views in ([ABS_B], [ABS_B, REL_AB], [ABS_B, REL_AB, REL_GROUP], EXACT_PAIR):
        posterior = _posterior(CORR3, M3, views)
        gram = module_under_test._exact_gram(module_under_test._posterior_factor(posterior.source))
        assert tuple(tuple(row) for row in gram) == posterior.uncertainty_covariance
        assert [[v.as_tuple() for v in row] for row in gram] == [[v.as_tuple() for v in row] for row in posterior.uncertainty_covariance]
        factor = module_under_test._posterior_factor(posterior.source)
        assert len(factor) == 3 and all(len(row) == 3 + len(views) for row in factor)  # F = [F_prior | F_noise]


def test_prior_square_root_factor_reproduces_the_prior_uncertainty() -> None:
    ctx = _ctx()
    root = module_under_test._prior_factor([list(row) for row in _rows(CORR3)], ctx)
    for i in range(3):
        for j in range(3):
            assert abs(sum(F(root[i][a]) * F(root[j][a]) for a in range(3)) - F(D(CORR3[i][j]))) <= TOL  # U = C C'
    assert all(root[i][j] == 0 for i in range(3) for j in range(i + 1, 3)) and all(root[i][i] > 0 for i in range(3))


# --- deterministic structurally valid corpus ---------------------------------------------------------------------

_CORPUS_ROWS = (
    (KIND.ABSOLUTE, ("1", "0", "0")), (KIND.ABSOLUTE, ("0", "1", "0")), (KIND.ABSOLUTE, ("0", "0", "1")),
    (KIND.RELATIVE, ("1", "-1", "0")), (KIND.RELATIVE, ("1", "0", "-1")), (KIND.RELATIVE, ("0", "1", "-1")),
    (KIND.RELATIVE, ("0.5", "0.5", "-1")),
)


def _fraction_rank(rows) -> int:
    work = [[F(D(v)) for v in row] for row in rows]
    rank, columns = 0, len(work[0])
    for col in range(columns):
        pivot = next((r for r in range(rank, len(work)) if work[r][col] != 0), None)
        if pivot is None:
            continue
        work[rank], work[pivot] = work[pivot], work[rank]
        for r in range(len(work)):
            if r != rank and work[r][col] != 0:
                factor = work[r][col] / work[rank][col]
                work[r] = [a - factor * b for a, b in zip(work[r], work[rank])]
        rank += 1
    return rank


def _structurally_valid_cases(count: int = 600):
    """Validity comes from construction (L L' with positive diagonal => strictly PD; Phase 20A-valid canonical views;
    no linearly dependent all-noiseless system), never from asking Phase 20B whether it succeeds."""
    rng = random.Random(20260429)
    cases = []
    while len(cases) < count:
        lower = [[D(0)] * 3 for _ in range(3)]
        for i in range(3):
            lower[i][i] = D(rng.randint(5, 20)) / 10
            for j in range(i):
                lower[i][j] = D(rng.randint(-12, 12)) / 10
        prior = [[str(sum((lower[i][k] * lower[j][k] for k in range(3)), D(0))) for j in range(3)] for i in range(3)]
        chosen = rng.sample(range(len(_CORPUS_ROWS)), rng.choice([1, 2, 2, 2, 3]))
        confidences = [rng.choice(["1", "0.7", "0.5", "0.25"]) for _ in chosen]
        if all(c == "1" for c in confidences) and _fraction_rank([_CORPUS_ROWS[i][1] for i in chosen]) < len(chosen):
            continue  # genuinely dependent all-noiseless system: invalid by Phase 20B's fail-closed policy
        returns = tuple(str(D(rng.randint(0, 100)) / 1000) for _ in range(3))
        views = [
            _view(_CORPUS_ROWS[i][0], _CORPUS_ROWS[i][1], str(D(rng.randint(-20, 100)) / 1000 if _CORPUS_ROWS[i][0] is KIND.RELATIVE else D(rng.randint(0, 100)) / 1000), c)
            for i, c in zip(chosen, confidences)
        ]
        cases.append((prior, returns, views))
    return cases


def test_corpus_of_600_structurally_valid_cases_has_zero_false_rejections() -> None:
    cases = _structurally_valid_cases(600)
    assert len(cases) == 600
    failures = []
    for index, (prior, returns, views) in enumerate(cases):
        try:
            posterior = _posterior(prior, returns, views)
        except ValueError as error:  # any rejection of a structurally valid case is a false negative
            failures.append((index, str(error)))
            continue
        covariance = posterior.uncertainty_covariance
        assert all(covariance[i][j].as_tuple() == covariance[j][i].as_tuple() and not (covariance[i][j].is_zero() and covariance[i][j].is_signed())
                   for i in range(3) for j in range(3))
        assert all(covariance[i][i] >= 0 for i in range(3))
    assert failures == []


def test_corpus_prefix_matches_the_independent_fraction_reference() -> None:
    for prior, returns, views in _structurally_valid_cases(600)[:25]:
        _assert_matches_reference(_posterior(prior, returns, views))


# --- Decimal isolation / determinism / range ---------------------------------------------------------------------

def _hostile_contexts():
    return [
        decimal.Context(prec=2, rounding=decimal.ROUND_DOWN, Emin=-3, Emax=3,
                        traps=[decimal.InvalidOperation, decimal.Overflow, decimal.Inexact, decimal.Rounded]),
        decimal.Context(prec=1, rounding=decimal.ROUND_CEILING, traps=[]),
    ]


def test_hostile_ambient_context_changes_nothing_and_is_not_mutated() -> None:
    baseline = _posterior(CORR3, M3, [ABS_B, REL_AB, REL_GROUP])
    for hostile in _hostile_contexts():
        with decimal.localcontext(hostile) as ambient:
            ambient.clear_flags()
            snapshot = (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps))
            posterior = _posterior(CORR3, M3, [ABS_B, REL_AB, REL_GROUP])
            derived = (posterior.projected_prior_returns, posterior.view_innovations, posterior.innovation_covariance,
                       posterior.posterior_view_returns, posterior.posterior_shift)
            assert (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps)) == snapshot
            assert not any(ambient.flags.values())
        assert posterior == baseline
        assert [v.as_tuple() for v in posterior.expected_returns] == [v.as_tuple() for v in baseline.expected_returns]
        assert [[v.as_tuple() for v in row] for row in posterior.uncertainty_covariance] == [[v.as_tuple() for v in row] for row in baseline.uncertainty_covariance]
        assert derived == (baseline.projected_prior_returns, baseline.view_innovations, baseline.innovation_covariance,
                           baseline.posterior_view_returns, baseline.posterior_shift)


def test_repeated_builds_are_identical() -> None:
    viewset = _viewset(CORR3, M3, [ABS_B, REL_AB, REL_GROUP])
    first = build_bayesian_expected_return_posterior(view_set=viewset)
    for _ in range(3):
        again = build_bayesian_expected_return_posterior(view_set=viewset)
        assert again == first and [v.as_tuple() for v in again.expected_returns] == [v.as_tuple() for v in first.expected_returns]


def test_decimal_overflow_is_a_static_range_error(monkeypatch) -> None:
    viewset = _viewset(CORR3, M3, [ABS_B, REL_AB])
    good = build_bayesian_expected_return_posterior(view_set=viewset)

    def _tiny_range_context() -> decimal.Context:
        return decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=-5, Emax=0)

    monkeypatch.setattr(module_under_test, "_analytics_context", _tiny_range_context)
    with pytest.raises(ValueError, match=ERR_RANGE):
        build_bayesian_expected_return_posterior(view_set=viewset)
    with pytest.raises(ValueError, match=ERR_RANGE):
        _ = good.innovation_covariance


def test_negative_zero_is_normalized_in_public_outputs() -> None:
    posterior = _posterior(DIAG2, M2, [_view(KIND.ABSOLUTE, ("1", "0"), "0.04", "1")])
    values = list(posterior.expected_returns) + [v for row in posterior.uncertainty_covariance for v in row] + list(posterior.posterior_shift)
    assert all(not v.is_signed() for v in values if v.is_zero())


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/allocation_user_view_posterior.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_only_the_phase_20a_authority_is_imported() -> None:
    plain: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            plain.add(node.module or "")
    assert plain <= {"__future__", "dataclasses", "decimal", "uuid", "backend.engine.private.allocation_user_views"}
    for forbidden in ("allocation_matrix", "allocation_benchmarks", "allocation_risk_parity", "allocation_hrp", "allocation_cvar",
                      "numpy", "scipy", "pandas", "math", "random", "time", "os"):
        assert not any(forbidden in name for name in plain), forbidden


def test_one_fresh_analytics_context_no_ambient_state_and_no_float() -> None:
    owners = [f.name for f in ast.walk(_TREE) if isinstance(f, ast.FunctionDef)
              for c in ast.walk(f) if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "Context"]
    assert owners == ["_analytics_context"]
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not names & {"getcontext", "setcontext", "localcontext", "float", "fmean"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is float]
    assert "prec=50" in _SOURCE and "ROUND_HALF_EVEN" in _SOURCE
    assert "Emin=decimal.MIN_EMIN" in _SOURCE and "Emax=decimal.MAX_EMAX" in _SOURCE
    assert not [n for n in _TREE.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "attr", "") == "Context"]


def test_no_historical_return_or_covariance_access() -> None:
    attributes = {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)}
    assert not (attributes | names) & {"covariance", "sample_means", "series", "points", "simple_return", "AllocationReturnPanel",
                                       "AllocationCovarianceMatrix", "periods", "observation_count"}


_FORBIDDEN_FRAGMENTS = (
    "risk_aversion", "equilibrium", "market_cap", "market_portfolio", "reverse_optim", "capm", "pinv", "linalg", "invert",
    "adjugate", "determinant", "lstsq", "weights", "benchmark", "cvar", "optimiz", "score", "rank", "recommend", "rebalance",
    "persist", "repository", "user_id", "owner_id", "supabase",
)


def test_no_tau_inverse_equilibrium_allocation_or_persistence_surface() -> None:
    identifiers: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
    for identifier in identifiers:
        lowered = identifier.lower()
        assert lowered not in {"tau", "inv", "inverse", "pinv"}, identifier
        for fragment in _FORBIDDEN_FRAGMENTS:
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    builder = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "build_bayesian_expected_return_posterior")
    assert [a.arg for a in builder.args.kwonlyargs] == ["view_set"] and not builder.args.args
    public = {n for n in dir(module_under_test) if not n.startswith("_")}
    assert {"BayesianExpectedReturnPosterior", "build_bayesian_expected_return_posterior"} <= public


def test_documents_the_methodology() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("Black-Litterman-LIKE", "Joseph", "S = P U P' + Omega", "never forms", "strictly positive definite", "positive semidefinite",
                   "singular", "simple-return support", "no tau", "independent of the targets", "square-root", "Gram", "context-free"):
        assert needle in doc, needle
