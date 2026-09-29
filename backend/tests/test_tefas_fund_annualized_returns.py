"""
backend/tests/test_tefas_fund_annualized_returns.py
===================================================
Tests for annualized TEFAS rolling returns and the Calmar diagnostic (Phase 16F).

annualized = (1 + R) ** (12 / H) - 1 with H in {12, 36, 60} months (calendar horizon, no day count), computed
with deterministic Decimal roots (no float, no math.pow). Calmar = latest annualized rolling return / max
drawdown, over the SAME authoritative price series; None when history is empty or the drawdown is zero.
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

from backend.engine.private import fund_annualized_returns as module_under_test
from backend.engine.private.domain import Currency, DataConfidenceLevel, Horizon
from backend.engine.private.fund_annualized_returns import (
    TefasFundAnnualizedRollingReturnSeries,
    TefasFundAnnualizedReturnPoint,
    TefasFundCalmar,
    annualize_tefas_fund_rolling_returns,
    calculate_tefas_fund_calmar,
)
from backend.engine.private.fund_drawdown import TefasFundDrawdown, calculate_tefas_fund_drawdown
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
_POINTS_TYPE_MSG = r"^points must be a tuple of exact TefasFundAnnualizedReturnPoint instances$"
_MATCH_MSG = r"^points must match the canonical annualization of source exactly$"
_RANGE_MSG = r"^TEFAS annualized return exceeds supported Decimal analytics range$"
_CONVERGE_MSG = r"^TEFAS annualized return root calculation did not converge$"
_CALMAR_RANGE_MSG = r"^TEFAS Calmar ratio exceeds supported Decimal analytics range$"
_SAME_SOURCE_MSG = r"^Calmar inputs must share the same authoritative price source$"
_CALMAR_MATCH_MSG = r"^Calmar ratio must match the canonical calculation from its inputs$"


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


def _rolling(prices, horizon=H12) -> TefasFundRollingReturnSeries:
    return build_tefas_fund_rolling_return_series(price_series=_price_series(prices), horizon=horizon)


def _rolling_single(horizon: Horizon, cumulative: str) -> TefasFundRollingReturnSeries:
    """One window with exactly the requested cumulative return."""
    months = horizon.months
    prices = ["100"] * months + [str(Decimal("100") * (Decimal(1) + Decimal(cumulative)))]
    return _rolling(prices, horizon)


def _ann(rolling) -> TefasFundAnnualizedRollingReturnSeries:
    return annualize_tefas_fund_rolling_returns(rolling_series=rolling)


class _DecimalSub(Decimal):
    pass


class _Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")


# --- point ------------------------------------------------------------------------------

def test_point_fields_frozen_and_validation() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundAnnualizedReturnPoint)] == [
        "start_date", "end_date", "annualized_return"]
    point = TefasFundAnnualizedReturnPoint(date(2025, 1, 1), date(2026, 1, 1), Decimal("0.1"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.annualized_return = Decimal(0)  # type: ignore[misc]
    for ret in ("-1", "-1.0", "0", "5", "1E+3"):
        assert TefasFundAnnualizedReturnPoint(date(2025, 1, 1), date(2026, 1, 1), Decimal(ret))
    for ret in ("-1.0000000000000000000000000000000000000000000000001", "-2", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            TefasFundAnnualizedReturnPoint(date(2025, 1, 1), date(2026, 1, 1), Decimal(ret))
    for ret in (0.1, 0, True, "0.1", None, _DecimalSub("0.1"), _Hostile()):
        with pytest.raises(TypeError):
            TefasFundAnnualizedReturnPoint(date(2025, 1, 1), date(2026, 1, 1), ret)  # type: ignore[arg-type]
    for bad in (datetime(2025, 1, 1), "2025-01-01", None):
        with pytest.raises(TypeError):
            TefasFundAnnualizedReturnPoint(bad, date(2026, 1, 1), Decimal(0))  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            TefasFundAnnualizedReturnPoint(date(2025, 1, 1), bad, Decimal(0))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TefasFundAnnualizedReturnPoint(date(2026, 1, 1), date(2025, 1, 1), Decimal(0))
    point = TefasFundAnnualizedReturnPoint(date(2025, 1, 1), date(2026, 1, 1), Decimal("0.1"))
    for attr in ("horizon", "instrument_id", "currency", "cumulative_return", "simple_return"):
        assert not hasattr(point, attr)


# --- 12M identity ---------------------------------------------------------------------------

@pytest.mark.parametrize("cumulative", ["0.10", "-0.25", "0", "0.5", "3"])
def test_twelve_month_annualization_is_exact_identity(cumulative: str) -> None:
    rolling = _rolling_single(H12, cumulative)
    result = _ann(rolling)
    assert result.points[0].annualized_return == rolling.points[0].simple_return
    assert result.points[0].annualized_return == Decimal(cumulative)


def test_twelve_month_identity_for_rounded_minus_one() -> None:
    rolling = _rolling(["1"] * 12 + ["1E-1000000"])
    assert rolling.points[0].simple_return == Decimal("-1")
    assert _ann(rolling).points[0].annualized_return == Decimal("-1")


# --- 36M / 60M -----------------------------------------------------------------------------------

@pytest.mark.parametrize("horizon,cumulative,expected", [
    (H36, "0.331", "0.1"),        # 1.1 ** 3 = 1.331
    (H36, "-0.488", "-0.2"),      # 0.8 ** 3 = 0.512
    (H60, "0.61051", "0.1"),      # 1.1 ** 5 = 1.61051
    (H60, "-0.67232", "-0.2"),    # 0.8 ** 5 = 0.32768
    (H36, "0", "0"),
    (H60, "0", "0"),
])
def test_long_horizon_annualization_exact_roots(horizon, cumulative, expected) -> None:
    rolling = _rolling_single(horizon, cumulative)
    point = _ann(rolling).points[0]
    assert point.annualized_return == Decimal(expected)
    assert type(point.annualized_return) is Decimal
    assert (point.start_date, point.end_date) == (rolling.points[0].start_date, rolling.points[0].end_date)


@pytest.mark.parametrize("horizon", [H36, H60])
def test_total_loss_annualizes_to_minus_one(horizon) -> None:
    rolling = _rolling(["1"] * horizon.months + ["1E-1000000"], horizon)
    assert rolling.points[0].simple_return == Decimal("-1")
    assert _ann(rolling).points[0].annualized_return == Decimal("-1")


def test_irrational_root_is_deterministic_fifty_digit_and_ordered() -> None:
    result = _ann(_rolling_single(H36, "1"))  # 2 ** (1/3) - 1
    value = result.points[0].annualized_return
    assert Decimal("0.259921049894873164767210607278228350570251464701507") - value in (
        Decimal(0),) or abs(value - Decimal("0.25992104989487316476721060727822835057025146470151")) < Decimal("1E-48")
    assert len(value.as_tuple().digits) <= 50
    assert _ann(_rolling_single(H36, "1")).points[0].annualized_return == value
    assert _ann(_rolling_single(H36, "0.5")).points[0].annualized_return < value


def test_fifth_root_of_two() -> None:
    value = _ann(_rolling_single(H60, "1")).points[0].annualized_return
    assert abs(value - Decimal("0.148698354997035006798626946777927589")) < Decimal("1E-30")


@pytest.mark.parametrize("horizon,years", [(H12, 1), (H36, 3), (H60, 5)])
@pytest.mark.parametrize("cumulative", ["0.331", "-0.488", "0.61051", "1", "-0.5", "2.5", "0.07"])
def test_compounding_consistency_in_decimal_arithmetic(horizon, years, cumulative) -> None:
    rolling = _rolling_single(horizon, cumulative)
    a = _ann(rolling).points[0].annualized_return
    ctx = _ctx()
    rebuilt = ctx.power(ctx.add(Decimal(1), a), years)
    target = ctx.add(Decimal(1), rolling.points[0].simple_return)
    assert abs(rebuilt - target) <= Decimal("1E-45") * max(Decimal(1), target)


def test_no_day_count_annualization_actual_dates_are_ignored() -> None:
    a = _rolling(["100"] * 36 + ["133.1"], H36)
    dated = [(date(*_month(i), 3), "100") for i in range(36)] + [(date(*_month(36), 27), "133.1")]
    b = build_tefas_fund_rolling_return_series(
        price_series=TefasFundPriceSeries(
            instrument_id=_INST, mode=CR, as_of=None, requested_dates=tuple(d for d, _ in dated),
            points=tuple(_p(d, x) for d, x in dated), gaps=()), horizon=H36)
    assert _ann(a).points[0].annualized_return == _ann(b).points[0].annualized_return == Decimal("0.1")


# --- series structure / empty / mapping -------------------------------------------------------------

def test_series_fields_frozen_identity_and_mapping() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundAnnualizedRollingReturnSeries)] == ["source", "points"]
    rolling = _rolling([str(100 + i) for i in range(15)])
    result = _ann(rolling)
    assert result.source is rolling and not hasattr(result, "horizon")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.points = ()  # type: ignore[misc]
    assert len(result.points) == len(rolling.points) == 3
    for src, out in zip(rolling.points, result.points):
        assert (out.start_date, out.end_date, out.annualized_return) == (
            src.start_date, src.end_date, src.simple_return)
    assert result.is_available is True


@pytest.mark.parametrize("horizon", [H12, H36, H60])
def test_empty_rolling_series_gives_valid_empty_annualized_series(horizon) -> None:
    rolling = _rolling([str(100 + i) for i in range(horizon.months)], horizon)
    assert rolling.points == ()
    result = _ann(rolling)
    assert result.points == () and result.is_available is False and result.source is rolling


@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile(), (Decimal("0.1"),)])
def test_builder_rejects_non_rolling_series(bad) -> None:
    with pytest.raises(TypeError, match=_SOURCE_MSG):
        annualize_tefas_fund_rolling_returns(rolling_series=bad)  # type: ignore[arg-type]


def test_builder_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        annualize_tefas_fund_rolling_returns(_rolling_single(H12, "0.1"))  # type: ignore[misc]


def test_source_subclass_rejected() -> None:
    class _Sub(TefasFundRollingReturnSeries):
        pass
    base = _rolling_single(H12, "0.1")
    sub = _Sub(**{f.name: getattr(base, f.name) for f in dataclasses.fields(base)})
    with pytest.raises(TypeError, match=_SOURCE_MSG):
        annualize_tefas_fund_rolling_returns(rolling_series=sub)
    with pytest.raises(TypeError, match=_SOURCE_MSG):
        TefasFundAnnualizedRollingReturnSeries(source=sub, points=())


# --- constructor forgery protection -------------------------------------------------------------------

def test_direct_construction_and_forgeries() -> None:
    rolling = _rolling([str(100 + 3 * i) for i in range(15)], H12)
    good = _ann(rolling)
    assert TefasFundAnnualizedRollingReturnSeries(source=rolling, points=good.points) == good
    p0, p1, p2 = good.points
    forged = [
        (dataclasses.replace(p0, start_date=date(2025, 1, 1)), p1, p2),
        (dataclasses.replace(p0, end_date=date(2026, 1, 1)), p1, p2),
        (dataclasses.replace(p0, annualized_return=p0.annualized_return + Decimal("0.0001")), p1, p2),
        (p0, p1), (p0, p1, p2, p2), (p1, p0, p2), (),
    ]
    for points in forged:
        with pytest.raises(ValueError, match=_MATCH_MSG):
            TefasFundAnnualizedRollingReturnSeries(source=rolling, points=points)


def test_forged_annualization_of_long_horizon_is_rejected() -> None:
    rolling = _rolling_single(H36, "0.331")
    p = _ann(rolling).points[0]
    forged = dataclasses.replace(p, annualized_return=rolling.points[0].simple_return)  # skipped the root
    with pytest.raises(ValueError, match=_MATCH_MSG):
        TefasFundAnnualizedRollingReturnSeries(source=rolling, points=(forged,))


@pytest.mark.parametrize("bad", [[], None, "x", [object()], (object(),)])
def test_constructor_rejects_bad_points_container(bad) -> None:
    with pytest.raises(TypeError, match=_POINTS_TYPE_MSG):
        TefasFundAnnualizedRollingReturnSeries(source=_rolling_single(H12, "0.1"), points=bad)  # type: ignore[arg-type]


def test_constructor_rejects_point_subclass() -> None:
    class _Sub(TefasFundAnnualizedReturnPoint):
        pass
    rolling = _rolling_single(H12, "0.1")
    good = _ann(rolling).points[0]
    sub = _Sub(good.start_date, good.end_date, good.annualized_return)
    with pytest.raises(TypeError, match=_POINTS_TYPE_MSG):
        TefasFundAnnualizedRollingReturnSeries(source=rolling, points=(sub,))


def test_constructor_errors_are_static() -> None:
    with pytest.raises(TypeError) as info:
        TefasFundAnnualizedRollingReturnSeries(source=_rolling_single(H12, "0.1"), points=(_Hostile(),))  # type: ignore[arg-type]
    assert str(info.value) == "points must be a tuple of exact TefasFundAnnualizedReturnPoint instances"


# --- root arithmetic / range / convergence ------------------------------------------------------------------

@pytest.mark.parametrize("n,value,expected", [
    (3, "0", "0"), (3, "1", "1"), (3, "8", "2"), (3, "0.125", "0.5"), (3, "27", "3"),
    (5, "0", "0"), (5, "1", "1"), (5, "32", "2"), (5, "0.03125", "0.5"), (5, "243", "3"),
])
def test_nth_root_exact_cases(n, value, expected) -> None:
    assert module_under_test._nth_root(Decimal(value), n) == Decimal(expected)


@pytest.mark.parametrize("n", [0, 1, 2, 4, 6, -3, True, 3.0, "3", None])
def test_nth_root_supports_only_three_and_five(n) -> None:
    with pytest.raises((ValueError, TypeError)):
        module_under_test._nth_root(Decimal("8"), n)  # type: ignore[arg-type]


@pytest.mark.parametrize("n", [3, 5])
@pytest.mark.parametrize("value", ["1E-30", "0.001", "0.999999", "1.000001", "7", "123456.789", "1E+40", "1E+300",
                                   "1E-300"])
def test_nth_root_round_trips_within_fifty_digits(n, value) -> None:
    v = Decimal(value)
    root = module_under_test._nth_root(v, n)
    assert root > 0
    back = _ctx().power(root, n)
    assert abs(back - v) <= Decimal("1E-45") * v


def test_nth_root_handles_extreme_magnitudes_without_iteration_blowup() -> None:
    for value in (f"1E+{decimal.MAX_EMAX}", f"1E-{decimal.MAX_EMAX}", "1E+1000000", "1E-1000000"):
        for n in (3, 5):
            assert module_under_test._nth_root(Decimal(value), n) > 0


def test_root_does_not_converge_error_is_static(monkeypatch) -> None:
    monkeypatch.setattr(module_under_test, "_MAX_ROOT_ITERATIONS", 1)
    with pytest.raises(ValueError, match=_CONVERGE_MSG):
        _ann(_rolling_single(H36, "0.5"))


def test_range_error_translates_only_overflow(monkeypatch) -> None:
    rolling = _rolling(["1"] * 36 + ["1E+10"], H36)
    small = decimal.Context(prec=50, Emin=-5, Emax=5, traps=[decimal.InvalidOperation, decimal.DivisionByZero,
                                                            decimal.Overflow])
    monkeypatch.setattr(module_under_test, "_annualization_context", lambda: small)
    with pytest.raises(ValueError, match=_RANGE_MSG) as info:
        _ann(rolling)
    assert info.value.__cause__ is None and info.value.__suppress_context__ is True


def test_unrelated_errors_are_not_swallowed(monkeypatch) -> None:
    def boom():
        raise KeyError("unrelated")
    monkeypatch.setattr(module_under_test, "_annualization_context", boom)
    with pytest.raises(KeyError):
        _ann(_rolling_single(H36, "0.5"))


def test_contexts_are_explicit() -> None:
    ctx = module_under_test._annualization_context()
    assert (ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax) == (50, decimal.ROUND_HALF_EVEN, decimal.MIN_EMIN,
                                                            decimal.MAX_EMAX)
    guard = module_under_test._root_guard_context()
    assert (guard.prec, guard.rounding, guard.Emin, guard.Emax) == (60, decimal.ROUND_HALF_EVEN,
                                                                     decimal.MIN_EMIN, decimal.MAX_EMAX)
    assert module_under_test._MAX_ROOT_ITERATIONS == 256


def test_annualization_independent_of_ambient_context() -> None:
    rolling = _rolling([str(100 + 7 * i) for i in range(39)], H36)
    baseline = _ann(rolling).points
    for prec, rounding in ((4, decimal.ROUND_DOWN), (2, decimal.ROUND_UP), (9, decimal.ROUND_HALF_UP)):
        with decimal.localcontext() as ctx:
            ctx.prec = prec
            ctx.rounding = rounding
            assert _ann(rolling).points == baseline
            assert ctx.prec == prec and ctx.rounding == rounding
    before = (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))
    _ann(rolling)
    assert before == (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))


# --- Calmar ---------------------------------------------------------------------------------------------------

def _calmar_case(final: str, second: str = "125", third: str = "100"):
    """13 monthly prices; the only 12M window is final/100 - 1; max drawdown from the 125 -> 100 dip."""
    prices = ["100", second, third] + ["100"] * 9 + [final]
    rolling = _rolling(prices)
    drawdown = calculate_tefas_fund_drawdown(price_series=rolling.source)
    return _ann(rolling), drawdown


def _calmar(ann, dd) -> TefasFundCalmar:
    return calculate_tefas_fund_calmar(annualized_series=ann, drawdown=dd)


def test_calmar_fields_frozen_and_no_copied_metadata() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundCalmar)] == ["annualized_source", "drawdown", "calmar_ratio"]
    ann, dd = _calmar_case("110")
    result = _calmar(ann, dd)
    assert result.annualized_source is ann and result.drawdown is dd
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.calmar_ratio = Decimal(0)  # type: ignore[misc]
    for attr in ("horizon", "instrument_id", "currency", "sortino", "sharpe", "score", "rank", "limit"):
        assert not hasattr(result, attr)


def test_calmar_positive() -> None:
    ann, dd = _calmar_case("110")
    assert ann.points[-1].annualized_return == Decimal("0.1") and dd.max_drawdown == Decimal("0.2")
    assert _calmar(ann, dd).calmar_ratio == Decimal("0.5")


def test_calmar_negative() -> None:
    prices = ["100", "112.5"] + ["100"] * 10 + ["90"]
    rolling = _rolling(prices)
    dd = calculate_tefas_fund_drawdown(price_series=rolling.source)
    ann = _ann(rolling)
    assert ann.points[-1].annualized_return == Decimal("-0.1") and dd.max_drawdown == Decimal("0.2")
    assert _calmar(ann, dd).calmar_ratio == Decimal("-0.5")


def test_calmar_zero_return_is_zero_not_none() -> None:
    ann, dd = _calmar_case("100")
    result = _calmar(ann, dd)
    assert result.calmar_ratio == Decimal("0") and result.calmar_ratio is not None


def test_calmar_zero_drawdown_is_none() -> None:
    rolling = _rolling(["100"] * 12 + ["110"])
    dd = calculate_tefas_fund_drawdown(price_series=rolling.source)
    assert dd.max_drawdown == Decimal("0")
    assert _calmar(_ann(rolling), dd).calmar_ratio is None


def test_calmar_without_annualized_history_is_none() -> None:
    rolling = _rolling([str(100 + i) for i in range(12)])
    assert rolling.points == ()
    dd = calculate_tefas_fund_drawdown(price_series=rolling.source)
    assert _calmar(_ann(rolling), dd).calmar_ratio is None


def test_calmar_uses_only_the_most_recent_annualized_point() -> None:
    prices = ["100", "125", "100"] + ["100"] * 9 + ["110", "150"]
    rolling = _rolling(prices)
    ann = _ann(rolling)
    dd = calculate_tefas_fund_drawdown(price_series=rolling.source)
    assert [p.annualized_return for p in ann.points] == [Decimal("0.1"), Decimal("0.2")]
    assert dd.max_drawdown == Decimal("0.2")
    assert _calmar(ann, dd).calmar_ratio == Decimal("1")  # latest 0.2 / 0.2, not the 0.1 or an average


def test_calmar_long_horizon_uses_annualized_not_cumulative() -> None:
    prices = ["100", "125", "100"] + ["100"] * 33 + ["133.1"]
    rolling = _rolling(prices, H36)
    dd = calculate_tefas_fund_drawdown(price_series=rolling.source)
    ann = _ann(rolling)
    assert ann.points[-1].annualized_return == Decimal("0.1") and dd.max_drawdown == Decimal("0.2")
    assert _calmar(ann, dd).calmar_ratio == Decimal("0.5")


def test_calmar_requires_the_same_price_source_object() -> None:
    ann, dd = _calmar_case("110")
    _, other_dd = _calmar_case("110")  # equal content, different object
    assert other_dd.source == dd.source and other_dd.source is not dd.source
    with pytest.raises(ValueError, match=_SAME_SOURCE_MSG):
        _calmar(ann, other_dd)
    with pytest.raises(ValueError, match=_SAME_SOURCE_MSG):
        TefasFundCalmar(annualized_source=ann, drawdown=other_dd, calmar_ratio=Decimal("0.5"))


@pytest.mark.parametrize("bad", [None, "x", 1, object(), _Hostile()])
def test_calmar_type_validation(bad) -> None:
    ann, dd = _calmar_case("110")
    with pytest.raises(TypeError):
        calculate_tefas_fund_calmar(annualized_series=bad, drawdown=dd)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        calculate_tefas_fund_calmar(annualized_series=ann, drawdown=bad)  # type: ignore[arg-type]


def test_calmar_builder_is_keyword_only() -> None:
    ann, dd = _calmar_case("110")
    with pytest.raises(TypeError):
        calculate_tefas_fund_calmar(ann, dd)  # type: ignore[misc]


def test_calmar_forgery_and_type_validation() -> None:
    ann, dd = _calmar_case("110")
    good = _calmar(ann, dd)
    assert TefasFundCalmar(annualized_source=ann, drawdown=dd, calmar_ratio=good.calmar_ratio) == good
    for bad in (Decimal("0.4"), Decimal("0"), Decimal("-0.5"), None):
        with pytest.raises(ValueError, match=_CALMAR_MATCH_MSG):
            TefasFundCalmar(annualized_source=ann, drawdown=dd, calmar_ratio=bad)
    for bad in (0.5, 1, True, "0.5", _DecimalSub("0.5"), _Hostile()):
        with pytest.raises(TypeError):
            TefasFundCalmar(annualized_source=ann, drawdown=dd, calmar_ratio=bad)  # type: ignore[arg-type]
    for bad in (Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            TefasFundCalmar(annualized_source=ann, drawdown=dd, calmar_ratio=bad)


def test_calmar_forged_zero_when_undefined_or_none_when_defined() -> None:
    rolling = _rolling(["100"] * 12 + ["110"])
    dd = calculate_tefas_fund_drawdown(price_series=rolling.source)
    ann = _ann(rolling)
    with pytest.raises(ValueError, match=_CALMAR_MATCH_MSG):
        TefasFundCalmar(annualized_source=ann, drawdown=dd, calmar_ratio=Decimal("0"))
    assert TefasFundCalmar(annualized_source=ann, drawdown=dd, calmar_ratio=None).calmar_ratio is None


def test_calmar_subclass_inputs_rejected() -> None:
    ann, dd = _calmar_case("110")

    class _DD(TefasFundDrawdown):
        pass
    sub = _DD(**{f.name: getattr(dd, f.name) for f in dataclasses.fields(dd)})
    with pytest.raises(TypeError):
        _calmar(ann, sub)


def test_calmar_range_error_and_ambient_independence(monkeypatch) -> None:
    prices = ["1"] * 12 + ["1E+10", "5E+9"]
    rolling = _rolling(prices)
    dd = calculate_tefas_fund_drawdown(price_series=rolling.source)
    ann = _ann(rolling)
    baseline = _calmar(ann, dd)
    for prec, rounding in ((4, decimal.ROUND_DOWN), (2, decimal.ROUND_UP)):
        with decimal.localcontext() as ctx:
            ctx.prec = prec
            ctx.rounding = rounding
            assert _calmar(ann, dd).calmar_ratio == baseline.calmar_ratio
    small = decimal.Context(prec=50, Emin=-5, Emax=5, traps=[decimal.InvalidOperation, decimal.DivisionByZero,
                                                            decimal.Overflow])
    monkeypatch.setattr(module_under_test, "_annualization_context", lambda: small)
    with pytest.raises(ValueError, match=_CALMAR_RANGE_MSG) as info:
        _calmar(ann, dd)
    assert info.value.__cause__ is None and info.value.__suppress_context__ is True


def test_calmar_is_a_diagnostic_not_a_limit_or_score() -> None:
    for name in ("sortino", "sharpe", "score", "rank", "limit", "signal", "buy", "sell", "threshold"):
        assert not hasattr(module_under_test, name)


# --- purity / scope ---------------------------------------------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_imports_are_pure_and_do_not_use_float_or_math_pow() -> None:
    imports = _imports()
    for banned in ("pandas", "numpy", "polars", "scipy", "requests", "httpx", "os", "pathlib", "random", "secrets",
                   "hashlib", "hmac", "socket", "sqlite3", "math", "statistics"):
        assert banned not in imports
    assert not any(m.startswith("backend.engine.private.market_data") for m in imports)
    source = Path(module_under_test.__file__).read_text(encoding="utf-8")
    assert "math.pow" not in source and "float(" not in source


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_phase_16_imports() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/fund_annualized_returns.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert sg.scan_g1(source, rel) == []
    assert sg.scan_g2(source, rel) == []
    assert sg.scan_g4(source, rel) == []
    assert sg.scan_g5(source, rel) == []
    assert {v.node_kind for v in sg.scan_g3(source, rel)} == {
        "PrivateImport:backend.engine.private.fund_drawdown",
        "PrivateImport:backend.engine.private.fund_rolling_returns",
    }
    assert rel not in sg.PURE_MANIFEST
