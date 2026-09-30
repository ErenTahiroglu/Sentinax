"""
backend/tests/test_us_macro_evidence.py
=======================================
Tests for PIT-safe monthly U.S. macro evidence transforms (Phase 17G): 3m/3m SAAR growth, raw real yield / broad
dollar / financial stress / curve levels, and 120-month prior-window robust normalization (median / MAD).
One snapshot per explicit mode/as_of; no score, regime, weight or portfolio action.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private.domain import AsOfMode, DataConfidenceLevel, DataStatus, SourceTier
from backend.engine.private.macro import us_macro_evidence as module_under_test
from backend.engine.private.macro.state_inputs import MacroStateInputFact, build_macro_state_input_fact
from backend.engine.private.macro.us_macro_evidence import (
    GrowthMomentumEvidence,
    NormalizedMacroLevelEvidence,
    RobustNormalizationEvidence,
    USMacroEvidenceSnapshot,
    YieldCurveEvidence,
    FinancialStressEvidence,
    build_us_macro_evidence_snapshot,
)
from backend.engine.private.macro.us_treasury_curve import (
    USTreasuryCurveSlopePoint,
    build_us_treasury_curve_slope_history,
)

UTC = timezone.utc
AS_OF = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
BEFORE = AS_OF - timedelta(days=40)
K_IP, K_REAL, K_USD, K_STRESS = (
    "US_INDUSTRIAL_PRODUCTION", "US_TREASURY_REAL_10Y_YIELD", "US_BROAD_DOLLAR_INDEX", "US_FINANCIAL_STRESS_INDEX")
K10, K2, K3M = "US_TREASURY_PAR_10Y", "US_TREASURY_PAR_2Y", "US_TREASURY_PAR_3M"
SEP_2026 = 200  # month index: 2010-01 == 0
_counter = [0]
_REF = decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=decimal.MIN_EMIN, Emax=decimal.MAX_EMAX)


def _mon(n: int, day: int = 1) -> date:
    return date(2010 + n // 12, n % 12 + 1, day)


def _fact(key, d, value="1", *, mode=AsOfMode.SYSTEM_AS_OF, as_of=AS_OF, status=None) -> MacroStateInputFact:
    _counter[0] += 1
    if status is None:
        status = DataStatus.UNAVAILABLE if value is None else DataStatus.COMPLETE
    return build_macro_state_input_fact(
        canonical_key=key, effective_date=d, value=None if value is None else Decimal(value),
        data_status=status, confidence_level=DataConfidenceLevel.HIGH, source_tier=SourceTier.TIER_1_REGULATORY,
        mode=mode, as_of=as_of, published_at=BEFORE, observed_at=BEFORE, ingested_at=BEFORE, superseded_at=None,
        observation_id=UUID(int=1000 + _counter[0]), snapshot_id=UUID(int=2000 + _counter[0]))


def _curve(days_values, *, as_of=AS_OF, mode=AsOfMode.SYSTEM_AS_OF):
    """days_values: list of (date, ten, two, three) -> Phase17D points."""
    ten, two, three = [], [], []
    for d, a, b, c in days_values:
        if a is not None:
            ten.append(_fact(K10, d, a, mode=mode, as_of=as_of))
        if b is not None:
            two.append(_fact(K2, d, b, mode=mode, as_of=as_of))
        if c is not None:
            three.append(_fact(K3M, d, c, mode=mode, as_of=as_of))
    return build_us_treasury_curve_slope_history(
        ten_year_history=tuple(ten), two_year_history=tuple(two), three_month_history=tuple(three),
        mode=mode, as_of=as_of)


def _build(ip=(), real=(), usd=(), stress=(), curve=(), **over):
    kw = dict(industrial_production_history=tuple(ip), real_10y_yield_history=tuple(real),
              broad_dollar_history=tuple(usd), financial_stress_history=tuple(stress),
              treasury_curve_history=tuple(curve), mode=AsOfMode.SYSTEM_AS_OF, as_of=AS_OF)
    kw.update(over)
    return build_us_macro_evidence_snapshot(**kw)


def _monthly(key, start, values, day=15):
    return [_fact(key, _mon(start + i, day), v) for i, v in enumerate(values)]


def _window_values():
    """120 prior months (80..199) with values 1..120, then current month 200 == 200."""
    return [str(k) for k in range(1, 121)] + ["200"]


# --- surface ----------------------------------------------------------------------------------------------------

def test_dataclass_fields_are_exact_and_frozen() -> None:
    def names(cls):
        return [f.name for f in dataclasses.fields(cls)]
    assert names(RobustNormalizationEvidence) == [
        "window_start_month", "window_end_month", "median", "mad", "scaled_mad", "z"]
    assert names(GrowthMomentumEvidence) == ["reference_month", "source_facts", "raw_3m3m_saar", "normalization"]
    assert names(NormalizedMacroLevelEvidence) == ["reference_month", "source_fact", "raw_value", "normalization"]
    assert names(YieldCurveEvidence) == [
        "reference_month", "source_point", "primary_10y_3m", "diagnostic_10y_2y", "normalization"]
    assert names(FinancialStressEvidence) == ["reference_month", "source_fact", "raw_value"]
    assert names(USMacroEvidenceSnapshot) == [
        "mode", "as_of", "growth", "real_yield", "yield_curve", "broad_dollar", "financial_stress"]
    snap = _build()
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.growth = None


def test_builder_is_keyword_only_without_defaults() -> None:
    params = inspect.signature(build_us_macro_evidence_snapshot).parameters
    assert list(params) == [
        "industrial_production_history", "real_10y_yield_history", "broad_dollar_history",
        "financial_stress_history", "treasury_curve_history", "mode", "as_of"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
               for p in params.values())


def test_returns_one_snapshot_and_no_history_builder() -> None:
    assert type(_build()) is USMacroEvidenceSnapshot
    assert not [n for n in dir(module_under_test) if n.startswith("build_") and n.endswith("_history")]
    assert [n for n in dir(module_under_test) if n.startswith("build_")] == ["build_us_macro_evidence_snapshot"]


def test_empty_inputs_give_snapshot_with_unavailable_evidence() -> None:
    mode = AsOfMode.SOURCE_AS_OF
    snap = _build(mode=mode)
    assert snap == USMacroEvidenceSnapshot(mode=mode, as_of=AS_OF, growth=None, real_yield=None, yield_curve=None,
                                           broad_dollar=None, financial_stress=None)
    assert snap.mode is mode and snap.as_of is AS_OF


# --- type contract ----------------------------------------------------------------------------------------------

_SLOTS = ["industrial_production_history", "real_10y_yield_history", "broad_dollar_history",
          "financial_stress_history", "treasury_curve_history"]


@pytest.mark.parametrize("slot", _SLOTS)
def test_histories_must_be_exact_tuples(slot) -> None:
    for bad in ([], (x for x in ()), set(), None, "x"):
        with pytest.raises(TypeError, match=r"history must be an exact tuple"):
            build_us_macro_evidence_snapshot(**{**_empty_kwargs(), slot: bad})

    class _T(tuple):
        pass
    with pytest.raises(TypeError, match=r"history must be an exact tuple"):
        build_us_macro_evidence_snapshot(**{**_empty_kwargs(), slot: _T()})


def _empty_kwargs():
    return {s: () for s in _SLOTS} | {"mode": AsOfMode.SYSTEM_AS_OF, "as_of": AS_OF}


def test_members_must_be_exact_types() -> None:
    @dataclasses.dataclass(frozen=True)
    class _Sub(MacroStateInputFact):
        pass
    good = _fact(K_REAL, _mon(SEP_2026, 15))
    sub = _Sub(**{f.name: getattr(good, f.name) for f in dataclasses.fields(good)})
    for bad in (object(), None, 4.0, sub):
        with pytest.raises(TypeError, match=r"every history member must be an exact MacroStateInputFact"):
            _build(real=[bad])
    with pytest.raises(TypeError, match=r"every history member must be an exact USTreasuryCurveSlopePoint"):
        _build(curve=[good])
    with pytest.raises(TypeError, match=r"every history member must be an exact USTreasuryCurveSlopePoint"):
        _build(curve=[None])


def test_mode_and_as_of_types_are_exact() -> None:
    class _DT(datetime):
        pass
    for bad in ("system_as_of", None):
        with pytest.raises(TypeError, match=r"mode must be an exact AsOfMode"):
            _build(mode=bad)
    for bad in (datetime(2026, 9, 30, 12), "2026-09-30", None, date(2026, 9, 30), _DT(2026, 9, 30, 12, tzinfo=UTC)):
        with pytest.raises(TypeError, match=r"as_of must be an exact timezone-aware datetime"):
            _build(as_of=bad)


# --- canonical series slots -------------------------------------------------------------------------------------

def test_series_slots_are_locked() -> None:
    d = _mon(SEP_2026 - 1, 15)
    cases = [
        ("ip", K_REAL), ("ip", "US_REAL_GDP"), ("real", "US_EFFECTIVE_FED_FUNDS_RATE"), ("real", K_USD),
        ("usd", "TR_FX_USDTRY"), ("usd", K_REAL), ("stress", K_USD), ("stress", "US_TREASURY_PAR_10Y"),
    ]
    for slot, key in cases:
        with pytest.raises(ValueError, match=r"canonical_key must be US_"):
            _build(**{slot: [_fact(key, d)]})


# --- PIT context ------------------------------------------------------------------------------------------------

def test_wrong_mode_and_as_of_are_rejected() -> None:
    d = _mon(SEP_2026 - 1, 15)
    with pytest.raises(ValueError, match=r"mode does not match"):
        _build(real=[_fact(K_REAL, d, mode=AsOfMode.SOURCE_AS_OF)])
    with pytest.raises(ValueError, match=r"as_of does not match"):
        _build(usd=[_fact(K_USD, d, as_of=AS_OF + timedelta(hours=1))])
    pts = _curve([(d, "4", "3", "2")], as_of=AS_OF + timedelta(hours=1))
    with pytest.raises(ValueError, match=r"as_of does not match"):
        _build(curve=pts)
    pts = _curve([(d, "4", "3", "2")], mode=AsOfMode.SOURCE_AS_OF)
    with pytest.raises(ValueError, match=r"mode does not match"):
        _build(curve=pts)


def test_unsorted_and_duplicate_histories_are_rejected_not_repaired() -> None:
    a, b = _fact(K_REAL, date(2026, 9, 10)), _fact(K_REAL, date(2026, 9, 11))
    with pytest.raises(ValueError, match=r"strictly increasing"):
        _build(real=[b, a])
    with pytest.raises(ValueError, match=r"strictly increasing"):
        _build(real=[a, _fact(K_REAL, date(2026, 9, 10))])
    pts = _curve([(date(2026, 9, 10), "4", "3", "2"), (date(2026, 9, 11), "4", "3", "2")])
    with pytest.raises(ValueError, match=r"strictly increasing"):
        _build(curve=(pts[1], pts[0]))
    with pytest.raises(ValueError, match=r"strictly increasing"):
        _build(curve=(pts[0], pts[0]))


def test_future_effective_dates_are_rejected_not_filtered() -> None:
    future = AS_OF.date() + timedelta(days=1)
    for slot, key in (("ip", K_IP), ("real", K_REAL), ("usd", K_USD), ("stress", K_STRESS)):
        with pytest.raises(ValueError, match=r"effective_date is after as_of"):
            _build(**{slot: [_fact(key, future)]})
    with pytest.raises(ValueError, match=r"effective_date is after as_of"):
        _build(curve=_curve([(future, "4", "3", "2")]))
    _build(real=[_fact(K_REAL, AS_OF.date())])  # same day is allowed


# --- monthly sampling -------------------------------------------------------------------------------------------

def test_last_actual_object_in_month_is_selected() -> None:
    a, b = _fact(K_REAL, date(2026, 9, 10), "1.5"), _fact(K_REAL, date(2026, 9, 20), "2.5")
    r = _build(real=[a, b]).real_yield
    assert r.source_fact is b and r.raw_value == Decimal("2.5") and r.reference_month == date(2026, 9, 1)


def test_explicit_unavailable_wins_when_latest_in_month() -> None:
    a, b = _fact(K_REAL, date(2026, 9, 20), "2.5"), _fact(K_REAL, date(2026, 9, 27), None)
    r = _build(real=[a, b]).real_yield
    assert r.source_fact is b and r.raw_value is None and r.normalization is None


def test_latest_unavailable_does_not_fall_back_to_prior_month() -> None:
    july, aug = _fact(K_REAL, date(2026, 7, 15), "2"), _fact(K_REAL, date(2026, 8, 15), None)
    snap = _build(real=[july, aug], usd=[_fact(K_USD, date(2026, 8, 15), "100"), _fact(K_USD, date(2026, 8, 20), None)],
                  stress=[_fact(K_STRESS, date(2026, 7, 3), "0.1"), _fact(K_STRESS, date(2026, 8, 7), None)])
    assert snap.real_yield is not None and snap.real_yield.source_fact is aug
    assert snap.real_yield.reference_month == date(2026, 8, 1) and snap.real_yield.raw_value is None
    assert snap.broad_dollar.raw_value is None and snap.broad_dollar.normalization is None
    assert snap.financial_stress.raw_value is None and snap.financial_stress.reference_month == date(2026, 8, 1)


def test_no_fill_or_interpolation_reference_month_is_real() -> None:
    snap = _build(real=[_fact(K_REAL, date(2026, 5, 29), "1")])
    assert snap.real_yield.reference_month == date(2026, 5, 1)


# --- growth -----------------------------------------------------------------------------------------------------

def _ip(months_values):
    return [_fact(K_IP, _mon(m), v) for m, v in months_values]


def test_growth_formula_exact_fixture() -> None:
    facts = _ip([(SEP_2026 - 5 + i, v) for i, v in enumerate(["1", "1", "1", "2", "2", "2"])])
    g = _build(ip=facts).growth
    assert g.raw_3m3m_saar == Decimal("1500") and g.reference_month == _mon(SEP_2026)
    assert g.normalization is None
    assert len(g.source_facts) == 6
    assert all(g.source_facts[i] is facts[i] for i in range(6))


def test_growth_uses_sums_and_annualizes_by_four() -> None:
    facts = _ip([(SEP_2026 - 5 + i, v) for i, v in enumerate(["100", "100", "100", "101", "101", "101"])])
    expected = _REF.multiply(Decimal(100), _REF.subtract(_REF.power(_REF.divide(Decimal(303), Decimal(300)),
                                                                   Decimal(4)), Decimal(1)))
    assert _build(ip=facts).growth.raw_3m3m_saar == expected


def test_growth_calendar_gap_is_not_compressed() -> None:
    facts = _ip([(m, "1") for m in (SEP_2026 - 6, SEP_2026 - 5, SEP_2026 - 4, SEP_2026 - 2, SEP_2026 - 1, SEP_2026)])
    g = _build(ip=facts).growth
    assert g.raw_3m3m_saar is None and g.normalization is None
    assert g.source_facts[2] is None  # t-3 is the genuinely absent calendar month
    assert [s is None for s in g.source_facts].count(True) == 1


def test_growth_explicit_unavailable_slot_is_kept_and_blocks_raw() -> None:
    facts = _ip([(SEP_2026 - 5 + i, "1") for i in range(6)])
    facts[2] = _fact(K_IP, _mon(SEP_2026 - 3), None)
    g = _build(ip=facts).growth
    assert g.source_facts[2] is facts[2] and g.raw_3m3m_saar is None


def test_growth_latest_unavailable_has_reference_month_and_no_fallback() -> None:
    facts = _ip([(SEP_2026 - 6 + i, "1") for i in range(6)]) + [_fact(K_IP, _mon(SEP_2026), None)]
    g = _build(ip=facts).growth
    assert g is not None and g.reference_month == _mon(SEP_2026)
    assert g.raw_3m3m_saar is None and g.normalization is None and g.source_facts[-1] is facts[-1]


def test_growth_reference_month_may_lag_other_components() -> None:
    ip = _ip([(SEP_2026 - 2 - 5 + i, "1") for i in range(6)])
    snap = _build(ip=ip, real=[_fact(K_REAL, date(2026, 9, 25), "1")],
                  usd=[_fact(K_USD, date(2026, 9, 25), "100")],
                  stress=[_fact(K_STRESS, date(2026, 8, 28), "0.5")],
                  curve=_curve([(date(2026, 9, 25), "4", "3", "2")]))
    assert snap.growth.reference_month == date(2026, 7, 1)
    assert snap.real_yield.reference_month == date(2026, 9, 1)
    assert snap.yield_curve.reference_month == date(2026, 9, 1)
    assert snap.broad_dollar.reference_month == date(2026, 9, 1)
    assert snap.financial_stress.reference_month == date(2026, 8, 1)


@pytest.mark.parametrize("bad", ["0", "-1", "-0.5"])
def test_non_positive_industrial_index_is_malformed(bad) -> None:
    facts = _ip([(SEP_2026 - 5 + i, "1") for i in range(6)])
    facts[0] = _fact(K_IP, _mon(SEP_2026 - 5), bad)
    with pytest.raises(ValueError, match=r"^industrial production index must be positive$"):
        _build(ip=facts)


def test_two_industrial_facts_in_one_month_fail_closed() -> None:
    facts = [_fact(K_IP, date(2026, 9, 1), "1"), _fact(K_IP, date(2026, 9, 15), "1")]
    with pytest.raises(ValueError, match=r"at most one fact per calendar month"):
        _build(ip=facts)


def test_growth_normalization_needs_126_consecutive_months_and_excludes_current() -> None:
    def val(m: int) -> str:
        return str(100 + (m * 7) % 13 + m // 10)

    def raw(fs, m):
        s = [Decimal(fs[k]) for k in range(m - 5, m + 1)]
        ratio = _REF.divide(_REF.add(_REF.add(s[3], s[4]), s[5]), _REF.add(_REF.add(s[0], s[1]), s[2]))
        return _REF.multiply(Decimal(100), _REF.subtract(_REF.power(ratio, Decimal(4)), Decimal(1)))
    vals = {m: val(m) for m in range(SEP_2026 - 125, SEP_2026 + 1)}
    facts = _ip([(m, vals[m]) for m in sorted(vals)])
    g = _build(ip=facts).growth
    n = g.normalization
    assert g.raw_3m3m_saar == raw(vals, SEP_2026)
    assert n is not None and n.window_start_month == _mon(SEP_2026 - 120) and n.window_end_month == _mon(SEP_2026 - 1)
    window = sorted(raw(vals, m) for m in range(SEP_2026 - 120, SEP_2026))
    med = _REF.divide(_REF.add(window[59], window[60]), Decimal(2))
    assert n.median == med
    # one month short -> raw exists, normalization None
    g2 = _build(ip=facts[1:]).growth
    assert g2.raw_3m3m_saar is not None and g2.normalization is None


# --- raw evidence -----------------------------------------------------------------------------------------------

def test_real_yield_negative_is_valid_and_raw() -> None:
    f = _fact(K_REAL, date(2026, 9, 29), "-1.25")
    r = _build(real=[f]).real_yield
    assert r.raw_value == Decimal("-1.25") and r.source_fact is f and r.normalization is None


def test_broad_dollar_is_raw_level() -> None:
    f = _fact(K_USD, date(2026, 9, 29), "121.5")
    r = _build(usd=[f]).broad_dollar
    assert r.raw_value == Decimal("121.5") and r.source_fact is f


def test_financial_stress_negative_is_raw_without_normalization() -> None:
    f = _fact(K_STRESS, date(2026, 9, 25), "-0.75")
    s = _build(stress=[f]).financial_stress
    assert s.raw_value == Decimal("-0.75") and s.source_fact is f
    assert "normalization" not in {x.name for x in dataclasses.fields(FinancialStressEvidence)}
    z = _fact(K_STRESS, date(2026, 9, 26), "0")
    assert _build(stress=[f, z]).financial_stress.raw_value == Decimal("0")


def test_partial_degraded_stale_values_are_used() -> None:
    for status in (DataStatus.PARTIAL, DataStatus.DEGRADED, DataStatus.STALE):
        f = _fact(K_REAL, date(2026, 9, 25), "1.5", status=status)
        assert _build(real=[f]).real_yield.raw_value == Decimal("1.5")


def test_curve_primary_and_diagnostic_and_identity() -> None:
    pts = _curve([(date(2026, 9, 25), "4.5", "4.1", "5.0")])
    c = _build(curve=pts).yield_curve
    assert c.source_point is pts[0]
    assert c.primary_10y_3m == Decimal("-0.5") and c.diagnostic_10y_2y == Decimal("0.4")
    assert c.normalization is None and c.reference_month == date(2026, 9, 1)


def test_curve_latest_point_with_missing_primary_does_not_fall_back() -> None:
    pts = _curve([(date(2026, 9, 10), "4.5", "4.1", "5.0"), (date(2026, 9, 20), "4.5", "4.1", None)])
    c = _build(curve=pts).yield_curve
    assert c.source_point is pts[1] and c.primary_10y_3m is None and c.diagnostic_10y_2y == Decimal("0.4")
    assert c.normalization is None


def test_curve_only_primary_is_normalized() -> None:
    days = [(_mon(m, 15), str(2 + (m % 5)), "1", str(1 + (m % 3))) for m in range(SEP_2026 - 120, SEP_2026 + 1)]
    pts = _curve(days)
    c = _build(curve=pts).yield_curve
    assert c.normalization is not None and c.normalization.window_start_month == _mon(SEP_2026 - 120)
    assert c.diagnostic_10y_2y is not None
    assert not hasattr(c, "diagnostic_normalization")


# --- robust normalization ---------------------------------------------------------------------------------------

def test_robust_normalization_values_window_and_no_clipping() -> None:
    r = _build(real=_monthly(K_REAL, SEP_2026 - 120, _window_values())).real_yield
    n = r.normalization
    assert r.raw_value == Decimal("200")
    assert n.window_start_month == date(2016, 9, 1) and n.window_end_month == date(2026, 8, 1)
    assert n.median == Decimal("60.5")  # current month (200) excluded; inclusion would give 61
    assert n.mad == Decimal("30")
    assert n.scaled_mad == Decimal("44.478")
    assert n.z == _REF.divide(Decimal("139.5"), Decimal("44.478"))
    assert n.z > 3  # uncapped


def test_current_month_is_excluded_from_its_own_window() -> None:
    base = _build(real=_monthly(K_REAL, SEP_2026 - 120, _window_values())).real_yield.normalization
    wild = _build(real=_monthly(K_REAL, SEP_2026 - 120, _window_values()[:-1] + ["100000"])).real_yield.normalization
    assert (base.median, base.mad, base.scaled_mad) == (wild.median, wild.mad, wild.scaled_mad)


def test_calendar_gap_disables_normalization_not_last_120_observations() -> None:
    vals = _monthly(K_REAL, SEP_2026 - 121, [str(k) for k in range(1, 123)])  # 122 facts, months 79..200
    del vals[SEP_2026 - 150 - (SEP_2026 - 121)]  # drop one month inside the window
    assert len(vals) == 121  # still 120 prior observations + current
    r = _build(real=vals).real_yield
    assert r.raw_value is not None and r.normalization is None


def test_unavailable_month_inside_window_disables_normalization() -> None:
    vals = _monthly(K_REAL, SEP_2026 - 120, _window_values())
    vals[30] = _fact(K_REAL, _mon(SEP_2026 - 120 + 30, 15), None)
    assert _build(real=vals).real_yield.normalization is None


def test_119_months_gives_no_fallback_window() -> None:
    assert _build(real=_monthly(K_REAL, SEP_2026 - 119, _window_values()[1:])).real_yield.normalization is None


def test_monthly_sampling_inside_window_uses_last_observation_of_each_month() -> None:
    vals = []
    for i, v in enumerate(_window_values()):
        m = SEP_2026 - 120 + i
        if i < 120:
            vals.append(_fact(K_USD, _mon(m, 3), "9999"))  # earlier in month, ignored
        vals.append(_fact(K_USD, _mon(m, 20), v))
    n = _build(usd=vals).broad_dollar.normalization
    assert n.median == Decimal("60.5") and n.mad == Decimal("30")


def test_mad_zero_keeps_window_statistics_and_z_none() -> None:
    n = _build(real=_monthly(K_REAL, SEP_2026 - 120, ["3.5"] * 120 + ["4"])).real_yield.normalization
    assert n is not None and n.median == Decimal("3.5") and n.mad == 0 and n.scaled_mad == 0 and n.z is None


def test_odd_free_median_even_window_averages_middle_pair() -> None:
    vals = ["1"] * 59 + ["2", "4"] + ["9"] * 59 + ["5"]
    n = _build(real=_monthly(K_REAL, SEP_2026 - 120, vals)).real_yield.normalization
    assert n.median == Decimal("3")  # (2 + 4) / 2 after sorting


# --- decimal contract -------------------------------------------------------------------------------------------

def _decimal_inputs():
    ip = _ip([(m, str(100 + (m * 7) % 13 + m // 10)) for m in range(SEP_2026 - 125, SEP_2026 + 1)])
    real = _monthly(K_REAL, SEP_2026 - 120, [f"{k}.333" for k in range(1, 122)])
    return dict(ip=ip, real=real)


def test_results_do_not_depend_on_ambient_context() -> None:
    kw = _decimal_inputs()
    baseline = _build(**kw)
    for prec, rounding in ((2, decimal.ROUND_HALF_EVEN), (3, decimal.ROUND_DOWN), (1, decimal.ROUND_CEILING)):
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = prec, rounding
            result = _build(**kw)
            assert (ctx.prec, ctx.rounding) == (prec, rounding)
        assert result.growth.raw_3m3m_saar == baseline.growth.raw_3m3m_saar
        assert result.growth.normalization == baseline.growth.normalization
        assert result.real_yield.normalization == baseline.real_yield.normalization
    assert baseline.growth.normalization.z is not None and baseline.real_yield.normalization.z is not None


def test_global_context_is_not_mutated() -> None:
    before = decimal.getcontext().copy()
    _build(**_decimal_inputs())
    after = decimal.getcontext()
    assert (before.prec, before.rounding, before.traps, before.Emin, before.Emax) == (
        after.prec, after.rounding, after.traps, after.Emin, after.Emax)


def test_overflow_is_translated_to_static_error() -> None:
    huge = "9E+999999999999999999"
    facts = _ip([(SEP_2026 - 5 + i, v) for i, v in enumerate(["1", "1", "1", huge, huge, huge])])
    with pytest.raises(ValueError, match=r"^US macro evidence exceeds supported Decimal analytics range$"):
        _build(ip=facts)


# --- provenance / scope -----------------------------------------------------------------------------------------

def test_source_objects_keep_identity_and_mode_as_of_objects() -> None:
    mode = AsOfMode.SYSTEM_AS_OF
    r, u, s = _fact(K_REAL, date(2026, 9, 1)), _fact(K_USD, date(2026, 9, 1)), _fact(K_STRESS, date(2026, 9, 1))
    pts = _curve([(date(2026, 9, 1), "4", "3", "2")])
    snap = _build(real=[r], usd=[u], stress=[s], curve=pts, mode=mode, as_of=AS_OF)
    assert snap.real_yield.source_fact is r and snap.broad_dollar.source_fact is u
    assert snap.financial_stress.source_fact is s and snap.yield_curve.source_point is pts[0]
    assert snap.mode is mode and snap.as_of is AS_OF


def _source() -> str:
    return Path(module_under_test.__file__).read_text(encoding="utf-8")


def test_no_forbidden_names_or_io() -> None:
    tree = ast.parse(_source())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not {"float", "round", "Fraction", "math"} & names
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not {"quantize", "now", "utcnow", "today", "getenv", "environ", "setcontext", "getcontext",
                "localcontext", "prec", "sqrt", "stdev"} & attrs
    assert not any(isinstance(n, ast.Constant) and isinstance(n.value, float) for n in ast.walk(tree))
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imports |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    for banned in ("supabase", "requests", "httpx", "os", "providers", "orchestrator", "state_query", "random",
                   "numpy", "pandas", "scipy", "fractions", "math", "statistics", "uuid"):
        assert not any(banned == m or banned in m for m in imports), banned


_BANNED_SURFACE = ("score", "regime", "label", "risk_on", "risk_off", "recommendation", "buy", "sell", "hold",
                   "allocation", "weight", "tilt", "probability", "signal", "macro_state", "composite")


def test_no_interpretation_surface() -> None:
    field_names = {f.name for cls in (RobustNormalizationEvidence, GrowthMomentumEvidence,
                                      NormalizedMacroLevelEvidence, YieldCurveEvidence,
                                      FinancialStressEvidence, USMacroEvidenceSnapshot)
                   for f in dataclasses.fields(cls)}
    defined = {n.name for n in ast.walk(ast.parse(_source()))
               if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for name in field_names | defined:
        assert not any(b in name.lower() for b in _BANNED_SURFACE), name


def test_no_winsorization_or_std_fallback_in_source() -> None:
    tree = ast.parse(_source())
    idents = {n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name)}
    idents |= {n.attr.lower() for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not [i for i in idents if any(t in i for t in ("clip", "winsor", "stdev", "epsilon", "statistics"))]

def test_module_is_clean_for_static_guards() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/macro/us_macro_evidence.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    for scan in (sg.scan_g1, sg.scan_g2, sg.scan_g4, sg.scan_g5):
        assert scan(source, rel) == []
    assert {v.node_kind for v in sg.scan_g3(source, rel)} <= {
        "PrivateImport:backend.engine.private.macro.state_inputs",
        "PrivateImport:backend.engine.private.macro.us_treasury_curve",
    }
    assert rel not in sg.PURE_MANIFEST
