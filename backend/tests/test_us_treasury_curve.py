"""
backend/tests/test_us_treasury_curve.py
=======================================
Tests for exact U.S. Treasury yield-curve slope evidence (Phase 17D): 10Y-2Y and 10Y-3M over Phase 17C histories,
exact-date union alignment, exact Decimal subtraction, no fill / normalization / regime.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private.domain import AsOfMode, DataConfidenceLevel, DataStatus, SourceTier
from backend.engine.private.macro import us_treasury_curve as module_under_test
from backend.engine.private.macro.state_inputs import MacroStateInputFact, build_macro_state_input_fact
from backend.engine.private.macro.us_treasury_curve import (
    USTreasuryCurveSlopePoint,
    build_us_treasury_curve_slope_history,
)

UTC = timezone.utc
AS_OF = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)
BEFORE = AS_OF - timedelta(days=1)
K10, K2, K3M = "US_TREASURY_PAR_10Y", "US_TREASURY_PAR_2Y", "US_TREASURY_PAR_3M"
_counter = [0]


def _fact(key: str, day: int, value: str | None = "4.000000", *, mode=AsOfMode.SYSTEM_AS_OF, as_of=AS_OF,
          status=None) -> MacroStateInputFact:
    _counter[0] += 1
    if status is None:
        status = DataStatus.UNAVAILABLE if value is None else DataStatus.COMPLETE
    return build_macro_state_input_fact(
        canonical_key=key, effective_date=date(2026, 1, day), value=None if value is None else Decimal(value),
        data_status=status, confidence_level=DataConfidenceLevel.HIGH, source_tier=SourceTier.TIER_1_REGULATORY,
        mode=mode, as_of=as_of, published_at=BEFORE, observed_at=BEFORE, ingested_at=BEFORE, superseded_at=None,
        observation_id=UUID(int=1000 + _counter[0]), snapshot_id=UUID(int=2000 + _counter[0]))


def _build(ten=(), two=(), three=(), **over):
    kw = dict(ten_year_history=tuple(ten), two_year_history=tuple(two), three_month_history=tuple(three),
              mode=AsOfMode.SYSTEM_AS_OF, as_of=AS_OF)
    kw.update(over)
    return build_us_treasury_curve_slope_history(**kw)


# --- surface ----------------------------------------------------------------------------------------------------

def test_dataclass_fields_are_exact_and_frozen() -> None:
    assert [f.name for f in dataclasses.fields(USTreasuryCurveSlopePoint)] == [
        "effective_date", "mode", "as_of", "ten_year", "two_year", "three_month", "slope_10y_2y", "slope_10y_3m"]
    point = _build([_fact(K10, 2)])[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.slope_10y_2y = Decimal(1)


def test_builder_is_keyword_only_without_defaults() -> None:
    import inspect
    params = inspect.signature(build_us_treasury_curve_slope_history).parameters
    assert list(params) == ["ten_year_history", "two_year_history", "three_month_history", "mode", "as_of"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
               for p in params.values())


# --- type contract ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("slot", ["ten_year_history", "two_year_history", "three_month_history"])
def test_histories_must_be_exact_tuples(slot) -> None:
    for bad in ([], (x for x in ()), set(), {}, None, "x"):
        with pytest.raises(TypeError, match=r"history must be an exact tuple"):
            _build(**{slot: bad})

    class _T(tuple):
        pass
    with pytest.raises(TypeError, match=r"history must be an exact tuple"):
        _build(**{slot: _T()})


def test_members_must_be_exact_facts() -> None:
    @dataclasses.dataclass(frozen=True)
    class _Sub(MacroStateInputFact):
        pass
    good = _fact(K10, 2)
    sub = _Sub(**{f.name: getattr(good, f.name) for f in dataclasses.fields(good)})
    for bad in (object(), None, 4.0, {"a": 1}, sub):
        with pytest.raises(TypeError, match=r"every history member must be an exact MacroStateInputFact"):
            _build(ten=[bad])


def test_mode_and_as_of_types_are_exact() -> None:
    class _DT(datetime):
        pass
    for bad in ("system_as_of", "SYSTEM_AS_OF", None):
        with pytest.raises(TypeError, match=r"mode must be an exact AsOfMode"):
            _build(mode=bad)
    for bad in (datetime(2026, 6, 30, 12), "2026-06-30", None, date(2026, 6, 30), _DT(2026, 6, 30, 12, tzinfo=UTC)):
        with pytest.raises(TypeError, match=r"as_of must be an exact timezone-aware datetime"):
            _build(as_of=bad)


# --- canonical series -------------------------------------------------------------------------------------------

def test_series_slots_are_bound_without_reassignment() -> None:
    cases = [
        dict(ten=[_fact(K2, 2)]), dict(two=[_fact(K3M, 2)]), dict(three=[_fact(K10, 2)]),
        dict(ten=[_fact("US_TREASURY_PAR_30Y", 2)]), dict(two=[_fact("US_TREASURY_PAR_30Y", 2)]),
        dict(three=[_fact("US_TREASURY_PAR_30Y", 2)]), dict(ten=[_fact("TR_FX_USDTRY", 2)]),
        dict(two=[_fact("TR_FX_USDTRY", 2)]), dict(three=[_fact("TR_FX_USDTRY", 2)]),
        dict(ten=[_fact("US_EFFECTIVE_FED_FUNDS_RATE", 2)]) if False else dict(ten=[_fact(K10, 2), _fact(K2, 3)]),
    ]
    for kw in cases:
        with pytest.raises(ValueError, match=r"canonical_key must be US_TREASURY_PAR_"):
            _build(**kw)


# --- PIT context ------------------------------------------------------------------------------------------------

def test_wrong_and_mixed_modes_are_rejected() -> None:
    with pytest.raises(ValueError, match=r"mode does not match"):
        _build(ten=[_fact(K10, 2, mode=AsOfMode.SOURCE_AS_OF)])
    with pytest.raises(ValueError, match=r"mode does not match"):
        _build(two=[_fact(K2, 2), _fact(K2, 3, mode=AsOfMode.SOURCE_AS_OF)])
    with pytest.raises(ValueError, match=r"mode does not match"):
        _build(ten=[_fact(K10, 2)], three=[_fact(K3M, 2, mode=AsOfMode.SOURCE_AS_OF)])


def test_wrong_and_mixed_as_of_are_rejected() -> None:
    other = AS_OF + timedelta(hours=1)
    with pytest.raises(ValueError, match=r"as_of does not match"):
        _build(ten=[_fact(K10, 2, as_of=other)])
    with pytest.raises(ValueError, match=r"as_of does not match"):
        _build(ten=[_fact(K10, 2), _fact(K10, 3, as_of=other)])
    with pytest.raises(ValueError, match=r"as_of does not match"):
        _build(two=[_fact(K2, 2, as_of=other)], three=[_fact(K3M, 2)])


@pytest.mark.parametrize("mode", [AsOfMode.SYSTEM_AS_OF, AsOfMode.SOURCE_AS_OF])
def test_point_stores_caller_mode_and_as_of(mode) -> None:
    points = _build(ten=[_fact(K10, 2, mode=mode)], mode=mode)
    assert points[0].mode is mode and points[0].as_of is AS_OF


def test_equal_as_of_in_other_offset_is_accepted_and_caller_object_kept() -> None:
    plus3 = timezone(timedelta(hours=3))
    same_instant = AS_OF.astimezone(plus3)
    points = _build(ten=[_fact(K10, 2, as_of=same_instant)])
    assert points[0].as_of is AS_OF


# --- ordering ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("slot,key", [("ten", K10), ("two", K2), ("three", K3M)])
def test_ordering_is_validated_per_history(slot, key) -> None:
    for days in ([2, 2], [3, 2], [2, 4, 3], [2, 4, 4]):
        with pytest.raises(ValueError, match=r"strictly increasing effective_date"):
            _build(**{slot: [_fact(key, d) for d in days]})


# --- alignment --------------------------------------------------------------------------------------------------

def test_empty_histories_yield_empty_tuple() -> None:
    result = _build()
    assert type(result) is tuple and result == ()


def test_perfect_overlap() -> None:
    result = _build([_fact(K10, 2), _fact(K10, 3)], [_fact(K2, 2), _fact(K2, 3)], [_fact(K3M, 2), _fact(K3M, 3)])
    assert type(result) is tuple and [p.effective_date for p in result] == [date(2026, 1, 2), date(2026, 1, 3)]
    assert all(p.ten_year and p.two_year and p.three_month for p in result)


def test_union_dates_without_fill_or_interpolation() -> None:
    ten = [_fact(K10, 2, "4.5"), _fact(K10, 3, "4.6"), _fact(K10, 5, "4.7")]
    two = [_fact(K2, 2, "4.1"), _fact(K2, 5, "4.2")]
    three = [_fact(K3M, 2, "5.0"), _fact(K3M, 3, "5.1"), _fact(K3M, 5, "5.2")]
    result = _build(ten, two, three)
    assert [p.effective_date.day for p in result] == [2, 3, 5]
    jan3 = result[1]
    assert jan3.ten_year is ten[1] and jan3.two_year is None and jan3.three_month is three[1]
    assert jan3.slope_10y_2y is None and jan3.slope_10y_3m == Decimal("-0.5")


def test_missing_3m_date_keeps_the_2y_slope() -> None:
    result = _build([_fact(K10, 2, "4.5")], [_fact(K2, 2, "4.1")], [])
    assert result[0].three_month is None and result[0].slope_10y_3m is None
    assert result[0].slope_10y_2y == Decimal("0.4")


def test_date_in_only_one_history_still_yields_a_point() -> None:
    result = _build([], [_fact(K2, 4)], [])
    assert len(result) == 1 and result[0].effective_date == date(2026, 1, 4)
    assert result[0].ten_year is None and result[0].three_month is None
    assert result[0].slope_10y_2y is None and result[0].slope_10y_3m is None


def test_output_is_sorted_even_when_dates_appear_first_in_later_histories() -> None:
    result = _build([_fact(K10, 9)], [_fact(K2, 3)], [_fact(K3M, 6)])
    assert [p.effective_date.day for p in result] == [3, 6, 9]


# --- explicit unavailable ---------------------------------------------------------------------------------------

def test_explicit_unavailable_is_retained_and_distinct_from_missing() -> None:
    un = _fact(K3M, 2, None)
    result = _build([_fact(K10, 2, "4.5")], [_fact(K2, 2, "4.1")], [un])
    p = result[0]
    assert p.three_month is un and p.three_month.value is None and p.three_month.data_status is DataStatus.UNAVAILABLE
    assert p.slope_10y_3m is None and p.slope_10y_2y == Decimal("0.4")
    missing = _build([_fact(K10, 2, "4.5")], [_fact(K2, 2, "4.1")], [])[0]
    assert missing.three_month is None and p.three_month is not None


def test_unavailable_ten_year_nulls_both_slopes() -> None:
    p = _build([_fact(K10, 2, None)], [_fact(K2, 2, "4.1")], [_fact(K3M, 2, "5.0")])[0]
    assert p.slope_10y_2y is None and p.slope_10y_3m is None and p.ten_year.value is None


# --- slopes -----------------------------------------------------------------------------------------------------

def _slopes(ten, two, three):
    p = _build([_fact(K10, 2, ten)], [_fact(K2, 2, two)], [_fact(K3M, 2, three)])[0]
    return p.slope_10y_2y, p.slope_10y_3m


def test_spec_examples() -> None:
    s2, _ = _slopes("4.500000", "4.100000", "5.000000")
    _, s3 = _slopes("4.500000", "4.100000", "5.000000")
    assert str(s2) == "0.400000" and str(s3) == "-0.500000"


def test_negative_positive_zero_and_exponents() -> None:
    assert _slopes("4.000000", "5.000000", "4.000000")[0] == Decimal("-1.000000")
    zero = _slopes("4.000000", "4.000000", "4.000000")
    assert zero[0] == 0 and zero[0] is not None and str(zero[0]) == "0.000000" and str(zero[1]) == "0.000000"
    assert str(_slopes("1E+2", "0.5", "1")[0]) == "99.5"
    assert str(_slopes("123456789012.123456", "0.000001", "0")[0]) == "123456789012.123455"
    assert str(_slopes("-1.5", "-2", "-3")[0]) == "0.5"
    assert str(_slopes("-0.000000", "0.000000", "0")[0]) == "0.000000"


def test_subtraction_is_exact_under_low_ambient_context() -> None:
    ten, two, three = (_fact(K10, 2, "123456789.123456"), _fact(K2, 2, "0.000001"), _fact(K3M, 2, "9.999999"))
    with localcontext() as ctx:
        ctx.prec = 2
        assert Decimal("123456789.123456") - Decimal("0.000001") != Decimal("123456789.123455")  # plain math rounds
        p = _build([ten], [two], [three])[0]
        assert str(p.slope_10y_2y) == "123456789.123455"
        assert str(p.slope_10y_3m) == "123456779.123457"
    with localcontext() as ctx:
        ctx.prec = 1
        p = _build([_fact(K10, 2, "4.500000")], [_fact(K2, 2, "4.100000")], [_fact(K3M, 2, "5.000000")])[0]
        assert str(p.slope_10y_2y) == "0.400000" and str(p.slope_10y_3m) == "-0.500000"
        assert ctx.prec == 1


def test_arithmetic_does_not_mutate_global_context() -> None:
    from decimal import getcontext
    before = getcontext().copy()
    _slopes("4.5", "4.1", "5.0")
    after = getcontext()
    assert (before.prec, before.rounding, before.traps) == (after.prec, after.rounding, after.traps)


# --- provenance -------------------------------------------------------------------------------------------------

def test_component_facts_keep_identity() -> None:
    a, b, c = _fact(K10, 2, "4.5"), _fact(K2, 2, "4.1"), _fact(K3M, 2, "5.0")
    p = _build([a], [b], [c])[0]
    assert p.ten_year is a and p.two_year is b and p.three_month is c
    assert (p.ten_year.observation_id, p.ten_year.snapshot_id) == (a.observation_id, a.snapshot_id)


def test_partial_degraded_stale_values_still_calculate() -> None:
    for status in (DataStatus.PARTIAL, DataStatus.DEGRADED, DataStatus.STALE):
        p = _build([_fact(K10, 2, "4.5", status=status)], [_fact(K2, 2, "4.1")], [])[0]
        assert p.slope_10y_2y == Decimal("0.4") and p.ten_year.data_status is status


# --- purity / scope ---------------------------------------------------------------------------------------------

def _source() -> str:
    return Path(module_under_test.__file__).read_text(encoding="utf-8")


def test_no_forbidden_names_or_io() -> None:
    tree = ast.parse(_source())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not {"float", "round", "quantize", "Fraction"} & names
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not {"quantize", "now", "utcnow", "today", "getenv", "environ", "setcontext", "getcontext"} & attrs
    assert not any(isinstance(n, ast.Constant) and isinstance(n.value, float) for n in ast.walk(tree))
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imports |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    for banned in ("supabase", "requests", "httpx", "os", "providers", "orchestrator", "state_query", "random",
                   "numpy", "pandas", "fractions"):
        assert not any(banned == m or banned in m for m in imports), banned


def test_dataclass_has_no_interpretation_fields() -> None:
    names = {f.name for f in dataclasses.fields(USTreasuryCurveSlopePoint)}
    for banned in ("score", "label", "regime", "is_inverted", "risk_signal", "recommendation", "confidence", "weight",
                   "probability"):
        assert not any(banned in n for n in names)


def test_module_is_clean_for_static_guards() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/macro/us_treasury_curve.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    for scan in (sg.scan_g1, sg.scan_g2, sg.scan_g4, sg.scan_g5):
        assert scan(source, rel) == []
    assert {v.node_kind for v in sg.scan_g3(source, rel)} <= {"PrivateImport:backend.engine.private.macro.state_inputs"}
    assert rel not in sg.PURE_MANIFEST
