"""
backend/tests/test_tefas_fund_price_series.py
=============================================
Tests for the PIT-safe TEFAS fund price series (Phase 16A).

The series only wraps authoritative resolver output: exact Decimal prices, explicit gaps, no
interpolation, no returns, no calendar fabrication, no FX conversion. The resolver stays the sole
PIT authority.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private import fund_price_series as module_under_test
from backend.engine.private.domain import Currency, DataConfidenceLevel, InstrumentType
from backend.engine.private.fund_price_series import (
    TefasFundPriceGap,
    TefasFundPricePoint,
    TefasFundPriceSeries,
    build_tefas_fund_price_series,
)
from backend.engine.private.market_data.models import (
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
    MarketObservationResolutionResult,
)
from backend.engine.private.market_data.resolver import PointInTimeMarketDataResolver
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
_D1, _D2, _D3 = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)
_T0 = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
_KEY = "res-key"


# --- fixture factories (mirroring the resolver suite) -------------------------

def _obs(trade_date: date, price: str = "1.2345", currency: Currency = Currency.TRY,
         instrument_id: UUID = _INST, status: TefasObservationStatus = TefasObservationStatus.VALID
         ) -> TefasFundPriceObservation:
    return TefasFundPriceObservation(
        instrument_id=instrument_id, provider_symbol="MAC", trade_date=trade_date,
        unit_price=Decimal(price), currency=currency, instrument_type=InstrumentType.TEFAS_FUND,
        status=status, confidence_level=DataConfidenceLevel.HIGH,
    )


def _snap(observations: List[TefasFundPriceObservation], retrieved_at: datetime = _T0,
          payload_hash: str = "hash_a", instrument_id: UUID = _INST,
          trade_date_range: Optional[Tuple[Optional[date], Optional[date]]] = None,
          period_months: int = 60) -> TefasFundPriceSnapshot:
    sid = uuid4()
    if trade_date_range is None:
        dates = [o.trade_date for o in observations if o.is_valid]
        trade_date_range = (min(dates), max(dates)) if dates else (None, None)
    linked = [
        TefasFundPriceObservation(
            instrument_id=o.instrument_id, provider_symbol=o.provider_symbol, trade_date=o.trade_date,
            unit_price=o.unit_price, currency=o.currency, instrument_type=o.instrument_type,
            status=o.status, snapshot_id=sid, payload_hash=payload_hash,
            confidence_level=o.confidence_level,
        )
        for o in observations
    ]
    return TefasFundPriceSnapshot(
        id=sid, provider="TEFAS", provider_symbol="MAC", retrieved_at=retrieved_at, http_status=200,
        payload_hash=payload_hash, raw_payload="{}", instrument_id=instrument_id,
        period_months=period_months, trade_date_range=trade_date_range, observations=linked,
    )


def _build(snapshots, dates=(_D1, _D2, _D3), mode=CR, as_of=None, **kw) -> TefasFundPriceSeries:
    return build_tefas_fund_price_series(
        instrument_id=kw.pop("instrument_id", _INST), trade_dates=dates, snapshots=snapshots,
        mode=mode, as_of=as_of, **kw,
    )


def _point(d: date = _D1, **overrides) -> TefasFundPricePoint:
    kwargs = dict(trade_date=d, unit_price=Decimal("1.5"), currency=Currency.TRY,
                  confidence=DataConfidenceLevel.HIGH, snapshot_retrieved_at=_T0, resolution_key=_KEY)
    kwargs.update(overrides)
    return TefasFundPricePoint(**kwargs)


class _Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")


class _DecimalSub(Decimal):
    pass


# --- point ---------------------------------------------------------------------

def test_point_fields_and_frozen() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundPricePoint)] == [
        "trade_date", "unit_price", "currency", "confidence", "snapshot_retrieved_at", "resolution_key"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _point().unit_price = Decimal("2")  # type: ignore[misc]


@pytest.mark.parametrize("field,value,exc", [
    ("trade_date", datetime(2026, 3, 2), TypeError), ("trade_date", "2026-03-02", TypeError),
    ("trade_date", None, TypeError), ("trade_date", True, TypeError),
    ("unit_price", 1.5, TypeError), ("unit_price", 1, TypeError), ("unit_price", True, TypeError),
    ("unit_price", "1.5", TypeError), ("unit_price", None, TypeError), ("unit_price", _DecimalSub("1"), TypeError),
    ("unit_price", Decimal("0"), ValueError), ("unit_price", Decimal("-1"), ValueError),
    ("unit_price", Decimal("NaN"), ValueError), ("unit_price", Decimal("Infinity"), ValueError),
    ("currency", "TRY", TypeError), ("currency", None, TypeError),
    ("confidence", "HIGH", TypeError), ("confidence", None, TypeError),
    ("snapshot_retrieved_at", datetime(2026, 3, 10), TypeError), ("snapshot_retrieved_at", "x", TypeError),
    ("snapshot_retrieved_at", None, TypeError),
    ("resolution_key", "", TypeError), ("resolution_key", None, TypeError), ("resolution_key", b"k", TypeError),
])
def test_point_validation(field, value, exc) -> None:
    with pytest.raises(exc):
        _point(**{field: value})


def test_point_hostile_values_do_not_run_callbacks() -> None:
    for field in ("trade_date", "unit_price", "currency", "confidence", "snapshot_retrieved_at", "resolution_key"):
        with pytest.raises(TypeError):
            _point(**{field: _Hostile()})


# --- gap -----------------------------------------------------------------------

def test_gap_fields_frozen_and_validation() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundPriceGap)] == ["trade_date", "status"]
    gap = TefasFundPriceGap(trade_date=_D1, status=ST.NO_SNAPSHOT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        gap.status = ST.NO_SNAPSHOT  # type: ignore[misc]
    for bad_date in (datetime(2026, 3, 2), "x", None):
        with pytest.raises(TypeError):
            TefasFundPriceGap(trade_date=bad_date, status=ST.NO_SNAPSHOT)  # type: ignore[arg-type]
    for bad_status in ("NO_SNAPSHOT", None, 1):
        with pytest.raises(TypeError):
            TefasFundPriceGap(trade_date=_D1, status=bad_status)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TefasFundPriceGap(trade_date=_D1, status=ST.SELECTED)


# --- series --------------------------------------------------------------------

def _series(**overrides) -> TefasFundPriceSeries:
    kwargs = dict(instrument_id=_INST, mode=CR, as_of=None, requested_dates=(_D1, _D2),
                  points=(_point(_D1),), gaps=(TefasFundPriceGap(_D2, ST.NO_SNAPSHOT),))
    kwargs.update(overrides)
    return TefasFundPriceSeries(**kwargs)


def test_series_fields_frozen_and_is_complete() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundPriceSeries)] == [
        "instrument_id", "mode", "as_of", "requested_dates", "points", "gaps"]
    s = _series()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.points = ()  # type: ignore[misc]
    assert s.is_complete is False
    complete = _series(points=(_point(_D1), _point(_D2)), gaps=())
    assert complete.is_complete is True
    assert not any(hasattr(s, a) for a in ("coverage", "returns", "score", "latest", "fill"))


@pytest.mark.parametrize("overrides", [
    {"instrument_id": str(_INST)}, {"instrument_id": None}, {"mode": "CURRENT_REPORTED"}, {"mode": None},
    {"as_of": datetime(2026, 3, 10)}, {"as_of": "x"},
    {"requested_dates": ()}, {"requested_dates": [_D1, _D2]}, {"requested_dates": (_D2, _D1)},
    {"requested_dates": (_D1, _D1)}, {"requested_dates": (datetime(2026, 3, 2), _D2)},
    {"points": [_point(_D1)]}, {"points": (object(),)}, {"gaps": []}, {"gaps": (object(),)},
])
def test_series_rejects_invalid_structure(overrides) -> None:
    with pytest.raises((TypeError, ValueError)):
        _series(**overrides)


@pytest.mark.parametrize("points,gaps", [
    ((), ()),                                                                       # neither
    ((_point(_D1), _point(_D2)), (TefasFundPriceGap(_D2, ST.NO_SNAPSHOT),)),        # both
    ((_point(_D1),), ()),                                                           # missing d2
    ((_point(_D1), _point(date(2026, 3, 9))), ()),                                  # unrequested date
    ((_point(_D1), _point(_D1)), (TefasFundPriceGap(_D2, ST.NO_SNAPSHOT),)),        # duplicate point
    ((_point(_D2), _point(_D1)), ()),                                               # unordered points
    ((), (TefasFundPriceGap(_D2, ST.NO_SNAPSHOT), TefasFundPriceGap(_D1, ST.NO_SNAPSHOT))),  # unordered gaps
])
def test_series_requires_exact_partition_in_requested_order(points, gaps) -> None:
    with pytest.raises(ValueError):
        _series(points=points, gaps=gaps)


def test_series_rejects_mixed_currencies() -> None:
    with pytest.raises(ValueError, match=r"^TEFAS fund price series contains inconsistent currencies$"):
        _series(points=(_point(_D1), _point(_D2, currency=Currency.USD)), gaps=())


# --- builder: happy paths ------------------------------------------------------

def test_current_reported_all_selected() -> None:
    snap = _snap([_obs(_D1, "1.10"), _obs(_D2, "1.2000"), _obs(_D3, "1.333333333333333333")])
    s = _build([snap])
    assert s.is_complete is True and s.gaps == ()
    assert [p.trade_date for p in s.points] == [_D1, _D2, _D3]
    assert [p.unit_price for p in s.points] == [Decimal("1.10"), Decimal("1.2000"), Decimal("1.333333333333333333")]
    assert all(type(p.unit_price) is Decimal for p in s.points)
    assert all(p.currency is Currency.TRY and p.confidence is DataConfidenceLevel.HIGH for p in s.points)
    assert all(p.snapshot_retrieved_at == _T0 for p in s.points)
    assert len({p.resolution_key for p in s.points}) == 3 and all(p.resolution_key for p in s.points)
    assert s.instrument_id == _INST and s.mode is CR and s.as_of is None
    assert s.requested_dates == (_D1, _D2, _D3)


def test_series_does_not_retain_snapshots() -> None:
    snap = _snap([_obs(_D1)])
    s = _build([snap], dates=(_D1,))
    assert all(snap is not v and snap not in (v if isinstance(v, tuple) else ()) for v in vars(s).values())


def test_current_reported_matches_direct_resolver_output() -> None:
    from backend.engine.private.market_data.models import TefasFundPriceQueryKey
    snap = _snap([_obs(_D1, "2.5")])
    result = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        TefasFundPriceQueryKey(instrument_id=_INST, trade_date=_D1), [snap], mode=CR, as_of=None)
    point = _build([snap], dates=(_D1,)).points[0]
    assert point.resolution_key == result.resolution_key
    assert point.snapshot_retrieved_at == result.snapshot_retrieved_at
    assert point.unit_price == result.selected_observation.unit_price


def test_system_as_of_isolates_future_snapshots() -> None:
    cutoff = _T0 + timedelta(days=1)
    good = _snap([_obs(_D1, "1.1"), _obs(_D2, "1.2")], retrieved_at=_T0, payload_hash="good")
    future_conflict = _snap([_obs(_D1, "999"), _obs(_D2, "888")], retrieved_at=cutoff + timedelta(days=5),
                            payload_hash="future")
    future_corrupt = _snap([_obs(_D1, "7", status=TefasObservationStatus.INVALID_OBSERVATION)],
                           retrieved_at=cutoff + timedelta(days=6), payload_hash="future2",
                           trade_date_range=(_D1, _D2))
    clean = _build([good], dates=(_D1, _D2), mode=SYS, as_of=cutoff)
    polluted = _build([good, future_conflict, future_corrupt], dates=(_D1, _D2), mode=SYS, as_of=cutoff)
    assert polluted == clean
    assert [p.unit_price for p in polluted.points] == [Decimal("1.1"), Decimal("1.2")]
    assert polluted.as_of == cutoff and polluted.mode is SYS


def test_system_as_of_pre_correction_state() -> None:
    old = _snap([_obs(_D1, "1.0")], retrieved_at=_T0, payload_hash="old")
    new = _snap([_obs(_D1, "2.0")], retrieved_at=_T0 + timedelta(days=2), payload_hash="new")
    assert _build([old, new], dates=(_D1,), mode=SYS, as_of=_T0 + timedelta(days=1)).points[0].unit_price == Decimal("1.0")
    assert _build([old, new], dates=(_D1,), mode=CR).points[0].unit_price == Decimal("2.0")


# --- as_of contract -------------------------------------------------------------

@pytest.mark.parametrize("as_of", [None, datetime(2026, 3, 10), "2026-03-10", 0])
def test_system_as_of_requires_exact_aware_datetime(as_of) -> None:
    with pytest.raises(TypeError, match=r"^as_of must be an exact timezone-aware datetime for SYSTEM_AS_OF$"):
        _build([_snap([_obs(_D1)])], dates=(_D1,), mode=SYS, as_of=as_of)


def test_current_reported_rejects_as_of() -> None:
    with pytest.raises(ValueError, match=r"^as_of must be None for CURRENT_REPORTED$"):
        _build([_snap([_obs(_D1)])], dates=(_D1,), mode=CR, as_of=_T0)


def test_source_as_of_is_all_gaps_never_downgraded() -> None:
    s = _build([_snap([_obs(_D1), _obs(_D2), _obs(_D3)])], mode=SRC, as_of=None)
    assert s.points == () and s.is_complete is False
    assert [g.status for g in s.gaps] == [ST.UNAVAILABLE_SOURCE_AS_OF] * 3
    assert s.mode is SRC
    with_as_of = _build([_snap([_obs(_D1)])], dates=(_D1,), mode=SRC, as_of=_T0)
    assert with_as_of.gaps[0].status is ST.UNAVAILABLE_SOURCE_AS_OF and with_as_of.as_of == _T0


# --- explicit gaps / missing != zero ------------------------------------------

def test_no_snapshot_gap_for_empty_and_other_instrument() -> None:
    other = _snap([_obs(_D1, instrument_id=uuid4())], instrument_id=uuid4())
    for snaps in ([], [other]):
        s = _build(snaps, dates=(_D1, _D2))
        assert s.points == () and [g.status for g in s.gaps] == [ST.NO_SNAPSHOT] * 2


def test_no_snapshot_as_of_gap() -> None:
    s = _build([_snap([_obs(_D1)])], dates=(_D1,), mode=SYS, as_of=_T0 - timedelta(days=1))
    assert s.gaps == (TefasFundPriceGap(_D1, ST.NO_SNAPSHOT_AS_OF),)


def test_covered_but_missing_row_is_gap_not_fabricated() -> None:
    snap = _snap([_obs(_D1), _obs(_D3)], trade_date_range=(_D1, _D3))  # D2 covered by range, no row
    s = _build([snap])
    assert [p.trade_date for p in s.points] == [_D1, _D3]
    assert s.gaps == (TefasFundPriceGap(_D2, ST.NO_ELIGIBLE_OBSERVATION),)
    assert all(p.unit_price > 0 for p in s.points)


def test_snapshot_conflict_gap() -> None:
    a = _snap([_obs(_D1, "1.0")], payload_hash="ha")
    b = _snap([_obs(_D1, "2.0")], payload_hash="hb")
    assert _build([a, b], dates=(_D1,)).gaps == (TefasFundPriceGap(_D1, ST.SNAPSHOT_CONFLICT),)


def test_observation_conflict_gap() -> None:
    snap = _snap([_obs(_D1, "1.0"), _obs(_D1, "2.0")], trade_date_range=(_D1, _D1))
    assert _build([snap], dates=(_D1,)).gaps == (TefasFundPriceGap(_D1, ST.OBSERVATION_CONFLICT),)


def test_gap_statuses_other_than_selected_are_all_preserved() -> None:
    s = _build([_snap([_obs(_D1)], trade_date_range=(_D1, _D1))], dates=(_D1, _D2, _D3))
    assert s.points and s.gaps
    assert {g.status for g in s.gaps} <= set(ST) - {ST.SELECTED}


def test_no_resurrection_of_older_price() -> None:
    older = _snap([_obs(_D1, "1.0"), _obs(_D2, "1.5")], retrieved_at=_T0, payload_hash="old",
                  trade_date_range=(_D1, _D3))
    newer = _snap([_obs(_D1, "1.1")], retrieved_at=_T0 + timedelta(days=1), payload_hash="new",
                  trade_date_range=(_D1, _D3))  # covers D2, omits it
    s = _build([older, newer], dates=(_D1, _D2))
    assert [p.unit_price for p in s.points] == [Decimal("1.1")]
    assert s.gaps == (TefasFundPriceGap(_D2, ST.NO_ELIGIBLE_OBSERVATION),)


def test_invalid_row_in_newer_snapshot_is_gap_not_older_backfill() -> None:
    older = _snap([_obs(_D1, "1.0")], retrieved_at=_T0, payload_hash="old", trade_date_range=(_D1, _D1))
    newer = _snap([_obs(_D1, "3.0", status=TefasObservationStatus.INVALID_OBSERVATION)],
                  retrieved_at=_T0 + timedelta(days=1), payload_hash="new", trade_date_range=(_D1, _D1))
    s = _build([older, newer], dates=(_D1,))
    assert s.points == () and s.gaps == (TefasFundPriceGap(_D1, ST.NO_ELIGIBLE_OBSERVATION),)


# --- currency consistency ------------------------------------------------------

def test_currency_conflict_across_selected_dates_fails_closed() -> None:
    snap = _snap([_obs(_D1, currency=Currency.TRY), _obs(_D2, currency=Currency.USD)])
    with pytest.raises(ValueError, match=r"^TEFAS fund price series contains inconsistent currencies$"):
        _build([snap], dates=(_D1, _D2))


# --- determinism ---------------------------------------------------------------

def test_equivalent_snapshots_with_different_uuids_are_economically_identical() -> None:
    a = _snap([_obs(_D1, "1.25"), _obs(_D2, "1.30")], payload_hash="same")
    b = _snap([_obs(_D1, "1.25"), _obs(_D2, "1.30")], payload_hash="same")
    assert a.id != b.id
    sa, sb = _build([a], dates=(_D1, _D2)), _build([b], dates=(_D1, _D2))
    econ = lambda s: [(p.trade_date, p.unit_price, p.currency, p.confidence) for p in s.points]  # noqa: E731
    assert econ(sa) == econ(sb)
    assert sa.points[0].resolution_key == sb.points[0].resolution_key
    assert _build([a, b], dates=(_D1, _D2)) == sa


def test_snapshot_input_order_independence() -> None:
    a = _snap([_obs(_D1, "1.0")], retrieved_at=_T0, payload_hash="a")
    b = _snap([_obs(_D1, "2.0")], retrieved_at=_T0 + timedelta(days=1), payload_hash="b")
    assert _build([a, b], dates=(_D1,)) == _build([b, a], dates=(_D1,))


# --- builder input validation --------------------------------------------------

@pytest.mark.parametrize("dates", [(), [_D1], (_D2, _D1), (_D1, _D1), (datetime(2026, 3, 2),), ("2026-03-02",), None])
def test_builder_rejects_invalid_trade_dates(dates) -> None:
    with pytest.raises((TypeError, ValueError)):
        _build([_snap([_obs(_D1)])], dates=dates)


@pytest.mark.parametrize("bad", [str(_INST), None, 1])
def test_builder_rejects_invalid_instrument(bad) -> None:
    with pytest.raises(TypeError):
        _build([_snap([_obs(_D1)])], dates=(_D1,), instrument_id=bad)


@pytest.mark.parametrize("bad", ["CURRENT_REPORTED", None, 1])
def test_builder_rejects_invalid_mode(bad) -> None:
    with pytest.raises(TypeError):
        _build([_snap([_obs(_D1)])], dates=(_D1,), mode=bad)


@pytest.mark.parametrize("bad", ["abc", b"abc", None, 1, [object()], [None], (_snap([_obs(_D1)]), "x")])
def test_builder_rejects_invalid_snapshots(bad) -> None:
    with pytest.raises(TypeError, match=r"^snapshots must be a sequence of exact TefasFundPriceSnapshot instances$"):
        _build(bad, dates=(_D1,))


def test_builder_rejects_snapshot_subclass() -> None:
    class _Sub(TefasFundPriceSnapshot):
        pass
    base = _snap([_obs(_D1)])
    sub = _Sub(**{f.name: getattr(base, f.name) for f in dataclasses.fields(base)})
    with pytest.raises(TypeError):
        _build([sub], dates=(_D1,))


def test_provider_symbol_is_diagnostic_only() -> None:
    snap = _snap([_obs(_D1)])
    assert _build([snap], dates=(_D1,), provider_symbol=None) == _build([snap], dates=(_D1,), provider_symbol="ANYTHING")
    with pytest.raises(TypeError):
        _build([snap], dates=(_D1,), provider_symbol=123)


def test_builder_arguments_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        build_tefas_fund_price_series(_INST, (_D1,), [], CR, None)  # type: ignore[misc]


# --- defensive checks on resolver output ---------------------------------------

def _fake_result(observation, status=ST.SELECTED):
    return MarketObservationResolutionResult(
        status=status, resolution_mode=CR, as_of=None, observation_type="TEFAS_FUND_PRICE",
        effective_date=_D1, selected_observation=observation, snapshot_retrieved_at=_T0,
        resolution_key=_KEY, canonical_instrument_id=_INST,
    )


@pytest.mark.parametrize("observation", [
    None, object(), _obs(_D2), _obs(_D1, instrument_id=uuid4()),
])
def test_selected_result_with_mismatching_observation_fails_closed(monkeypatch, observation) -> None:
    monkeypatch.setattr(PointInTimeMarketDataResolver, "resolve_tefas_fund_price",
                        classmethod(lambda cls, *a, **k: _fake_result(observation)))
    with pytest.raises((TypeError, ValueError)):
        _build([], dates=(_D1,))


def test_selected_result_with_non_positive_or_missing_price_fails_closed(monkeypatch) -> None:
    bad = TefasFundPriceObservation(provider_symbol="MAC", trade_date=_D1, unit_price=None, currency=Currency.TRY,
                                    instrument_id=_INST)
    monkeypatch.setattr(PointInTimeMarketDataResolver, "resolve_tefas_fund_price",
                        classmethod(lambda cls, *a, **k: _fake_result(bad)))
    with pytest.raises((TypeError, ValueError)):
        _build([], dates=(_D1,))


# --- purity / scope ------------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_imports_are_pure() -> None:
    imports = _imports()
    for banned in ("pandas", "numpy", "polars", "scipy", "requests", "httpx", "os", "pathlib", "random",
                   "secrets", "hashlib", "hmac", "socket", "sqlite3"):
        assert banned not in imports
    assert not any("persistence" in m or "repository" in m for m in imports)
    assert not any(m.startswith("backend.engine.private.portfolio") for m in imports)


def test_no_return_or_metric_surface() -> None:
    for name in ("simple_return", "log_return", "volatility", "max_drawdown", "sharpe", "cvar", "score",
                 "forward_fill", "interpolate", "convert_currency"):
        assert not hasattr(module_under_test, name)


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_resolver_market_data_imports() -> None:
    """
    The module cannot join PURE_MANIFEST: the market_data models/resolver it must call are not pure manifest
    modules (uuid4 default factories, datetime.now defaults, hashlib), so G3 flags exactly those imports.
    Everything else is guarded here directly; G4/G5 also scan the whole private tree.
    """
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/fund_price_series.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert sg.scan_g1(source, rel) == []
    assert sg.scan_g2(source, rel) == []
    assert sg.scan_g4(source, rel) == []
    assert sg.scan_g5(source, rel) == []
    g3 = {v.node_kind for v in sg.scan_g3(source, rel)}
    assert g3 == {
        "PrivateImport:backend.engine.private.market_data.models",
        "PrivateImport:backend.engine.private.market_data.resolver",
        "PrivateImport:backend.engine.private.market_data.tefas_models",
    }
    assert rel not in sg.PURE_MANIFEST
