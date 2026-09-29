"""
backend/tests/test_tefas_fund_drawdown.py
=========================================
Tests for the deterministic TEFAS maximum drawdown + recovery diagnostics (Phase 16C).

Drawdown derives only from a complete authoritative TefasFundPriceSeries. Loss magnitude in [0, 1]:
1 - P_t / running_peak, explicit 50-digit ROUND_HALF_EVEN Decimal context, earliest-peak / earliest-maximum
tie semantics, first recovery >= peak, calendar-day durations, unrecovered -> None.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import fund_drawdown as module_under_test
from backend.engine.private.domain import Currency, DataConfidenceLevel
from backend.engine.private.fund_drawdown import TefasFundDrawdown, calculate_tefas_fund_drawdown
from backend.engine.private.fund_price_series import TefasFundPriceGap, TefasFundPricePoint, TefasFundPriceSeries
from backend.engine.private.market_data.models import MarketDataResolutionMode, MarketDataResolutionStatus

CR = MarketDataResolutionMode.CURRENT_REPORTED
SYS = MarketDataResolutionMode.SYSTEM_AS_OF
SRC = MarketDataResolutionMode.SOURCE_AS_OF
ST = MarketDataResolutionStatus

_INST = UUID("11111111-1111-4111-8111-111111111111")
_T0 = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
_D = [date(2026, 3, 2) + timedelta(days=i) for i in range(10)]

_INCOMPLETE_MSG = r"^TEFAS fund drawdown requires a complete price series$"
_TWO_MSG = r"^TEFAS fund drawdown requires at least two price points$"
_SOURCE_MSG = r"^source must be an exact TefasFundPriceSeries instance$"
_MATCH_MSG = r"^drawdown result must match the canonical calculation from source$"


def _p(d: date, price: str) -> TefasFundPricePoint:
    return TefasFundPricePoint(trade_date=d, unit_price=Decimal(price), currency=Currency.TRY,
                               confidence=DataConfidenceLevel.HIGH, snapshot_retrieved_at=_T0,
                               resolution_key=f"key-{d.isoformat()}")


def _source(prices, dates=None, mode=CR, as_of=None) -> TefasFundPriceSeries:
    dates = list(dates) if dates is not None else _D[: len(prices)]
    return TefasFundPriceSeries(instrument_id=_INST, mode=mode, as_of=as_of, requested_dates=tuple(dates),
                                points=tuple(_p(d, x) for d, x in zip(dates, prices)), gaps=())


def _dd(prices, dates=None, **kw) -> TefasFundDrawdown:
    return calculate_tefas_fund_drawdown(price_series=_source(prices, dates, **kw))


class _DecimalSub(Decimal):
    pass


class _Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")


# --- structure --------------------------------------------------------------------

def test_result_fields_frozen_and_source_identity() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundDrawdown)] == [
        "source", "max_drawdown", "peak_date", "trough_date", "recovery_date"]
    source = _source(("100", "80"))
    result = calculate_tefas_fund_drawdown(price_series=source)
    assert result.source is source
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.max_drawdown = Decimal("0")  # type: ignore[misc]
    for attr in ("instrument_id", "mode", "currency", "resolution_key", "as_of", "volatility", "sortino", "calmar"):
        assert not hasattr(result, attr)


def test_builder_is_keyword_only_and_takes_only_the_series() -> None:
    with pytest.raises(TypeError):
        calculate_tefas_fund_drawdown(_source(("1", "2")))  # type: ignore[misc]
    with pytest.raises(TypeError):
        calculate_tefas_fund_drawdown(price_series=_source(("1", "2")), mode=CR)  # type: ignore[call-arg]


# --- numeric matrix -----------------------------------------------------------------

def test_flat_series_is_observed_zero_drawdown() -> None:
    r = _dd(("100", "100"))
    assert r.max_drawdown == Decimal("0") and type(r.max_drawdown) is Decimal
    assert r.peak_date == r.trough_date == r.recovery_date == _D[0]
    assert (r.decline_days, r.recovery_days, r.underwater_days) == (0, 0, 0)


def test_monotonic_rise_is_zero_drawdown() -> None:
    r = _dd(("100", "110", "121"))
    assert r.max_drawdown == 0
    assert r.peak_date == r.trough_date == r.recovery_date == _D[0]
    assert (r.decline_days, r.recovery_days, r.underwater_days) == (0, 0, 0)


def test_simple_decline_unrecovered() -> None:
    r = _dd(("100", "80"))
    assert r.max_drawdown == Decimal("0.2")
    assert (r.peak_date, r.trough_date, r.recovery_date) == (_D[0], _D[1], None)
    assert r.decline_days == 1 and r.recovery_days is None and r.underwater_days is None


def test_decline_then_recovery_at_peak() -> None:
    r = _dd(("100", "80", "100"))
    assert r.max_drawdown == Decimal("0.2")
    assert (r.peak_date, r.trough_date, r.recovery_date) == (_D[0], _D[1], _D[2])
    assert (r.decline_days, r.recovery_days, r.underwater_days) == (1, 1, 2)


def test_rise_then_fall_is_measured_from_the_running_peak() -> None:
    r = _dd(("100", "150", "75"))
    assert r.max_drawdown == Decimal("0.5")
    assert (r.peak_date, r.trough_date, r.recovery_date) == (_D[1], _D[2], None)


def test_peak_120_trough_60_recovered_and_unrecovered() -> None:
    recovered = _dd(("100", "120", "60", "120"))
    assert recovered.max_drawdown == Decimal("0.5")
    assert (recovered.peak_date, recovered.trough_date, recovered.recovery_date) == (_D[1], _D[2], _D[3])
    unrecovered = _dd(("100", "120", "60", "119"))
    assert unrecovered.max_drawdown == Decimal("0.5")
    assert unrecovered.recovery_date is None
    assert unrecovered.recovery_days is None and unrecovered.underwater_days is None
    assert unrecovered.decline_days == 1


def test_recovery_is_first_point_at_or_above_peak_not_a_later_higher_one() -> None:
    r = _dd(("100", "120", "60", "120", "130"))
    assert r.recovery_date == _D[3]
    assert (r.decline_days, r.recovery_days, r.underwater_days) == (1, 1, 2)


def test_unrecovered_example_does_not_use_final_date() -> None:
    r = _dd(("100", "120", "60", "90"))
    assert r.max_drawdown == Decimal("0.5")
    assert (r.peak_date, r.trough_date) == (_D[1], _D[2])
    assert r.recovery_date is None and r.recovery_days is None and r.underwater_days is None


def test_new_higher_peak_changes_the_reference() -> None:
    r = _dd(("100", "80", "120", "90"))
    assert r.max_drawdown == Decimal("0.25")  # 1 - 90/120, deeper than the earlier 0.20
    assert (r.peak_date, r.trough_date, r.recovery_date) == (_D[2], _D[3], None)


def test_recovery_must_be_strictly_after_trough_and_at_or_above_peak() -> None:
    r = _dd(("100", "80", "99", "100"))
    assert r.recovery_date == _D[3]  # 99 < 100 does not recover


def test_calendar_day_durations_use_actual_dates() -> None:
    dates = [date(2026, 3, 2), date(2026, 3, 5), date(2026, 3, 12)]
    r = _dd(("100", "80", "100"), dates=dates)
    assert (r.decline_days, r.recovery_days, r.underwater_days) == (3, 7, 10)


# --- ties -----------------------------------------------------------------------------

def test_equal_running_peak_keeps_first_peak_date() -> None:
    r = _dd(("100", "100", "80"))
    assert r.peak_date == _D[0] and r.trough_date == _D[2] and r.max_drawdown == Decimal("0.2")


def test_equal_maximum_depth_retains_first_episode() -> None:
    r = _dd(("100", "80", "150", "120"))  # both episodes are exactly 0.2
    assert r.max_drawdown == Decimal("0.2")
    assert (r.peak_date, r.trough_date, r.recovery_date) == (_D[0], _D[1], _D[2])  # 150 >= 100 recovers


def test_equal_depth_same_peak_retains_first_trough() -> None:
    r = _dd(("100", "80", "90", "80"))
    assert r.trough_date == _D[1]
    assert r.recovery_date is None


# --- decimal context -----------------------------------------------------------------

def test_decimal_context_is_explicit_and_maximal() -> None:
    ctx = module_under_test._drawdown_context()
    assert ctx.prec == 50 and ctx.rounding == decimal.ROUND_HALF_EVEN
    assert ctx.Emin == decimal.MIN_EMIN and ctx.Emax == decimal.MAX_EMAX
    assert module_under_test._DRAWDOWN_DECIMAL_PRECISION == 50


def test_repeating_drawdown_is_deterministic_fifty_digit() -> None:
    r = _dd(("3", "2"))
    assert r.max_drawdown == Decimal("0.33333333333333333333333333333333333333333333333333")


def test_extreme_tiny_trough_rounds_to_one_without_zero_price() -> None:
    source = _source(("1", "1E-1000000"))
    assert all(p.unit_price > 0 for p in source.points)
    r = calculate_tefas_fund_drawdown(price_series=source)
    assert r.max_drawdown == Decimal("1")
    assert (r.peak_date, r.trough_date, r.recovery_date) == (_D[0], _D[1], None)


def test_extreme_range_extremes_do_not_leak_decimal_exceptions() -> None:
    huge = f"1E+{decimal.MAX_EMAX}"
    tiny = f"1E-{decimal.MAX_EMAX}"
    assert calculate_tefas_fund_drawdown(price_series=_source((huge, tiny))).max_drawdown == Decimal("1")
    assert calculate_tefas_fund_drawdown(price_series=_source((tiny, huge))).max_drawdown == Decimal("0")


@pytest.mark.parametrize("spelling", ["100", "100.0", "100.00", "1E+2"])
def test_price_spellings_are_equivalent(spelling: str) -> None:
    r = _dd((spelling, "80"))
    assert r.max_drawdown == Decimal("0.2")


def test_result_independent_of_ambient_decimal_context_and_does_not_mutate_it() -> None:
    prices = ("3", "7", "2", "5", "1")
    baseline = _dd(prices)
    for prec, rounding in ((4, decimal.ROUND_DOWN), (2, decimal.ROUND_UP), (9, decimal.ROUND_HALF_UP)):
        with decimal.localcontext() as ctx:
            ctx.prec = prec
            ctx.rounding = rounding
            perturbed = _dd(prices)
            assert perturbed.max_drawdown == baseline.max_drawdown
            assert (perturbed.peak_date, perturbed.trough_date, perturbed.recovery_date) == (
                baseline.peak_date, baseline.trough_date, baseline.recovery_date)
            assert ctx.prec == prec and ctx.rounding == rounding
    before = (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))
    _dd(prices)
    assert before == (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))


def test_drawdown_stays_within_zero_and_one() -> None:
    for prices in (("1", "1E-40", "1E-1000000"), ("5", "1", "9", "3", "27", "1")):
        r = _dd(prices)
        assert Decimal(0) <= r.max_drawdown <= Decimal(1)


# --- gaps / insufficiency ---------------------------------------------------------------

def test_gap_in_source_fails_before_any_metric() -> None:
    source = TefasFundPriceSeries(instrument_id=_INST, mode=CR, as_of=None, requested_dates=(_D[0], _D[1], _D[2]),
                                  points=(_p(_D[0], "100"), _p(_D[2], "50")),
                                  gaps=(TefasFundPriceGap(_D[1], ST.NO_ELIGIBLE_OBSERVATION),))
    with pytest.raises(ValueError, match=_INCOMPLETE_MSG):
        calculate_tefas_fund_drawdown(price_series=source)
    with pytest.raises(ValueError, match=_INCOMPLETE_MSG):
        TefasFundDrawdown(source=source, max_drawdown=Decimal("0.5"), peak_date=_D[0], trough_date=_D[2],
                          recovery_date=None)


def test_source_as_of_series_is_all_gaps_and_fails() -> None:
    source = TefasFundPriceSeries(instrument_id=_INST, mode=SRC, as_of=None, requested_dates=(_D[0], _D[1]),
                                  points=(), gaps=(TefasFundPriceGap(_D[0], ST.UNAVAILABLE_SOURCE_AS_OF),
                                                   TefasFundPriceGap(_D[1], ST.UNAVAILABLE_SOURCE_AS_OF)))
    with pytest.raises(ValueError, match=_INCOMPLETE_MSG):
        calculate_tefas_fund_drawdown(price_series=source)


def test_fewer_than_two_points_fails_not_zero() -> None:
    with pytest.raises(ValueError, match=_TWO_MSG):
        calculate_tefas_fund_drawdown(price_series=_source(("100",)))


def test_complete_system_as_of_source_is_evaluated_and_retained() -> None:
    source = _source(("100", "80"), mode=SYS, as_of=_T0)
    r = calculate_tefas_fund_drawdown(price_series=source)
    assert r.source is source and r.source.mode is SYS and r.max_drawdown == Decimal("0.2")


@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile()])
def test_builder_rejects_non_series(bad) -> None:
    with pytest.raises(TypeError, match=_SOURCE_MSG):
        calculate_tefas_fund_drawdown(price_series=bad)  # type: ignore[arg-type]


def test_builder_and_constructor_reject_source_subclass() -> None:
    class _Sub(TefasFundPriceSeries):
        pass
    base = _source(("100", "80"))
    sub = _Sub(**{f.name: getattr(base, f.name) for f in dataclasses.fields(base)})
    with pytest.raises(TypeError, match=_SOURCE_MSG):
        calculate_tefas_fund_drawdown(price_series=sub)
    with pytest.raises(TypeError, match=_SOURCE_MSG):
        TefasFundDrawdown(source=sub, max_drawdown=Decimal("0.2"), peak_date=_D[0], trough_date=_D[1],
                          recovery_date=None)


# --- derived properties ---------------------------------------------------------------

def test_derived_properties_are_calendar_day_ints() -> None:
    r = _dd(("100", "80", "100"))
    assert all(type(v) is int for v in (r.decline_days, r.recovery_days, r.underwater_days))


# --- forgery protection ------------------------------------------------------------------

def _good():
    source = _source(("100", "120", "60", "120"))
    return source, calculate_tefas_fund_drawdown(price_series=source)


def _forge(source, **overrides):
    kwargs = dict(source=source, max_drawdown=Decimal("0.5"), peak_date=_D[1], trough_date=_D[2],
                  recovery_date=_D[3])
    kwargs.update(overrides)
    return TefasFundDrawdown(**kwargs)


def test_direct_construction_with_canonical_values_succeeds() -> None:
    source, good = _good()
    assert _forge(source) == good


@pytest.mark.parametrize("override", [
    {"max_drawdown": Decimal("0.4")}, {"max_drawdown": Decimal("0.5000000001")}, {"max_drawdown": Decimal("0")},
    {"peak_date": _D[0]}, {"trough_date": _D[3]}, {"recovery_date": None}, {"recovery_date": _D[2]},
    {"peak_date": _D[2], "trough_date": _D[2]},
])
def test_forged_values_are_rejected(override) -> None:
    source, _ = _good()
    with pytest.raises(ValueError):
        _forge(source, **override)


def test_forged_recovery_on_unrecovered_series_is_rejected() -> None:
    source = _source(("100", "120", "60", "119"))
    with pytest.raises(ValueError, match=_MATCH_MSG):
        TefasFundDrawdown(source=source, max_drawdown=Decimal("0.5"), peak_date=_D[1], trough_date=_D[2],
                          recovery_date=_D[3])


def test_forged_zero_drawdown_on_falling_series_is_rejected() -> None:
    source = _source(("100", "80"))
    with pytest.raises(ValueError, match=_MATCH_MSG):
        TefasFundDrawdown(source=source, max_drawdown=Decimal("0"), peak_date=_D[0], trough_date=_D[0],
                          recovery_date=_D[0])


@pytest.mark.parametrize("value,exc", [
    (0.5, TypeError), (1, TypeError), (True, TypeError), ("0.5", TypeError), (None, TypeError),
    (_DecimalSub("0.5"), TypeError), (Decimal("NaN"), ValueError), (Decimal("Infinity"), ValueError),
    (Decimal("-0.1"), ValueError), (Decimal("1.0000000001"), ValueError),
])
def test_max_drawdown_type_and_range_validation(value, exc) -> None:
    source, _ = _good()
    with pytest.raises(exc):
        _forge(source, max_drawdown=value)


@pytest.mark.parametrize("field,value", [
    ("peak_date", datetime(2026, 3, 3)), ("peak_date", "2026-03-03"), ("peak_date", None),
    ("trough_date", datetime(2026, 3, 4)), ("trough_date", None),
    ("recovery_date", datetime(2026, 3, 5)), ("recovery_date", "x"),
])
def test_date_fields_require_exact_dates(field, value) -> None:
    source, _ = _good()
    with pytest.raises(TypeError):
        _forge(source, **{field: value})


def test_date_ordering_is_enforced() -> None:
    source, _ = _good()
    with pytest.raises(ValueError):
        _forge(source, peak_date=_D[3], trough_date=_D[2])
    with pytest.raises(ValueError):
        _forge(source, recovery_date=_D[1])


def test_constructor_errors_are_static() -> None:
    source, _ = _good()
    with pytest.raises(TypeError) as info:
        _forge(source, peak_date=_Hostile())
    assert str(info.value) == "peak_date must be an exact date instance"


# --- purity / scope ---------------------------------------------------------------------

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


def test_no_other_metric_surface() -> None:
    for name in ("volatility", "downside_deviation", "sortino", "calmar", "cvar", "var", "sharpe", "rolling",
                 "rank", "score", "cagr", "annualize"):
        assert not hasattr(module_under_test, name)


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_fund_price_series_import() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/fund_drawdown.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert sg.scan_g1(source, rel) == []
    assert sg.scan_g2(source, rel) == []
    assert sg.scan_g4(source, rel) == []
    assert sg.scan_g5(source, rel) == []
    assert {v.node_kind for v in sg.scan_g3(source, rel)} == {"PrivateImport:backend.engine.private.fund_price_series"}
    assert rel not in sg.PURE_MANIFEST
