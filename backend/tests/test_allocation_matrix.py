"""
backend/tests/test_allocation_matrix.py
=======================================
Phase 18A: generic aligned monthly return panel + exact Decimal sample covariance foundation.

The module validates mathematical structure only. Source / point-in-time authority is resolved upstream.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import allocation_matrix as module_under_test
from backend.engine.private.allocation_matrix import (
    AllocationCovarianceMatrix,
    AllocationInstrumentReturnSeries,
    AllocationMonth,
    AllocationMonthlyReturnPoint,
    AllocationReturnPanel,
    build_allocation_covariance_matrix,
    build_allocation_return_panel,
)

D = Decimal
U1 = UUID(int=1)
U2 = UUID(int=2)
U3 = UUID(int=3)


def _months(count: int, start: tuple[int, int] = (2026, 1)) -> list[AllocationMonth]:
    year, month = start
    out = []
    for _ in range(count):
        out.append(AllocationMonth(year=year, month=month))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


def _series(instrument_id: UUID, values: list[str], start: tuple[int, int] = (2026, 1)) -> AllocationInstrumentReturnSeries:
    points = tuple(
        AllocationMonthlyReturnPoint(period=p, simple_return=D(v)) for p, v in zip(_months(len(values), start), values)
    )
    return AllocationInstrumentReturnSeries(instrument_id=instrument_id, points=points)


def _panel(*pairs: tuple[UUID, list[str]]) -> AllocationReturnPanel:
    return build_allocation_return_panel(series=tuple(_series(i, v) for i, v in pairs))


# --- AllocationMonth ---------------------------------------------------------------------------------------------

def test_valid_month_and_frozen() -> None:
    month = AllocationMonth(year=2026, month=3)
    assert (month.year, month.month) == (2026, 3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        month.year = 2027  # type: ignore[misc]


@pytest.mark.parametrize("year", [0, -1])
def test_year_must_be_positive(year: int) -> None:
    with pytest.raises(ValueError):
        AllocationMonth(year=year, month=1)


@pytest.mark.parametrize("month", [0, 13, -1])
def test_month_must_be_between_1_and_12(month: int) -> None:
    with pytest.raises(ValueError):
        AllocationMonth(year=2026, month=month)


class _IntSub(int):
    pass


@pytest.mark.parametrize("bad", [True, False, _IntSub(2026), 2026.0, "2026", None])
def test_year_must_be_exact_int(bad: object) -> None:
    with pytest.raises(TypeError):
        AllocationMonth(year=bad, month=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [True, _IntSub(1), 1.0, "1", None])
def test_month_must_be_exact_int(bad: object) -> None:
    with pytest.raises(TypeError):
        AllocationMonth(year=2026, month=bad)  # type: ignore[arg-type]


def test_month_index_is_strictly_ordered_and_consecutive_across_year_end() -> None:
    dec = AllocationMonth(year=2025, month=12)
    jan = AllocationMonth(year=2026, month=1)
    assert jan.month_index == dec.month_index + 1
    assert AllocationMonth(year=2026, month=1).month_index < AllocationMonth(year=2026, month=2).month_index


# --- AllocationMonthlyReturnPoint --------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["0.05", "-0.05", "-1", "0", "0.000", "1E+3"])
def test_valid_returns(value: str) -> None:
    point = AllocationMonthlyReturnPoint(period=AllocationMonth(2026, 1), simple_return=D(value))
    assert point.simple_return == D(value)


class _DecSub(Decimal):
    pass


@pytest.mark.parametrize("bad", [0.1, 1, True, "0.1", None, _DecSub("0.1")])
def test_return_must_be_exact_decimal(bad: object) -> None:
    with pytest.raises(TypeError):
        AllocationMonthlyReturnPoint(period=AllocationMonth(2026, 1), simple_return=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity", "-1.0000001", "-2"])
def test_return_must_be_finite_and_at_least_minus_one(bad: str) -> None:
    with pytest.raises(ValueError):
        AllocationMonthlyReturnPoint(period=AllocationMonth(2026, 1), simple_return=D(bad))


def test_period_must_be_exact_allocation_month() -> None:
    with pytest.raises(TypeError):
        AllocationMonthlyReturnPoint(period=(2026, 1), simple_return=D("0.1"))  # type: ignore[arg-type]


def test_point_is_frozen() -> None:
    point = AllocationMonthlyReturnPoint(period=AllocationMonth(2026, 1), simple_return=D("0.1"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.simple_return = D("0.2")  # type: ignore[misc]


# --- AllocationInstrumentReturnSeries ----------------------------------------------------------------------------

def _point(year: int, month: int, value: str = "0.01") -> AllocationMonthlyReturnPoint:
    return AllocationMonthlyReturnPoint(period=AllocationMonth(year, month), simple_return=D(value))


def test_series_valid_and_frozen() -> None:
    series = _series(U1, ["0.1", "0.2", "0.3"])
    assert series.instrument_id == U1 and len(series.points) == 3
    with pytest.raises(dataclasses.FrozenInstanceError):
        series.instrument_id = U2  # type: ignore[misc]


def test_series_points_must_be_exact_tuple() -> None:
    with pytest.raises(TypeError):
        AllocationInstrumentReturnSeries(instrument_id=U1, points=[_point(2026, 1), _point(2026, 2)])  # type: ignore[arg-type]


def test_series_points_must_be_exact_point_type() -> None:
    with pytest.raises(TypeError):
        AllocationInstrumentReturnSeries(instrument_id=U1, points=(_point(2026, 1), "x"))  # type: ignore[arg-type]


class _UuidSub(UUID):
    pass


@pytest.mark.parametrize("bad", ["00000000-0000-0000-0000-000000000001", 1, None, _UuidSub(int=1)])
def test_series_instrument_id_must_be_exact_uuid(bad: object) -> None:
    with pytest.raises(TypeError):
        AllocationInstrumentReturnSeries(instrument_id=bad, points=(_point(2026, 1), _point(2026, 2)))  # type: ignore[arg-type]


@pytest.mark.parametrize("count", [0, 1])
def test_series_requires_at_least_two_observations(count: int) -> None:
    with pytest.raises(ValueError):
        AllocationInstrumentReturnSeries(instrument_id=U1, points=tuple(_point(2026, m + 1) for m in range(count)))


def test_series_two_observations_is_the_minimum() -> None:
    assert len(_series(U1, ["0.1", "0.2"]).points) == 2


def test_series_months_must_be_strictly_increasing() -> None:
    with pytest.raises(ValueError):
        AllocationInstrumentReturnSeries(instrument_id=U1, points=(_point(2026, 2), _point(2026, 1)))


def test_series_duplicate_month_is_rejected() -> None:
    with pytest.raises(ValueError):
        AllocationInstrumentReturnSeries(instrument_id=U1, points=(_point(2026, 1), _point(2026, 1), _point(2026, 2)))


def test_series_month_gap_is_rejected_not_filled() -> None:
    with pytest.raises(ValueError):
        AllocationInstrumentReturnSeries(instrument_id=U1, points=(_point(2026, 1), _point(2026, 3)))


def test_series_year_boundary_is_consecutive() -> None:
    series = AllocationInstrumentReturnSeries(instrument_id=U1, points=(_point(2025, 12), _point(2026, 1)))
    assert len(series.points) == 2


def test_series_constructor_does_not_sort_or_repair() -> None:
    with pytest.raises(ValueError):
        AllocationInstrumentReturnSeries(instrument_id=U1, points=(_point(2026, 3), _point(2026, 2), _point(2026, 1)))


# --- AllocationReturnPanel ---------------------------------------------------------------------------------------

def test_panel_one_instrument_is_valid() -> None:
    panel = _panel((U1, ["0.1", "0.2"]))
    assert panel.instrument_count == 1 and panel.observation_count == 2
    assert panel.instrument_ids == (U1,)
    assert panel.periods == tuple(_months(2))


def test_panel_multi_instrument_derived_properties() -> None:
    panel = _panel((U1, ["0.1", "0.2", "0.3"]), (U2, ["0.3", "0.2", "0.1"]))
    assert panel.instrument_ids == (U1, U2)
    assert panel.instrument_count == 2 and panel.observation_count == 3
    assert panel.periods == tuple(_months(3))


def test_panel_derived_properties_are_not_stored_fields() -> None:
    assert [f.name for f in dataclasses.fields(AllocationReturnPanel)] == ["series"]


def test_panel_series_must_be_exact_tuple_of_exact_series() -> None:
    s = _series(U1, ["0.1", "0.2"])
    with pytest.raises(TypeError):
        AllocationReturnPanel(series=[s])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AllocationReturnPanel(series=(s, "x"))  # type: ignore[arg-type]


def test_panel_requires_at_least_one_instrument() -> None:
    with pytest.raises(ValueError):
        AllocationReturnPanel(series=())


def test_panel_duplicate_instrument_is_rejected() -> None:
    s = _series(U1, ["0.1", "0.2"])
    with pytest.raises(ValueError):
        AllocationReturnPanel(series=(s, s))


def test_panel_constructor_requires_canonical_order_and_does_not_sort() -> None:
    a, b = _series(U1, ["0.1", "0.2"]), _series(U2, ["0.1", "0.2"])
    assert AllocationReturnPanel(series=(a, b)).instrument_ids == (U1, U2)
    with pytest.raises(ValueError):
        AllocationReturnPanel(series=(b, a))


def test_panel_canonical_order_is_the_uuid_string_order() -> None:
    low = UUID("00000000-0000-0000-0000-00000000000a")
    high = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    panel = build_allocation_return_panel(series=(_series(high, ["0.1", "0.2"]), _series(low, ["0.1", "0.2"])))
    assert panel.instrument_ids == (low, high)
    assert [str(i) for i in panel.instrument_ids] == sorted(str(i) for i in panel.instrument_ids)


def test_panel_mismatched_periods_are_rejected() -> None:
    a = _series(U1, ["0.1", "0.2", "0.3"])
    b = _series(U2, ["0.1", "0.2", "0.3"], start=(2026, 2))
    with pytest.raises(ValueError):
        AllocationReturnPanel(series=(a, b))


def test_panel_missing_period_is_rejected_not_intersected() -> None:
    a = _series(U1, ["0.1", "0.2", "0.3"])
    b = _series(U2, ["0.1", "0.2"])
    with pytest.raises(ValueError):
        build_allocation_return_panel(series=(a, b))
    with pytest.raises(ValueError):
        build_allocation_return_panel(series=(b, a))


def test_builder_is_input_order_independent_and_preserves_series_identity() -> None:
    a, b, c = (_series(U1, ["0.1", "0.2"]), _series(U2, ["0.3", "0.4"]), _series(U3, ["-0.1", "0"]))
    first = build_allocation_return_panel(series=(c, a, b))
    second = build_allocation_return_panel(series=(b, c, a))
    assert first == second
    assert first.series == (a, b, c)
    assert first.series[0] is a and first.series[2] is c  # values, months and points are never rebuilt


def test_builder_requires_exact_tuple_of_exact_series() -> None:
    s = _series(U1, ["0.1", "0.2"])
    with pytest.raises(TypeError):
        build_allocation_return_panel(series=[s])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_allocation_return_panel(series=(s, object()))  # type: ignore[arg-type]


def test_builder_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        build_allocation_return_panel((_series(U1, ["0.1", "0.2"]),))  # type: ignore[misc]


# --- covariance --------------------------------------------------------------------------------------------------

def test_hand_checkable_two_asset_example_is_exact() -> None:
    panel = _panel((U1, ["0.10", "0.20", "0.30"]), (U2, ["0.30", "0.20", "0.10"]))
    result = build_allocation_covariance_matrix(return_panel=panel)
    assert result.sample_means == (D("0.20"), D("0.20"))
    assert result.covariance == (
        (D("0.01"), D("-0.01")),
        (D("-0.01"), D("0.01")),
    )


def test_one_asset_sample_variance_uses_n_minus_one() -> None:
    panel = _panel((U1, ["0.10", "0.20", "0.30"]))
    result = build_allocation_covariance_matrix(return_panel=panel)
    assert result.covariance == ((D("0.01"),),)  # population variance would be 0.00666...
    assert result.dimension == 1 and result.observation_count == 3


def test_two_observations_denominator_is_one() -> None:
    result = build_allocation_covariance_matrix(return_panel=_panel((U1, ["0.00", "0.10"])))
    assert result.sample_means == (D("0.05"),)
    assert result.covariance == ((D("0.005"),),)


def test_two_assets_positive_covariance() -> None:
    panel = _panel((U1, ["0.10", "0.20", "0.30"]), (U2, ["0.20", "0.40", "0.60"]))
    result = build_allocation_covariance_matrix(return_panel=panel)
    assert result.covariance[0][1] == D("0.02") and result.covariance[1][1] == D("0.04")


def test_negative_covariance_is_preserved_not_clipped() -> None:
    result = build_allocation_covariance_matrix(
        return_panel=_panel((U1, ["0.10", "0.20", "0.30"]), (U2, ["0.30", "0.20", "0.10"]))
    )
    assert result.covariance[0][1] < 0 and result.covariance[1][0] < 0


def test_zero_covariance() -> None:
    panel = _panel((U1, ["0.10", "0.20", "0.30"]), (U2, ["0.10", "0.40", "0.10"]))
    result = build_allocation_covariance_matrix(return_panel=panel)
    assert result.covariance[0][1] == 0 and result.covariance[1][0] == 0


def test_constant_series_has_zero_variance_and_is_a_valid_observed_value() -> None:
    panel = _panel((U1, ["0.05", "0.05", "0.05"]), (U2, ["0.10", "0.20", "0.30"]))
    result = build_allocation_covariance_matrix(return_panel=panel)
    assert result.covariance[0][0] == 0
    assert result.covariance[0][1] == 0 and result.covariance[1][0] == 0
    assert result.sample_means[0] == D("0.05")


def test_covariance_is_exactly_symmetric_and_diagonal_non_negative() -> None:
    panel = _panel(
        (U1, ["0.011", "-0.023", "0.0071", "0.04"]),
        (U2, ["-0.5", "0.31", "0.0009", "-0.002"]),
        (U3, ["0.2", "0.2", "-0.9", "0.13"]),
    )
    cov = build_allocation_covariance_matrix(return_panel=panel).covariance
    for i in range(3):
        assert cov[i][i] >= 0
        for j in range(3):
            assert cov[i][j] == cov[j][i]
            assert cov[i][j].as_tuple() == cov[j][i].as_tuple()  # identical spelling, one calculation path


def test_instrument_order_follows_the_canonical_panel_order() -> None:
    forward = build_allocation_covariance_matrix(return_panel=_panel((U1, ["0.1", "0.3"]), (U2, ["0.2", "0.9"])))
    shuffled_input = build_allocation_return_panel(series=(_series(U2, ["0.2", "0.9"]), _series(U1, ["0.1", "0.3"])))
    assert build_allocation_covariance_matrix(return_panel=shuffled_input) == forward
    assert forward.instrument_ids == (U1, U2)


def test_result_retains_the_panel_by_identity() -> None:
    panel = _panel((U1, ["0.1", "0.2", "0.3"]))
    result = build_allocation_covariance_matrix(return_panel=panel)
    assert result.source is panel


def test_builder_requires_exact_panel_type() -> None:
    class _Duck:
        series = ()

    with pytest.raises(TypeError):
        build_allocation_covariance_matrix(return_panel=_Duck())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_allocation_covariance_matrix(return_panel=None)  # type: ignore[arg-type]


def test_builder_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        build_allocation_covariance_matrix(_panel((U1, ["0.1", "0.2"])))  # type: ignore[misc]


def test_non_terminating_division_is_rounded_half_even_at_50_digits() -> None:
    result = build_allocation_covariance_matrix(return_panel=_panel((U1, ["0", "0", "1"])))
    mean = result.sample_means[0]
    assert mean == D(1) / D(3) or len(mean.as_tuple().digits) == 50
    assert len(mean.as_tuple().digits) == 50


def test_numeric_range_overflow_is_a_static_error() -> None:
    huge = "1E+900000000000000000"
    panel = _panel((U1, [huge, "0"]))
    with pytest.raises(ValueError, match="allocation covariance exceeds supported Decimal analytics range"):
        build_allocation_covariance_matrix(return_panel=panel)


# --- forge resistance --------------------------------------------------------------------------------------------

def _good() -> tuple[AllocationReturnPanel, AllocationCovarianceMatrix]:
    panel = _panel((U1, ["0.10", "0.20", "0.30"]), (U2, ["0.30", "0.20", "0.10"]))
    return panel, build_allocation_covariance_matrix(return_panel=panel)


def test_forged_sample_mean_is_rejected() -> None:
    panel, good = _good()
    with pytest.raises(ValueError):
        AllocationCovarianceMatrix(source=panel, sample_means=(D("0.21"), D("0.20")), covariance=good.covariance)


def test_forged_covariance_value_is_rejected() -> None:
    panel, good = _good()
    forged = ((D("0.01"), D("-0.02")), (D("-0.02"), D("0.01")))
    with pytest.raises(ValueError):
        AllocationCovarianceMatrix(source=panel, sample_means=good.sample_means, covariance=forged)


def test_asymmetric_covariance_is_rejected() -> None:
    panel, good = _good()
    forged = ((D("0.01"), D("-0.01")), (D("-0.02"), D("0.01")))
    with pytest.raises(ValueError):
        AllocationCovarianceMatrix(source=panel, sample_means=good.sample_means, covariance=forged)


def test_reordered_matrix_is_rejected() -> None:
    panel = _panel((U1, ["0.10", "0.20", "0.30"]), (U2, ["0.20", "0.40", "0.90"]))
    good = build_allocation_covariance_matrix(return_panel=panel)
    swapped = (
        (good.covariance[1][1], good.covariance[1][0]),
        (good.covariance[0][1], good.covariance[0][0]),
    )
    with pytest.raises(ValueError):
        AllocationCovarianceMatrix(
            source=panel, sample_means=(good.sample_means[1], good.sample_means[0]), covariance=swapped
        )


def test_wrong_dimension_is_rejected() -> None:
    panel, good = _good()
    with pytest.raises(ValueError):
        AllocationCovarianceMatrix(source=panel, sample_means=good.sample_means[:1], covariance=good.covariance)
    with pytest.raises(ValueError):
        AllocationCovarianceMatrix(source=panel, sample_means=good.sample_means, covariance=good.covariance[:1])
    with pytest.raises(ValueError):
        AllocationCovarianceMatrix(
            source=panel, sample_means=good.sample_means, covariance=(good.covariance[0][:1], good.covariance[1])
        )


def test_lists_instead_of_tuples_are_rejected() -> None:
    panel, good = _good()
    with pytest.raises(TypeError):
        AllocationCovarianceMatrix(source=panel, sample_means=list(good.sample_means), covariance=good.covariance)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AllocationCovarianceMatrix(source=panel, sample_means=good.sample_means, covariance=list(good.covariance))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AllocationCovarianceMatrix(
            source=panel, sample_means=good.sample_means, covariance=([*good.covariance[0]], good.covariance[1])  # type: ignore[arg-type]
        )


def test_non_decimal_values_are_rejected() -> None:
    panel, good = _good()
    with pytest.raises(TypeError):
        AllocationCovarianceMatrix(source=panel, sample_means=(0.2, 0.2), covariance=good.covariance)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AllocationCovarianceMatrix(
            source=panel, sample_means=good.sample_means, covariance=((0.01, -0.01), (-0.01, 0.01))  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        AllocationCovarianceMatrix(
            source=panel, sample_means=good.sample_means,
            covariance=((_DecSub("0.01"), D("-0.01")), (D("-0.01"), D("0.01"))),
        )


def test_source_must_be_exact_panel() -> None:
    _, good = _good()
    with pytest.raises(TypeError):
        AllocationCovarianceMatrix(source=object(), sample_means=good.sample_means, covariance=good.covariance)  # type: ignore[arg-type]


def test_result_is_frozen() -> None:
    _, good = _good()
    with pytest.raises(dataclasses.FrozenInstanceError):
        good.sample_means = ()  # type: ignore[misc]


# --- Decimal isolation -------------------------------------------------------------------------------------------

def _hostile_contexts():
    return [
        decimal.Context(prec=2, rounding=decimal.ROUND_DOWN, Emin=-3, Emax=3, traps=[decimal.InvalidOperation, decimal.Overflow, decimal.Inexact, decimal.Rounded]),
        decimal.Context(prec=1, rounding=decimal.ROUND_CEILING, traps=[]),
    ]


def test_hostile_ambient_context_cannot_change_results_or_be_mutated() -> None:
    panel = _panel((U1, ["0", "0", "1"]), (U2, ["0.1", "-0.3", "0.17"]))
    baseline = build_allocation_covariance_matrix(return_panel=panel)
    for hostile in _hostile_contexts():
        with decimal.localcontext(hostile) as ambient:
            ambient.clear_flags()
            snapshot = (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps))
            result = build_allocation_covariance_matrix(return_panel=panel)
            assert (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps)) == snapshot
            assert not any(ambient.flags.values())
        assert result == baseline
        assert [v.as_tuple() for v in result.sample_means] == [v.as_tuple() for v in baseline.sample_means]
        assert [[v.as_tuple() for v in row] for row in result.covariance] == [[v.as_tuple() for v in row] for row in baseline.covariance]


def test_repeated_builds_are_identical() -> None:
    panel = _panel((U1, ["0", "0", "1"]), (U2, ["0.1", "-0.3", "0.17"]))
    first = build_allocation_covariance_matrix(return_panel=panel)
    for _ in range(3):
        again = build_allocation_covariance_matrix(return_panel=panel)
        assert again == first and again.covariance == first.covariance


def test_global_context_is_untouched_by_a_build() -> None:
    before = decimal.getcontext().copy()
    build_allocation_covariance_matrix(return_panel=_panel((U1, ["0.1", "0.2", "0.35"])))
    after = decimal.getcontext()
    assert (after.prec, after.rounding, after.Emin, after.Emax) == (before.prec, before.rounding, before.Emin, before.Emax)


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)


def _imported_roots() -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not allowed"
            roots.add((node.module or "").split(".")[0])
    return roots


def test_imports_are_standard_library_only_and_exclude_float_math_stacks() -> None:
    roots = _imported_roots()
    assert roots <= {"__future__", "dataclasses", "decimal", "uuid"}
    for forbidden in ("numpy", "pandas", "scipy", "sklearn", "statistics", "math", "random", "backend"):
        assert forbidden not in roots


def test_no_provider_resolver_database_or_network_imports() -> None:
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            for name in names:
                lowered = name.lower()
                for fragment in ("provider", "resolver", "supabase", "requests", "httpx", "socket", "sqlalchemy", "deprecated", "market_data", "tefas"):
                    assert fragment not in lowered, name


def test_no_ambient_decimal_context_or_float_arithmetic() -> None:
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not names & {"getcontext", "setcontext", "localcontext", "float", "sqrt", "fmean"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is float]


def test_fresh_50_digit_context_per_calculation() -> None:
    assert "prec=50" in _SOURCE and "ROUND_HALF_EVEN" in _SOURCE
    assert "Emin=decimal.MIN_EMIN" in _SOURCE and "Emax=decimal.MAX_EMAX" in _SOURCE
    module_level_contexts = [
        n for n in _TREE.body
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and getattr(n.value.func, "attr", "") == "Context"
    ]
    assert module_level_contexts == []  # no module-global mutable Context


_FORBIDDEN_FRAGMENTS = (
    "weight", "optimi", "expected_return", "forecast", "alpha", "sharpe", "sortino", "utility", "risk_free",
    "risk_parity", "erc", "hrp", "minimum_variance", "mean_variance", "cvar", "risk_budget", "bisection",
    "cluster", "correlation", "recommend", "rebalance", "target_allocation", "tilt", "score", "rank",
)


def test_no_weighting_optimizer_or_decision_surface() -> None:
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
    public = {n for n in dir(module_under_test) if not n.startswith("_")}
    assert {"AllocationMonth", "AllocationMonthlyReturnPoint", "AllocationInstrumentReturnSeries", "AllocationReturnPanel",
            "AllocationCovarianceMatrix", "build_allocation_return_panel", "build_allocation_covariance_matrix"} <= public


def test_documents_the_authority_limit() -> None:
    doc = module_under_test.__doc__ or ""
    assert "mathematical structure only" in doc
    assert "upstream" in doc
