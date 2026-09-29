"""
backend/tests/test_tefas_fund_rolling_returns.py
================================================
Tests for the month-end rolling 12M / 36M / 60M TEFAS returns (Phase 16D).

A window compares the LAST authoritative observation of a calendar month with the last observation of the
month `horizon_months` earlier (calendar-month matching, no 365-day logic). Monthly continuity is required;
insufficient history yields an empty (valid) series. Return arithmetic is owned by Phase 16B.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import fund_rolling_returns as module_under_test
from backend.engine.private.domain import Currency, DataConfidenceLevel, Horizon
from backend.engine.private.fund_price_series import TefasFundPriceGap, TefasFundPricePoint, TefasFundPriceSeries
from backend.engine.private.fund_return_series import _simple_return
from backend.engine.private.fund_rolling_returns import (
    TefasFundRollingReturnPoint,
    TefasFundRollingReturnSeries,
    build_tefas_fund_rolling_return_series,
)
from backend.engine.private.market_data.models import MarketDataResolutionMode, MarketDataResolutionStatus

CR = MarketDataResolutionMode.CURRENT_REPORTED
SYS = MarketDataResolutionMode.SYSTEM_AS_OF
SRC = MarketDataResolutionMode.SOURCE_AS_OF
ST = MarketDataResolutionStatus
H12, H36, H60 = Horizon.ALLOCATION_12M, Horizon.STRATEGIC_3Y, Horizon.STRATEGIC_5Y

_INST = UUID("11111111-1111-4111-8111-111111111111")
_T0 = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)

_INCOMPLETE_MSG = r"^TEFAS rolling returns require a complete price series$"
_TWO_MSG = r"^TEFAS rolling returns require at least two price points$"
_CONTINUITY_MSG = r"^TEFAS rolling returns require continuous monthly price coverage$"
_SOURCE_MSG = r"^source must be an exact TefasFundPriceSeries instance$"
_HORIZON_TYPE_MSG = r"^horizon must be an exact Horizon instance$"
_HORIZON_VALUE_MSG = r"^horizon must be ALLOCATION_12M, STRATEGIC_3Y or STRATEGIC_5Y$"
_POINTS_TYPE_MSG = r"^points must be a tuple of exact TefasFundRollingReturnPoint instances$"
_MATCH_MSG = r"^points must match the canonical monthly rolling windows exactly$"
_RANGE_MSG = r"^TEFAS fund return exceeds supported Decimal analytics range$"


def _month(i: int, base=(2025, 1)) -> tuple[int, int]:
    index = base[0] * 12 + (base[1] - 1) + i
    return index // 12, index % 12 + 1


def _p(d: date, price: str) -> TefasFundPricePoint:
    return TefasFundPricePoint(trade_date=d, unit_price=Decimal(price), currency=Currency.TRY,
                               confidence=DataConfidenceLevel.HIGH, snapshot_retrieved_at=_T0,
                               resolution_key=f"key-{d.isoformat()}")


def _series(dated_prices, mode=CR, as_of=None) -> TefasFundPriceSeries:
    dates = tuple(d for d, _ in dated_prices)
    return TefasFundPriceSeries(instrument_id=_INST, mode=mode, as_of=as_of, requested_dates=dates,
                                points=tuple(_p(d, x) for d, x in dated_prices), gaps=())


def _monthly(prices, day: int = 28, base=(2025, 1), **kw) -> TefasFundPriceSeries:
    dated = []
    for i, price in enumerate(prices):
        y, m = _month(i, base)
        dated.append((date(y, m, day), price))
    return _series(dated, **kw)


def _linear(n: int, start: int = 100, step: int = 1, **kw) -> TefasFundPriceSeries:
    return _monthly([str(start + step * i) for i in range(n)], **kw)


def _roll(source, horizon=H12) -> TefasFundRollingReturnSeries:
    return build_tefas_fund_rolling_return_series(price_series=source, horizon=horizon)


class _Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")


class _ForeignEnum(Enum):
    ALLOCATION_12M = "12M"


# --- horizon contract ----------------------------------------------------------------

@pytest.mark.parametrize("horizon", [H12, H36, H60])
def test_allowed_horizons_build(horizon) -> None:
    assert _roll(_linear(3), horizon).horizon is horizon


@pytest.mark.parametrize("horizon", [Horizon.TACTICAL_1M, Horizon.TACTICAL_3M, Horizon.ALLOCATION_6M,
                                     Horizon.ALLOCATION_24M])
def test_other_horizons_are_rejected(horizon) -> None:
    with pytest.raises(ValueError, match=_HORIZON_VALUE_MSG):
        _roll(_linear(14), horizon)


@pytest.mark.parametrize("horizon", [12, "12M", None, _ForeignEnum.ALLOCATION_12M, True, _Hostile()])
def test_non_horizon_values_are_rejected(horizon) -> None:
    with pytest.raises(TypeError, match=_HORIZON_TYPE_MSG):
        _roll(_linear(14), horizon)


# --- structure / lineage --------------------------------------------------------------

def test_fields_frozen_and_source_identity() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundRollingReturnSeries)] == ["source", "horizon", "points"]
    assert [f.name for f in dataclasses.fields(TefasFundRollingReturnPoint)] == [
        "start_date", "end_date", "simple_return"]
    source = _linear(13)
    result = _roll(source)
    assert result.source is source
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.points = ()  # type: ignore[misc]
    point = result.points[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.simple_return = Decimal("0")  # type: ignore[misc]
    for attr in ("instrument_id", "currency", "mode", "as_of", "start_price", "end_price", "resolution_key",
                 "annualized", "cagr"):
        assert not hasattr(point, attr)


def test_properties_is_available_and_window_count() -> None:
    assert _roll(_linear(13)).is_available is True and _roll(_linear(13)).window_count == 1
    assert _roll(_linear(12)).is_available is False and _roll(_linear(12)).window_count == 0
    assert _roll(_linear(15)).window_count == 3


def test_system_as_of_source_is_retained_not_reinterpreted() -> None:
    source = _linear(13, mode=SYS, as_of=_T0)
    result = _roll(source)
    assert result.source is source and result.source.mode is SYS and result.source.as_of == _T0


# --- 12M windows ----------------------------------------------------------------------

def test_flat_thirteen_months_has_one_zero_return_window() -> None:
    result = _roll(_monthly(["100"] * 13))
    assert result.window_count == 1
    point = result.points[0]
    assert point.simple_return == Decimal("0")
    assert (point.start_date, point.end_date) == (date(2025, 1, 28), date(2026, 1, 28))


def test_growth_and_loss_windows() -> None:
    growth = _roll(_monthly(["100"] + ["105"] * 11 + ["110"]))
    assert growth.points[0].simple_return == Decimal("0.1")
    loss = _roll(_monthly(["100"] + ["90"] * 11 + ["75"]))
    assert loss.points[0].simple_return == Decimal("-0.25")


def test_fifteen_months_yield_three_windows_verified_individually() -> None:
    prices = [str(100 + 10 * i) for i in range(15)]
    result = _roll(_monthly(prices))
    assert result.window_count == 3
    for k, point in enumerate(result.points):
        y0, m0 = _month(k)
        y1, m1 = _month(k + 12)
        assert (point.start_date, point.end_date) == (date(y0, m0, 28), date(y1, m1, 28))
        assert point.simple_return == _simple_return(Decimal(prices[k]), Decimal(prices[k + 12]))
    assert [p.end_date for p in result.points] == sorted(p.end_date for p in result.points)


def test_twelve_months_and_horizon_yield_no_window_but_valid() -> None:
    result = _roll(_linear(12))
    assert result.points == ()
    assert type(result.points) is tuple


# --- 36M / 60M -------------------------------------------------------------------------

@pytest.mark.parametrize("horizon,months", [(H36, 36), (H60, 60)])
def test_long_horizons_window_counts(horizon, months) -> None:
    assert _roll(_linear(months, step=1), horizon).window_count == 0
    one = _roll(_linear(months + 1, step=1), horizon)
    assert one.window_count == 1
    assert one.points[0].simple_return == _simple_return(Decimal("100"), Decimal(str(100 + months)))
    y0, m0 = _month(0)
    y1, m1 = _month(months)
    assert (one.points[0].start_date, one.points[0].end_date) == (date(y0, m0, 28), date(y1, m1, 28))
    assert _roll(_linear(months + 3), horizon).window_count == 3


def test_same_source_supports_all_three_horizons_independently() -> None:
    source = _linear(61)
    counts = [_roll(source, h).window_count for h in (H12, H36, H60)]
    assert counts == [49, 25, 1]


# --- month-end selection ---------------------------------------------------------------

def test_only_last_observation_of_each_month_is_used() -> None:
    dated = []
    for i in range(14):
        y, m = _month(i)
        dated.append((date(y, m, 1), "1000"))       # early-month noise
        dated.append((date(y, m, 15), "1"))         # mid-month noise
        dated.append((date(y, m, 27), str(100 + i)))  # last observation
    result = _roll(_series(dated))
    assert result.window_count == 2
    for k, point in enumerate(result.points):
        y0, m0 = _month(k)
        y1, m1 = _month(k + 12)
        assert (point.start_date, point.end_date) == (date(y0, m0, 27), date(y1, m1, 27))
        assert point.simple_return == _simple_return(Decimal(100 + k), Decimal(100 + k + 12))


def test_representative_days_need_not_match_between_months() -> None:
    dated = [(date(2025, 9, 1), "1"), (date(2025, 9, 15), "2"), (date(2025, 9, 29), "100")]
    for i in range(1, 12):
        y, m = _month(8 + i)
        dated.append((date(y, m, 10), "100"))
    dated += [(date(2026, 9, 5), "3"), (date(2026, 9, 30), "110")]
    result = _roll(_series(dated))
    assert result.window_count == 1
    assert (result.points[0].start_date, result.points[0].end_date) == (date(2025, 9, 29), date(2026, 9, 30))
    assert result.points[0].simple_return == Decimal("0.1")


def test_february_matches_across_leap_and_non_leap_years() -> None:
    dated = []
    for i in range(0, 15):
        y, m = _month(i, base=(2023, 12))
        if (y, m) == (2024, 2):
            dated.append((date(2024, 2, 29), "100"))
        elif (y, m) == (2025, 2):
            dated.append((date(2025, 2, 28), "120"))
        else:
            dated.append((date(y, m, 15), "100"))
    result = _roll(_series(dated))
    feb = [p for p in result.points if p.end_date == date(2025, 2, 28)]
    assert len(feb) == 1
    assert (feb[0].start_date, feb[0].simple_return) == (date(2024, 2, 29), Decimal("0.2"))


# --- continuity / gaps ------------------------------------------------------------------

def test_missing_calendar_month_is_rejected_not_bridged() -> None:
    source = _series([(date(2025, 1, 31), "100"), (date(2025, 2, 28), "101"), (date(2025, 4, 30), "102")])
    with pytest.raises(ValueError, match=_CONTINUITY_MSG):
        _roll(source)
    with pytest.raises(ValueError, match=_CONTINUITY_MSG):
        TefasFundRollingReturnSeries(source=source, horizon=H12, points=())


def test_continuity_is_enforced_even_when_history_is_shorter_than_horizon() -> None:
    source = _series([(date(2025, 1, 31), "100"), (date(2025, 3, 31), "101")])
    with pytest.raises(ValueError, match=_CONTINUITY_MSG):
        _roll(source)


def test_a_month_gap_across_a_year_boundary_is_rejected() -> None:
    source = _series([(date(2025, 12, 31), "100"), (date(2026, 2, 28), "101")])
    with pytest.raises(ValueError, match=_CONTINUITY_MSG):
        _roll(source)


def test_daily_completeness_is_not_required() -> None:
    dated = [(date(*_month(i), 3), str(100 + i)) for i in range(13)]  # one observation per month
    assert _roll(_series(dated)).window_count == 1


def test_source_with_explicit_gap_fails_before_monthly_processing() -> None:
    source = TefasFundPriceSeries(
        instrument_id=_INST, mode=CR, as_of=None,
        requested_dates=(date(2025, 1, 31), date(2025, 2, 28), date(2025, 3, 31)),
        points=(_p(date(2025, 1, 31), "100"), _p(date(2025, 3, 31), "101")),
        gaps=(TefasFundPriceGap(date(2025, 2, 28), ST.NO_ELIGIBLE_OBSERVATION),))
    with pytest.raises(ValueError, match=_INCOMPLETE_MSG):
        _roll(source)
    with pytest.raises(ValueError, match=_INCOMPLETE_MSG):
        TefasFundRollingReturnSeries(source=source, horizon=H12, points=())


def test_source_as_of_all_gap_series_cannot_reach_rolling() -> None:
    source = TefasFundPriceSeries(
        instrument_id=_INST, mode=SRC, as_of=None, requested_dates=(date(2025, 1, 31), date(2025, 2, 28)),
        points=(), gaps=(TefasFundPriceGap(date(2025, 1, 31), ST.UNAVAILABLE_SOURCE_AS_OF),
                         TefasFundPriceGap(date(2025, 2, 28), ST.UNAVAILABLE_SOURCE_AS_OF)))
    with pytest.raises(ValueError, match=_INCOMPLETE_MSG):
        _roll(source)


def test_fewer_than_two_points_is_rejected() -> None:
    with pytest.raises(ValueError, match=_TWO_MSG):
        _roll(_series([(date(2025, 1, 31), "100")]))


@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile()])
def test_builder_rejects_non_series(bad) -> None:
    with pytest.raises(TypeError, match=_SOURCE_MSG):
        _roll(bad)


def test_builder_and_constructor_reject_source_subclass() -> None:
    class _Sub(TefasFundPriceSeries):
        pass
    base = _linear(13)
    sub = _Sub(**{f.name: getattr(base, f.name) for f in dataclasses.fields(base)})
    with pytest.raises(TypeError, match=_SOURCE_MSG):
        _roll(sub)
    with pytest.raises(TypeError, match=_SOURCE_MSG):
        TefasFundRollingReturnSeries(source=sub, horizon=H12, points=())


def test_builder_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        build_tefas_fund_rolling_return_series(_linear(13), H12)  # type: ignore[misc]


# --- decimal arithmetic reuse -----------------------------------------------------------------

@pytest.mark.parametrize("spelling", ["100", "100.0", "100.00", "1E+2"])
def test_price_spellings_produce_equal_rolling_returns(spelling: str) -> None:
    result = _roll(_monthly([spelling] + ["100"] * 11 + ["110"]))
    assert result.points[0].simple_return == Decimal("0.1")


def test_repeating_return_has_exact_parity_with_phase_16b_arithmetic() -> None:
    result = _roll(_monthly(["3"] + ["3"] * 11 + ["4"]))
    assert result.points[0].simple_return == _simple_return(Decimal("3"), Decimal("4"))
    assert result.points[0].simple_return == Decimal("0." + "3" * 49)


def test_extreme_loss_rounds_to_minus_one_under_phase_16b_precision() -> None:
    result = _roll(_monthly(["1"] + ["1"] * 11 + ["1E-1000000"]))
    assert result.points[0].simple_return == Decimal("-1")


def test_out_of_range_positive_return_propagates_phase_16b_static_error() -> None:
    tiny, huge = f"1E-{decimal.MAX_EMAX}", f"1E+{decimal.MAX_EMAX}"
    source = _monthly([tiny] + ["100"] * 11 + [huge])
    with pytest.raises(ValueError, match=_RANGE_MSG):
        _roll(source)


def test_result_independent_of_ambient_context_and_does_not_mutate_it() -> None:
    source = _monthly([str(3 + (i * 7) % 11) for i in range(15)])
    baseline = _roll(source).points
    for prec, rounding in ((4, decimal.ROUND_DOWN), (2, decimal.ROUND_UP), (9, decimal.ROUND_HALF_UP)):
        with decimal.localcontext() as ctx:
            ctx.prec = prec
            ctx.rounding = rounding
            assert _roll(source).points == baseline
            assert ctx.prec == prec and ctx.rounding == rounding
    before = (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))
    _roll(source)
    assert before == (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))


# --- rolling point validation ----------------------------------------------------------------

def _rp(start: date, end: date, ret: str) -> TefasFundRollingReturnPoint:
    return TefasFundRollingReturnPoint(start_date=start, end_date=end, simple_return=Decimal(ret))


@pytest.mark.parametrize("ret", ["0", "0.5", "-0.5", "-1", "-1.0", "1E+3"])
def test_point_valid_returns(ret: str) -> None:
    assert _rp(date(2025, 1, 1), date(2026, 1, 1), ret).simple_return == Decimal(ret)


@pytest.mark.parametrize("ret", ["-1.0000000000000000000000000000000000000000000000001", "-2", "NaN", "Infinity"])
def test_point_invalid_return_values(ret: str) -> None:
    with pytest.raises(ValueError):
        _rp(date(2025, 1, 1), date(2026, 1, 1), ret)


@pytest.mark.parametrize("ret", [0.1, 0, True, "0.1", None])
def test_point_invalid_return_types(ret) -> None:
    with pytest.raises(TypeError):
        TefasFundRollingReturnPoint(start_date=date(2025, 1, 1), end_date=date(2026, 1, 1),
                                    simple_return=ret)  # type: ignore[arg-type]


def test_point_date_validation() -> None:
    for bad in (datetime(2025, 1, 1), "2025-01-01", None):
        with pytest.raises(TypeError):
            TefasFundRollingReturnPoint(start_date=bad, end_date=date(2026, 1, 1), simple_return=Decimal(0))
        with pytest.raises(TypeError):
            TefasFundRollingReturnPoint(start_date=date(2025, 1, 1), end_date=bad, simple_return=Decimal(0))
    for start, end in ((date(2026, 1, 1), date(2025, 1, 1)), (date(2025, 1, 1), date(2025, 1, 1))):
        with pytest.raises(ValueError):
            TefasFundRollingReturnPoint(start_date=start, end_date=end, simple_return=Decimal(0))


def test_point_rejects_decimal_subclass() -> None:
    class _D(Decimal):
        pass
    with pytest.raises(TypeError):
        TefasFundRollingReturnPoint(start_date=date(2025, 1, 1), end_date=date(2026, 1, 1),
                                    simple_return=_D("0.1"))


# --- constructor forgery protection ------------------------------------------------------------

def _good():
    source = _linear(15)
    return source, _roll(source)


def test_direct_construction_with_canonical_points_succeeds() -> None:
    source, good = _good()
    assert TefasFundRollingReturnSeries(source=source, horizon=H12, points=good.points) == good


def test_empty_points_are_valid_only_when_no_window_exists() -> None:
    assert TefasFundRollingReturnSeries(source=_linear(12), horizon=H12, points=()).window_count == 0
    with pytest.raises(ValueError, match=_MATCH_MSG):
        TefasFundRollingReturnSeries(source=_linear(13), horizon=H12, points=())


def test_forged_points_are_rejected() -> None:
    source, good = _good()
    p0, p1, p2 = good.points
    forgeries = [
        (dataclasses.replace(p0, start_date=date(2025, 1, 1)), p1, p2),
        (dataclasses.replace(p0, end_date=date(2026, 1, 1)), p1, p2),
        (dataclasses.replace(p0, simple_return=p0.simple_return + Decimal("0.0001")), p1, p2),
        (p0, p1),                     # missing
        (p0, p1, p2, p2),             # extra
        (p1, p0, p2),                 # reordered
        (),                           # empty
    ]
    for points in forgeries:
        with pytest.raises(ValueError, match=_MATCH_MSG):
            TefasFundRollingReturnSeries(source=source, horizon=H12, points=points)


def test_forged_horizon_mismatch_is_rejected() -> None:
    source, good = _good()
    with pytest.raises(ValueError, match=_MATCH_MSG):
        TefasFundRollingReturnSeries(source=source, horizon=H36, points=good.points)


@pytest.mark.parametrize("bad", [[], None, "x", [object()], (object(),)])
def test_constructor_rejects_bad_points_container(bad) -> None:
    with pytest.raises(TypeError, match=_POINTS_TYPE_MSG):
        TefasFundRollingReturnSeries(source=_linear(13), horizon=H12, points=bad)  # type: ignore[arg-type]


def test_constructor_rejects_point_subclass() -> None:
    class _Sub(TefasFundRollingReturnPoint):
        pass
    source = _linear(13)
    good = _roll(source).points[0]
    sub = _Sub(start_date=good.start_date, end_date=good.end_date, simple_return=good.simple_return)
    with pytest.raises(TypeError, match=_POINTS_TYPE_MSG):
        TefasFundRollingReturnSeries(source=source, horizon=H12, points=(sub,))


@pytest.mark.parametrize("bad", [None, 12, "12M", Horizon.TACTICAL_1M])
def test_constructor_rejects_invalid_horizon(bad) -> None:
    with pytest.raises((TypeError, ValueError)):
        TefasFundRollingReturnSeries(source=_linear(13), horizon=bad, points=())  # type: ignore[arg-type]


def test_constructor_errors_are_static() -> None:
    with pytest.raises(TypeError) as info:
        TefasFundRollingReturnSeries(source=_linear(13), horizon=H12, points=(_Hostile(),))  # type: ignore[arg-type]
    assert str(info.value) == "points must be a tuple of exact TefasFundRollingReturnPoint instances"


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


def test_imports_are_pure_and_reuse_phase_16b_arithmetic() -> None:
    imports = _imports()
    for banned in ("pandas", "numpy", "polars", "scipy", "requests", "httpx", "os", "pathlib", "random",
                   "secrets", "hashlib", "hmac", "socket", "sqlite3", "math", "statistics", "calendar",
                   "dateutil"):
        assert banned not in imports
    assert not any(m.startswith("backend.engine.private.market_data") for m in imports)
    assert "backend.engine.private.fund_return_series" in imports
    assert "backend.engine.private.fund_price_series" in imports
    assert not hasattr(module_under_test, "_return_context")  # arithmetic authority stays in Phase 16B


def test_no_out_of_scope_metric_surface() -> None:
    for name in ("mean_return", "median", "win_rate", "percentile", "persistence", "dispersion", "rank", "score",
                 "annualize", "cagr", "volatility", "sortino", "calmar", "cvar", "sharpe"):
        assert not hasattr(module_under_test, name)


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_phase_16_imports() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/fund_rolling_returns.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert sg.scan_g1(source, rel) == []
    assert sg.scan_g2(source, rel) == []
    assert sg.scan_g4(source, rel) == []
    assert sg.scan_g5(source, rel) == []
    assert {v.node_kind for v in sg.scan_g3(source, rel)} == {
        "PrivateImport:backend.engine.private.fund_price_series",
        "PrivateImport:backend.engine.private.fund_return_series",
    }
    assert rel not in sg.PURE_MANIFEST
