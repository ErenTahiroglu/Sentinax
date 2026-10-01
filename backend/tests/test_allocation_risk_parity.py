"""
backend/tests/test_allocation_risk_parity.py
============================================
Phase 18C: true Equal Risk Contribution (risk parity) over the FULL Phase 18A sample covariance matrix, solved by
deterministic cyclical coordinate descent in 50-digit Decimal analytics.

Long-only, unlevered, covariance only. Not Inverse Volatility, not HRP, not an optimizer, not a recommendation.
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

from backend.engine.private import allocation_risk_parity as module_under_test
from backend.engine.private.allocation_benchmarks import build_inverse_volatility_benchmark
from backend.engine.private.allocation_matrix import (
    AllocationCovarianceMatrix,
    AllocationInstrumentReturnSeries,
    AllocationMonth,
    AllocationMonthlyReturnPoint,
    build_allocation_covariance_matrix,
    build_allocation_return_panel,
)
from backend.engine.private.allocation_risk_parity import (
    EqualRiskContributionResult,
    EqualRiskContributionUnavailableReason,
    build_equal_risk_contribution_benchmark,
)

D = Decimal
UUIDS = [UUID(int=i) for i in range(1, 9)]
TOLERANCE = D("1E-24")
REASON = EqualRiskContributionUnavailableReason


def _series(instrument_id: UUID, values: list[str]) -> AllocationInstrumentReturnSeries:
    points = tuple(
        AllocationMonthlyReturnPoint(period=AllocationMonth(2026, i + 1), simple_return=D(v)) for i, v in enumerate(values)
    )
    return AllocationInstrumentReturnSeries(instrument_id=instrument_id, points=points)


def _cov(*columns: list[str]) -> AllocationCovarianceMatrix:
    panel = build_allocation_return_panel(series=tuple(_series(UUIDS[i], v) for i, v in enumerate(columns)))
    return build_allocation_covariance_matrix(return_panel=panel)


def _erc(*columns: list[str]) -> EqualRiskContributionResult:
    return build_equal_risk_contribution_benchmark(covariance=_cov(*columns))


def _exact_sum(values) -> Fraction:
    """Context-independent exact sum (Fraction(Decimal) is exact; no Decimal context participates)."""
    return sum((Fraction(value) for value in values), Fraction(0))


def _assert_equal_risk(result: EqualRiskContributionResult) -> None:
    assert result.is_available
    target = D(1) / D(result.dimension)
    for rrc in result.relative_risk_contributions:
        assert abs(rrc - target) <= TOLERANCE
    assert result.max_relative_risk_contribution_error <= TOLERANCE


# Hand-built sources -------------------------------------------------------------------------------------------------
# Mutually orthogonal centered patterns: exactly zero off-diagonal covariance (diagonal covariance matrix).
DIAG_A = ["0.5", "-0.5", "0.5", "-0.5"]     # variance 1/3
DIAG_B = ["1", "1", "-1", "-1"]             # variance 4/3   (sigma ratio 2 vs DIAG_A)
DIAG_C = ["0.25", "-0.25", "-0.25", "0.25"]  # variance 1/12  (sigma ratio 1/2 vs DIAG_A)
# Same multiset => identical diagonal; different arrangement => different off-diagonal covariance.
PERM_A = ["0", "1", "2", "3"]
SRC1 = (PERM_A, ["1", "0", "3", "2"], ["1", "3", "0", "2"])
SRC2 = (PERM_A, ["0", "2", "1", "3"], ["2", "0", "3", "1"])


# --- taxonomy / contracts ----------------------------------------------------------------------------------------

def test_unavailable_reason_enum_is_exactly_the_three_reasons() -> None:
    assert issubclass(EqualRiskContributionUnavailableReason, Enum)
    assert {m.name: m.value for m in EqualRiskContributionUnavailableReason} == {
        "ZERO_VARIANCE": "zero_variance",
        "DEGENERATE_RISK_GEOMETRY": "degenerate_risk_geometry",
        "NON_CONVERGENCE": "non_convergence",
    }


def test_numerical_constants_are_explicit() -> None:
    assert module_under_test._ERC_RELATIVE_TOLERANCE == D("1E-24")
    assert module_under_test._ERC_MAX_CYCLES == 10000
    assert type(module_under_test._ERC_RELATIVE_TOLERANCE) is Decimal


def test_result_has_exactly_four_stored_fields_and_is_frozen() -> None:
    assert [f.name for f in dataclasses.fields(EqualRiskContributionResult)] == [
        "source", "weights", "cycles", "unavailable_reason",
    ]
    result = _erc(["1", "-1", "1", "-1"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.cycles = 1  # type: ignore[misc]


def test_builder_is_keyword_only_exact_type_and_retains_the_source_by_identity() -> None:
    covariance = _cov(DIAG_A, DIAG_B)
    with pytest.raises(TypeError):
        build_equal_risk_contribution_benchmark(covariance)  # type: ignore[misc]
    with pytest.raises(TypeError):
        build_equal_risk_contribution_benchmark()  # type: ignore[call-arg]

    class _Duck:
        sample_means = ()
        covariance = ()

    for bad in (None, _Duck(), covariance.source, ((D(1),),)):
        with pytest.raises(TypeError):
            build_equal_risk_contribution_benchmark(covariance=bad)  # type: ignore[arg-type]

    class _Sub(AllocationCovarianceMatrix):
        pass

    sub = object.__new__(_Sub)
    for name in ("source", "sample_means", "covariance"):
        object.__setattr__(sub, name, getattr(covariance, name))
    with pytest.raises(TypeError):
        build_equal_risk_contribution_benchmark(covariance=sub)
    result = build_equal_risk_contribution_benchmark(covariance=covariance)
    assert result.source is covariance
    assert result.instrument_ids == covariance.instrument_ids and result.dimension == 2


# --- positive cases ----------------------------------------------------------------------------------------------

def test_one_asset_is_unit_weight_zero_cycles_and_unit_contribution() -> None:
    result = _erc(["1", "-1", "1", "-1"])
    assert result.weights == (D("1"),) and result.cycles == 0 and result.unavailable_reason is None
    assert result.relative_risk_contributions == (D("1"),)
    assert result.portfolio_variance == D(4) / D(3) or result.portfolio_variance > 0
    assert result.max_relative_risk_contribution_error == 0


def test_diagonal_two_assets_equals_inverse_volatility_with_zero_cycles() -> None:
    covariance = _cov(DIAG_A, DIAG_B)
    assert covariance.covariance[0][1] == 0
    erc = build_equal_risk_contribution_benchmark(covariance=covariance)
    inverse = build_inverse_volatility_benchmark(covariance=covariance)
    assert erc.cycles == 0  # inverse-volatility already satisfies ERC for a diagonal matrix
    for got, want in zip(erc.weights, inverse.weights):
        assert abs(got - want) <= D("1E-45")
    assert _exact_sum(erc.weights) == 1
    _assert_equal_risk(erc)


def test_diagonal_multi_asset_equals_inverse_volatility() -> None:
    covariance = _cov(DIAG_A, DIAG_B, DIAG_C)
    assert all(covariance.covariance[i][j] == 0 for i in range(3) for j in range(3) if i != j)
    erc = build_equal_risk_contribution_benchmark(covariance=covariance)
    inverse = build_inverse_volatility_benchmark(covariance=covariance)
    assert erc.cycles == 0
    for got, want in zip(erc.weights, inverse.weights):
        assert abs(got - want) <= D("1E-45")
    # sigma = (1, 2, 1/2) up to a common factor -> weights proportional to (2, 1, 4) / 7
    assert erc.weights[2] > erc.weights[0] > erc.weights[1]
    _assert_equal_risk(erc)


def test_two_assets_reduce_to_inverse_volatility_even_with_nonzero_covariance() -> None:
    scaled = [str(2 * int(v)) for v in ["3", "2", "0", "1"]]  # sigma ratio 2, correlation -0.8
    for second in (scaled, [str(2 * int(v)) for v in ["1", "0", "3", "2"]]):  # negative then positive correlation
        covariance = _cov(PERM_A, second)
        assert covariance.covariance[0][1] != 0
        erc = build_equal_risk_contribution_benchmark(covariance=covariance)
        inverse = build_inverse_volatility_benchmark(covariance=covariance)
        for got, want in zip(erc.weights, inverse.weights):
            assert abs(got - want) <= D("1E-20")
        _assert_equal_risk(erc)


def test_three_asset_full_covariance_meets_equal_risk_contribution() -> None:
    result = _erc(*SRC1)
    assert result.is_available and result.cycles > 0
    _assert_equal_risk(result)
    assert all(w.is_finite() and 0 < w <= 1 for w in result.weights)
    assert _exact_sum(result.weights) == 1


# T = 8 observations > N = 5 assets keeps the sample covariance full rank (T <= N would make it singular).
WIDE = (
    ["0.01", "0.03", "-0.02", "0.04", "0.00", "-0.01", "0.02", "0.05"],
    ["0.02", "-0.01", "0.03", "0.01", "0.04", "0.00", "-0.02", "0.03"],
    ["-0.03", "0.02", "0.01", "-0.01", "0.02", "0.05", "0.00", "0.01"],
    ["0.00", "0.04", "-0.02", "0.02", "-0.01", "0.03", "0.01", "-0.03"],
    ["0.03", "0.00", "0.02", "-0.02", "0.01", "-0.01", "0.04", "0.02"],
)


@pytest.mark.parametrize("count", [4, 5])
def test_four_and_five_asset_full_covariance_converge(count: int) -> None:
    result = _erc(*WIDE[:count])
    assert result.is_available and result.cycles > 0
    _assert_equal_risk(result)
    assert _exact_sum(result.weights) == 1


def test_risk_contributions_obey_the_euler_identity() -> None:
    result = _erc(*SRC1)
    ctx = decimal.Context(prec=60, rounding=decimal.ROUND_HALF_EVEN)
    total = D(0)
    for rrc in result.relative_risk_contributions:
        total = ctx.add(total, rrc)
    assert abs(total - 1) <= D("1E-40")  # diagnostics are NOT pushed through weight residual closure


def test_full_covariance_matters_same_diagonal_different_off_diagonal() -> None:
    one, two = _cov(*SRC1), _cov(*SRC2)
    assert [one.covariance[i][i] for i in range(3)] == [two.covariance[i][i] for i in range(3)]
    assert one.covariance != two.covariance and one.covariance[0][1] != two.covariance[0][1]
    inverse_one = build_inverse_volatility_benchmark(covariance=one)
    inverse_two = build_inverse_volatility_benchmark(covariance=two)
    assert inverse_one.weights == inverse_two.weights  # Inverse Volatility is blind to the off-diagonal
    erc_one = build_equal_risk_contribution_benchmark(covariance=one)
    erc_two = build_equal_risk_contribution_benchmark(covariance=two)
    assert erc_one.is_available and erc_two.is_available
    assert erc_one.weights != erc_two.weights
    assert max(abs(a - b) for a, b in zip(erc_one.weights, erc_two.weights)) > D("1E-3")
    _assert_equal_risk(erc_one)
    _assert_equal_risk(erc_two)


def test_sample_means_do_not_matter() -> None:
    shifted = [[str(D(v) + D("0.25")) for v in column] for column in SRC1]  # shifts means, leaves covariance
    base, moved = _cov(*SRC1), _cov(*shifted)
    assert base.sample_means != moved.sample_means and base.covariance == moved.covariance
    assert build_equal_risk_contribution_benchmark(covariance=base).weights == build_equal_risk_contribution_benchmark(covariance=moved).weights


def test_input_order_independence_through_the_canonical_panel() -> None:
    forward = build_allocation_return_panel(series=tuple(_series(UUIDS[i], v) for i, v in enumerate(SRC1)))
    backward = build_allocation_return_panel(series=tuple(_series(UUIDS[i], v) for i, v in reversed(list(enumerate(SRC1)))))
    first = build_equal_risk_contribution_benchmark(covariance=build_allocation_covariance_matrix(return_panel=forward))
    second = build_equal_risk_contribution_benchmark(covariance=build_allocation_covariance_matrix(return_panel=backward))
    assert first == second and first.instrument_ids == tuple(UUIDS[:3])


def test_weights_are_exact_positive_long_only_and_unlevered() -> None:
    for columns in (SRC1, SRC2, (DIAG_A, DIAG_B, DIAG_C)):
        result = _erc(*columns)
        assert result.weights is not None and len(result.weights) == result.dimension
        assert all(type(w) is Decimal and w.is_finite() and 0 < w <= 1 for w in result.weights)
        assert _exact_sum(result.weights) == 1


def test_negative_correlation_nondegenerate_three_assets() -> None:
    result = _erc(PERM_A, ["0", "2", "1", "3"], ["2", "0", "3", "1"])
    assert result.is_available
    _assert_equal_risk(result)


# --- unavailable cases -------------------------------------------------------------------------------------------

def test_zero_variance_is_unavailable_without_arithmetic_errors() -> None:
    for columns in ((["0.2", "0.2", "0.2"],), (DIAG_A, ["0.1", "0.1", "0.1", "0.1"])):
        if len({len(c) for c in columns}) != 1:
            continue
        result = _erc(*columns)
        assert result.weights is None and result.unavailable_reason is REASON.ZERO_VARIANCE and result.cycles == 0
        assert result.is_available is False
    constant = _erc(["0.3", "0.3", "0.3", "0.3"], DIAG_A)
    assert constant.unavailable_reason is REASON.ZERO_VARIANCE and constant.weights is None


def test_unavailable_results_expose_none_diagnostics() -> None:
    result = _erc(["0.3", "0.3", "0.3", "0.3"], DIAG_A)
    for name in ("portfolio_variance", "portfolio_volatility", "relative_risk_contributions",
                 "max_relative_risk_contribution_error"):
        assert getattr(result, name) is None


def test_perfectly_anti_correlated_equal_volatility_is_degenerate_geometry() -> None:
    covariance = _cov(["-1", "1"], ["1", "-1"])
    assert covariance.covariance[0][1] == -covariance.covariance[0][0]
    result = build_equal_risk_contribution_benchmark(covariance=covariance)
    assert result.weights is None and result.unavailable_reason is REASON.DEGENERATE_RISK_GEOMETRY
    assert result.relative_risk_contributions is None  # no fabricated risk contributions


def test_forced_non_convergence_returns_no_partial_weights(monkeypatch) -> None:
    covariance = _cov(*SRC1)
    assert build_equal_risk_contribution_benchmark(covariance=covariance).cycles > 1
    monkeypatch.setattr(module_under_test, "_ERC_MAX_CYCLES", 1)
    result = build_equal_risk_contribution_benchmark(covariance=covariance)
    assert result.weights is None and result.unavailable_reason is REASON.NON_CONVERGENCE
    assert result.cycles == 1 and result.relative_risk_contributions is None


def test_decimal_range_failure_is_a_static_error_distinct_from_non_convergence(monkeypatch) -> None:
    covariance = _cov(*[[str(10 * D(v)) for v in column] for column in SRC1])
    assert build_equal_risk_contribution_benchmark(covariance=covariance).is_available

    def _tiny_range_context() -> decimal.Context:  # genuine decimal.Overflow inside the solver's own arithmetic
        return decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=-5, Emax=0)

    monkeypatch.setattr(module_under_test, "_analytics_context", _tiny_range_context)
    with pytest.raises(ValueError, match="equal risk contribution exceeds supported Decimal analytics range"):
        build_equal_risk_contribution_benchmark(covariance=covariance)


# --- stable quadratic coordinate update --------------------------------------------------------------------------

@pytest.mark.parametrize("c", ["0", "0.37", "5", "1E+12", "-0.37", "-5", "-1E+12", "-1E+30", "1E-30"])
def test_coordinate_update_solves_the_positive_root_in_both_branches(c: str) -> None:
    a, b = D("2.5"), D("0.25")
    ctx = decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=decimal.MIN_EMIN, Emax=decimal.MAX_EMAX)
    x = module_under_test._coordinate_update(a, D(c), b, ctx)
    assert x > 0
    wide = decimal.Context(prec=200)
    residual = wide.subtract(wide.add(wide.multiply(a, wide.multiply(x, x)), wide.multiply(D(c), x)), b)
    scale = max(abs(a * x * x), abs(D(c) * x), b)
    assert abs(residual) <= scale * D("1E-45")


def test_coordinate_update_branches_are_both_exercised() -> None:
    source = Path(module_under_test.__file__).read_text(encoding="utf-8")
    assert ">= 0" in source  # c >= 0 branch: 2b / (c + d)
    ctx = decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN)
    positive = module_under_test._coordinate_update(D(1), D(3), D(1), ctx)
    negative = module_under_test._coordinate_update(D(1), D(-3), D(1), ctx)
    assert positive < negative  # larger cross-risk shrinks the coordinate; negative cross-risk grows it


# --- result self validation / forge resistance -------------------------------------------------------------------

def _good() -> EqualRiskContributionResult:
    return _erc(*SRC1)


def _make(source, weights, cycles, reason) -> EqualRiskContributionResult:
    return EqualRiskContributionResult(source=source, weights=weights, cycles=cycles, unavailable_reason=reason)


def test_valid_result_reconstructs_equal() -> None:
    good = _good()
    assert _make(good.source, good.weights, good.cycles, None) == good


def test_wrong_weights_are_rejected() -> None:
    good = _good()
    forged = (good.weights[0] + D("1E-10"), good.weights[1] - D("1E-10"), good.weights[2])
    with pytest.raises(ValueError):
        _make(good.source, forged, good.cycles, None)
    with pytest.raises(ValueError):  # the inverse-volatility weights are a valid long-only vector but not the ERC solution
        _make(good.source, build_inverse_volatility_benchmark(covariance=good.source).weights, good.cycles, None)


def test_wrong_cycles_are_rejected() -> None:
    good = _good()
    for cycles in (good.cycles + 1, good.cycles - 1, 0):
        with pytest.raises(ValueError):
            _make(good.source, good.weights, cycles, None)


@pytest.mark.parametrize("bad", [True, False, 1.0, "3", None, -1])
def test_cycles_must_be_a_non_negative_exact_int(bad: object) -> None:
    good = _good()
    with pytest.raises((TypeError, ValueError)):
        _make(good.source, good.weights, bad, None)


def test_cycles_cannot_exceed_the_resource_cap() -> None:
    good = _good()
    with pytest.raises(ValueError):
        _make(good.source, good.weights, module_under_test._ERC_MAX_CYCLES + 1, None)


def test_pairing_and_reason_forgery_are_rejected() -> None:
    good = _good()
    with pytest.raises(ValueError):
        _make(good.source, None, good.cycles, None)
    with pytest.raises(ValueError):
        _make(good.source, good.weights, good.cycles, REASON.NON_CONVERGENCE)
    with pytest.raises(ValueError):  # unavailable forged onto an available source
        _make(good.source, None, 0, REASON.ZERO_VARIANCE)
    with pytest.raises(TypeError):
        _make(good.source, None, 0, "zero_variance")  # type: ignore[arg-type]
    zero = _erc(["0.3", "0.3", "0.3", "0.3"], DIAG_A)
    assert _make(zero.source, None, 0, REASON.ZERO_VARIANCE) == zero
    with pytest.raises(ValueError):
        _make(zero.source, None, 0, REASON.DEGENERATE_RISK_GEOMETRY)
    with pytest.raises(ValueError):
        _make(zero.source, good.weights, 0, REASON.ZERO_VARIANCE)


def test_weights_type_shape_and_range_are_enforced() -> None:
    good = _good()
    with pytest.raises(TypeError):
        _make(good.source, list(good.weights), good.cycles, None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _make(good.source, (0.3, 0.3, 0.4), good.cycles, None)  # type: ignore[arg-type]

    class _DecSub(Decimal):
        pass

    with pytest.raises(TypeError):
        _make(good.source, (_DecSub(good.weights[0]), good.weights[1], good.weights[2]), good.cycles, None)
    with pytest.raises(ValueError):
        _make(good.source, good.weights[:2], good.cycles, None)
    for bad in ("NaN", "Infinity", "0", "-0.1", "1.1"):
        with pytest.raises(ValueError):
            _make(good.source, (D(bad), good.weights[1], good.weights[2]), good.cycles, None)
    with pytest.raises(ValueError):
        _make(good.source, (good.weights[0], good.weights[1], good.weights[2] - D("1E-40")), good.cycles, None)


def test_source_type_is_enforced_on_the_result() -> None:
    good = _good()
    for bad in (None, good.source.source, object()):
        with pytest.raises(TypeError):
            _make(bad, good.weights, good.cycles, None)


# --- Decimal isolation / determinism -----------------------------------------------------------------------------

def _hostile_contexts():
    return [
        decimal.Context(prec=2, rounding=decimal.ROUND_DOWN, Emin=-3, Emax=3,
                        traps=[decimal.InvalidOperation, decimal.Overflow, decimal.Inexact, decimal.Rounded]),
        decimal.Context(prec=1, rounding=decimal.ROUND_CEILING, traps=[]),
    ]


def test_hostile_ambient_context_cannot_change_the_result_or_be_mutated() -> None:
    covariance = _cov(*SRC1)
    baseline = build_equal_risk_contribution_benchmark(covariance=covariance)
    for hostile in _hostile_contexts():
        with decimal.localcontext(hostile) as ambient:
            ambient.clear_flags()
            snapshot = (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps))
            result = build_equal_risk_contribution_benchmark(covariance=covariance)
            diagnostics = (result.portfolio_variance, result.relative_risk_contributions)
            assert (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps)) == snapshot
            assert not any(ambient.flags.values())
        assert result == baseline and result.cycles == baseline.cycles
        assert [w.as_tuple() for w in result.weights] == [w.as_tuple() for w in baseline.weights]
        assert diagnostics == (baseline.portfolio_variance, baseline.relative_risk_contributions)


def test_repeated_builds_are_identical() -> None:
    covariance = _cov(*SRC2)
    first = build_equal_risk_contribution_benchmark(covariance=covariance)
    for _ in range(3):
        again = build_equal_risk_contribution_benchmark(covariance=covariance)
        assert again == first and again.cycles == first.cycles
        assert [w.as_tuple() for w in again.weights] == [w.as_tuple() for w in first.weights]
        assert again.relative_risk_contributions == first.relative_risk_contributions


def test_diagnostics_use_the_stored_weights_and_full_covariance() -> None:
    result = _good()
    rows, weights = result.source.covariance, result.weights
    exact_variance = sum(
        (Fraction(weights[i]) * Fraction(rows[i][j]) * Fraction(weights[j]) for i in range(3) for j in range(3)), Fraction(0)
    )
    assert exact_variance > 0
    assert abs(Fraction(result.portfolio_variance) - exact_variance) <= Fraction(1, 10**45)  # context-free reference
    volatility = result.portfolio_volatility
    assert abs(Fraction(volatility) ** 2 - Fraction(result.portfolio_variance)) <= Fraction(1, 10**45)
    exact_contributions = [
        Fraction(weights[i]) * sum((Fraction(rows[i][j]) * Fraction(weights[j]) for j in range(3)), Fraction(0)) / exact_variance
        for i in range(3)
    ]
    for got, want in zip(result.relative_risk_contributions, exact_contributions):
        assert abs(Fraction(got) - want) <= Fraction(1, 10**45)


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
    for forbidden in ("numpy", "pandas", "scipy", "sklearn", "statistics", "math", "random", "time", "datetime", "os"):
        assert forbidden not in plain


def test_exact_closure_is_reused_from_phase_18b_not_duplicated() -> None:
    assert "_close_to_one" in _SOURCE and "_sums_to_exactly_one" in _SOURCE
    defined = {n.name for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)}
    assert not defined & {"_close_to_one", "_sums_to_exactly_one", "_aligned", "_split", "_exact_decimal"}
    assert "as_tuple" not in _SOURCE


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
    assert "sample_means" not in attributes
    assert "covariance" in attributes  # the full covariance matrix is consumed


_FORBIDDEN_FRAGMENTS = (
    "hrp", "cluster", "linkage", "bisection", "quasi", "distance", "cvar", "shortfall", "expected_return",
    "sharpe", "sortino", "score", "rank", "recommend", "rebalance", "leverage", "tilt", "utility", "risk_budget",
    "turnover", "constraint", "scipy", "numpy",
)


def test_no_hrp_cvar_leverage_custom_budget_or_decision_symbols() -> None:
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
        for fragment in _FORBIDDEN_FRAGMENTS:
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    builder = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "build_equal_risk_contribution_benchmark")
    assert [a.arg for a in builder.args.kwonlyargs] == ["covariance"] and not builder.args.args  # equal budgets only


def test_documents_tolerance_cap_and_the_inverse_volatility_distinction() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("1E-24", "10000", "NOT Inverse Volatility", "numerical", "full covariance"):
        assert needle in doc, needle
