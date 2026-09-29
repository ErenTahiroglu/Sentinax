"""
backend/tests/test_tefas_fund_downside_deviation.py
===================================================
Tests for the rolling TEFAS downside deviation (Phase 16E).

DD = sqrt((1/N) * sum(min(r_t - MAR, 0)^2)) with N = ALL rolling windows, an explicit mandatory same-horizon
MAR, and an explicit 50-digit ROUND_HALF_EVEN Decimal context. Empty history is None (unavailable); a
non-empty series with no shortfall is an observed 0. Descriptive only: overlapping windows, no Sortino.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import fund_downside_deviation as module_under_test
from backend.engine.private.domain import Currency, DataConfidenceLevel, Horizon
from backend.engine.private.fund_downside_deviation import TefasFundDownsideDeviation, calculate_tefas_fund_downside_deviation
from backend.engine.private.fund_price_series import TefasFundPricePoint, TefasFundPriceSeries
from backend.engine.private.fund_rolling_returns import (
    TefasFundRollingReturnSeries,
    build_tefas_fund_rolling_return_series,
)
from backend.engine.private.market_data.models import MarketDataResolutionMode

CR = MarketDataResolutionMode.CURRENT_REPORTED
H12, H36, H60 = Horizon.ALLOCATION_12M, Horizon.STRATEGIC_3Y, Horizon.STRATEGIC_5Y

_INST = UUID("11111111-1111-4111-8111-111111111111")
_T0 = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)

_SOURCE_MSG = r"^source must be an exact TefasFundRollingReturnSeries instance$"
_MAR_TYPE_MSG = r"^minimum_acceptable_return must be an exact Decimal instance$"
_MAR_VALUE_MSG = r"^minimum_acceptable_return must be a finite Decimal greater than or equal to -1$"
_DD_TYPE_MSG = r"^downside_deviation must be None or an exact Decimal instance$"
_DD_VALUE_MSG = r"^downside_deviation must be a finite Decimal greater than or equal to 0$"
_MATCH_MSG = r"^downside deviation result must match the canonical calculation from source$"
_RANGE_MSG = r"^TEFAS downside deviation exceeds supported Decimal analytics range$"


def _ctx() -> decimal.Context:
    return decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=decimal.MIN_EMIN,
                           Emax=decimal.MAX_EMAX, traps=[decimal.InvalidOperation, decimal.DivisionByZero,
                                                         decimal.Overflow])


def _month(i: int, base=(2025, 1)) -> tuple[int, int]:
    index = base[0] * 12 + (base[1] - 1) + i
    return index // 12, index % 12 + 1


def _p(d: date, price: str) -> TefasFundPricePoint:
    return TefasFundPricePoint(trade_date=d, unit_price=Decimal(price), currency=Currency.TRY,
                               confidence=DataConfidenceLevel.HIGH, snapshot_retrieved_at=_T0,
                               resolution_key=f"key-{d.isoformat()}")


def _price_series(prices) -> TefasFundPriceSeries:
    dated = [(date(*_month(i), 28), x) for i, x in enumerate(prices)]
    return TefasFundPriceSeries(instrument_id=_INST, mode=CR, as_of=None,
                                requested_dates=tuple(d for d, _ in dated),
                                points=tuple(_p(d, x) for d, x in dated), gaps=())


def _rolling_from_returns(returns, base: str = "100") -> TefasFundRollingReturnSeries:
    """12M rolling series whose windows have exactly the requested returns (at most 12 windows)."""
    assert len(returns) <= 12
    prices = [base] * 12 + [str(Decimal(base) * (Decimal(1) + Decimal(r))) for r in returns]
    return build_tefas_fund_rolling_return_series(price_series=_price_series(prices), horizon=H12)


def _empty(horizon=H12) -> TefasFundRollingReturnSeries:
    n = {H12: 12, H36: 36, H60: 60}[horizon]
    return build_tefas_fund_rolling_return_series(
        price_series=_price_series([str(100 + i) for i in range(n)]), horizon=horizon)


def _dd(rolling, mar="0") -> TefasFundDownsideDeviation:
    return calculate_tefas_fund_downside_deviation(rolling_series=rolling, minimum_acceptable_return=Decimal(mar))


class _DecimalSub(Decimal):
    pass


class _Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")


# --- source / structure -------------------------------------------------------------

def test_fields_frozen_and_source_identity() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundDownsideDeviation)] == [
        "source", "minimum_acceptable_return", "downside_deviation"]
    rolling = _rolling_from_returns(["-0.1", "0.2"])
    result = _dd(rolling)
    assert result.source is rolling
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.downside_deviation = Decimal("0")  # type: ignore[misc]
    for attr in ("horizon", "mean", "sortino", "sharpe", "calmar", "annualized"):
        assert not hasattr(result, attr)


def test_builder_arguments_are_keyword_only_without_defaults() -> None:
    rolling = _rolling_from_returns(["0.1"])
    with pytest.raises(TypeError):
        calculate_tefas_fund_downside_deviation(rolling, Decimal(0))  # type: ignore[misc]
    with pytest.raises(TypeError):
        calculate_tefas_fund_downside_deviation(rolling_series=rolling)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        calculate_tefas_fund_downside_deviation(minimum_acceptable_return=Decimal(0))  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile(), [Decimal("0.1")], (Decimal("0.1"),)])
def test_builder_rejects_non_rolling_series(bad) -> None:
    with pytest.raises(TypeError, match=_SOURCE_MSG):
        calculate_tefas_fund_downside_deviation(rolling_series=bad, minimum_acceptable_return=Decimal(0))


def test_builder_and_constructor_reject_source_subclass() -> None:
    class _Sub(TefasFundRollingReturnSeries):
        pass
    base = _rolling_from_returns(["0.1"])
    sub = _Sub(**{f.name: getattr(base, f.name) for f in dataclasses.fields(base)})
    with pytest.raises(TypeError, match=_SOURCE_MSG):
        calculate_tefas_fund_downside_deviation(rolling_series=sub, minimum_acceptable_return=Decimal(0))
    with pytest.raises(TypeError, match=_SOURCE_MSG):
        TefasFundDownsideDeviation(source=sub, minimum_acceptable_return=Decimal(0), downside_deviation=Decimal(0))


# --- explicit MAR ------------------------------------------------------------------------

@pytest.mark.parametrize("mar", ["-1", "-0.5", "0", "0.05", "1E+3", "-1.0"])
def test_valid_mar(mar: str) -> None:
    assert _dd(_rolling_from_returns(["0.1"]), mar).minimum_acceptable_return == Decimal(mar)


@pytest.mark.parametrize("mar", [0.0, 0, 1, True, "0", None, b"0", _DecimalSub("0"), _Hostile()])
def test_invalid_mar_types(mar) -> None:
    with pytest.raises(TypeError, match=_MAR_TYPE_MSG):
        calculate_tefas_fund_downside_deviation(rolling_series=_rolling_from_returns(["0.1"]),
                                                minimum_acceptable_return=mar)  # type: ignore[arg-type]


@pytest.mark.parametrize("mar", ["-1.0000000000000000000000000000000000000000000000001", "-2", "NaN", "sNaN",
                                 "Infinity", "-Infinity"])
def test_invalid_mar_values(mar: str) -> None:
    with pytest.raises(ValueError, match=_MAR_VALUE_MSG):
        _dd(_rolling_from_returns(["0.1"]), mar)


def test_mar_is_mandatory_and_has_no_hidden_default() -> None:
    import inspect
    params = inspect.signature(calculate_tefas_fund_downside_deviation).parameters
    assert params["minimum_acceptable_return"].default is inspect.Parameter.empty
    assert params["minimum_acceptable_return"].kind is inspect.Parameter.KEYWORD_ONLY


# --- availability ------------------------------------------------------------------------

@pytest.mark.parametrize("horizon", [H12, H36, H60])
def test_empty_history_is_unavailable_none_not_zero(horizon) -> None:
    rolling = _empty(horizon)
    assert rolling.points == ()
    result = _dd(rolling, "0")
    assert result.downside_deviation is None
    assert result.is_available is False
    assert result.observation_count == 0 and result.shortfall_count == 0
    assert result.source.horizon is horizon


def test_non_empty_no_shortfall_is_observed_zero_distinct_from_none() -> None:
    result = _dd(_rolling_from_returns(["0.10", "0.20"]), "0")
    assert result.downside_deviation == Decimal("0") and result.downside_deviation is not None
    assert type(result.downside_deviation) is Decimal
    assert result.is_available is True and result.shortfall_count == 0 and result.observation_count == 2
    assert _dd(_empty(), "0").downside_deviation is None


# --- formula / denominator ---------------------------------------------------------------

def test_one_downside_window_uses_all_windows_in_the_denominator() -> None:
    result = _dd(_rolling_from_returns(["-0.10", "0.20"]), "0")
    assert result.downside_deviation == Decimal("0.070710678118654752440084436210484903928483593768847")
    assert result.downside_deviation == _ctx().sqrt(_ctx().divide(Decimal("0.01"), Decimal(2)))
    assert result.downside_deviation != Decimal("0.10")  # not "downside-only" N
    assert result.shortfall_count == 1 and result.observation_count == 2


def test_both_downside_windows() -> None:
    result = _dd(_rolling_from_returns(["-0.10", "-0.20"]), "0")
    assert result.downside_deviation == Decimal("0.15811388300841896659994467722163592668597775696626")
    assert result.shortfall_count == 2


def test_positive_hurdle_is_applied_to_every_term() -> None:
    result = _dd(_rolling_from_returns(["0.10", "0.00"]), "0.05")
    assert result.downside_deviation == Decimal("0.035355339059327376220042218105242451964241796884424")
    assert result.shortfall_count == 1


def test_return_equal_to_mar_contributes_zero_and_is_not_a_shortfall() -> None:
    result = _dd(_rolling_from_returns(["0.05", "0.05"]), "0.05")
    assert result.downside_deviation == Decimal("0") and result.shortfall_count == 0
    mixed = _dd(_rolling_from_returns(["0.05", "0.00"]), "0.05")
    assert mixed.shortfall_count == 1
    assert mixed.downside_deviation == _ctx().sqrt(_ctx().divide(Decimal("0.0025"), Decimal(2)))


def test_no_sample_correction() -> None:
    three = _dd(_rolling_from_returns(["-0.10", "0.05", "0.20"]), "0")
    assert three.downside_deviation == _ctx().sqrt(_ctx().divide(Decimal("0.01"), Decimal(3)))
    assert three.downside_deviation != _ctx().sqrt(_ctx().divide(Decimal("0.01"), Decimal(2)))


def test_many_windows_average_over_all_windows() -> None:
    returns = ["-0.10"] + ["0.30"] * 11
    result = _dd(_rolling_from_returns(returns), "0")
    assert result.observation_count == 12
    assert result.downside_deviation == _ctx().sqrt(_ctx().divide(Decimal("0.01"), Decimal(12)))


def test_extreme_minus_one_return_and_mar_boundary() -> None:
    rolling = build_tefas_fund_rolling_return_series(
        price_series=_price_series(["1"] * 12 + ["1E-1000000"]), horizon=H12)
    assert rolling.points[0].simple_return == Decimal("-1")
    at_zero = _dd(rolling, "0")
    assert at_zero.downside_deviation == Decimal("1") and at_zero.shortfall_count == 1
    at_floor = _dd(rolling, "-1")
    assert at_floor.downside_deviation == Decimal("0") and at_floor.shortfall_count == 0


def test_mar_minus_one_with_ordinary_returns_has_no_shortfall() -> None:
    result = _dd(_rolling_from_returns(["-0.9", "0", "1"]), "-1")
    assert result.downside_deviation == Decimal("0") and result.shortfall_count == 0


# --- horizons ----------------------------------------------------------------------------

@pytest.mark.parametrize("horizon,months", [(H12, 12), (H36, 36), (H60, 60)])
def test_horizon_reaches_result_only_through_the_source(horizon, months) -> None:
    rolling = build_tefas_fund_rolling_return_series(
        price_series=_price_series([str(100 + i) for i in range(months + 3)]), horizon=horizon)
    result = _dd(rolling, "1")
    assert result.source is rolling and result.source.horizon is horizon
    assert result.observation_count == 3
    assert not hasattr(result, "horizon")
    assert result.downside_deviation is not None and result.downside_deviation > 0  # all returns < 100% hurdle
    assert result.shortfall_count == 3


# --- decimal context / range ---------------------------------------------------------------

def test_decimal_context_is_explicit_and_maximal() -> None:
    ctx = module_under_test._downside_context()
    assert (ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax) == (50, decimal.ROUND_HALF_EVEN, decimal.MIN_EMIN,
                                                            decimal.MAX_EMAX)
    assert module_under_test._DOWNSIDE_DECIMAL_PRECISION == 50


def test_repeating_result_is_deterministic_fifty_digit() -> None:
    result = _dd(_rolling_from_returns(["-0.05", "0.20", "0.10"]), "0")
    assert result.downside_deviation == Decimal("0.064549722436790281419654423329706660180548695088193") or \
        result.downside_deviation == _ctx().sqrt(_ctx().divide(Decimal("0.0025"), Decimal(3)))
    assert len(result.downside_deviation.as_tuple().digits) <= 50


def test_out_of_range_result_fails_closed_with_static_error() -> None:
    huge_mar = Decimal(f"1E+{decimal.MAX_EMAX}")
    with pytest.raises(ValueError, match=_RANGE_MSG) as info:
        _dd(_rolling_from_returns(["0.10"]), str(huge_mar))
    assert info.value.__cause__ is None and info.value.__suppress_context__ is True
    with pytest.raises(ValueError, match=_RANGE_MSG):
        TefasFundDownsideDeviation(source=_rolling_from_returns(["0.10"]), minimum_acceptable_return=huge_mar,
                                   downside_deviation=Decimal(1))


def test_empty_source_never_hits_arithmetic_even_with_extreme_mar() -> None:
    huge_mar = Decimal(f"1E+{decimal.MAX_EMAX}")
    assert _dd(_empty(), str(huge_mar)).downside_deviation is None


def test_range_handler_does_not_swallow_programming_errors(monkeypatch) -> None:
    def boom():
        raise KeyError("unrelated")
    monkeypatch.setattr(module_under_test, "_downside_context", boom)
    with pytest.raises(KeyError):
        _dd(_rolling_from_returns(["-0.1"]))


def test_result_independent_of_ambient_context_and_does_not_mutate_it() -> None:
    rolling = _rolling_from_returns(["-0.13", "0.07", "-0.29", "0.31", "-0.02"])
    baseline = _dd(rolling, "0.01")
    for prec, rounding in ((4, decimal.ROUND_DOWN), (2, decimal.ROUND_UP), (9, decimal.ROUND_HALF_UP)):
        with decimal.localcontext() as ctx:
            ctx.prec = prec
            ctx.rounding = rounding
            perturbed = _dd(rolling, "0.01")
            assert perturbed.downside_deviation == baseline.downside_deviation
            assert ctx.prec == prec and ctx.rounding == rounding
    before = (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))
    _dd(rolling, "0.01")
    assert before == (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))


# --- self-validation / forgery ---------------------------------------------------------------

def _forge(source, dd, mar="0") -> TefasFundDownsideDeviation:
    return TefasFundDownsideDeviation(source=source, minimum_acceptable_return=Decimal(mar), downside_deviation=dd)


def test_direct_construction_with_canonical_value_succeeds() -> None:
    rolling = _rolling_from_returns(["-0.10", "0.20"])
    good = _dd(rolling)
    assert _forge(rolling, good.downside_deviation) == good


def test_forged_values_are_rejected() -> None:
    rolling = _rolling_from_returns(["-0.10", "0.20"])
    for bad in (Decimal("0.10"), Decimal("0"), Decimal("0.0707106781186547524400844362104849039284835938"),
                None):
        with pytest.raises(ValueError, match=_MATCH_MSG):
            _forge(rolling, bad)


def test_forged_none_when_observations_exist_and_zero_when_unavailable() -> None:
    with pytest.raises(ValueError, match=_MATCH_MSG):
        _forge(_rolling_from_returns(["-0.1"]), None)
    with pytest.raises(ValueError, match=_MATCH_MSG):
        _forge(_empty(), Decimal("0"))
    assert _forge(_empty(), None).downside_deviation is None


def test_forged_mar_is_rejected() -> None:
    rolling = _rolling_from_returns(["-0.10", "0.20"])
    correct_for_zero = _dd(rolling, "0").downside_deviation
    with pytest.raises(ValueError, match=_MATCH_MSG):
        _forge(rolling, correct_for_zero, mar="0.05")


@pytest.mark.parametrize("value", [0.1, 0, 1, True, "0", _DecimalSub("0.1"), _Hostile()])
def test_downside_deviation_type_validation(value) -> None:
    with pytest.raises(TypeError, match=_DD_TYPE_MSG):
        _forge(_rolling_from_returns(["-0.1"]), value)


@pytest.mark.parametrize("value", ["-0.0001", "NaN", "Infinity", "-Infinity"])
def test_downside_deviation_value_validation(value: str) -> None:
    with pytest.raises(ValueError, match=_DD_VALUE_MSG):
        _forge(_rolling_from_returns(["-0.1"]), Decimal(value))


def test_constructor_errors_are_static() -> None:
    with pytest.raises(TypeError) as info:
        _forge(_rolling_from_returns(["-0.1"]), _Hostile())
    assert str(info.value) == "downside_deviation must be None or an exact Decimal instance"


# --- derived counts --------------------------------------------------------------------------

def test_counts_are_derived_not_stored() -> None:
    result = _dd(_rolling_from_returns(["-0.1", "0.0", "0.2", "-0.3"]), "0")
    assert result.observation_count == 4 and result.shortfall_count == 2
    assert not any(f.name in ("observation_count", "shortfall_count") for f in dataclasses.fields(result))
    assert isinstance(result.observation_count, int) and isinstance(result.shortfall_count, int)


# --- purity / scope ----------------------------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_imports_are_pure_and_use_only_the_rolling_dependency() -> None:
    imports = _imports()
    for banned in ("pandas", "numpy", "polars", "scipy", "requests", "httpx", "os", "pathlib", "random",
                   "secrets", "hashlib", "hmac", "socket", "sqlite3", "math", "statistics"):
        assert banned not in imports
    assert not any(m.startswith("backend.engine.private.market_data") for m in imports)
    assert "backend.engine.private.fund_rolling_returns" in imports
    assert "backend.engine.private.fund_return_series" not in imports


def test_no_out_of_scope_metric_surface() -> None:
    for name in ("sortino", "sharpe", "calmar", "cvar", "mean_return", "excess_return", "annualize", "cagr",
                 "percentile", "persistence", "rank", "score", "infer_mar", "goal_mar", "standard_error"):
        assert not hasattr(module_under_test, name)


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_the_rolling_dependency() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/fund_downside_deviation.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert sg.scan_g1(source, rel) == []
    assert sg.scan_g2(source, rel) == []
    assert sg.scan_g4(source, rel) == []
    assert sg.scan_g5(source, rel) == []
    assert {v.node_kind for v in sg.scan_g3(source, rel)} == {"PrivateImport:backend.engine.private.fund_rolling_returns"}
    assert rel not in sg.PURE_MANIFEST
