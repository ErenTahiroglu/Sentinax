"""
backend/tests/test_allocation_benchmarks.py
===========================================
Phase 18B: Equal Weight + Inverse Volatility benchmarks over the Phase 18A sample covariance authority.

Closed-form, long-only, descriptive benchmarks. Not ERC / risk parity, not an optimizer, not a recommendation.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
import re
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import allocation_benchmarks as module_under_test
from backend.engine.private.allocation_benchmarks import (
    AllocationBenchmarkMethod,
    AllocationBenchmarkResult,
    AllocationBenchmarkUnavailableReason,
    build_equal_weight_benchmark,
    build_inverse_volatility_benchmark,
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


def _series(instrument_id: UUID, values: list[str]) -> AllocationInstrumentReturnSeries:
    points = tuple(
        AllocationMonthlyReturnPoint(period=AllocationMonth(2026, i + 1), simple_return=D(v)) for i, v in enumerate(values)
    )
    return AllocationInstrumentReturnSeries(instrument_id=instrument_id, points=points)


def _cov(*columns: list[str]) -> AllocationCovarianceMatrix:
    panel = build_allocation_return_panel(series=tuple(_series(UUIDS[i], v) for i, v in enumerate(columns)))
    return build_allocation_covariance_matrix(return_panel=panel)


def _exact_sum(values) -> Fraction:
    """Context-independent exact sum: Fraction(Decimal) is exact and no Decimal context participates."""
    return sum((Fraction(value) for value in values), Fraction(0))


def _coefficient_sum_is_one(weights) -> bool:
    """Exact coefficient arithmetic from Decimal.as_tuple() at the common exponent (no Decimal arithmetic at all)."""
    parts = []
    for weight in weights:
        sign, digits, exponent = weight.as_tuple()
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        parts.append((-coefficient if sign else coefficient, exponent))
    common = min([e for _, e in parts] + [0])
    return sum(c * 10 ** (e - common) for c, e in parts) == 10 ** (0 - common)


def _third() -> Decimal:
    return decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN).divide(D(1), D(3))


# sigma_A = 1, sigma_B = 2 (sample variances 1 and 4); three different covariances with the same diagonal
A = ["0", "1", "2"]
B_POS = ["0", "2", "4"]
B_NEG = ["4", "2", "0"]
B_MID = ["2", "0", "4"]


# --- taxonomy ----------------------------------------------------------------------------------------------------

def test_method_enum_is_exactly_equal_weight_and_inverse_volatility() -> None:
    assert issubclass(AllocationBenchmarkMethod, Enum)
    assert {m.name: m.value for m in AllocationBenchmarkMethod} == {
        "EQUAL_WEIGHT": "equal_weight",
        "INVERSE_VOLATILITY": "inverse_volatility",
    }


def test_unavailable_reason_enum_has_only_zero_volatility() -> None:
    assert issubclass(AllocationBenchmarkUnavailableReason, Enum)
    assert {m.name: m.value for m in AllocationBenchmarkUnavailableReason} == {"ZERO_VOLATILITY": "zero_volatility"}


def test_result_has_exactly_four_stored_fields_and_is_frozen() -> None:
    assert [f.name for f in dataclasses.fields(AllocationBenchmarkResult)] == [
        "source", "method", "weights", "unavailable_reason",
    ]
    result = build_equal_weight_benchmark(covariance=_cov(A, B_POS))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.weights = None  # type: ignore[misc]


# --- builder signatures and source contract ----------------------------------------------------------------------

@pytest.mark.parametrize("builder", [build_equal_weight_benchmark, build_inverse_volatility_benchmark])
def test_builders_are_keyword_only_with_no_default(builder) -> None:
    covariance = _cov(A, B_POS)
    with pytest.raises(TypeError):
        builder(covariance)  # type: ignore[misc]
    with pytest.raises(TypeError):
        builder()  # type: ignore[call-arg]
    assert builder(covariance=covariance).source is covariance


@pytest.mark.parametrize("builder", [build_equal_weight_benchmark, build_inverse_volatility_benchmark])
def test_builders_require_exact_covariance_type(builder) -> None:
    class _Duck:
        sample_means = ()
        covariance = ()

    for bad in (None, _Duck(), _cov(A, B_POS).source, ((D(1),),)):
        with pytest.raises(TypeError):
            builder(covariance=bad)


def test_source_subclass_is_rejected() -> None:
    good = _cov(A, B_POS)

    class _Sub(AllocationCovarianceMatrix):
        pass

    sub = object.__new__(_Sub)
    for name in ("source", "sample_means", "covariance"):
        object.__setattr__(sub, name, getattr(good, name))
    with pytest.raises(TypeError):
        build_equal_weight_benchmark(covariance=sub)
    with pytest.raises(TypeError):
        build_inverse_volatility_benchmark(covariance=sub)


def test_result_retains_the_source_by_identity_and_exposes_derived_state() -> None:
    covariance = _cov(A, B_POS, B_NEG)
    result = build_equal_weight_benchmark(covariance=covariance)
    assert result.source is covariance
    assert result.instrument_ids == covariance.instrument_ids == tuple(UUIDS[:3])
    assert result.dimension == 3 and result.is_available is True
    assert [f.name for f in dataclasses.fields(AllocationBenchmarkResult)].count("instrument_ids") == 0


# --- equal weight ------------------------------------------------------------------------------------------------

def test_equal_weight_one_asset() -> None:
    result = build_equal_weight_benchmark(covariance=_cov(A))
    assert result.method is AllocationBenchmarkMethod.EQUAL_WEIGHT
    assert result.weights == (D("1"),) and result.unavailable_reason is None


def test_equal_weight_two_assets_is_exactly_half_without_residual() -> None:
    result = build_equal_weight_benchmark(covariance=_cov(A, B_POS))
    assert result.weights == (D("0.5"), D("0.5"))
    assert _exact_sum(result.weights) == 1


def test_equal_weight_three_assets_uses_canonical_residual_closure_on_the_first_index() -> None:
    result = build_equal_weight_benchmark(covariance=_cov(A, B_POS, B_NEG))
    third = _third()
    assert result.weights[1] == third and result.weights[2] == third
    assert result.weights[0] == decimal.Context(prec=100).add(third, D("1E-50"))  # residual of 1 - 3 * (1/3 at 50 digits) lands on the lowest index
    assert result.weights[0] != result.weights[1]  # not claimed bit-for-bit equal
    assert _exact_sum(result.weights) == 1


@pytest.mark.parametrize("count", [4, 5, 6, 7, 8])
def test_equal_weight_many_assets_sum_exactly_to_one(count: int) -> None:
    result = build_equal_weight_benchmark(covariance=_cov(*[[str(j * (i + 1)) for j in range(3)] for i in range(count)]))
    assert len(result.weights) == count and _exact_sum(result.weights) == 1
    assert all(w.is_finite() and 0 < w <= 1 for w in result.weights)


def test_equal_weight_remains_available_with_a_zero_variance_asset() -> None:
    covariance = _cov(["0.1", "0.1", "0.1"], B_POS)
    assert covariance.covariance[0][0] == 0
    result = build_equal_weight_benchmark(covariance=covariance)
    assert result.is_available and result.weights == (D("0.5"), D("0.5"))


def test_equal_weight_ignores_covariance_values_and_sample_means() -> None:
    first = build_equal_weight_benchmark(covariance=_cov(A, B_POS, B_NEG))
    second = build_equal_weight_benchmark(covariance=_cov(["5", "9", "1"], ["0", "0", "3"], ["-1", "7", "7"]))
    assert first.weights == second.weights


# --- inverse volatility ------------------------------------------------------------------------------------------

def test_inverse_volatility_one_positive_vol_asset() -> None:
    result = build_inverse_volatility_benchmark(covariance=_cov(A))
    assert result.method is AllocationBenchmarkMethod.INVERSE_VOLATILITY
    assert result.weights == (D("1"),) and result.unavailable_reason is None


def test_inverse_volatility_one_zero_vol_asset_is_unavailable() -> None:
    result = build_inverse_volatility_benchmark(covariance=_cov(["0.2", "0.2", "0.2"]))
    assert result.weights is None
    assert result.unavailable_reason is AllocationBenchmarkUnavailableReason.ZERO_VOLATILITY
    assert result.is_available is False


def test_inverse_volatility_unequal_vols_two_thirds_one_third() -> None:
    covariance = _cov(A, B_POS)
    assert covariance.covariance[0][0] == 1 and covariance.covariance[1][1] == 4
    result = build_inverse_volatility_benchmark(covariance=covariance)
    ctx = decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN)
    assert result.weights == (ctx.divide(D(2), D(3)), ctx.divide(D(1), D(3)))
    assert _exact_sum(result.weights) == 1
    assert result.weights[0] > result.weights[1]


def test_inverse_volatility_three_assets_sum_exactly_to_one_and_follow_inverse_vol_order() -> None:
    result = build_inverse_volatility_benchmark(covariance=_cov(A, B_POS, ["0", "4", "8"]))
    assert _exact_sum(result.weights) == 1
    assert result.weights[0] > result.weights[1] > result.weights[2]
    assert all(w.is_finite() and 0 < w <= 1 for w in result.weights)
    # sigma = 1, 2, 4  ->  4/7, 2/7, 1/7
    ctx = decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN)
    for got, num in zip(result.weights, (4, 2, 1)):
        assert abs(got - ctx.divide(D(num), D(7))) <= D("1E-49")


def test_inverse_volatility_equal_vols_is_conceptually_equal_allocation() -> None:
    result = build_inverse_volatility_benchmark(covariance=_cov(A, ["2", "1", "0"], ["1", "2", "0"]))
    assert abs(result.weights[0] - result.weights[1]) <= D("1E-49")
    assert abs(result.weights[0] - result.weights[2]) <= D("1E-49")
    assert _exact_sum(result.weights) == 1


def test_any_zero_variance_makes_inverse_volatility_unavailable_without_substitution() -> None:
    result = build_inverse_volatility_benchmark(covariance=_cov(A, ["3", "3", "3"], B_POS))
    assert result.weights is None
    assert result.unavailable_reason is AllocationBenchmarkUnavailableReason.ZERO_VOLATILITY
    assert result.method is AllocationBenchmarkMethod.INVERSE_VOLATILITY


def test_negative_off_diagonal_covariance_is_accepted() -> None:
    covariance = _cov(A, B_NEG)
    assert covariance.covariance[0][1] < 0
    assert build_inverse_volatility_benchmark(covariance=covariance).is_available


def test_zero_off_diagonal_covariance_is_accepted() -> None:
    covariance = _cov(["0", "1", "0", "1"], ["0", "0", "1", "1"])
    assert covariance.covariance[0][1] == 0
    result = build_inverse_volatility_benchmark(covariance=covariance)
    assert result.is_available and result.weights == (D("0.5"), D("0.5"))


def test_same_diagonal_different_covariance_gives_identical_weights() -> None:
    sources = [_cov(A, B_POS), _cov(A, B_NEG), _cov(A, B_MID)]
    assert len({c.covariance[0][1] for c in sources}) == 3  # genuinely different off-diagonal covariance
    assert len({(c.covariance[0][0], c.covariance[1][1]) for c in sources}) == 1  # same diagonal
    weights = [build_inverse_volatility_benchmark(covariance=c).weights for c in sources]
    assert weights[0] == weights[1] == weights[2]
    assert [[w.as_tuple() for w in ws] for ws in weights][0] == [[w.as_tuple() for w in ws] for ws in weights][1]


def test_weights_follow_the_source_instrument_order() -> None:
    forward = build_inverse_volatility_benchmark(covariance=_cov(A, B_POS))
    reversed_ids = _cov(B_POS, A)  # instrument 1 now has sigma 2, instrument 2 has sigma 1
    flipped = build_inverse_volatility_benchmark(covariance=reversed_ids)
    assert forward.weights == flipped.weights[::-1]
    assert flipped.instrument_ids == (UUIDS[0], UUIDS[1])


def test_extreme_volatility_ratio_beyond_the_representation_ceiling_fails_closed() -> None:
    covariance = _cov(A, ["0", "1E+1100", "2E+1100"])
    with pytest.raises(ValueError, match="allocation benchmark exceeds supported Decimal analytics range"):
        build_inverse_volatility_benchmark(covariance=covariance)


# --- result self validation / forge resistance -------------------------------------------------------------------

def _good_inverse() -> AllocationBenchmarkResult:
    return build_inverse_volatility_benchmark(covariance=_cov(A, B_POS))


def _make(source, method, weights, reason) -> AllocationBenchmarkResult:
    return AllocationBenchmarkResult(source=source, method=method, weights=weights, unavailable_reason=reason)


def test_valid_result_can_be_reconstructed() -> None:
    good = _good_inverse()
    assert _make(good.source, good.method, good.weights, None) == good


def test_wrong_method_is_rejected() -> None:
    good = _good_inverse()
    with pytest.raises(ValueError):
        _make(good.source, AllocationBenchmarkMethod.EQUAL_WEIGHT, good.weights, None)
    for bad in ("inverse_volatility", None, 1):
        with pytest.raises(TypeError):
            _make(good.source, bad, good.weights, None)


class _FakeEnum(Enum):
    INVERSE_VOLATILITY = "inverse_volatility"


def test_method_of_another_enum_type_is_rejected() -> None:
    good = _good_inverse()
    with pytest.raises(TypeError):
        _make(good.source, _FakeEnum.INVERSE_VOLATILITY, good.weights, None)


def test_wrong_weight_is_rejected() -> None:
    good = _good_inverse()
    forged = (good.weights[0] - D("0.01"), good.weights[1] + D("0.01"))
    with pytest.raises(ValueError):
        _make(good.source, good.method, forged, None)


def test_wrong_order_is_rejected() -> None:
    good = _good_inverse()
    with pytest.raises(ValueError):
        _make(good.source, good.method, good.weights[::-1], None)


def test_available_without_weights_is_rejected() -> None:
    good = _good_inverse()
    with pytest.raises(ValueError):
        _make(good.source, good.method, None, None)


def test_weights_supplied_when_unavailable_are_rejected() -> None:
    zero = build_inverse_volatility_benchmark(covariance=_cov(["1", "1", "1"], B_POS))
    with pytest.raises(ValueError):
        _make(zero.source, zero.method, (D("0.5"), D("0.5")), AllocationBenchmarkUnavailableReason.ZERO_VOLATILITY)


def test_unavailable_with_wrong_reason_type_or_missing_reason_is_rejected() -> None:
    zero = build_inverse_volatility_benchmark(covariance=_cov(["1", "1", "1"], B_POS))
    with pytest.raises(TypeError):
        _make(zero.source, zero.method, None, "zero_volatility")
    with pytest.raises(ValueError):
        _make(zero.source, zero.method, None, None)
    assert _make(zero.source, zero.method, None, AllocationBenchmarkUnavailableReason.ZERO_VOLATILITY) == zero


def test_unavailable_state_forged_onto_an_available_inverse_volatility_source_is_rejected() -> None:
    good = _good_inverse()
    with pytest.raises(ValueError):
        _make(good.source, good.method, None, AllocationBenchmarkUnavailableReason.ZERO_VOLATILITY)


def test_equal_weight_can_never_be_unavailable() -> None:
    covariance = _cov(["1", "1", "1"], B_POS)
    with pytest.raises(ValueError):
        _make(covariance, AllocationBenchmarkMethod.EQUAL_WEIGHT, None, AllocationBenchmarkUnavailableReason.ZERO_VOLATILITY)


def test_wrong_number_of_weights_is_rejected() -> None:
    good = _good_inverse()
    for forged in ((D("1"),), (good.weights[0], good.weights[1], D("0.1"))):
        with pytest.raises(ValueError):
            _make(good.source, good.method, forged, None)


def test_weights_must_be_an_exact_tuple_of_exact_decimals() -> None:
    good = _good_inverse()
    with pytest.raises(TypeError):
        _make(good.source, good.method, list(good.weights), None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _make(good.source, good.method, (0.5, 0.5), None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _make(good.source, good.method, (1, 0), None)  # type: ignore[arg-type]

    class _DecSub(Decimal):
        pass

    with pytest.raises(TypeError):
        _make(good.source, good.method, (_DecSub(good.weights[0]), good.weights[1]), None)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_weight_is_rejected(bad: str) -> None:
    good = _good_inverse()
    with pytest.raises(ValueError):
        _make(good.source, good.method, (D(bad), good.weights[1]), None)


@pytest.mark.parametrize("bad", ["0", "-0.1", "1.0000001"])
def test_non_positive_or_over_one_weight_is_rejected(bad: str) -> None:
    good = _good_inverse()
    with pytest.raises(ValueError):
        _make(good.source, good.method, (D(bad), good.weights[1]), None)


def test_weights_that_do_not_sum_to_one_are_rejected() -> None:
    good = _good_inverse()
    with pytest.raises(ValueError):
        _make(good.source, good.method, (good.weights[0], good.weights[1] - D("1E-40")), None)


def test_result_source_must_be_exact_covariance_type() -> None:
    good = _good_inverse()
    with pytest.raises(TypeError):
        _make(good.source.source, good.method, good.weights, None)
    with pytest.raises(TypeError):
        _make(None, good.method, good.weights, None)


# --- Decimal isolation / determinism -----------------------------------------------------------------------------

def _hostile_contexts():
    return [
        decimal.Context(prec=2, rounding=decimal.ROUND_DOWN, Emin=-3, Emax=3,
                        traps=[decimal.InvalidOperation, decimal.Overflow, decimal.Inexact, decimal.Rounded]),
        decimal.Context(prec=1, rounding=decimal.ROUND_CEILING, traps=[]),
    ]


@pytest.mark.parametrize("builder", [build_equal_weight_benchmark, build_inverse_volatility_benchmark])
def test_hostile_ambient_context_cannot_change_the_result_or_be_mutated(builder) -> None:
    covariance = _cov(A, B_POS, ["0", "3", "9"])
    baseline = builder(covariance=covariance)
    for hostile in _hostile_contexts():
        with decimal.localcontext(hostile) as ambient:
            ambient.clear_flags()
            snapshot = (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps))
            result = builder(covariance=covariance)
            assert (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps)) == snapshot
            assert not any(ambient.flags.values())
        assert result == baseline
        assert [w.as_tuple() for w in result.weights] == [w.as_tuple() for w in baseline.weights]


@pytest.mark.parametrize("builder", [build_equal_weight_benchmark, build_inverse_volatility_benchmark])
def test_repeated_builds_are_identical(builder) -> None:
    covariance = _cov(A, B_POS, ["0", "3", "9"])
    first = builder(covariance=covariance)
    for _ in range(3):
        again = builder(covariance=covariance)
        assert again == first
        assert [w.as_tuple() for w in again.weights] == [w.as_tuple() for w in first.weights]


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)


def test_imports_are_standard_library_plus_the_phase_18a_authority_only() -> None:
    plain: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            plain.add(node.module or "")
    assert plain <= {"__future__", "dataclasses", "decimal", "enum", "uuid", "backend.engine.private.allocation_matrix"}
    for forbidden in ("numpy", "pandas", "scipy", "sklearn", "statistics", "math", "random", "time", "datetime", "os"):
        assert forbidden not in plain


def test_no_ambient_context_float_or_global_context() -> None:
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not names & {"getcontext", "setcontext", "localcontext", "float"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is float]
    assert "prec=50" in _SOURCE and "ROUND_HALF_EVEN" in _SOURCE
    assert "Emin=decimal.MIN_EMIN" in _SOURCE and "Emax=decimal.MAX_EMAX" in _SOURCE
    module_level = [
        n for n in _TREE.body
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and getattr(n.value.func, "attr", "") == "Context"
    ]
    assert module_level == []


def test_weighting_never_reads_sample_means_or_off_diagonal_covariance_names() -> None:
    attributes = {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert "sample_means" not in attributes
    assert "source" in attributes  # the covariance authority is reached only through the retained source / covariance


_FORBIDDEN_FRAGMENTS = (
    "erc", "risk_parity", "risk_contribution", "marginal", "hrp", "cluster", "distance", "optimi", "cvar",
    "expected_return", "sharpe", "sortino", "score", "rank", "recommend", "rebalance", "tilt", "objective",
    "solver", "gradient", "minimum_variance", "mean_variance", "correlation", "portfolio_vol", "risk_budget",
)


def test_no_erc_hrp_optimizer_or_decision_symbols() -> None:
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
    members = {m.name for e in (AllocationBenchmarkMethod, AllocationBenchmarkUnavailableReason) for m in e}
    assert members == {"EQUAL_WEIGHT", "INVERSE_VOLATILITY", "ZERO_VOLATILITY"}


def test_documents_the_diagonal_only_dependency_and_that_it_is_not_risk_parity() -> None:
    doc = module_under_test.__doc__ or ""
    assert "diagonal" in doc and "not" in doc.lower()
    assert "residual" in doc


# --- Phase 18B-R1: one analytics Decimal context + context-free exact residual closure ---------------------------

def _context_constructions() -> list[str]:
    owners: list[str] = []
    for func in [n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)]:
        for call in [n for n in ast.walk(func) if isinstance(n, ast.Call)]:
            if getattr(call.func, "attr", "") == "Context":
                owners.append(func.name)
    return owners


def test_r1_only_the_analytics_context_constructs_a_decimal_context() -> None:
    assert _context_constructions() == ["_analytics_context"]
    assert not hasattr(module_under_test, "_closure_context")
    assert "_closure_context" not in _SOURCE
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert "Inexact" not in names and "localcontext" not in names and "getcontext" not in names and "setcontext" not in names


def test_r2_no_second_precision_regime() -> None:
    assert not hasattr(module_under_test, "_CLOSURE_PRECISION")
    assert "_CLOSURE_PRECISION" not in _SOURCE
    assert re.findall(r"prec\s*=\s*(\d+)", _SOURCE) == ["50"]


def test_r3_three_way_equal_weight_closes_exactly_by_coefficient_arithmetic() -> None:
    weights = build_equal_weight_benchmark(covariance=_cov(A, B_POS, B_NEG)).weights
    assert _coefficient_sum_is_one(weights)
    assert _exact_sum(weights) == 1
    # the stored Decimals are exactly the 50-digit analytics values plus the single-weight residual
    assert weights[0].as_tuple() == D("0.33333333333333333333333333333333333333333333333334").as_tuple()
    assert weights[1].as_tuple() == D("0.33333333333333333333333333333333333333333333333333").as_tuple()
    assert weights[2].as_tuple() == weights[1].as_tuple()


@pytest.mark.parametrize("builder", [build_equal_weight_benchmark, build_inverse_volatility_benchmark])
def test_r3_exact_coefficient_sum_for_every_dimension(builder) -> None:
    for count in range(1, 9):
        covariance = _cov(*[[str(j * (i + 1) + (i % 3)) for j in range(4)] for i in range(count)])
        result = builder(covariance=covariance)
        if result.is_available:
            assert _coefficient_sum_is_one(result.weights) and _exact_sum(result.weights) == 1


def test_r4_hostile_ambient_context_does_not_alter_the_exact_closure() -> None:
    covariance = _cov(A, B_POS, B_NEG)
    baseline = build_equal_weight_benchmark(covariance=covariance).weights
    for hostile in _hostile_contexts():
        with decimal.localcontext(hostile) as ambient:
            ambient.clear_flags()
            weights = build_equal_weight_benchmark(covariance=covariance).weights
            assert not any(ambient.flags.values())
        assert [w.as_tuple() for w in weights] == [w.as_tuple() for w in baseline]
        assert _coefficient_sum_is_one(weights)


def _wide_spread_covariance(exponent: int) -> AllocationCovarianceMatrix:
    return _cov(A, ["0", f"1E+{exponent}", f"2E+{exponent}"])


def test_r5_failure_is_classified_as_the_explicit_resource_ceiling(monkeypatch) -> None:
    assert module_under_test._MAX_CLOSURE_DECIMAL_PLACES == 1000
    covariance = _wide_spread_covariance(30)
    assert build_inverse_volatility_benchmark(covariance=covariance).is_available  # fine under the real ceiling
    monkeypatch.setattr(module_under_test, "_MAX_CLOSURE_DECIMAL_PLACES", 10)
    with pytest.raises(ValueError, match="allocation benchmark exceeds supported Decimal analytics range"):
        build_inverse_volatility_benchmark(covariance=covariance)
    monkeypatch.undo()
    with pytest.raises(ValueError, match="allocation benchmark exceeds supported Decimal analytics range"):
        build_inverse_volatility_benchmark(covariance=_wide_spread_covariance(1100))


def test_r5_hostile_exponents_are_rejected_before_any_large_allocation() -> None:
    covariance = _cov(A)
    good = build_equal_weight_benchmark(covariance=covariance)
    forged = (D("1E-999999999"),)
    with pytest.raises(ValueError):
        AllocationBenchmarkResult(source=good.source, method=good.method, weights=forged, unavailable_reason=None)


def test_r6_large_exponent_spread_below_the_ceiling_succeeds_and_sums_exactly() -> None:
    result = build_inverse_volatility_benchmark(covariance=_wide_spread_covariance(900))
    assert result.is_available
    assert _coefficient_sum_is_one(result.weights) and _exact_sum(result.weights) == 1
    assert all(w.is_finite() and 0 < w <= 1 for w in result.weights)
    assert result.weights[1] == D("1E-900")  # the tiny weight is neither dropped, zeroed nor floored
    assert _exact_sum(result.weights[:1]) == 1 - Fraction(1, 10**900)  # the residual lands on the largest weight


def test_r7_reference_economics_are_unchanged_down_to_as_tuple() -> None:
    ctx = decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN)
    two_thirds, one_third = ctx.divide(D(2), D(3)), ctx.divide(D(1), D(3))
    inverse = build_inverse_volatility_benchmark(covariance=_cov(A, B_POS)).weights
    assert [w.as_tuple() for w in inverse] == [two_thirds.as_tuple(), one_third.as_tuple()]  # zero residual: untouched
    half = build_equal_weight_benchmark(covariance=_cov(A, B_POS)).weights
    assert [w.as_tuple() for w in half] == [D("0.5").as_tuple(), D("0.5").as_tuple()]
    single = build_equal_weight_benchmark(covariance=_cov(A)).weights
    assert [w.as_tuple() for w in single] == [D("1").as_tuple()]
