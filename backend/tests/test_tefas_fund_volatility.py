"""
backend/tests/test_tefas_fund_volatility.py
===========================================
Tests for canonical month-end monthly returns and historical monthly / annualized fund volatility (Phase 16K).

Phase 16K engineering convention (the methodology does not define sampling or annualization): last authoritative
observation per calendar month, adjacent-month simple returns, sample standard deviation (denominator N - 1),
annualized volatility = sqrt(sample variance * 12). Not derived from overlapping Phase 16D rolling windows.
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

from backend.engine.private import fund_volatility as module_under_test
from backend.engine.private.domain import Currency, DataConfidenceLevel
from backend.engine.private.fund_price_series import TefasFundPriceGap, TefasFundPricePoint, TefasFundPriceSeries
from backend.engine.private.fund_volatility import (
    TefasFundHistoricalVolatility,
    TefasFundMonthlyReturnPoint,
    TefasFundMonthlyReturnSeries,
    build_tefas_fund_monthly_return_series,
    calculate_tefas_fund_historical_volatility,
)
from backend.engine.private.market_data.models import MarketDataResolutionMode, MarketDataResolutionStatus

CR = MarketDataResolutionMode.CURRENT_REPORTED
SYS = MarketDataResolutionMode.SYSTEM_AS_OF
SRC = MarketDataResolutionMode.SOURCE_AS_OF
_IID = UUID(int=1)
_T0 = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
_CUTOFF = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

_ERR_COMPLETE = r"^TEFAS fund volatility requires a complete price series$"
_ERR_CONTINUITY = r"^TEFAS fund volatility requires continuous monthly price coverage$"
_ERR_POINTS_MATCH = r"^points must match the canonical month-end monthly returns exactly$"
_ERR_PAIR = r"^monthly_volatility and annualized_volatility must both be None or both be Decimal$"
_ERR_VALUE_RANGE = r"^volatility values must be finite and non-negative$"
_ERR_VOL_MATCH = r"^historical volatility must match the canonical sample calculation exactly$"
_ERR_OVERFLOW = r"^TEFAS fund volatility exceeds supported Decimal analytics range$"


def _ctx() -> decimal.Context:
    return decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=decimal.MIN_EMIN, Emax=decimal.MAX_EMAX)


def _series(entries, mode=CR, as_of=None, gaps=()) -> TefasFundPriceSeries:
    points = tuple(
        TefasFundPricePoint(trade_date=d, unit_price=Decimal(p), currency=Currency.TRY,
                            confidence=DataConfidenceLevel.HIGH, snapshot_retrieved_at=_T0,
                            resolution_key=f"k-{d.isoformat()}")
        for d, p in entries)
    requested = tuple(sorted([d for d, _ in entries] + [g.trade_date for g in gaps]))
    return TefasFundPriceSeries(instrument_id=_IID, mode=mode, as_of=as_of, requested_dates=requested,
                                points=points, gaps=tuple(gaps))


def _month_end_entries(prices, start=(2026, 1), day: int = 28):
    entries = []
    for i, price in enumerate(prices):
        index = start[0] * 12 + (start[1] - 1) + i
        entries.append((date(index // 12, index % 12 + 1, day), price))
    return entries


def _prices_for_returns(returns):
    prices, current = [Decimal(100)], Decimal(100)
    for r in returns:
        current = current * (Decimal(1) + Decimal(r))
        prices.append(current)
    return [str(p) for p in prices]


def _monthly(prices, **kw) -> TefasFundMonthlyReturnSeries:
    return build_tefas_fund_monthly_return_series(price_series=_series(_month_end_entries(prices, **kw)))


def _monthly_for_returns(returns) -> TefasFundMonthlyReturnSeries:
    series = _monthly(_prices_for_returns(returns))
    assert [p.simple_return for p in series.points] == [Decimal(r) for r in returns]
    return series


def _vol(series: TefasFundMonthlyReturnSeries) -> TefasFundHistoricalVolatility:
    return calculate_tefas_fund_historical_volatility(monthly_returns=series)


# --- shapes ---------------------------------------------------------------------------------------------------

def test_dataclass_fields_are_exact_and_frozen() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundMonthlyReturnPoint)] == [
        "start_date", "end_date", "simple_return"]
    assert [f.name for f in dataclasses.fields(TefasFundMonthlyReturnSeries)] == ["source", "points"]
    assert [f.name for f in dataclasses.fields(TefasFundHistoricalVolatility)] == [
        "source", "monthly_volatility", "annualized_volatility"]
    series = _monthly_for_returns(["0.1", "0.2"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        series.points = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _vol(series).monthly_volatility = Decimal(0)  # type: ignore[misc]


def test_builders_are_keyword_only() -> None:
    series = _series(_month_end_entries(["100", "110"]))
    with pytest.raises(TypeError):
        build_tefas_fund_monthly_return_series(series)  # type: ignore[misc]
    with pytest.raises(TypeError):
        calculate_tefas_fund_historical_volatility(_monthly(["100", "110"]))  # type: ignore[misc]


def test_no_forbidden_surface() -> None:
    result = _vol(_monthly_for_returns(["0.1", "0.2"]))
    for name in ("sharpe", "sortino", "mar", "risk_free", "excess_return", "rank", "score", "label",
                 "recommendation", "bootstrap", "stress", "covariance", "trading_days", "population_volatility"):
        assert not hasattr(result, name) and not hasattr(module_under_test, name)


# --- monthly point validation ---------------------------------------------------------------------------------------

def test_monthly_point_validation() -> None:
    d1, d2 = date(2026, 1, 28), date(2026, 2, 27)
    assert TefasFundMonthlyReturnPoint(start_date=d1, end_date=d2, simple_return=Decimal("-1"))
    with pytest.raises(ValueError):
        TefasFundMonthlyReturnPoint(start_date=d2, end_date=d1, simple_return=Decimal(0))
    with pytest.raises(ValueError):
        TefasFundMonthlyReturnPoint(start_date=d1, end_date=d1, simple_return=Decimal(0))
    for bad in (Decimal("-1.0000000000000000000000000001"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            TefasFundMonthlyReturnPoint(start_date=d1, end_date=d2, simple_return=bad)
    for bad in (0.5, 1, True, "0.5", None):
        with pytest.raises(TypeError):
            TefasFundMonthlyReturnPoint(start_date=d1, end_date=d2, simple_return=bad)  # type: ignore[arg-type]
    for bad in ("2026-01-28", None, datetime(2026, 1, 28, tzinfo=timezone.utc)):
        with pytest.raises(TypeError):
            TefasFundMonthlyReturnPoint(start_date=bad, end_date=d2, simple_return=Decimal(0))  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            TefasFundMonthlyReturnPoint(start_date=d1, end_date=bad, simple_return=Decimal(0))  # type: ignore[arg-type]


# --- monthly sampling ---------------------------------------------------------------------------------------------------

def test_source_is_retained_by_identity_and_points_span_adjacent_months() -> None:
    price_series = _series(_month_end_entries(["100", "110", "132"]))
    monthly = build_tefas_fund_monthly_return_series(price_series=price_series)
    assert monthly.source is price_series
    assert [(p.start_date, p.end_date, p.simple_return) for p in monthly.points] == [
        (date(2026, 1, 28), date(2026, 2, 28), Decimal("0.1")),
        (date(2026, 2, 28), date(2026, 3, 28), Decimal("0.2"))]
    assert (monthly.is_available, monthly.observation_count) == (True, 2)


def test_last_observation_of_each_month_is_the_representative() -> None:
    clean = _series([(date(2026, 1, 29), "100"), (date(2026, 2, 27), "110"), (date(2026, 3, 31), "132")])
    noisy = _series([
        (date(2026, 1, 2), "5"), (date(2026, 1, 15), "9000"), (date(2026, 1, 29), "100"),
        (date(2026, 2, 1), "1"), (date(2026, 2, 13), "77777"), (date(2026, 2, 27), "110"),
        (date(2026, 3, 3), "0.001"), (date(2026, 3, 20), "31"), (date(2026, 3, 31), "132")])
    a, b = (build_tefas_fund_monthly_return_series(price_series=s) for s in (clean, noisy))
    assert a.points == b.points
    assert _vol(a).monthly_volatility == _vol(b).monthly_volatility
    assert _vol(a).annualized_volatility == _vol(b).annualized_volatility


def test_different_day_of_month_is_valid() -> None:
    monthly = build_tefas_fund_monthly_return_series(price_series=_series(
        [(date(2026, 1, 29), "100"), (date(2026, 2, 27), "110"), (date(2026, 3, 31), "132")]))
    assert [(p.start_date.day, p.end_date.day) for p in monthly.points] == [(29, 27), (27, 31)]


def test_leap_year_uses_calendar_month_adjacency_only() -> None:
    monthly = build_tefas_fund_monthly_return_series(price_series=_series(
        [(date(2024, 1, 31), "100"), (date(2024, 2, 29), "110"), (date(2024, 3, 29), "121")]))
    assert [(p.start_date, p.end_date) for p in monthly.points] == [
        (date(2024, 1, 31), date(2024, 2, 29)), (date(2024, 2, 29), date(2024, 3, 29))]
    assert [p.simple_return for p in monthly.points] == [Decimal("0.1"), Decimal("0.1")]


def test_year_boundary_is_adjacent() -> None:
    monthly = build_tefas_fund_monthly_return_series(price_series=_series(
        [(date(2025, 12, 30), "100"), (date(2026, 1, 30), "110")]))
    assert len(monthly.points) == 1


def test_missing_calendar_month_fails_closed() -> None:
    with pytest.raises(ValueError, match=_ERR_CONTINUITY):
        build_tefas_fund_monthly_return_series(price_series=_series(
            [(date(2026, 1, 30), "100"), (date(2026, 2, 27), "110"), (date(2026, 4, 30), "120")]))
    with pytest.raises(ValueError, match=_ERR_CONTINUITY):
        build_tefas_fund_monthly_return_series(price_series=_series(
            [(date(2025, 12, 30), "100"), (date(2026, 2, 27), "110")]))


def test_single_represented_month_is_valid_and_empty() -> None:
    for entries in ([(date(2026, 1, 30), "100")], [(date(2026, 1, 2), "100"), (date(2026, 1, 30), "105")]):
        monthly = build_tefas_fund_monthly_return_series(price_series=_series(entries))
        assert monthly.points == ()
        assert (monthly.is_available, monthly.observation_count) == (False, 0)


def test_explicit_gaps_are_rejected_without_bridging() -> None:
    gap = TefasFundPriceGap(trade_date=date(2026, 2, 27), status=MarketDataResolutionStatus.NO_SNAPSHOT)
    gapped = _series([(date(2026, 1, 30), "100"), (date(2026, 3, 31), "120")], gaps=(gap,))
    with pytest.raises(ValueError, match=_ERR_COMPLETE):
        build_tefas_fund_monthly_return_series(price_series=gapped)


def test_source_type_validation() -> None:
    class _Sub(TefasFundPriceSeries):
        pass
    good = _series(_month_end_entries(["100", "110"]))
    sub = _Sub(**{f.name: getattr(good, f.name) for f in dataclasses.fields(good)})
    for bad in (None, "x", object(), good.points, sub):
        with pytest.raises(TypeError):
            build_tefas_fund_monthly_return_series(price_series=bad)  # type: ignore[arg-type]


def test_equivalent_price_spellings_are_economically_identical() -> None:
    results = []
    for spelling in ("100", "100.0", "100.00", "1E+2"):
        monthly = _monthly([spelling, "110", spelling, "110"])
        results.append((tuple(p.simple_return for p in monthly.points), _vol(monthly).monthly_volatility,
                        _vol(monthly).annualized_volatility))
    assert all(r == results[0] for r in results[1:])


def test_pit_context_is_preserved_by_the_source() -> None:
    sys_series = _series(_month_end_entries(["100", "110", "121"]), mode=SYS, as_of=_CUTOFF)
    monthly = build_tefas_fund_monthly_return_series(price_series=sys_series)
    assert monthly.source is sys_series and monthly.source.mode is SYS and monthly.source.as_of == _CUTOFF
    assert _vol(monthly).source is monthly
    gaps = tuple(TefasFundPriceGap(trade_date=d, status=MarketDataResolutionStatus.UNAVAILABLE_SOURCE_AS_OF)
                 for d in (date(2026, 1, 30), date(2026, 2, 27)))
    source_as_of = TefasFundPriceSeries(instrument_id=_IID, mode=SRC, as_of=_CUTOFF,
                                        requested_dates=tuple(g.trade_date for g in gaps), points=(), gaps=gaps)
    with pytest.raises(ValueError, match=_ERR_COMPLETE):
        build_tefas_fund_monthly_return_series(price_series=source_as_of)


def test_extreme_monthly_returns_are_exact_and_unclamped() -> None:
    monthly = _monthly(["100", "1E-60", "1E-60"])
    assert [p.simple_return for p in monthly.points] == [Decimal(-1), Decimal(0)]
    big = _monthly(["100", "1E+30"])
    assert big.points[0].simple_return == Decimal(10 ** 28 - 1)


# --- monthly series forgery ------------------------------------------------------------------------------------------------

def test_monthly_series_constructor_rejects_forgery() -> None:
    good = _monthly_for_returns(["0.1", "0.2", "0.3"])
    p0, p1, p2 = good.points

    class _P(TefasFundMonthlyReturnPoint):
        pass

    class _S(TefasFundPriceSeries):
        pass
    assert TefasFundMonthlyReturnSeries(source=good.source, points=good.points) == good
    forged_points = [
        (p0, p1),                                                                        # missing
        (p0, p1, p2, p2),                                                                # extra
        (p1, p0, p2),                                                                    # reordered
        (TefasFundMonthlyReturnPoint(start_date=p0.start_date, end_date=p0.end_date, simple_return=Decimal("0.5")),
         p1, p2),                                                                        # wrong return
        (TefasFundMonthlyReturnPoint(start_date=date(2026, 1, 2), end_date=p0.end_date, simple_return=p0.simple_return),
         p1, p2),                                                                        # wrong start
        (TefasFundMonthlyReturnPoint(start_date=p0.start_date, end_date=date(2026, 2, 2), simple_return=p0.simple_return),
         p1, p2),                                                                        # wrong end
        (),
    ]
    for points in forged_points:
        with pytest.raises(ValueError, match=_ERR_POINTS_MATCH):
            TefasFundMonthlyReturnSeries(source=good.source, points=points)
    subclass_point = _P(start_date=p0.start_date, end_date=p0.end_date, simple_return=p0.simple_return)
    for bad in ((subclass_point, p1, p2), [p0, p1, p2], None, (p0, object())):
        with pytest.raises(TypeError):
            TefasFundMonthlyReturnSeries(source=good.source, points=bad)  # type: ignore[arg-type]
    sub_source = _S(**{f.name: getattr(good.source, f.name) for f in dataclasses.fields(good.source)})
    with pytest.raises(TypeError):
        TefasFundMonthlyReturnSeries(source=sub_source, points=good.points)
    with pytest.raises(ValueError, match=_ERR_CONTINUITY):
        TefasFundMonthlyReturnSeries(source=_series([(date(2026, 1, 30), "100"), (date(2026, 3, 31), "110")]),
                                     points=())


# --- volatility: availability -------------------------------------------------------------------------------------------------

def test_fewer_than_two_returns_is_unavailable_not_zero() -> None:
    for prices in (["100"], ["100", "110"]):
        result = _vol(_monthly(prices))
        assert result.monthly_volatility is None and result.annualized_volatility is None


def test_constant_returns_have_zero_volatility() -> None:
    result = _vol(_monthly_for_returns(["0.10", "0.10"]))
    assert result.monthly_volatility == Decimal(0) and result.annualized_volatility == Decimal(0)
    assert type(result.monthly_volatility) is Decimal


# --- volatility: numeric matrix ---------------------------------------------------------------------------------------------------

def test_symmetric_returns() -> None:
    result = _vol(_monthly_for_returns(["-0.10", "0.10"]))
    ctx = _ctx()
    assert result.monthly_volatility == ctx.sqrt(Decimal("0.02"))
    assert result.annualized_volatility == ctx.sqrt(Decimal("0.24"))
    assert result.monthly_volatility == Decimal("0.14142135623730950488016887242096980785696718753769")
    assert result.annualized_volatility == Decimal("0.48989794855663561963945681494117827839318949613133")


def test_unequal_positive_returns() -> None:
    result = _vol(_monthly_for_returns(["0.10", "0.20"]))
    ctx = _ctx()
    assert result.monthly_volatility == ctx.sqrt(Decimal("0.005"))
    assert result.annualized_volatility == ctx.sqrt(Decimal("0.06"))


def test_sample_denominator_is_n_minus_one_with_three_returns() -> None:
    result = _vol(_monthly_for_returns(["0.10", "0.20", "0.30"]))
    ctx = _ctx()
    assert result.monthly_volatility == ctx.sqrt(Decimal("0.01"))  # sum sq dev 0.02 / (3 - 1)
    assert result.monthly_volatility == Decimal("0.1")
    assert result.annualized_volatility == ctx.sqrt(Decimal("0.12"))  # sqrt(0.01 * 12) directly


def test_annualization_is_computed_directly_from_variance() -> None:
    result = _vol(_monthly_for_returns(["-0.10", "0.05", "0.20", "-0.02"]))
    ctx = _ctx()
    returns = [Decimal("-0.10"), Decimal("0.05"), Decimal("0.20"), Decimal("-0.02")]
    mean = ctx.divide(sum(returns, Decimal(0)), Decimal(4))
    variance = ctx.divide(sum((ctx.power(r - mean, 2) for r in returns), Decimal(0)), Decimal(3))
    assert result.monthly_volatility == ctx.sqrt(variance)
    assert result.annualized_volatility == ctx.sqrt(ctx.multiply(variance, Decimal(12)))
    assert result.annualized_volatility >= result.monthly_volatility >= 0


def test_extreme_minus_one_return_is_included_exactly() -> None:
    result = _vol(_monthly(["100", "1E-60", "1E-60"]))  # returns -1 and 0
    assert result.monthly_volatility == _ctx().sqrt(Decimal("0.5"))


# --- decimal / range ------------------------------------------------------------------------------------------------------------------

def test_volatility_context_is_explicit() -> None:
    ctx = module_under_test._volatility_context()
    assert (ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax) == (50, decimal.ROUND_HALF_EVEN, decimal.MIN_EMIN,
                                                            decimal.MAX_EMAX)


def test_overflow_is_translated_and_unrelated_errors_propagate(monkeypatch) -> None:
    series = _monthly(["100", "10100", "1020100"])  # two returns of exactly 100
    tiny = decimal.Context(prec=50, Emin=-5, Emax=0,
                           traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow])
    monkeypatch.setattr(module_under_test, "_volatility_context", lambda: tiny)
    with pytest.raises(ValueError, match=_ERR_OVERFLOW) as info:
        _vol(series)
    assert info.value.__cause__ is None and info.value.__suppress_context__ is True

    def boom():
        raise KeyError("unrelated")
    monkeypatch.setattr(module_under_test, "_volatility_context", boom)
    with pytest.raises(KeyError):
        _vol(series)


def test_return_range_errors_propagate_from_phase_16b_unchanged() -> None:
    with pytest.raises(ValueError, match="analytics range"):
        _monthly(["1E-500000000000000000", "1E+500000000000000000"])


def test_ambient_context_is_ignored_and_untouched() -> None:
    entries = _month_end_entries(_prices_for_returns(["-0.10", "0.05", "0.20", "-0.02"]))
    price_series = _series(entries)
    baseline_monthly = build_tefas_fund_monthly_return_series(price_series=price_series)
    baseline = _vol(baseline_monthly)
    for prec, rounding in ((3, decimal.ROUND_DOWN), (2, decimal.ROUND_UP), (9, decimal.ROUND_HALF_UP)):
        with decimal.localcontext() as ctx:
            ctx.prec, ctx.rounding = prec, rounding
            monthly = build_tefas_fund_monthly_return_series(price_series=price_series)
            got = _vol(monthly)
            assert monthly.points == baseline_monthly.points
            assert got.monthly_volatility == baseline.monthly_volatility
            assert got.annualized_volatility == baseline.annualized_volatility
            assert (ctx.prec, ctx.rounding) == (prec, rounding)
    before = (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))
    _vol(baseline_monthly)
    assert before == (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))


# --- volatility: validation / forgery -------------------------------------------------------------------------------------------------

def _kw(result: TefasFundHistoricalVolatility, **over) -> dict:
    kw = dict(source=result.source, monthly_volatility=result.monthly_volatility,
              annualized_volatility=result.annualized_volatility)
    kw.update(over)
    return kw


def test_volatility_builder_and_constructor_agree() -> None:
    result = _vol(_monthly_for_returns(["0.10", "0.20"]))
    assert result.source is not None
    assert TefasFundHistoricalVolatility(**_kw(result)) == result


def test_forged_volatility_values_are_rejected() -> None:
    result = _vol(_monthly_for_returns(["0.10", "0.20"]))
    with pytest.raises(ValueError, match=_ERR_VOL_MATCH):
        TefasFundHistoricalVolatility(**_kw(result, monthly_volatility=Decimal("0.07")))
    with pytest.raises(ValueError, match=_ERR_VOL_MATCH):
        TefasFundHistoricalVolatility(**_kw(result, annualized_volatility=Decimal("0.25")))
    with pytest.raises(ValueError, match=_ERR_VOL_MATCH):
        TefasFundHistoricalVolatility(**_kw(result, monthly_volatility=None, annualized_volatility=None))
    with pytest.raises(ValueError, match=_ERR_PAIR):
        TefasFundHistoricalVolatility(**_kw(result, annualized_volatility=None))
    with pytest.raises(ValueError, match=_ERR_PAIR):
        TefasFundHistoricalVolatility(**_kw(result, monthly_volatility=None))
    for bad in (Decimal("-0.1"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError, match=_ERR_VALUE_RANGE):
            TefasFundHistoricalVolatility(**_kw(result, monthly_volatility=bad))
        with pytest.raises(ValueError, match=_ERR_VALUE_RANGE):
            TefasFundHistoricalVolatility(**_kw(result, annualized_volatility=bad))


def test_forged_zero_for_insufficient_data_is_rejected() -> None:
    for prices in (["100"], ["100", "110"]):
        result = _vol(_monthly(prices))
        assert result.monthly_volatility is None
        with pytest.raises(ValueError, match=_ERR_VOL_MATCH):
            TefasFundHistoricalVolatility(**_kw(result, monthly_volatility=Decimal(0),
                                                annualized_volatility=Decimal(0)))


def test_volatility_type_validation() -> None:
    result = _vol(_monthly_for_returns(["0.10", "0.20"]))

    class _D(Decimal):
        pass

    class _M(TefasFundMonthlyReturnSeries):
        pass
    for bad in (0.1, 1, True, "0.1", _D("0.1")):
        with pytest.raises(TypeError):
            TefasFundHistoricalVolatility(**_kw(result, monthly_volatility=bad))
        with pytest.raises(TypeError):
            TefasFundHistoricalVolatility(**_kw(result, annualized_volatility=bad))
    sub = _M(source=result.source.source, points=result.source.points)
    for bad in (None, object(), sub):
        with pytest.raises(TypeError):
            TefasFundHistoricalVolatility(**_kw(result, source=bad))
        with pytest.raises(TypeError):
            calculate_tefas_fund_historical_volatility(monthly_returns=bad)  # type: ignore[arg-type]


# --- scope / purity -------------------------------------------------------------------------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_imports_exclude_overlapping_rolling_modules_and_float() -> None:
    imports = _imports()
    assert {m for m in imports if m.startswith("backend.")} == {
        "backend.engine.private.fund_price_series", "backend.engine.private.fund_return_series"}
    for banned in ("backend.engine.private.fund_rolling_returns", "backend.engine.private.fund_annualized_returns"):
        assert banned not in imports
    assert imports <= {"__future__", "dataclasses", "datetime", "decimal", "backend.engine.private.fund_price_series",
                       "backend.engine.private.fund_return_series"}
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    assert not any(isinstance(n, ast.Name) and n.id == "float" for n in ast.walk(tree))
    assert not any(isinstance(n, ast.Constant) and isinstance(n.value, float) for n in ast.walk(tree))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"Horizon", "TefasFundRollingReturnSeries"}


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_the_required_imports() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/fund_volatility.py"
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
