"""
backend/tests/test_tefas_fund_return_series.py
==============================================
Tests for the deterministic TEFAS fund simple-return series (Phase 16B).

Returns derive only from a complete authoritative TefasFundPriceSeries: simple returns P1/P0 - 1 in an explicit,
ambient-context-independent 50-digit ROUND_HALF_EVEN Decimal context; no gap bridging, no annualization, no
risk metrics.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private import fund_return_series as module_under_test
from backend.engine.private.domain import Currency, DataConfidenceLevel, InstrumentType
from backend.engine.private.fund_price_series import (
    TefasFundPriceGap,
    TefasFundPricePoint,
    TefasFundPriceSeries,
    build_tefas_fund_price_series,
)
from backend.engine.private.fund_return_series import (
    TefasFundReturnPoint,
    TefasFundReturnSeries,
    build_tefas_fund_return_series,
)
from backend.engine.private.market_data.models import MarketDataResolutionMode, MarketDataResolutionStatus
from backend.engine.private.market_data.tefas_models import (
    TefasFundPriceObservation,
    TefasFundPriceSnapshot,
    TefasObservationStatus,
)

CR = MarketDataResolutionMode.CURRENT_REPORTED
SYS = MarketDataResolutionMode.SYSTEM_AS_OF
SRC = MarketDataResolutionMode.SOURCE_AS_OF
ST = MarketDataResolutionStatus

_INST = UUID("11111111-1111-4111-8111-111111111111")
_T0 = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
_D = [date(2026, 3, 2) + timedelta(days=i) for i in range(6)]

_INCOMPLETE_MSG = r"^TEFAS fund return series requires a complete price series$"
_TWO_MSG = r"^TEFAS fund return series requires at least two price points$"
_SOURCE_TYPE_MSG = r"^source must be an exact TefasFundPriceSeries instance$"
_POINTS_TYPE_MSG = r"^points must be a tuple of exact TefasFundReturnPoint instances$"
_MATCH_MSG = r"^points must match consecutive source price points exactly$"


def _p(d: date, price: str, currency: Currency = Currency.TRY) -> TefasFundPricePoint:
    return TefasFundPricePoint(trade_date=d, unit_price=Decimal(price), currency=currency,
                               confidence=DataConfidenceLevel.HIGH, snapshot_retrieved_at=_T0,
                               resolution_key=f"key-{d.isoformat()}")


def _source(prices, dates=None, mode=CR, as_of=None) -> TefasFundPriceSeries:
    dates = dates or _D[: len(prices)]
    return TefasFundPriceSeries(
        instrument_id=_INST, mode=mode, as_of=as_of, requested_dates=tuple(dates),
        points=tuple(_p(d, x) for d, x in zip(dates, prices)), gaps=(),
    )


def _rp(start: date, end: date, ret: str) -> TefasFundReturnPoint:
    return TefasFundReturnPoint(start_date=start, end_date=end, simple_return=Decimal(ret))


class _DecimalSub(Decimal):
    pass


class _Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")


# --- return point ----------------------------------------------------------------

def test_return_point_fields_and_frozen() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundReturnPoint)] == ["start_date", "end_date", "simple_return"]
    point = _rp(_D[0], _D[1], "0.1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.simple_return = Decimal("0")  # type: ignore[misc]


@pytest.mark.parametrize("ret", ["0", "0.0", "0.5", "-0.5", "-0.9999999999", "1E+3", "-0"])
def test_return_point_valid_returns(ret: str) -> None:
    assert _rp(_D[0], _D[1], ret).simple_return == Decimal(ret)


@pytest.mark.parametrize("ret", ["-1", "-1.0", "-1.5", "-100", "NaN", "sNaN", "Infinity", "-Infinity"])
def test_return_point_rejects_invalid_values(ret: str) -> None:
    with pytest.raises(ValueError):
        _rp(_D[0], _D[1], ret)


@pytest.mark.parametrize("ret", [0.1, 0, 1, True, "0.1", None, _DecimalSub("0.1"), _Hostile()])
def test_return_point_rejects_invalid_types(ret) -> None:
    with pytest.raises(TypeError):
        TefasFundReturnPoint(start_date=_D[0], end_date=_D[1], simple_return=ret)  # type: ignore[arg-type]


@pytest.mark.parametrize("start,end", [(datetime(2026, 3, 2), _D[1]), (_D[0], datetime(2026, 3, 3)),
                                       ("2026-03-02", _D[1]), (_D[0], None), (None, _D[1]), (True, _D[1])])
def test_return_point_rejects_invalid_dates(start, end) -> None:
    with pytest.raises(TypeError):
        TefasFundReturnPoint(start_date=start, end_date=end, simple_return=Decimal("0"))  # type: ignore[arg-type]


@pytest.mark.parametrize("start,end", [(_D[1], _D[0]), (_D[0], _D[0])])
def test_return_point_requires_end_after_start(start: date, end: date) -> None:
    with pytest.raises(ValueError):
        TefasFundReturnPoint(start_date=start, end_date=end, simple_return=Decimal("0"))


def test_return_point_holds_no_price_or_provenance() -> None:
    point = _rp(_D[0], _D[1], "0.1")
    for attr in ("start_price", "end_price", "currency", "resolution_key", "confidence", "instrument_id",
                 "mode", "as_of", "snapshot_retrieved_at", "log_return", "annualized"):
        assert not hasattr(point, attr)


# --- numeric matrix ---------------------------------------------------------------

@pytest.mark.parametrize("prices,expected", [
    (("100", "110"), "0.1"),
    (("100", "100"), "0"),
    (("100", "75"), "-0.25"),
    (("0.5", "1"), "1"),
    (("3", "4"), "0." + "3" * 49),
    (("3", "2"), "-0." + "3" * 50),
    (("7", "9"), "0.2857142857142857142857142857142857142857142857143"),
])
def test_return_values_are_exact_or_deterministic_fifty_digit(prices, expected: str) -> None:
    result = build_tefas_fund_return_series(price_series=_source(prices))
    assert len(result.points) == 1
    assert result.points[0].simple_return == Decimal(expected)
    assert type(result.points[0].simple_return) is Decimal
    assert result.points[0].start_date == _D[0] and result.points[0].end_date == _D[1]


def test_repeating_result_has_at_most_fifty_significant_digits() -> None:
    value = build_tefas_fund_return_series(price_series=_source(("3", "4"))).points[0].simple_return
    assert len(value.as_tuple().digits) <= 50


def test_fixed_expected_string_for_three_to_four() -> None:
    value = build_tefas_fund_return_series(price_series=_source(("3", "4"))).points[0].simple_return
    assert str(value) == "0.3333333333333333333333333333333333333333333333333"


@pytest.mark.parametrize("spelling", ["100", "100.0", "100.00", "1E+2", "1.00E+2"])
def test_price_spellings_produce_equal_returns(spelling: str) -> None:
    result = build_tefas_fund_return_series(price_series=_source((spelling, "110")))
    assert result.points[0].simple_return == Decimal("0.1")
    assert build_tefas_fund_return_series(price_series=_source(("110", spelling))).points[0].simple_return == \
        build_tefas_fund_return_series(price_series=_source(("110", "100"))).points[0].simple_return


def test_compounding_identity_matches_price_ratio_in_same_context() -> None:
    result = build_tefas_fund_return_series(price_series=_source(("100", "110", "99")))
    r1, r2 = (p.simple_return for p in result.points)
    ctx = decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN)
    growth = ctx.multiply(ctx.add(Decimal(1), r1), ctx.add(Decimal(1), r2))
    assert growth == ctx.divide(Decimal("99"), Decimal("100"))
    assert (r1, r2) == (Decimal("0.1"), Decimal("-0.1"))


def test_adjacent_pairs_and_dates_follow_source_order_across_calendar_gaps() -> None:
    dates = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 9)]  # weekend/holiday-like calendar jump
    result = build_tefas_fund_return_series(price_series=_source(("10", "11", "12.1"), dates=dates))
    assert [(p.start_date, p.end_date) for p in result.points] == [(dates[0], dates[1]), (dates[1], dates[2])]
    assert [p.simple_return for p in result.points] == [Decimal("0.1"), Decimal("0.1")]


def test_n_prices_yield_n_minus_one_returns() -> None:
    for n in (2, 3, 6):
        prices = [str(100 + i) for i in range(n)]
        assert len(build_tefas_fund_return_series(price_series=_source(prices)).points) == n - 1


# --- ambient decimal context isolation ------------------------------------------

def test_result_is_independent_of_ambient_decimal_context() -> None:
    baseline = build_tefas_fund_return_series(price_series=_source(("3", "4", "7"))).points
    for prec, rounding in ((4, decimal.ROUND_DOWN), (2, decimal.ROUND_UP), (10, decimal.ROUND_HALF_UP)):
        with decimal.localcontext() as ctx:
            ctx.prec = prec
            ctx.rounding = rounding
            perturbed = build_tefas_fund_return_series(price_series=_source(("3", "4", "7"))).points
            assert perturbed == baseline
            assert ctx.prec == prec and ctx.rounding == rounding


def test_builder_does_not_mutate_global_context() -> None:
    before = (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags),
              dict(decimal.getcontext().traps))
    build_tefas_fund_return_series(price_series=_source(("3", "4")))
    after = (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags),
             dict(decimal.getcontext().traps))
    assert before == after


def test_constructor_validation_is_independent_of_ambient_context() -> None:
    source = _source(("3", "4"))
    good = build_tefas_fund_return_series(price_series=source)
    with decimal.localcontext() as ctx:
        ctx.prec = 3
        assert TefasFundReturnSeries(source=source, points=good.points).points == good.points


# --- source contract / gaps ---------------------------------------------------------

def test_source_with_gap_is_rejected_and_never_bridged() -> None:
    dates = (_D[0], _D[1], _D[2])
    source = TefasFundPriceSeries(
        instrument_id=_INST, mode=CR, as_of=None, requested_dates=dates,
        points=(_p(_D[0], "100"), _p(_D[2], "121")), gaps=(TefasFundPriceGap(_D[1], ST.NO_ELIGIBLE_OBSERVATION),),
    )
    with pytest.raises(ValueError, match=_INCOMPLETE_MSG):
        build_tefas_fund_return_series(price_series=source)
    with pytest.raises(ValueError, match=_INCOMPLETE_MSG):
        TefasFundReturnSeries(source=source, points=(_rp(_D[0], _D[2], "0.21"),))


def test_fewer_than_two_points_is_rejected() -> None:
    with pytest.raises(ValueError, match=_TWO_MSG):
        build_tefas_fund_return_series(price_series=_source(("100",)))


def test_all_gap_source_as_of_series_is_rejected() -> None:
    source = TefasFundPriceSeries(
        instrument_id=_INST, mode=SRC, as_of=None, requested_dates=(_D[0], _D[1]), points=(),
        gaps=(TefasFundPriceGap(_D[0], ST.UNAVAILABLE_SOURCE_AS_OF), TefasFundPriceGap(_D[1], ST.UNAVAILABLE_SOURCE_AS_OF)),
    )
    with pytest.raises(ValueError, match=_INCOMPLETE_MSG):
        build_tefas_fund_return_series(price_series=source)


@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile()])
def test_builder_rejects_non_series(bad) -> None:
    with pytest.raises(TypeError, match=_SOURCE_TYPE_MSG):
        build_tefas_fund_return_series(price_series=bad)  # type: ignore[arg-type]


def test_builder_rejects_source_subclass() -> None:
    class _Sub(TefasFundPriceSeries):
        pass
    base = _source(("100", "110"))
    sub = _Sub(**{f.name: getattr(base, f.name) for f in dataclasses.fields(base)})
    with pytest.raises(TypeError, match=_SOURCE_TYPE_MSG):
        build_tefas_fund_return_series(price_series=sub)


def test_builder_is_keyword_only_and_takes_only_the_series() -> None:
    with pytest.raises(TypeError):
        build_tefas_fund_return_series(_source(("1", "2")))  # type: ignore[misc]
    with pytest.raises(TypeError):
        build_tefas_fund_return_series(price_series=_source(("1", "2")), mode=CR)  # type: ignore[call-arg]


# --- lineage --------------------------------------------------------------------------

def test_source_is_retained_by_identity_and_provenance_is_reachable() -> None:
    source = _source(("100", "110", "99"), mode=SYS, as_of=_T0)
    result = build_tefas_fund_return_series(price_series=source)
    assert result.source is source
    assert result.source.mode is SYS and result.source.as_of == _T0
    assert result.source.instrument_id == _INST
    assert [p.resolution_key for p in result.source.points] == [f"key-{d.isoformat()}" for d in _D[:3]]
    assert {p.currency for p in result.source.points} == {Currency.TRY}
    assert isinstance(result.points, tuple)


def test_return_series_fields_frozen() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundReturnSeries)] == ["source", "points"]
    result = build_tefas_fund_return_series(price_series=_source(("1", "2")))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.points = ()  # type: ignore[misc]


# --- direct constructor forgery protection ------------------------------------------------

def _valid_points(source):
    return build_tefas_fund_return_series(price_series=source).points


def test_direct_construction_with_valid_points_succeeds() -> None:
    source = _source(("100", "110", "99"))
    assert TefasFundReturnSeries(source=source, points=_valid_points(source)).source is source


@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile()])
def test_constructor_rejects_bad_source(bad) -> None:
    with pytest.raises(TypeError, match=_SOURCE_TYPE_MSG):
        TefasFundReturnSeries(source=bad, points=())  # type: ignore[arg-type]


def test_constructor_rejects_source_subclass() -> None:
    class _Sub(TefasFundPriceSeries):
        pass
    base = _source(("100", "110"))
    sub = _Sub(**{f.name: getattr(base, f.name) for f in dataclasses.fields(base)})
    with pytest.raises(TypeError, match=_SOURCE_TYPE_MSG):
        TefasFundReturnSeries(source=sub, points=_valid_points(base))


@pytest.mark.parametrize("bad", [[], None, "x", [object()], (object(),)])
def test_constructor_rejects_bad_points_container(bad) -> None:
    with pytest.raises(TypeError, match=_POINTS_TYPE_MSG):
        TefasFundReturnSeries(source=_source(("100", "110")), points=bad)  # type: ignore[arg-type]


def test_constructor_rejects_point_subclass() -> None:
    class _Sub(TefasFundReturnPoint):
        pass
    source = _source(("100", "110"))
    sub = _Sub(start_date=_D[0], end_date=_D[1], simple_return=Decimal("0.1"))
    with pytest.raises(TypeError, match=_POINTS_TYPE_MSG):
        TefasFundReturnSeries(source=source, points=(sub,))


def test_constructor_rejects_wrong_start_end_and_return() -> None:
    source = _source(("100", "110", "99"))
    good = _valid_points(source)
    forged = [
        (_rp(_D[1], _D[1] + timedelta(days=1), "0.1"), good[1]),                     # wrong start
        (_rp(_D[0], _D[2], "0.1"), good[1]),                                          # wrong end
        (_rp(_D[0], _D[1], "0.2"), good[1]),                                          # wrong return
        (good[0], _rp(_D[1], _D[2], "-0.1000000000000000000000000000000000000000000000001")),
    ]
    for points in forged:
        with pytest.raises(ValueError, match=_MATCH_MSG):
            TefasFundReturnSeries(source=source, points=points)


def test_constructor_rejects_missing_extra_and_reordered_points() -> None:
    source = _source(("100", "110", "99"))
    good = _valid_points(source)
    for points in ((good[0],), (), good + (good[1],), (good[1], good[0])):
        with pytest.raises(ValueError, match=_MATCH_MSG):
            TefasFundReturnSeries(source=source, points=points)


def test_forged_extra_bridging_point_is_rejected() -> None:
    source = _source(("100", "110", "99"))
    with pytest.raises(ValueError, match=_MATCH_MSG):
        TefasFundReturnSeries(source=source, points=(_rp(_D[0], _D[2], "-0.01"),))


def test_constructor_errors_are_static() -> None:
    with pytest.raises(TypeError) as info:
        TefasFundReturnSeries(source=_source(("1", "2")), points=(_Hostile(),))  # type: ignore[arg-type]
    assert str(info.value) == "points must be a tuple of exact TefasFundReturnPoint instances"


# --- end-to-end through the real Phase 16A builder ------------------------------------------

def _obs(d: date, price: str) -> TefasFundPriceObservation:
    return TefasFundPriceObservation(instrument_id=_INST, provider_symbol="MAC", trade_date=d,
                                     unit_price=Decimal(price), currency=Currency.TRY,
                                     instrument_type=InstrumentType.TEFAS_FUND,
                                     confidence_level=DataConfidenceLevel.HIGH)


def _snap(observations, trade_date_range=None) -> TefasFundPriceSnapshot:
    sid = uuid4()
    dates = [o.trade_date for o in observations]
    rng = trade_date_range or (min(dates), max(dates))
    linked = [TefasFundPriceObservation(instrument_id=o.instrument_id, provider_symbol=o.provider_symbol,
                                        trade_date=o.trade_date, unit_price=o.unit_price, currency=o.currency,
                                        instrument_type=o.instrument_type, snapshot_id=sid, payload_hash="h",
                                        confidence_level=o.confidence_level) for o in observations]
    return TefasFundPriceSnapshot(id=sid, provider="TEFAS", provider_symbol="MAC", retrieved_at=_T0,
                                  http_status=200, payload_hash="h", raw_payload="{}", instrument_id=_INST,
                                  period_months=60, trade_date_range=rng, observations=linked)


def test_end_to_end_current_reported_and_system_as_of() -> None:
    snap = _snap([_obs(_D[0], "100"), _obs(_D[1], "110"), _obs(_D[2], "99")])
    for mode, as_of in ((CR, None), (SYS, _T0 + timedelta(days=1))):
        prices = build_tefas_fund_price_series(instrument_id=_INST, trade_dates=tuple(_D[:3]), snapshots=[snap],
                                               mode=mode, as_of=as_of)
        returns = build_tefas_fund_return_series(price_series=prices)
        assert returns.source is prices
        assert [p.simple_return for p in returns.points] == [Decimal("0.1"), Decimal("-0.1")]


def test_end_to_end_gap_in_price_series_blocks_returns() -> None:
    snap = _snap([_obs(_D[0], "100"), _obs(_D[2], "121")], trade_date_range=(_D[0], _D[2]))
    prices = build_tefas_fund_price_series(instrument_id=_INST, trade_dates=tuple(_D[:3]), snapshots=[snap],
                                           mode=CR, as_of=None)
    assert not prices.is_complete
    with pytest.raises(ValueError, match=_INCOMPLETE_MSG):
        build_tefas_fund_return_series(price_series=prices)


def test_end_to_end_source_as_of_series_blocks_returns() -> None:
    snap = _snap([_obs(_D[0], "100"), _obs(_D[1], "110")])
    prices = build_tefas_fund_price_series(instrument_id=_INST, trade_dates=tuple(_D[:2]), snapshots=[snap],
                                           mode=SRC, as_of=None)
    with pytest.raises(ValueError, match=_INCOMPLETE_MSG):
        build_tefas_fund_return_series(price_series=prices)


# --- purity / scope -------------------------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_imports_are_pure_and_do_not_reach_the_resolver() -> None:
    imports = _imports()
    for banned in ("pandas", "numpy", "polars", "scipy", "requests", "httpx", "os", "pathlib", "random",
                   "secrets", "hashlib", "hmac", "socket", "sqlite3", "math", "statistics"):
        assert banned not in imports
    assert not any(m.startswith("backend.engine.private.market_data") for m in imports)
    assert not any("persistence" in m or "repository" in m for m in imports)
    assert "backend.engine.private.fund_price_series" in imports


def test_no_risk_metric_surface() -> None:
    for name in ("log_return", "annualize", "volatility", "downside_deviation", "max_drawdown", "sharpe",
                 "sortino", "calmar", "cvar", "var", "rolling", "score", "rank", "cagr", "mean_return"):
        assert not hasattr(module_under_test, name)


def test_decimal_context_constants_are_explicit() -> None:
    assert module_under_test._RETURN_DECIMAL_PRECISION == 50


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_fund_price_series_import() -> None:
    """
    Like fund_price_series.py, this module stays outside PURE_MANIFEST: it depends on fund_price_series.py,
    itself outside the manifest because it must call the market-data resolver.
    """
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/fund_return_series.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert sg.scan_g1(source, rel) == []
    assert sg.scan_g2(source, rel) == []
    assert sg.scan_g4(source, rel) == []
    assert sg.scan_g5(source, rel) == []
    assert {v.node_kind for v in sg.scan_g3(source, rel)} == {"PrivateImport:backend.engine.private.fund_price_series"}
    assert rel not in sg.PURE_MANIFEST
