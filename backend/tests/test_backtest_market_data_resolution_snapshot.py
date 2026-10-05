"""
backend/tests/test_backtest_market_data_resolution_snapshot.py
==============================================================
Phase 26C2C1: immutable canonical-JSON snapshot of the closed point-in-time market-data resolver's audit output, for the five existing resolution surfaces.
The closed resolver stays the selection authority; its mutable result and the mutable source snapshots are never retained.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from backend.engine.private import backtest_market_data_resolution_snapshot as module_under_test
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.backtest_market_data_bridge import (
    PrivateBacktestMarketDataContext,
    build_private_backtest_market_data_context,
)
from backend.engine.private.backtest_market_data_resolution_snapshot import (
    PrivateBacktestMarketDataKind,
    PrivateBacktestMarketDataResolutionSnapshot,
    bind_private_backtest_bist_eod_resolution,
    bind_private_backtest_global_eod_resolution,
    bind_private_backtest_precious_metal_resolution,
    bind_private_backtest_tefas_current_metrics_resolution,
    bind_private_backtest_tefas_fund_price_resolution,
)
from backend.engine.private.backtest_replay_point import build_private_backtest_replay_point
from backend.engine.private.domain import AsOfMode, Currency
from backend.engine.private.market_data import (
    BISTInstrumentQueryKey,
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
    PointInTimeMarketDataResolver,
    PreciousMetalSemanticKey,
)
from backend.engine.private.market_data.global_models import GlobalEODSnapshot
from backend.engine.private.market_data.models import (
    GlobalEODQueryKey,
    MarketObservationResolutionResult,
    TefasFundCurrentMetricsQueryKey,
    TefasFundPriceQueryKey,
)
from backend.engine.private.precious_metals.constants import PreciousMetalPriceType, PreciousMetalType, PreciousMetalUnit
from backend.tests.invariants import static_guards as sg
from backend.tests.test_global_eod_resolver import make_obs as make_global_obs, make_snapshot as make_global_snapshot
from backend.tests.test_market_data_resolver import (
    create_mock_bist_obs,
    create_mock_bist_snapshot,
    create_mock_pm_obs,
    create_mock_pm_snapshot,
)
from backend.tests.test_tefas_metrics_resolver import create_metrics_snapshot
from backend.tests.test_tefas_price_resolver import make_observation as make_tefas_obs, make_snapshot as make_tefas_snapshot

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
SO, SY = AsOfMode.SOURCE_AS_OF, AsOfMode.SYSTEM_AS_OF
TRADE_DATE = date(2026, 9, 8)
CUTOFF = datetime(2026, 9, 10, 10, 0, tzinfo=PLUS3)  # 07:00 UTC
T0 = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
K = PrivateBacktestMarketDataKind
S = MarketDataResolutionStatus


def ctx(mode=SY, cutoff=CUTOFF) -> PrivateBacktestMarketDataContext:
    point = build_private_backtest_replay_point(pit_context=AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff), evaluation_date=date(2026, 9, 9))
    return build_private_backtest_market_data_context(replay_point=point)


# --- per-surface fixtures: (builder, query_key, snapshots) ----------------------------------------------------------

def bist_case(retrieved_at=T0, price="100.50"):
    inst = uuid4()
    obs = create_mock_bist_obs("THYAO", inst, TRADE_DATE, Decimal(price))
    snap = create_mock_bist_snapshot(TRADE_DATE, retrieved_at, "h1", [obs])
    return bind_private_backtest_bist_eod_resolution, BISTInstrumentQueryKey(instrument_id=inst, trade_date=TRADE_DATE, symbol="THYAO"), (snap,), K.BIST_EOD


def global_case():
    inst = uuid4()
    obs = make_global_obs(inst, "TIINGO", "AAPL", TRADE_DATE, Decimal("190.10"))
    snap = make_global_snapshot(inst, "TIINGO", "AAPL", T0, "g1", observations=[obs])
    return bind_private_backtest_global_eod_resolution, GlobalEODQueryKey(instrument_id=inst, trade_date=TRADE_DATE, provider="TIINGO", provider_symbol="AAPL"), (snap,), K.GLOBAL_EOD


def tefas_price_case():
    inst = uuid4()
    obs = make_tefas_obs(inst, TRADE_DATE, Decimal("1.2345"))
    snap = make_tefas_snapshot(inst, T0, [obs])
    return bind_private_backtest_tefas_fund_price_resolution, TefasFundPriceQueryKey(instrument_id=inst, trade_date=TRADE_DATE, provider_symbol="MAC"), (snap,), K.TEFAS_FUND_PRICE


def tefas_metrics_case():
    inst = uuid4()
    snap = create_metrics_snapshot(inst, "MAC", T0)
    return bind_private_backtest_tefas_current_metrics_resolution, TefasFundCurrentMetricsQueryKey(instrument_id=inst, provider_symbol="MAC"), (snap,), K.TEFAS_CURRENT_METRICS


def pm_case():
    obs = create_mock_pm_obs(PreciousMetalType.GOLD, TRADE_DATE, Decimal("7000000.00"), Currency.TRY, PreciousMetalUnit.KG,
                             PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0"))
    snap = create_mock_pm_snapshot(TRADE_DATE, T0, "pm1", [obs])
    key = PreciousMetalSemanticKey(metal=PreciousMetalType.GOLD, effective_date=TRADE_DATE, price_currency=Currency.TRY,
                                   quantity_unit=PreciousMetalUnit.KG, price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0"))
    return bind_private_backtest_precious_metal_resolution, key, (snap,), K.PRECIOUS_METAL


CASES = [bist_case, global_case, tefas_price_case, tefas_metrics_case, pm_case]
RESOLVER_METHOD = {
    K.BIST_EOD: "resolve_bist_eod", K.GLOBAL_EOD: "resolve_global_eod", K.TEFAS_FUND_PRICE: "resolve_tefas_fund_price",
    K.TEFAS_CURRENT_METRICS: "resolve_tefas_current_metrics", K.PRECIOUS_METAL: "resolve_precious_metal",
}


def bound(case, context=None, snapshots=None):
    builder, key, snaps, kind = case()
    snapshot = builder(market_context=context or ctx(), query_key=key, snapshots=snaps if snapshots is None else snapshots)
    return snapshot, key, snaps, kind


def walk_json(value):
    yield value
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from walk_json(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_json(v)


# --- contract ------------------------------------------------------------------------------------------------------

def test_stored_shape_is_exactly_four_fields() -> None:
    fs = fields(PrivateBacktestMarketDataResolutionSnapshot)
    assert [f.name for f in fs] == ["market_context", "kind", "query_key", "resolution_payload_json"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestMarketDataResolutionSnapshot.__dataclass_params__.frozen is True


def test_query_key_annotation_is_exactly_the_five_query_key_classes() -> None:
    import typing

    hint = typing.get_type_hints(PrivateBacktestMarketDataResolutionSnapshot)["query_key"]
    assert typing.get_origin(hint) in (typing.Union, getattr(__import__("types"), "UnionType", typing.Union))
    assert set(typing.get_args(hint)) == {
        BISTInstrumentQueryKey, GlobalEODQueryKey, TefasFundPriceQueryKey, TefasFundCurrentMetricsQueryKey, PreciousMetalSemanticKey,
    }
    assert len(typing.get_args(hint)) == 5
    assert [f.name for f in fields(PrivateBacktestMarketDataResolutionSnapshot)] == ["market_context", "kind", "query_key", "resolution_payload_json"]


def test_kind_enum_is_exactly_five_members() -> None:
    assert {m.name: m.value for m in K} == {
        "BIST_EOD": "bist_eod", "GLOBAL_EOD": "global_eod", "TEFAS_FUND_PRICE": "tefas_fund_price",
        "TEFAS_CURRENT_METRICS": "tefas_current_metrics", "PRECIOUS_METAL": "precious_metal",
    }


@pytest.mark.parametrize("case", CASES)
def test_every_surface_selects_and_stores_identity_context(case) -> None:
    context = ctx()
    snapshot, key, _, kind = bound(case, context)
    assert snapshot.market_context is context
    assert snapshot.kind is kind
    assert snapshot.query_key is key
    assert snapshot.status is S.SELECTED
    assert snapshot.resolution_key is not None
    assert snapshot.selected_observation_payload() is not None
    payload = snapshot.resolution_payload()
    assert payload["resolution_mode"] == "SYSTEM_AS_OF"
    assert datetime.fromisoformat(payload["as_of"]) == CUTOFF
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.kind = K.GLOBAL_EOD  # type: ignore[misc]


@pytest.mark.parametrize("case", CASES)
def test_stored_object_holds_no_mutable_source_or_result(case) -> None:
    snapshot, _, snaps, _ = bound(case)
    for f in fields(snapshot):
        value = getattr(snapshot, f.name)
        assert not isinstance(value, (list, tuple, MarketObservationResolutionResult))
        assert not any(value is s for s in snaps)
    assert type(snapshot.resolution_payload_json) is str


# --- canonical JSON / no float ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES)
def test_json_is_canonical_roundtrip_and_float_free(case) -> None:
    snapshot, _, _, _ = bound(case)
    text = snapshot.resolution_payload_json
    parsed = json.loads(text)
    assert json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) == text
    assert not [v for v in walk_json(parsed) if isinstance(v, float)]
    assert set(parsed) == set(module_under_test._PAYLOAD_KEYS)


@pytest.mark.parametrize("case", CASES)
def test_canonical_text_equals_resolver_to_dict(case) -> None:
    context = ctx()
    builder, key, snaps, kind = case()
    snapshot = builder(market_context=context, query_key=key, snapshots=snaps)
    direct = getattr(PointInTimeMarketDataResolver, RESOLVER_METHOD[kind])(key, list(snaps), mode=context.resolution_mode, as_of=context.as_of)
    assert snapshot.resolution_payload_json == json.dumps(direct.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def test_payload_key_set_matches_closed_result_contract() -> None:
    probe = MarketObservationResolutionResult(status=S.NO_SNAPSHOT, resolution_mode=MarketDataResolutionMode.SYSTEM_AS_OF, as_of=CUTOFF, observation_type="X", effective_date=None)
    assert set(module_under_test._PAYLOAD_KEYS) == set(probe.to_dict())


@pytest.mark.parametrize("bad", [1.5, Decimal("1"), uuid4(), date(2026, 1, 1), (1,), K.BIST_EOD, object()])
def test_json_native_validation_rejects_non_native_values(bad) -> None:
    builder, key, snaps, kind = bist_case()
    real = PointInTimeMarketDataResolver.resolve_bist_eod

    def tainted(*a, **k):
        res = real(*a, **k)
        original = res.to_dict
        res.to_dict = lambda: {**original(), "diagnostics": [bad]}  # type: ignore[method-assign]
        return res

    PointInTimeMarketDataResolver.resolve_bist_eod = staticmethod(tainted)  # type: ignore[method-assign,assignment]
    try:
        with pytest.raises(RuntimeError):
            builder(market_context=ctx(), query_key=key, snapshots=snaps)
    finally:
        PointInTimeMarketDataResolver.resolve_bist_eod = real  # type: ignore[method-assign]


def test_json_native_validation_rejects_dict_and_list_subclasses(monkeypatch) -> None:
    class D(dict):
        pass

    class L(list):
        pass

    builder, key, snaps, _ = bist_case()
    real = PointInTimeMarketDataResolver.resolve_bist_eod
    for tainted_value in ({"diagnostics": L()}, {"selected_observation": D()}):
        def tainted(*a, _v=tainted_value, **k):
            res = real(*a, **k)
            original = res.to_dict
            res.to_dict = lambda: {**original(), **_v}  # type: ignore[method-assign]
            return res

        monkeypatch.setattr(PointInTimeMarketDataResolver, "resolve_bist_eod", staticmethod(tainted))
        with pytest.raises(RuntimeError):
            builder(market_context=ctx(), query_key=key, snapshots=snaps)


# --- historical mode authority ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES)
def test_resolver_called_with_context_mode_and_as_of(case, monkeypatch) -> None:
    builder, key, snaps, kind = case()
    context = ctx()
    name = RESOLVER_METHOD[kind]
    real = getattr(PointInTimeMarketDataResolver, name)
    seen = {}

    def spy(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return real(*args, **kwargs)

    monkeypatch.setattr(PointInTimeMarketDataResolver, name, staticmethod(spy))
    builder(market_context=context, query_key=key, snapshots=snaps)
    assert seen["kwargs"]["mode"] is context.resolution_mode
    assert seen["kwargs"]["as_of"] is context.as_of
    passed = seen["args"][1]
    assert type(passed) is list and tuple(passed) == snaps


@pytest.mark.parametrize("case", CASES)
def test_only_the_matching_resolver_method_is_called(case, monkeypatch) -> None:
    builder, key, snaps, kind = case()
    for other_kind, name in RESOLVER_METHOD.items():
        if other_kind is not kind:
            monkeypatch.setattr(PointInTimeMarketDataResolver, name, staticmethod(lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrong resolver"))))
    builder(market_context=ctx(), query_key=key, snapshots=snaps)


# --- frontier reconciliation -----------------------------------------------------------------------------------------

def _patched_result(monkeypatch, name, **changes):
    real = getattr(PointInTimeMarketDataResolver, name)

    def patched(*a, **k):
        res = real(*a, **k)
        for attr, value in changes.items():
            setattr(res, attr, value)
        return res

    monkeypatch.setattr(PointInTimeMarketDataResolver, name, staticmethod(patched))


@pytest.mark.parametrize("delta", [timedelta(microseconds=1), timedelta(microseconds=-1)])
def test_frontier_off_by_one_microsecond_is_runtime_error(monkeypatch, delta) -> None:
    builder, key, snaps, _ = bist_case()
    _patched_result(monkeypatch, "resolve_bist_eod", as_of=CUTOFF + delta)
    with pytest.raises(RuntimeError):
        builder(market_context=ctx(), query_key=key, snapshots=snaps)


def test_frontier_equivalent_offset_is_accepted(monkeypatch) -> None:
    builder, key, snaps, _ = bist_case()
    _patched_result(monkeypatch, "resolve_bist_eod", as_of=CUTOFF.astimezone(UTC))
    assert builder(market_context=ctx(), query_key=key, snapshots=snaps).status is S.SELECTED


def test_frontier_none_as_of_is_runtime_error(monkeypatch) -> None:
    builder, key, snaps, _ = bist_case()
    _patched_result(monkeypatch, "resolve_bist_eod", as_of=None)
    with pytest.raises(RuntimeError):
        builder(market_context=ctx(), query_key=key, snapshots=snaps)


def test_frontier_mode_contradiction_is_runtime_error(monkeypatch) -> None:
    builder, key, snaps, _ = bist_case()
    _patched_result(monkeypatch, "resolve_bist_eod", resolution_mode=MarketDataResolutionMode.SOURCE_AS_OF)
    with pytest.raises(RuntimeError):
        builder(market_context=ctx(), query_key=key, snapshots=snaps)


def test_non_result_resolver_output_is_runtime_error(monkeypatch) -> None:
    builder, key, snaps, _ = bist_case()
    monkeypatch.setattr(PointInTimeMarketDataResolver, "resolve_bist_eod", staticmethod(lambda *a, **k: {"status": "SELECTED"}))
    with pytest.raises(RuntimeError):
        builder(market_context=ctx(), query_key=key, snapshots=snaps)


# --- economic date / instrument id consistency -----------------------------------------------------------------------

@pytest.mark.parametrize("case", [bist_case, global_case, tefas_price_case, pm_case, tefas_metrics_case])
def test_effective_date_contradiction_is_runtime_error(case, monkeypatch) -> None:
    builder, key, snaps, kind = case()
    wrong = date(2020, 1, 1)
    _patched_result(monkeypatch, RESOLVER_METHOD[kind], effective_date=wrong)
    with pytest.raises(RuntimeError):
        builder(market_context=ctx(), query_key=key, snapshots=snaps)


def test_tefas_metrics_effective_date_is_null_and_non_null_is_rejected(monkeypatch) -> None:
    snapshot, _, _, _ = bound(tefas_metrics_case)
    assert snapshot.resolution_payload()["effective_date"] is None


@pytest.mark.parametrize("case", [bist_case, global_case, tefas_price_case, tefas_metrics_case])
def test_canonical_instrument_id_matches_query(case, monkeypatch) -> None:
    snapshot, key, _, _ = bound(case)
    assert snapshot.resolution_payload()["canonical_instrument_id"] == str(key.instrument_id)
    builder, key, snaps, kind = case()
    _patched_result(monkeypatch, RESOLVER_METHOD[kind], canonical_instrument_id=uuid4())
    with pytest.raises(RuntimeError):
        builder(market_context=ctx(), query_key=key, snapshots=snaps)


@pytest.mark.parametrize("case", [bist_case, global_case, tefas_price_case, tefas_metrics_case])
def test_missing_canonical_instrument_id_is_rejected(case, monkeypatch) -> None:
    builder, key, snaps, kind = case()
    _patched_result(monkeypatch, RESOLVER_METHOD[kind], canonical_instrument_id=None)
    with pytest.raises(RuntimeError):
        builder(market_context=ctx(), query_key=key, snapshots=snaps)


# --- statuses preserved ----------------------------------------------------------------------------------------------

def test_source_as_of_unavailable_is_snapshotted_without_retry() -> None:
    for case in (bist_case, tefas_metrics_case):
        snapshot, _, _, _ = bound(case, ctx(mode=SO))
        assert snapshot.status is S.UNAVAILABLE_SOURCE_AS_OF
        assert snapshot.resolution_payload()["resolution_mode"] == "SOURCE_AS_OF"
        assert snapshot.selected_observation_payload() is None


def test_empty_snapshot_tuple_preserves_missing_status() -> None:
    for case in CASES:
        snapshot, _, _, _ = bound(case, snapshots=())
        assert snapshot.status in (S.NO_SNAPSHOT, S.NO_SNAPSHOT_AS_OF)
        assert snapshot.selected_observation_payload() is None
        assert snapshot.resolution_key is None or type(snapshot.resolution_key) is str


def test_conflict_status_is_preserved_not_raised() -> None:
    inst = uuid4()
    obs_a = create_mock_bist_obs("THYAO", inst, TRADE_DATE, Decimal("100.50"))
    obs_b = create_mock_bist_obs("THYAO", inst, TRADE_DATE, Decimal("101.50"))
    snap_a = create_mock_bist_snapshot(TRADE_DATE, T0, "ha", [obs_a])
    snap_b = create_mock_bist_snapshot(TRADE_DATE, T0, "hb", [obs_b])
    key = BISTInstrumentQueryKey(instrument_id=inst, trade_date=TRADE_DATE, symbol="THYAO")
    snapshot = bind_private_backtest_bist_eod_resolution(market_context=ctx(), query_key=key, snapshots=(snap_a, snap_b))
    assert snapshot.status is S.SNAPSHOT_CONFLICT
    assert snapshot.selected_observation_payload() is None


def test_status_is_derived_not_stored() -> None:
    assert "status" not in {f.name for f in fields(PrivateBacktestMarketDataResolutionSnapshot)}
    snapshot, _, _, _ = bound(bist_case)
    assert type(snapshot.status) is MarketDataResolutionStatus


def test_more_than_one_status_is_valid_construction() -> None:
    seen = set()
    seen.add(bound(bist_case)[0].status)
    seen.add(bound(bist_case, ctx(mode=SO))[0].status)
    seen.add(bound(bist_case, snapshots=())[0].status)
    assert len(seen) >= 3


# --- system as-of future isolation -----------------------------------------------------------------------------------

def test_system_as_of_future_snapshot_is_isolated_and_later_mutation_is_inert() -> None:
    inst = uuid4()
    obs_a = create_mock_bist_obs("THYAO", inst, TRADE_DATE, Decimal("100.50"))
    obs_b = create_mock_bist_obs("THYAO", inst, TRADE_DATE, Decimal("999.99"))
    snap_a = create_mock_bist_snapshot(TRADE_DATE, T0, "ha", [obs_a])
    snap_b = create_mock_bist_snapshot(TRADE_DATE, T2, "hb", [obs_b])
    assert T0 < CUTOFF < T2
    key = BISTInstrumentQueryKey(instrument_id=inst, trade_date=TRADE_DATE, symbol="THYAO")
    snapshot = bind_private_backtest_bist_eod_resolution(market_context=ctx(), query_key=key, snapshots=(snap_a, snap_b))
    before = snapshot.resolution_payload_json
    payload = snapshot.resolution_payload()
    assert payload["snapshot_hash"] == "ha"
    assert payload["selected_observation"]["close"] == "100.50"
    snap_b.observations.append(create_mock_bist_obs("THYAO", inst, TRADE_DATE, Decimal("5")))
    snap_b.payload_hash = "mutated"
    assert snapshot.resolution_payload_json == before


# --- mutation isolation ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES)
def test_source_mutation_after_binding_does_not_alter_stored_json(case) -> None:
    snapshot, _, snaps, _ = bound(case)
    before = snapshot.resolution_payload_json
    expected = snapshot.resolution_payload()
    for snap in snaps:
        snap.payload_hash = "tampered"
        for obs in (getattr(snap, "observations", None) or ([snap.observation] if getattr(snap, "observation", None) else [])):
            for attr in ("close", "price", "unit_price", "portfolio_size"):
                if hasattr(obs, attr):
                    setattr(obs, attr, Decimal("0"))
    assert snapshot.resolution_payload_json == before
    assert snapshot.resolution_payload() == expected


def test_resolver_result_mutation_does_not_alter_stored_json(monkeypatch) -> None:
    builder, key, snaps, _ = bist_case()
    real = PointInTimeMarketDataResolver.resolve_bist_eod
    holder = {}

    def capture(*a, **k):
        holder["result"] = real(*a, **k)
        return holder["result"]

    monkeypatch.setattr(PointInTimeMarketDataResolver, "resolve_bist_eod", staticmethod(capture))
    snapshot = builder(market_context=ctx(), query_key=key, snapshots=snaps)
    before = snapshot.resolution_payload_json
    result = holder["result"]
    assert result.selected_observation is not None
    result.status = S.NO_SNAPSHOT
    result.diagnostics.append("tampered")
    result.evaluation_snapshot_ids.append("tampered")
    result.selected_observation.close = Decimal("0")
    assert snapshot.resolution_payload_json == before
    assert snapshot.status is S.SELECTED
    assert not any(v is result for v in (getattr(snapshot, f.name) for f in fields(snapshot)))


# --- accessors return fresh copies -----------------------------------------------------------------------------------

def test_payload_accessors_return_fresh_copies() -> None:
    snapshot, _, _, _ = bound(bist_case)
    before = snapshot.resolution_payload_json
    p1 = snapshot.resolution_payload()
    p1["status"] = "X"
    p1["diagnostics"].append("x")
    s1 = snapshot.selected_observation_payload()
    s1["close"] = "0"
    assert snapshot.resolution_payload_json == before
    assert snapshot.resolution_payload() is not snapshot.resolution_payload()
    assert snapshot.resolution_payload()["status"] == "SELECTED"
    assert snapshot.selected_observation_payload() is not snapshot.selected_observation_payload()
    assert snapshot.selected_observation_payload()["close"] == "100.50"
    assert snapshot.resolution_key == snapshot.resolution_payload()["resolution_key"]


# --- builder input type discipline -----------------------------------------------------------------------------------

class _CtxSub(PrivateBacktestMarketDataContext):
    pass


@pytest.mark.parametrize("case", CASES)
def test_builders_require_exact_types(case) -> None:
    builder, key, snaps, _ = case()
    ok = dict(market_context=ctx(), query_key=key, snapshots=snaps)
    sub_ctx = _CtxSub(replay_point=ctx().replay_point)
    for bad in (dict(ok, market_context=sub_ctx), dict(ok, market_context=object()), dict(ok, query_key=str(key)), dict(ok, query_key={"a": 1}),
                dict(ok, snapshots=list(snaps)), dict(ok, snapshots=(s for s in snaps)), dict(ok, snapshots=set()), dict(ok, snapshots=(object(),))):
        with pytest.raises(TypeError):
            builder(**bad)

    class T(tuple):
        pass

    with pytest.raises(TypeError):
        builder(**dict(ok, snapshots=T(snaps)))


def test_builders_reject_query_key_subclass_and_wrong_family() -> None:
    @dataclasses.dataclass(frozen=True)
    class SubKey(BISTInstrumentQueryKey):
        pass

    builder, key, snaps, _ = bist_case()
    with pytest.raises(TypeError):
        builder(market_context=ctx(), query_key=SubKey(instrument_id=key.instrument_id, trade_date=key.trade_date), snapshots=snaps)
    _, tkey, tsnaps, _ = tefas_price_case()
    with pytest.raises(TypeError):
        builder(market_context=ctx(), query_key=tkey, snapshots=snaps)
    with pytest.raises(TypeError):
        builder(market_context=ctx(), query_key=key, snapshots=tsnaps)


def test_builders_accept_only_keyword_arguments_and_no_caller_result() -> None:
    import inspect
    for fn in (bind_private_backtest_bist_eod_resolution, bind_private_backtest_global_eod_resolution, bind_private_backtest_tefas_fund_price_resolution,
               bind_private_backtest_tefas_current_metrics_resolution, bind_private_backtest_precious_metal_resolution):
        params = inspect.signature(fn).parameters
        assert list(params) == ["market_context", "query_key", "snapshots"]
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


# --- direct construction ---------------------------------------------------------------------------------------------

def _good():
    snapshot, key, _, kind = bound(bist_case)
    return snapshot, key, kind


def _build(snapshot, **over):
    args = dict(market_context=snapshot.market_context, kind=snapshot.kind, query_key=snapshot.query_key, resolution_payload_json=snapshot.resolution_payload_json)
    args.update(over)
    return PrivateBacktestMarketDataResolutionSnapshot(**args)


def _dump(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def test_direct_construction_accepts_builder_output_and_enforces_types() -> None:
    snapshot, _, _ = _good()
    assert _build(snapshot) == snapshot
    with pytest.raises(TypeError):
        _build(snapshot, market_context=_CtxSub(replay_point=snapshot.market_context.replay_point))
    with pytest.raises(TypeError):
        _build(snapshot, kind="bist_eod")
    with pytest.raises(TypeError):
        _build(snapshot, kind=K.GLOBAL_EOD)  # query key of another family
    with pytest.raises(TypeError):
        _build(snapshot, resolution_payload_json=snapshot.resolution_payload_json.encode())

    class StrSub(str):
        pass

    with pytest.raises(TypeError):
        _build(snapshot, resolution_payload_json=StrSub(snapshot.resolution_payload_json))


def test_direct_construction_validates_payload_envelope() -> None:
    snapshot, _, _ = _good()
    base = snapshot.resolution_payload()
    for bad_text in ("not json", "[]", "1", "null", '{"status":1.5}', "NaN"):
        with pytest.raises(ValueError):
            _build(snapshot, resolution_payload_json=bad_text)
    missing = dict(base)
    missing.pop("status")
    with pytest.raises(ValueError):
        _build(snapshot, resolution_payload_json=_dump(missing))
    with pytest.raises(ValueError):
        _build(snapshot, resolution_payload_json=_dump({**base, "extra": 1}))
    with pytest.raises(ValueError):
        _build(snapshot, resolution_payload_json=_dump({**base, "resolution_mode": "SOURCE_AS_OF"}))
    with pytest.raises(ValueError):
        _build(snapshot, resolution_payload_json=_dump({**base, "resolution_mode": "CURRENT" + "_REPORTED"}))
    with pytest.raises(ValueError):
        _build(snapshot, resolution_payload_json=_dump({**base, "status": "NOPE"}))
    with pytest.raises(ValueError):
        _build(snapshot, resolution_payload_json=_dump({**base, "as_of": None}))
    with pytest.raises(ValueError):
        _build(snapshot, resolution_payload_json=_dump({**base, "as_of": "2026-09-10T07:00:00.000001+00:00"}))
    with pytest.raises(ValueError):
        _build(snapshot, resolution_payload_json=_dump({**base, "as_of": "garbage"}))
    with pytest.raises(ValueError):
        _build(snapshot, resolution_payload_json=_dump({**base, "as_of": "2026-09-10T07:00:00"}))  # naive
    # equivalent offset representation of the same instant is valid
    assert _build(snapshot, resolution_payload_json=_dump({**base, "as_of": "2026-09-10T10:00:00+03:00"})).status is S.SELECTED
    assert _build(snapshot, resolution_payload_json=_dump({**base, "as_of": "2026-09-10T07:00:00+00:00"})).status is S.SELECTED


def test_direct_construction_rejects_malformed_selected_observation_and_key() -> None:
    snapshot, _, _ = _good()
    base = snapshot.resolution_payload()
    with pytest.raises(ValueError):
        _build(snapshot, resolution_payload_json=_dump({**base, "selected_observation": [1]}))
    with pytest.raises(ValueError):
        _build(snapshot, resolution_payload_json=_dump({**base, "resolution_key": 7}))


# --- module boundary -------------------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_market_data_resolution_snapshot.py"


def test_module_is_not_registered_pure() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_module_imports_only_closed_surfaces() -> None:
    allowed_prefixes = (
        "backend.engine.private.backtest_market_data_bridge", "backend.engine.private.market_data", "backend.engine.private.bist.models",
        "backend.engine.private.precious_metals.models",
    )
    for node in ast.walk(_TREE):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("backend"):
            assert node.module.startswith(allowed_prefixes), node.module
    banned = {"requests", "httpx", "urllib", "socket", "hashlib", "random", "secrets", "uuid", "os", "time", "pathlib", "sqlite3", "supabase", "postgrest"}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & banned
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned


def test_module_has_no_clock_random_hash_or_current_reported() -> None:
    names = ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)})
    assert not names & {"now", "utcnow", "today", "time", "uuid4", "random", "urandom", "hashlib", "sha256", "md5", "float"}
    assert "CURRENT" + "_REPORTED" not in _SOURCE
    assert "hashlib" not in _SOURCE
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and isinstance(n.value, float)]


def test_documentation_states_required_boundaries() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PRIVATE_BACKTEST_MARKET_DATA_RESOLUTION_SNAPSHOT.md").read_text(encoding="utf-8")
    for needle in ("NON-PURE", "SOURCE_AS_OF", "SYSTEM_AS_OF", "CURRENT_REPORTED", "canonical JSON", "mutable", "C2B2B3", "C2C2", "C2E", "completeness", "no hash"):
        assert needle in doc, needle
