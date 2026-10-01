"""
backend/tests/test_allocation_hrp.py
====================================
Phase 18D: Hierarchical Risk Parity over the Phase 18A sample covariance matrix.

correlation -> correlation distance -> distance-of-distance -> deterministic single linkage -> quasi-diagonal order
-> len // 2 recursive bisection -> cluster inverse-VARIANCE allocation -> exact stored weights.
No matrix inversion, no expected returns, no ERC reuse, no configurable variants.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import allocation_hrp as module_under_test
from backend.engine.private.allocation_benchmarks import build_inverse_volatility_benchmark
from backend.engine.private.allocation_hrp import (
    HierarchicalRiskParityResult,
    HierarchicalRiskParityUnavailableReason,
    build_hierarchical_risk_parity_benchmark,
)
from backend.engine.private.allocation_matrix import (
    AllocationCovarianceMatrix,
    AllocationInstrumentReturnSeries,
    AllocationMonth,
    AllocationMonthlyReturnPoint,
    build_allocation_covariance_matrix,
    build_allocation_return_panel,
)

D = Decimal
UUIDS = [UUID(int=i) for i in range(1, 9)]
REASON = HierarchicalRiskParityUnavailableReason


def _ctx() -> decimal.Context:
    return module_under_test._analytics_context()


def _series(instrument_id: UUID, values: list[str]) -> AllocationInstrumentReturnSeries:
    points = tuple(
        AllocationMonthlyReturnPoint(period=AllocationMonth(2026, i + 1), simple_return=D(v)) for i, v in enumerate(values)
    )
    return AllocationInstrumentReturnSeries(instrument_id=instrument_id, points=points)


def _cov(*columns: list[str]) -> AllocationCovarianceMatrix:
    panel = build_allocation_return_panel(series=tuple(_series(UUIDS[i], v) for i, v in enumerate(columns)))
    return build_allocation_covariance_matrix(return_panel=panel)


def _hrp(*columns: list[str]) -> HierarchicalRiskParityResult:
    return build_hierarchical_risk_parity_benchmark(covariance=_cov(*columns))


def _exact_sum(values) -> Fraction:
    """Context-independent exact sum (Fraction(Decimal) is exact; no Decimal context participates)."""
    return sum((Fraction(value) for value in values), Fraction(0))


def _rows(matrix: list[list[str]]) -> tuple[tuple[Decimal, ...], ...]:
    return tuple(tuple(D(v) for v in row) for row in matrix)


# Orthogonal centered patterns: exactly zero off-diagonal covariance.
DIAG_A = ["0.5", "-0.5", "0.5", "-0.5"]      # variance 1/3
DIAG_B = ["1", "1", "-1", "-1"]              # variance 4/3  (sigma ratio 2)
DIAG_C = ["0.25", "-0.25", "-0.25", "0.25"]  # variance 1/12 (sigma ratio 1/2)
# Same multiset => identical diagonal; different arrangement => different correlation structure.
PERM_A = ["0", "1", "2", "3"]
SRC1 = (PERM_A, ["1", "0", "3", "2"], ["1", "3", "0", "2"])
SRC2 = (PERM_A, ["0", "2", "1", "3"], ["2", "0", "3", "1"])
# T = 3 patterns with exactly representable correlations (rho = +1 / -1).
T3_A, T3_B, T3_BNEG = ["0", "1", "2"], ["0", "2", "4"], ["4", "2", "0"]


# --- taxonomy / contracts ----------------------------------------------------------------------------------------

def test_unavailable_reason_enum_is_exactly_the_three_reasons() -> None:
    assert issubclass(HierarchicalRiskParityUnavailableReason, Enum)
    assert {m.name: m.value for m in HierarchicalRiskParityUnavailableReason} == {
        "ZERO_VARIANCE": "zero_variance",
        "INVALID_CORRELATION_GEOMETRY": "invalid_correlation_geometry",
        "DEGENERATE_CLUSTER_VARIANCE": "degenerate_cluster_variance",
    }


def test_result_has_exactly_three_stored_fields_and_is_frozen() -> None:
    assert [f.name for f in dataclasses.fields(HierarchicalRiskParityResult)] == ["source", "weights", "unavailable_reason"]
    result = _hrp(DIAG_A, DIAG_B)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.weights = None  # type: ignore[misc]


def test_builder_is_keyword_only_exact_type_with_no_options() -> None:
    covariance = _cov(DIAG_A, DIAG_B)
    with pytest.raises(TypeError):
        build_hierarchical_risk_parity_benchmark(covariance)  # type: ignore[misc]
    with pytest.raises(TypeError):
        build_hierarchical_risk_parity_benchmark()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        build_hierarchical_risk_parity_benchmark(covariance=covariance, linkage="ward")  # type: ignore[call-arg]

    class _Duck:
        sample_means = ()
        covariance = ()

    for bad in (None, _Duck(), covariance.source, ((D(1),),)):
        with pytest.raises(TypeError):
            build_hierarchical_risk_parity_benchmark(covariance=bad)  # type: ignore[arg-type]

    class _Sub(AllocationCovarianceMatrix):
        pass

    sub = object.__new__(_Sub)
    for name in ("source", "sample_means", "covariance"):
        object.__setattr__(sub, name, getattr(covariance, name))
    with pytest.raises(TypeError):
        build_hierarchical_risk_parity_benchmark(covariance=sub)
    result = build_hierarchical_risk_parity_benchmark(covariance=covariance)
    assert result.source is covariance
    assert result.instrument_ids == covariance.instrument_ids and result.dimension == 2


# --- correlation -------------------------------------------------------------------------------------------------

def test_correlation_diagonal_is_exactly_one_and_symmetric() -> None:
    correlation = module_under_test._correlation(_cov(*SRC1).covariance, _ctx())
    assert all(correlation[i][i] == 1 and correlation[i][i].as_tuple() == D(1).as_tuple() for i in range(3))
    assert all(correlation[i][j].as_tuple() == correlation[j][i].as_tuple() for i in range(3) for j in range(3))


def test_zero_covariance_gives_zero_correlation() -> None:
    covariance = _cov(DIAG_A, DIAG_B)
    assert covariance.covariance[0][1] == 0
    assert module_under_test._correlation(covariance.covariance, _ctx())[0][1] == 0


def test_positive_and_negative_correlation_values() -> None:
    positive = module_under_test._correlation(_cov(PERM_A, ["1", "0", "3", "2"]).covariance, _ctx())
    negative = module_under_test._correlation(_cov(PERM_A, ["3", "2", "0", "1"]).covariance, _ctx())
    assert abs(positive[0][1] - D("0.6")) <= D("1E-45") and abs(negative[0][1] - D("-0.8")) <= D("1E-45")


def test_perfect_positive_and_negative_correlation_are_valid_boundaries() -> None:
    up = module_under_test._correlation(_cov(T3_A, T3_B).covariance, _ctx())
    down = module_under_test._correlation(_cov(T3_A, T3_BNEG).covariance, _ctx())
    assert up[0][1] == 1 and down[0][1] == -1


def test_correlation_outside_the_unit_interval_is_rejected_not_clipped() -> None:
    ctx = _ctx()
    assert module_under_test._correlation(_rows([["1", "2"], ["2", "1"]]), ctx) is None
    assert module_under_test._correlation(_rows([["1", "-1.0000000000000000000000000000000000000001"], ["-1.0000000000000000000000000000000000000001", "1"]]), ctx) is None
    assert module_under_test._correlation(_rows([["1", "1"], ["1", "1"]]), ctx) is not None


def test_invalid_correlation_geometry_maps_to_its_reason(monkeypatch) -> None:
    covariance = _cov(DIAG_A, DIAG_B)
    monkeypatch.setattr(module_under_test, "_correlation", lambda rows, ctx: None)
    result = build_hierarchical_risk_parity_benchmark(covariance=covariance)
    assert result.weights is None and result.unavailable_reason is REASON.INVALID_CORRELATION_GEOMETRY
    assert result.quasi_diagonal_order is None


# --- distance / distance-of-distance -----------------------------------------------------------------------------

def test_correlation_distance_formula_symmetry_and_range() -> None:
    ctx = _ctx()
    correlation = [[D(1), D("0.5"), D("-1")], [D("0.5"), D(1), D(0)], [D("-1"), D(0), D(1)]]
    distance = module_under_test._correlation_distance(correlation, ctx)
    assert all(distance[i][i] == 0 for i in range(3))
    assert distance[0][1] == ctx.sqrt(D("0.25")) == D("0.5")  # sqrt((1 - 0.5) / 2)
    assert distance[0][2] == 1                                  # rho = -1  ->  d = 1
    assert distance[1][2] == ctx.sqrt(D("0.5"))                 # rho = 0
    assert all(distance[i][j] == distance[j][i] and 0 <= distance[i][j] <= 1 for i in range(3) for j in range(3))
    assert module_under_test._correlation_distance([[D(1), D(1)], [D(1), D(1)]], ctx)[0][1] == 0  # rho = 1


def test_distance_of_distance_is_the_euclidean_distance_between_distance_rows() -> None:
    ctx = _ctx()
    distance = [[D(0), D("0.5"), D(1)], [D("0.5"), D(0), D("0.75")], [D(1), D("0.75"), D(0)]]
    dd = module_under_test._distance_of_distance(distance, ctx)
    expected_01 = ctx.sqrt(D("0.25") + D("0.25") + D("0.0625"))  # (0-0.5)^2 + (0.5-0)^2 + (1-0.75)^2
    assert dd[0][1] == expected_01 and dd[0][1] != distance[0][1]  # NOT the raw correlation distance
    assert all(dd[i][i] == 0 for i in range(3))
    assert all(dd[i][j] == dd[j][i] and dd[i][j] >= 0 for i in range(3) for j in range(3))
    assert dd[0][2] == ctx.sqrt(D("2.0625"))  # (0-1)^2 + (0.5-0.75)^2 + (1-0)^2


# --- single linkage ----------------------------------------------------------------------------------------------

def _line(*positions: str) -> list[list[Decimal]]:
    points = [D(p) for p in positions]
    return [[abs(a - b) for b in points] for a in points]


def test_single_linkage_ids_orientation_and_recurrence_on_a_hand_fixture() -> None:
    # positions 0, 11, 1, 10: nearest pairs (0,2) and (1,3) tie at distance 1
    merges = module_under_test._single_linkage(_line("0", "11", "1", "10"))
    assert merges == [
        (0, 2, D(1), 2),   # new id 4: tie resolved to the smaller pair, left = smaller id
        (1, 3, D(1), 2),   # new id 5
        (4, 5, D(9), 4),   # single linkage: min(|1 - 10|, |0 - 10|, |1 - 11|, |0 - 11|) = 9
    ]


def test_single_linkage_uses_the_minimum_member_distance_not_average_or_complete() -> None:
    merges = module_under_test._single_linkage(_line("0", "1", "5", "10"))
    assert merges[0] == (0, 1, D(1), 2)
    assert merges[1] == (2, 4, D(4), 3)   # single: min(|5-1|, |5-0|) = 4 (complete would be 5, average 4.5)
    assert merges[2] == (3, 5, D(5), 4)   # min(|10-5|, |10-1|, |10-0|) = 5


def test_explicit_tie_is_resolved_by_distance_then_smaller_then_larger_cluster_id() -> None:
    all_equal = [[D(0) if i == j else D(1) for j in range(4)] for i in range(4)]
    merges = module_under_test._single_linkage(all_equal)
    assert merges == [(0, 1, D(1), 2), (2, 3, D(1), 2), (4, 5, D(1), 4)]
    # a strictly smaller later pair beats a tied earlier pair; equal distance falls back to the smaller ids
    matrix = [[D(0), D(5), D(2), D(7)], [D(5), D(0), D(7), D(2)], [D(2), D(7), D(0), D(5)], [D(7), D(2), D(5), D(0)]]
    assert module_under_test._single_linkage(matrix)[0][:2] == (0, 2)  # (0,2) and (1,3) tie at 2 -> (0,2)
    # tie between a merged cluster and a leaf: key (1, 2, 4) beats (1, 3, 4)
    chain = [[D(0), D(1), D(9), D(9)], [D(1), D(0), D(1), D(1)], [D(9), D(1), D(0), D(9)], [D(9), D(1), D(9), D(0)]]
    merges = module_under_test._single_linkage(chain)
    assert merges[0][:2] == (0, 1) and merges[1][:2] == (2, 4) and merges[2][:2] == (3, 5)


def test_merge_orientation_is_smaller_id_left_regardless_of_cluster_size_or_distance() -> None:
    merges = module_under_test._single_linkage(_line("0", "1", "2", "10"))
    assert all(left < right for left, right, _, _ in merges)
    assert merges[-1][0] == 3 and merges[-1][1] == 5  # the singleton leaf 3 is LEFT of the 3-leaf cluster 5


def test_single_linkage_is_independent_of_dict_or_set_order() -> None:
    first = module_under_test._single_linkage(_line("3", "0", "1", "10", "11"))
    for _ in range(3):
        assert module_under_test._single_linkage(_line("3", "0", "1", "10", "11")) == first


# --- quasi-diagonalization ---------------------------------------------------------------------------------------

def test_quasi_diagonal_order_follows_left_then_right_expansion() -> None:
    merges = module_under_test._single_linkage(_line("0", "11", "1", "10"))
    assert module_under_test._quasi_diagonal_order(merges, 4) == (0, 2, 1, 3)
    # id4 = (1, 3); id5 = (0, 4); id6 = (2, 5)  ->  2, then (0, then (1, 3))
    hand = [(1, 3, D(1), 2), (0, 4, D(2), 3), (2, 5, D(3), 4)]
    assert module_under_test._quasi_diagonal_order(hand, 4) == (2, 0, 1, 3)


def test_quasi_diagonal_order_covers_every_leaf_once_without_a_post_sort() -> None:
    for positions in (("0", "11", "1", "10", "5"), ("9", "1", "5", "2", "8", "4")):
        merges = module_under_test._single_linkage(_line(*positions))
        order = module_under_test._quasi_diagonal_order(merges, len(positions))
        assert sorted(order) == list(range(len(positions))) and len(set(order)) == len(positions)
    assert module_under_test._quasi_diagonal_order(module_under_test._single_linkage(_line("0", "11", "1", "10")), 4) != (0, 1, 2, 3)


def test_one_asset_has_a_trivial_order() -> None:
    assert module_under_test._quasi_diagonal_order([], 1) == (0,)


# --- recursive bisection -----------------------------------------------------------------------------------------

def test_bisection_uses_floor_half_split_on_the_ordered_group() -> None:
    assert module_under_test._bisection_split((0, 1, 2, 3, 4)) == ((0, 1), (2, 3, 4))
    assert module_under_test._bisection_split((0, 1, 2, 3)) == ((0, 1), (2, 3))
    assert module_under_test._bisection_split((7, 3, 5)) == ((7,), (3, 5))
    assert module_under_test._bisection_split((4, 9)) == ((4,), (9,))


def test_cluster_weights_are_inverse_variance_not_inverse_volatility() -> None:
    rows = _rows([["1", "0.5"], ["0.5", "4"]])
    ctx = _ctx()
    q = module_under_test._inverse_variance_weights(rows, (0, 1), ctx)
    assert q == [D("0.8"), D("0.2")]                 # raw = 1/1, 1/4  ->  1/1.25, 0.25/1.25
    assert q != [ctx.divide(D(2), D(3)), ctx.divide(D(1), D(3))]  # raw = 1/sigma would give 2/3, 1/3


def test_cluster_variance_uses_the_full_covariance_inside_the_cluster() -> None:
    rows = _rows([["1", "0.5"], ["0.5", "4"]])
    ctx = _ctx()
    # q = (0.8, 0.2): 0.64*1 + 2*0.8*0.2*0.5 + 0.04*4 = 0.96
    assert module_under_test._cluster_variance(rows, (0, 1), ctx) == D("0.96")
    assert module_under_test._cluster_variance(_rows([["1", "0"], ["0", "4"]]), (0, 1), ctx) == D("0.80")  # off-diagonal matters
    assert module_under_test._cluster_variance(rows, (1,), ctx) == 4  # a singleton cluster is its own variance


def test_recursive_allocation_chain_on_a_hand_checkable_four_asset_case() -> None:
    rows = _rows([["1", "0.5", "0", "0"], ["0.5", "1", "0", "0"], ["0", "0", "4", "-1"], ["0", "0", "-1", "4"]])
    ctx = _ctx()
    weights = module_under_test._recursive_bisection(rows, (0, 1, 2, 3), ctx)
    # level 1: left (0,1) q=(.5,.5) V=.75 ; right (2,3) q=(.5,.5) V=1.5 ; alpha = 1.5 / 2.25 = 2/3
    # level 2: singletons -> alpha = V1 / (V0 + V1) = 1/2 for (0,1) and 1/2 for (2,3)
    third = Fraction(1, 3)
    expected = [Fraction(2, 3) / 2, Fraction(2, 3) / 2, third / 2, third / 2]
    for got, want in zip(weights, expected):
        assert abs(Fraction(got) - want) <= Fraction(1, 10**45)


def test_split_factors_follow_variance_ratio_and_weights_map_back_to_canonical_order() -> None:
    rows = _rows([["4", "0", "0"], ["0", "1", "0"], ["0", "0", "1"]])
    ctx = _ctx()
    # quasi-diagonal order (2, 0, 1): split (2) | (0, 1).  V(2) = 1 ; V(0,1) = (q=(.2,.8)) 0.04*4 + 0.64*1 = 0.8
    weights = module_under_test._recursive_bisection(rows, (2, 0, 1), ctx)
    alpha = Fraction(4, 5) / (Fraction(1) + Fraction(4, 5))  # V_right / (V_left + V_right) = 0.8 / 1.8
    assert abs(Fraction(weights[2]) - alpha) <= Fraction(1, 10**45)          # left (asset 2) gets alpha
    assert abs(Fraction(weights[0]) + Fraction(weights[1]) - (1 - alpha)) <= Fraction(1, 10**45)
    assert weights[0] > 0 and weights[1] > 0 and weights[0] != weights[1]  # asset-indexed, not order-indexed


def test_degenerate_child_cluster_variance_is_reported_not_faked() -> None:
    rows = _rows([["1", "0", "0"], ["0", "2", "-2"], ["0", "-2", "2"]])
    assert module_under_test._recursive_bisection(rows, (0, 1, 2), _ctx()) is None  # right child (1, 2) has variance 0


# --- allocation --------------------------------------------------------------------------------------------------

def test_one_asset_unit_weight_and_trivial_order() -> None:
    result = _hrp(["1", "-1", "1", "-1"])
    assert result.weights == (D("1"),) and result.unavailable_reason is None
    assert result.quasi_diagonal_order == (0,) and result.ordered_instrument_ids == (UUIDS[0],)


def test_diagonal_covariance_equals_inverse_variance_and_not_inverse_volatility() -> None:
    covariance = _cov(DIAG_A, DIAG_B, DIAG_C)
    assert all(covariance.covariance[i][j] == 0 for i in range(3) for j in range(3) if i != j)
    result = build_hierarchical_risk_parity_benchmark(covariance=covariance)
    variances = [Fraction(covariance.covariance[i][i]) for i in range(3)]
    total = sum(1 / v for v in variances)
    for got, variance in zip(result.weights, variances):
        assert abs(Fraction(got) - (1 / variance) / total) <= Fraction(1, 10**40)
    volatility = build_inverse_volatility_benchmark(covariance=covariance)
    assert max(abs(a - b) for a, b in zip(result.weights, volatility.weights)) > D("0.05")  # materially different
    assert _exact_sum(result.weights) == 1


def test_correlated_positive_and_negative_matrices_are_available_and_exact() -> None:
    for columns in (SRC1, SRC2, (PERM_A, ["0", "2", "1", "3"], ["2", "0", "3", "1"])):
        result = _hrp(*columns)
        assert result.is_available and len(result.weights) == 3
        assert all(type(w) is Decimal and w.is_finite() and 0 < w <= 1 for w in result.weights)
        assert _exact_sum(result.weights) == 1


def test_singular_covariance_with_valid_geometry_returns_hrp_weights() -> None:
    # T = 2 < N = 3: rank-1 covariance, all correlations exactly +1 (distance 0), no inverse is ever needed
    result = _hrp(["0", "1"], ["0", "2"], ["0", "3"])
    assert result.is_available
    assert _exact_sum(result.weights) == 1 and all(0 < w <= 1 for w in result.weights)
    # T = 4 = N = 4 also rank-deficient (centered vectors span at most 3 dimensions)
    singular = _hrp(PERM_A, ["1", "0", "3", "2"], ["1", "3", "0", "2"], ["2", "0", "3", "1"])
    assert singular.is_available and _exact_sum(singular.weights) == 1


def test_perfectly_correlated_assets_resolve_deterministically() -> None:
    first = _hrp(T3_A, T3_B, ["0", "3", "6"])
    again = _hrp(T3_A, T3_B, ["0", "3", "6"])
    assert first == again and first.quasi_diagonal_order == again.quasi_diagonal_order
    assert first.is_available


def test_correlation_structure_changes_the_hrp_result_with_the_same_diagonal() -> None:
    one, two = _cov(*SRC1), _cov(*SRC2)
    assert [one.covariance[i][i] for i in range(3)] == [two.covariance[i][i] for i in range(3)]
    first = build_hierarchical_risk_parity_benchmark(covariance=one)
    second = build_hierarchical_risk_parity_benchmark(covariance=two)
    assert first.is_available and second.is_available
    assert (first.quasi_diagonal_order != second.quasi_diagonal_order) or (first.weights != second.weights)
    assert first.weights != second.weights
    assert max(abs(a - b) for a, b in zip(first.weights, second.weights)) > D("1E-3")


def test_sample_means_do_not_matter() -> None:
    shifted = [[str(D(v) + D("0.25")) for v in column] for column in SRC1]
    base, moved = _cov(*SRC1), _cov(*shifted)
    assert base.sample_means != moved.sample_means and base.covariance == moved.covariance
    assert build_hierarchical_risk_parity_benchmark(covariance=base).weights == build_hierarchical_risk_parity_benchmark(covariance=moved).weights


def test_available_result_exposes_the_quasi_diagonal_audit_views() -> None:
    result = _hrp(*SRC1)
    order = result.quasi_diagonal_order
    assert sorted(order) == [0, 1, 2] and len(order) == 3
    assert result.ordered_instrument_ids == tuple(UUIDS[i] for i in order)
    assert "quasi_diagonal_order" not in [f.name for f in dataclasses.fields(HierarchicalRiskParityResult)]


def test_weights_follow_canonical_instrument_order_not_the_cluster_order() -> None:
    result = _hrp(*SRC1)
    assert result.quasi_diagonal_order == (2, 0, 1)  # the leaf order genuinely differs from the canonical order
    ctx = _ctx()
    rows = result.source.covariance
    preliminary = module_under_test._recursive_bisection(rows, result.quasi_diagonal_order, ctx)
    for got, want in zip(result.weights, preliminary):  # weights[i] <-> instrument i, whatever the leaf order was
        assert abs(got - want) <= D("1E-45")


# --- unavailable -------------------------------------------------------------------------------------------------

def test_zero_variance_is_unavailable_with_no_fallback() -> None:
    for columns in ((["0.2", "0.2", "0.2", "0.2"], DIAG_A), (DIAG_A, ["0.3", "0.3", "0.3", "0.3"])):
        result = _hrp(*columns)
        assert result.weights is None and result.unavailable_reason is REASON.ZERO_VARIANCE
        assert result.quasi_diagonal_order is None and result.ordered_instrument_ids is None
    single = _hrp(["0.2", "0.2", "0.2"])
    assert single.unavailable_reason is REASON.ZERO_VARIANCE and single.weights is None


def test_degenerate_cluster_variance_end_to_end() -> None:
    # three identical assets and one perfectly anti-correlated equal-variance asset
    columns = (["0", "1", "2"], ["0", "1", "2"], ["0", "1", "2"], ["2", "1", "0"])
    covariance = _cov(*columns)
    order, _ = module_under_test._structure(covariance, _ctx())
    assert order == (3, 2, 0, 1)  # the anti-correlated asset 3 sits in the same bisection child as asset 2: (3, 2) | (0, 1)
    result = build_hierarchical_risk_parity_benchmark(covariance=covariance)
    assert result.weights is None and result.unavailable_reason is REASON.DEGENERATE_CLUSTER_VARIANCE
    assert result.quasi_diagonal_order is None


def test_range_failure_is_a_static_error_distinct_from_the_availability_reasons(monkeypatch) -> None:
    covariance = _cov(*[[str(10 * D(v)) for v in column] for column in SRC1])
    assert build_hierarchical_risk_parity_benchmark(covariance=covariance).is_available

    def _tiny_range_context() -> decimal.Context:
        return decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=-5, Emax=0)

    monkeypatch.setattr(module_under_test, "_analytics_context", _tiny_range_context)
    with pytest.raises(ValueError, match="hierarchical risk parity exceeds supported Decimal analytics range"):
        build_hierarchical_risk_parity_benchmark(covariance=covariance)


# --- determinism / order independence ----------------------------------------------------------------------------

def test_repeated_builds_are_identical_down_to_as_tuple_and_linkage() -> None:
    covariance = _cov(*SRC2)
    first = build_hierarchical_risk_parity_benchmark(covariance=covariance)
    structure = module_under_test._structure(covariance, _ctx())
    for _ in range(3):
        again = build_hierarchical_risk_parity_benchmark(covariance=covariance)
        assert again == first
        assert [w.as_tuple() for w in again.weights] == [w.as_tuple() for w in first.weights]
        assert module_under_test._structure(covariance, _ctx()) == structure


def test_input_series_order_does_not_change_linkage_order_or_weights() -> None:
    forward = build_allocation_return_panel(series=tuple(_series(UUIDS[i], v) for i, v in enumerate(SRC1)))
    backward = build_allocation_return_panel(series=tuple(_series(UUIDS[i], v) for i, v in reversed(list(enumerate(SRC1)))))
    first_cov = build_allocation_covariance_matrix(return_panel=forward)
    second_cov = build_allocation_covariance_matrix(return_panel=backward)
    first = build_hierarchical_risk_parity_benchmark(covariance=first_cov)
    second = build_hierarchical_risk_parity_benchmark(covariance=second_cov)
    assert module_under_test._structure(first_cov, _ctx()) == module_under_test._structure(second_cov, _ctx())
    assert first.quasi_diagonal_order == second.quasi_diagonal_order and first.weights == second.weights


def _hostile_contexts():
    return [
        decimal.Context(prec=2, rounding=decimal.ROUND_DOWN, Emin=-3, Emax=3,
                        traps=[decimal.InvalidOperation, decimal.Overflow, decimal.Inexact, decimal.Rounded]),
        decimal.Context(prec=1, rounding=decimal.ROUND_CEILING, traps=[]),
    ]


def test_hostile_ambient_context_cannot_change_the_result_or_be_mutated() -> None:
    covariance = _cov(*SRC1)
    baseline = build_hierarchical_risk_parity_benchmark(covariance=covariance)
    for hostile in _hostile_contexts():
        with decimal.localcontext(hostile) as ambient:
            ambient.clear_flags()
            snapshot = (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps))
            result = build_hierarchical_risk_parity_benchmark(covariance=covariance)
            order = result.quasi_diagonal_order
            assert (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps)) == snapshot
            assert not any(ambient.flags.values())
        assert result == baseline and order == baseline.quasi_diagonal_order
        assert [w.as_tuple() for w in result.weights] == [w.as_tuple() for w in baseline.weights]


# --- result self validation / forge resistance -------------------------------------------------------------------

def _good() -> HierarchicalRiskParityResult:
    return _hrp(*SRC1)


def _make(source, weights, reason) -> HierarchicalRiskParityResult:
    return HierarchicalRiskParityResult(source=source, weights=weights, unavailable_reason=reason)


def test_valid_result_reconstructs_equal() -> None:
    good = _good()
    assert _make(good.source, good.weights, None) == good


def test_wrong_weight_and_order_equivalent_alternatives_are_rejected() -> None:
    good = _good()
    with pytest.raises(ValueError):
        _make(good.source, (good.weights[0] + D("1E-10"), good.weights[1] - D("1E-10"), good.weights[2]), None)
    with pytest.raises(ValueError):  # a valid long-only vector (reversed) that is not the HRP solution
        _make(good.source, good.weights[::-1], None)
    with pytest.raises(ValueError):  # equal weight is valid long-only but not HRP
        _make(good.source, (D("0.3333333333333333333333333333333333333333333333333"), D("0.3333333333333333333333333333333333333333333333333"),
                            D("0.3333333333333333333333333333333333333333333333334")), None)
    with pytest.raises(ValueError):
        _make(good.source, build_inverse_volatility_benchmark(covariance=good.source).weights, None)


def test_pairing_and_reason_forgery_are_rejected() -> None:
    good = _good()
    with pytest.raises(ValueError):
        _make(good.source, None, None)
    with pytest.raises(ValueError):
        _make(good.source, good.weights, REASON.ZERO_VARIANCE)
    with pytest.raises(ValueError):  # unavailable forged onto an available source
        _make(good.source, None, REASON.ZERO_VARIANCE)
    with pytest.raises(TypeError):
        _make(good.source, None, "zero_variance")  # type: ignore[arg-type]
    zero = _hrp(["0.3", "0.3", "0.3", "0.3"], DIAG_A)
    assert _make(zero.source, None, REASON.ZERO_VARIANCE) == zero
    with pytest.raises(ValueError):
        _make(zero.source, None, REASON.DEGENERATE_CLUSTER_VARIANCE)
    with pytest.raises(ValueError):
        _make(zero.source, None, REASON.INVALID_CORRELATION_GEOMETRY)


def test_weights_type_shape_and_range_are_enforced() -> None:
    good = _good()
    with pytest.raises(TypeError):
        _make(good.source, list(good.weights), None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _make(good.source, (0.3, 0.3, 0.4), None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _make(good.source, (1, 0, 0), None)  # type: ignore[arg-type]

    class _DecSub(Decimal):
        pass

    with pytest.raises(TypeError):
        _make(good.source, (_DecSub(good.weights[0]), good.weights[1], good.weights[2]), None)
    with pytest.raises(ValueError):
        _make(good.source, good.weights[:2], None)
    for bad in ("NaN", "Infinity", "-Infinity", "0", "-0.1", "1.1"):
        with pytest.raises(ValueError):
            _make(good.source, (D(bad), good.weights[1], good.weights[2]), None)
    with pytest.raises(ValueError):
        _make(good.source, (good.weights[0], good.weights[1], good.weights[2] - D("1E-40")), None)


def test_source_type_is_enforced_on_the_result() -> None:
    good = _good()
    for bad in (None, good.source.source, object()):
        with pytest.raises(TypeError):
            _make(bad, good.weights, None)


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)


def test_imports_are_standard_library_plus_the_two_allocation_authorities_only() -> None:
    plain: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            plain.add(node.module or "")
    assert plain <= {
        "__future__", "dataclasses", "decimal", "enum", "uuid",
        "backend.engine.private.allocation_matrix", "backend.engine.private.allocation_benchmarks",
    }
    assert "backend.engine.private.allocation_risk_parity" not in plain
    for forbidden in ("numpy", "pandas", "scipy", "sklearn", "statistics", "math", "random", "time", "datetime", "os", "deprecated"):
        assert forbidden not in plain


def test_phase_18b_exact_closure_is_reused_and_other_methods_are_not() -> None:
    assert "_close_to_one" in _SOURCE and "_sums_to_exactly_one" in _SOURCE
    assert not {n.name for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)} & {"_close_to_one", "_sums_to_exactly_one", "_aligned"}
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not names & {"build_inverse_volatility_benchmark", "build_equal_risk_contribution_benchmark", "build_equal_weight_benchmark"}


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


def test_weighting_never_reads_sample_means() -> None:
    attributes = {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert "sample_means" not in attributes and "covariance" in attributes


def test_no_matrix_inversion_optimizer_or_decision_symbols() -> None:
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
    exact = {"inv", "inverse", "invert", "pinv", "solve", "linalg", "lstsq", "cholesky", "det", "determinant"}
    fragments = ("pinv", "linalg", "solve", "matrix_inverse", "invert", "cvar", "shortfall", "expected_return", "sharpe",
                 "sortino", "score", "rank", "recommend", "rebalance", "leverage", "objective", "gradient", "optimi",
                 "black_litterman", "conviction", "kmeans", "spectral", "centroid", "average_link", "complete_link")
    for identifier in identifiers:
        lowered = identifier.lower()
        assert lowered not in exact, identifier
        for fragment in fragments:
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    assert any("inverse_variance" in i for i in identifiers)  # the legitimate inverse-VARIANCE allocation
    builder = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "build_hierarchical_risk_parity_benchmark")
    assert [a.arg for a in builder.args.kwonlyargs] == ["covariance"] and not builder.args.args


def test_documents_the_methodology() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("distance-of-distance", "Single linkage", "len // 2", "inverse VARIANCE", "NOT the Phase 18B inverse VOLATILITY",
                   "Singular", "smaller cluster id"):
        assert needle in doc, needle
