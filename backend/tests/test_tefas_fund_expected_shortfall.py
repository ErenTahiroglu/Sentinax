"""
backend/tests/test_tefas_fund_expected_shortfall.py
===================================================
Tests for horizon-specific historical empirical VaR + Expected Shortfall over Phase 16D rolling returns
(Phase 16J).

loss = -return (never floored); VaR = nearest-rank empirical quantile, k = ceil(alpha * N) by exact integer
arithmetic; ES = mean of ALL losses >= VaR (ties included). No distribution, bootstrap, stress or annualization.
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

from backend.engine.private import fund_expected_shortfall as module_under_test
from backend.engine.private.domain import Currency, DataConfidenceLevel, Horizon
from backend.engine.private.fund_expected_shortfall import (
    TefasFundHistoricalExpectedShortfall,
    calculate_tefas_fund_historical_expected_shortfall,
)
from backend.engine.private.fund_price_series import TefasFundPricePoint, TefasFundPriceSeries
from backend.engine.private.fund_rolling_returns import (
    TefasFundRollingReturnSeries,
    build_tefas_fund_rolling_return_series,
)
from backend.engine.private.market_data.models import MarketDataResolutionMode

H12, H36, H60 = Horizon.ALLOCATION_12M, Horizon.STRATEGIC_3Y, Horizon.STRATEGIC_5Y
_IID = UUID(int=1)
_T0 = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
_END = (2026, 9)

_ERR_CONF = r"^confidence_level must be a finite Decimal strictly between 0 and 1$"
_ERR_EMPTY = r"^value_at_risk and expected_shortfall must both be None for an empty rolling series$"
_ERR_RANGE = r"^value_at_risk and expected_shortfall must be finite and at most 1$"
_ERR_ORDER = r"^expected_shortfall must not be below value_at_risk$"
_ERR_MATCH = r"^historical VaR and expected shortfall must match the canonical empirical calculation exactly$"
_ERR_OVERFLOW = r"^TEFAS historical expected shortfall exceeds supported Decimal analytics range$"


def _series_from_prices(prices, horizon: Horizon = H12) -> TefasFundRollingReturnSeries:
    n = len(prices)
    dated = []
    for i, price in enumerate(prices):
        index = _END[0] * 12 + (_END[1] - 1) - (n - 1 - i)
        dated.append((date(index // 12, index % 12 + 1, 28), price))
    points = tuple(
        TefasFundPricePoint(trade_date=d, unit_price=Decimal(p), currency=Currency.TRY,
                            confidence=DataConfidenceLevel.HIGH, snapshot_retrieved_at=_T0,
                            resolution_key=f"k-{d.isoformat()}")
        for d, p in dated
    )
    price_series = TefasFundPriceSeries(
        instrument_id=_IID, mode=MarketDataResolutionMode.CURRENT_REPORTED, as_of=None,
        requested_dates=tuple(d for d, _ in dated), points=points, gaps=())
    return build_tefas_fund_rolling_return_series(price_series=price_series, horizon=horizon)


def _rolling(returns, horizon: Horizon = H12) -> TefasFundRollingReturnSeries:
    """Rolling series whose windows have exactly the given returns (at most `horizon.months` of them)."""
    assert len(returns) <= horizon.months
    prices = ["100"] * horizon.months + [str(Decimal(100) * (Decimal(1) + Decimal(r))) for r in returns]
    series = _series_from_prices(prices, horizon)
    assert [p.simple_return for p in series.points] == [Decimal(r) for r in returns]
    return series


def _empty(horizon: Horizon = H12) -> TefasFundRollingReturnSeries:
    series = _series_from_prices([str(100 + i) for i in range(horizon.months)], horizon)
    assert series.points == ()
    return series


def _es(returns, confidence, horizon: Horizon = H12) -> TefasFundHistoricalExpectedShortfall:
    return calculate_tefas_fund_historical_expected_shortfall(
        rolling_series=_rolling(returns, horizon), confidence_level=Decimal(confidence))


def _calc(series: TefasFundRollingReturnSeries, confidence: Decimal) -> TefasFundHistoricalExpectedShortfall:
    return calculate_tefas_fund_historical_expected_shortfall(rolling_series=series, confidence_level=confidence)


FOUR = ["0.10", "0", "-0.10", "-0.20"]  # losses -0.10, 0, 0.10, 0.20


# --- shape ------------------------------------------------------------------------------------------------------

def test_fields_frozen_and_source_retained_by_identity() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundHistoricalExpectedShortfall)] == [
        "source", "confidence_level", "value_at_risk", "expected_shortfall"]
    series = _rolling(FOUR)
    result = calculate_tefas_fund_historical_expected_shortfall(rolling_series=series,
                                                                confidence_level=Decimal("0.75"))
    assert result.source is series
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.expected_shortfall = Decimal(0)  # type: ignore[misc]
    assert not any(f.name in ("horizon", "observation_count", "tail_count") for f in dataclasses.fields(result))


def test_builder_is_keyword_only_without_defaults() -> None:
    series = _rolling(FOUR)
    with pytest.raises(TypeError):
        calculate_tefas_fund_historical_expected_shortfall(series, Decimal("0.95"))  # type: ignore[misc]
    with pytest.raises(TypeError):
        calculate_tefas_fund_historical_expected_shortfall(rolling_series=series)  # type: ignore[call-arg]


def test_no_forbidden_surface() -> None:
    result = _es(FOUR, "0.75")
    for name in ("volatility", "annualized", "bootstrap", "stress", "weights", "score", "rank", "label",
                 "recommendation", "max_loss", "standard_error", "confidence_interval"):
        assert not hasattr(result, name) and not hasattr(module_under_test, name)


# --- numeric matrix --------------------------------------------------------------------------------------------------

def test_empty_series_is_unavailable_not_zero() -> None:
    result = calculate_tefas_fund_historical_expected_shortfall(rolling_series=_empty(),
                                                                confidence_level=Decimal("0.95"))
    assert result.value_at_risk is None and result.expected_shortfall is None
    assert (result.is_available, result.observation_count, result.tail_count) == (False, 0, 0)
    assert result.horizon is H12


def test_single_observation() -> None:
    result = _es(["-0.20"], "0.95")
    assert (result.value_at_risk, result.expected_shortfall) == (Decimal("0.20"), Decimal("0.20"))
    assert (result.tail_count, result.observation_count, result.is_available) == (1, 1, True)


def test_four_observations_at_75_percent() -> None:
    result = _es(FOUR, "0.75")
    assert result.value_at_risk == Decimal("0.10")
    assert result.expected_shortfall == Decimal("0.15")
    assert (result.tail_count, result.observation_count) == (2, 4)


def test_four_observations_at_95_percent() -> None:
    result = _es(FOUR, "0.95")
    assert (result.value_at_risk, result.expected_shortfall, result.tail_count) == (
        Decimal("0.20"), Decimal("0.20"), 1)


def test_result_types_are_exact_decimal() -> None:
    result = _es(FOUR, "0.75")
    assert type(result.value_at_risk) is Decimal and type(result.expected_shortfall) is Decimal
    assert type(result.confidence_level) is Decimal


def test_ties_at_var_are_all_in_the_tail() -> None:
    losses_as_returns = ["-0.10", "-0.20", "-0.20", "-0.40"]  # losses 0.10, 0.20, 0.20, 0.40
    result = _es(losses_as_returns, "0.5")  # k = ceil(0.5*4) = 2 -> VaR 0.20
    assert result.value_at_risk == Decimal("0.20")
    assert result.tail_count == 3
    ctx = decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=decimal.MIN_EMIN, Emax=decimal.MAX_EMAX)
    assert result.expected_shortfall == ctx.divide(Decimal("0.80"), Decimal(3))
    assert result.expected_shortfall == Decimal("0.26666666666666666666666666666666666666666666666667")
    assert result.tail_count > 4 * (1 - Decimal("0.5"))  # discrete tail may exceed (1 - alpha) * N


def test_input_order_of_losses_is_irrelevant() -> None:
    forward = _es(["-0.10", "-0.20", "-0.20", "-0.40"], "0.5")
    shuffled = _es(["-0.40", "-0.20", "-0.10", "-0.20"], "0.5")
    assert (forward.value_at_risk, forward.expected_shortfall, forward.tail_count) == (
        shuffled.value_at_risk, shuffled.expected_shortfall, shuffled.tail_count)


# --- rank arithmetic / confidence -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("alpha,k", [
    ("0.5", 2), ("0.75", 3), ("0.95", 4), ("0.99", 4),
    ("0.25", 1), ("0.2500000000000000000000000000000000000000000000001", 2),  # just above the exact boundary
    ("0.2499999999999999999999999999999999999999999999999", 1),               # just below it
    ("0.7500000000000000000000000000000000000000000000001", 4),
])
def test_rank_follows_exact_decimal_ceiling(alpha: str, k: int) -> None:
    losses = [Decimal("-0.10"), Decimal(0), Decimal("0.10"), Decimal("0.20")]
    result = _es(FOUR, alpha)
    assert result.value_at_risk == losses[k - 1]


def test_rank_is_independent_of_ambient_decimal_context() -> None:
    series = _rolling(FOUR)
    alpha = Decimal("0.7500000000000000000000000000000000000000000000001")
    baseline = _calc(series, alpha)
    for prec, rounding in ((3, decimal.ROUND_DOWN), (2, decimal.ROUND_UP), (9, decimal.ROUND_HALF_UP)):
        with decimal.localcontext() as ctx:
            ctx.prec, ctx.rounding = prec, rounding
            got = _calc(series, alpha)
            assert (got.value_at_risk, got.expected_shortfall, got.tail_count) == (
                baseline.value_at_risk, baseline.expected_shortfall, baseline.tail_count)
            assert (ctx.prec, ctx.rounding) == (prec, rounding)
    assert baseline.value_at_risk == Decimal("0.20")


def test_equivalent_confidence_spellings_give_identical_results() -> None:
    results = [_es(FOUR, spelling) for spelling in ("0.95", "0.950", "9.5E-1")]
    for other in results[1:]:
        assert (other.value_at_risk, other.expected_shortfall, other.tail_count) == (
            results[0].value_at_risk, results[0].expected_shortfall, results[0].tail_count)


@pytest.mark.parametrize("bad", [Decimal(0), Decimal(1), Decimal("-0.5"), Decimal("1.5"), Decimal(95),
                                 Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("sNaN")])
def test_confidence_range_and_finiteness(bad: Decimal) -> None:
    with pytest.raises(ValueError, match=_ERR_CONF):
        calculate_tefas_fund_historical_expected_shortfall(rolling_series=_rolling(FOUR), confidence_level=bad)


@pytest.mark.parametrize("bad", [0.95, 1, 0, True, "0.95", None])
def test_confidence_type(bad) -> None:
    with pytest.raises(TypeError):
        calculate_tefas_fund_historical_expected_shortfall(
            rolling_series=_rolling(FOUR), confidence_level=bad)  # type: ignore[arg-type]


def test_confidence_decimal_subclass_rejected() -> None:
    class _D(Decimal):
        pass
    with pytest.raises(TypeError):
        calculate_tefas_fund_historical_expected_shortfall(rolling_series=_rolling(FOUR),
                                                           confidence_level=_D("0.95"))


def test_rolling_series_type_validation() -> None:
    class _Sub(TefasFundRollingReturnSeries):
        pass
    good = _rolling(FOUR)
    sub = _Sub(source=good.source, horizon=good.horizon, points=good.points)
    for bad in (None, "x", object(), good.points, sub):
        with pytest.raises(TypeError):
            calculate_tefas_fund_historical_expected_shortfall(
                rolling_series=bad, confidence_level=Decimal("0.95"))  # type: ignore[arg-type]


# --- boundaries --------------------------------------------------------------------------------------------------------------

def test_all_gain_history_yields_negative_var_and_es_without_flooring() -> None:
    result = _es(["0.10", "0.20", "0.30"], "0.95")
    assert result.is_available is True
    assert result.value_at_risk == Decimal("-0.10")  # k = 3 -> largest loss = -0.10
    assert result.expected_shortfall == Decimal("-0.10")
    assert result.tail_count == 1
    low = _es(["0.10", "0.20", "0.30"], "0.5")  # k = 2 -> VaR -0.20, tail {-0.20, -0.10}
    assert low.value_at_risk == Decimal("-0.20") and low.expected_shortfall == Decimal("-0.15")


def test_zero_return_gives_zero_loss_not_negative_zero() -> None:
    result = _es(["0"], "0.95")
    assert result.value_at_risk == 0 and result.expected_shortfall == 0
    assert not result.value_at_risk.is_signed() and not result.expected_shortfall.is_signed()


def test_total_loss_boundary() -> None:
    series = _series_from_prices(["100"] * 12 + ["1E-60"])
    assert series.points[0].simple_return == Decimal(-1)
    result = calculate_tefas_fund_historical_expected_shortfall(rolling_series=series,
                                                                confidence_level=Decimal("0.95"))
    assert result.value_at_risk == Decimal(1) and result.expected_shortfall == Decimal(1)


def test_very_large_gain_has_very_negative_loss_and_is_not_clamped() -> None:
    series = _series_from_prices(["100"] * 12 + ["1E+30"])
    assert series.points[0].simple_return == Decimal(10 ** 28 - 1)
    result = calculate_tefas_fund_historical_expected_shortfall(rolling_series=series,
                                                                confidence_level=Decimal("0.95"))
    assert result.value_at_risk == Decimal(-(10 ** 28 - 1)) == result.expected_shortfall


def test_es_is_never_below_var() -> None:
    for alpha in ("0.01", "0.3", "0.5", "0.75", "0.9", "0.999"):
        result = _es(["0.3", "-0.4", "0", "0.1", "-0.9", "0.05"], alpha)
        assert result.expected_shortfall >= result.value_at_risk
        assert result.tail_count >= 1


# --- horizons -----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("horizon", [H12, H36, H60])
def test_horizon_is_the_source_horizon_without_conversion(horizon: Horizon) -> None:
    result = _es(FOUR, "0.75", horizon)
    assert result.horizon is horizon and result.source.horizon is horizon
    assert result.expected_shortfall == Decimal("0.15")  # no annualization or cross-horizon scaling


# --- decimal / range ----------------------------------------------------------------------------------------------------------------

def test_es_context_is_explicit() -> None:
    ctx = module_under_test._es_context()
    assert (ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax) == (50, decimal.ROUND_HALF_EVEN, decimal.MIN_EMIN,
                                                            decimal.MAX_EMAX)


def test_overflow_is_translated_and_unrelated_errors_propagate(monkeypatch) -> None:
    series = _rolling(["100"])  # loss -100 has adjusted exponent 2
    tiny = decimal.Context(prec=50, Emin=-5, Emax=0,
                           traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow])
    monkeypatch.setattr(module_under_test, "_es_context", lambda: tiny)
    with pytest.raises(ValueError, match=_ERR_OVERFLOW) as info:
        calculate_tefas_fund_historical_expected_shortfall(rolling_series=series,
                                                           confidence_level=Decimal("0.95"))
    assert info.value.__cause__ is None and info.value.__suppress_context__ is True

    def boom():
        raise KeyError("unrelated")
    monkeypatch.setattr(module_under_test, "_es_context", boom)
    with pytest.raises(KeyError):
        calculate_tefas_fund_historical_expected_shortfall(rolling_series=series,
                                                           confidence_level=Decimal("0.95"))


def test_ambient_context_perturbation_leaves_results_and_global_context_untouched() -> None:
    series = _rolling(["-0.10", "-0.20", "-0.20", "-0.40"])
    baseline = _calc(series, Decimal("0.5"))
    for prec, rounding in ((4, decimal.ROUND_DOWN), (2, decimal.ROUND_UP), (9, decimal.ROUND_HALF_UP)):
        with decimal.localcontext() as ctx:
            ctx.prec, ctx.rounding = prec, rounding
            got = _calc(series, Decimal("0.5"))
            assert got.value_at_risk == baseline.value_at_risk
            assert got.expected_shortfall == baseline.expected_shortfall
            assert got.tail_count == baseline.tail_count
            assert (ctx.prec, ctx.rounding) == (prec, rounding)
    before = (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))
    _calc(series, Decimal("0.75"))
    assert before == (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))


# --- constructor forgery ---------------------------------------------------------------------------------------------------------------

def _kw(result: TefasFundHistoricalExpectedShortfall, **over) -> dict:
    kw = dict(source=result.source, confidence_level=result.confidence_level,
              value_at_risk=result.value_at_risk, expected_shortfall=result.expected_shortfall)
    kw.update(over)
    return kw


def test_direct_construction_with_canonical_values_succeeds() -> None:
    result = _es(FOUR, "0.75")
    assert TefasFundHistoricalExpectedShortfall(**_kw(result)) == result


def test_forged_values_are_rejected() -> None:
    result = _es(FOUR, "0.75")
    with pytest.raises(ValueError, match=_ERR_MATCH):
        TefasFundHistoricalExpectedShortfall(**_kw(result, value_at_risk=Decimal("0.05")))
    with pytest.raises(ValueError, match=_ERR_MATCH):
        TefasFundHistoricalExpectedShortfall(**_kw(result, expected_shortfall=Decimal("0.16")))
    with pytest.raises(ValueError, match=_ERR_MATCH):
        TefasFundHistoricalExpectedShortfall(**_kw(result, confidence_level=Decimal("0.95")))
    with pytest.raises(ValueError, match=_ERR_ORDER):
        TefasFundHistoricalExpectedShortfall(**_kw(result, expected_shortfall=Decimal("0.05")))
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("1.5")):
        with pytest.raises(ValueError, match=_ERR_RANGE):
            TefasFundHistoricalExpectedShortfall(**_kw(result, value_at_risk=bad))
        with pytest.raises(ValueError, match=_ERR_RANGE):
            TefasFundHistoricalExpectedShortfall(**_kw(result, expected_shortfall=bad))


def test_non_empty_source_rejects_none_and_non_decimal_values() -> None:
    result = _es(FOUR, "0.75")
    class _D(Decimal):
        pass
    for bad in (None, 0.1, 1, "0.1", True, _D("0.10")):
        with pytest.raises(TypeError):
            TefasFundHistoricalExpectedShortfall(**_kw(result, value_at_risk=bad))
        with pytest.raises(TypeError):
            TefasFundHistoricalExpectedShortfall(**_kw(result, expected_shortfall=bad))


def test_empty_source_direct_construction() -> None:
    series = _empty()
    good = TefasFundHistoricalExpectedShortfall(source=series, confidence_level=Decimal("0.95"),
                                                value_at_risk=None, expected_shortfall=None)
    assert good.is_available is False
    for var, es in ((Decimal(0), Decimal(0)), (Decimal("0.1"), None), (None, Decimal("0.1")),
                    (Decimal(0), None)):
        with pytest.raises(ValueError, match=_ERR_EMPTY):
            TefasFundHistoricalExpectedShortfall(source=series, confidence_level=Decimal("0.95"),
                                                 value_at_risk=var, expected_shortfall=es)


def test_constructor_validates_source_and_confidence() -> None:
    result = _es(FOUR, "0.75")
    for bad in (None, object(), result.source.points):
        with pytest.raises(TypeError):
            TefasFundHistoricalExpectedShortfall(**_kw(result, source=bad))
    with pytest.raises(ValueError, match=_ERR_CONF):
        TefasFundHistoricalExpectedShortfall(**_kw(result, confidence_level=Decimal(1)))
    with pytest.raises(TypeError):
        TefasFundHistoricalExpectedShortfall(**_kw(result, confidence_level=0.75))


# --- purity / scope ------------------------------------------------------------------------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_imports_are_limited_and_source_is_float_free() -> None:
    imports = _imports()
    assert {m for m in imports if m.startswith("backend.")} == {"backend.engine.private.fund_rolling_returns"}
    assert imports <= {"__future__", "dataclasses", "decimal", "uuid", "backend.engine.private.fund_rolling_returns"}
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    assert not any(isinstance(n, ast.Name) and n.id == "float" for n in ast.walk(tree))
    assert not any(isinstance(n, ast.Constant) and isinstance(n.value, float) for n in ast.walk(tree))


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_the_required_import() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/fund_expected_shortfall.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert sg.scan_g1(source, rel) == []
    assert sg.scan_g2(source, rel) == []
    assert sg.scan_g4(source, rel) == []
    assert sg.scan_g5(source, rel) == []
    assert {v.node_kind for v in sg.scan_g3(source, rel)} == {
        "PrivateImport:backend.engine.private.fund_rolling_returns"}
    assert rel not in sg.PURE_MANIFEST
