"""
backend/tests/test_backtest_market_data_selected_observation.py
===============================================================
Phase 26C2C2: strict typed reconstruction of the SELECTED observation stored in a Phase 26C2C1 market-data resolution snapshot. The immutable C2C1 JSON stays the
authority; the wrapper retains only that snapshot and `reconstruct()` returns a FRESH canonical typed representative of the audit payload (not the original object).
Positive cases come from the real closed resolver through the C2C1 builders; negative cases edit only the canonical JSON of a valid selected snapshot.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from backend.engine.private import backtest_market_data_selected_observation as module_under_test
from backend.engine.private.backtest_market_data_resolution_snapshot import PrivateBacktestMarketDataKind as K, PrivateBacktestMarketDataResolutionSnapshot
from backend.engine.private.backtest_market_data_selected_observation import (
    PrivateBacktestMarketDataSelectedObservation,
    reconstruct_private_backtest_selected_observation,
)
from backend.engine.private.bist.models import BISTEODObservation
from backend.engine.private.market_data.global_models import GlobalEODObservation
from backend.engine.private.market_data.models import MarketDataResolutionStatus as S
from backend.engine.private.market_data.resolver import PointInTimeMarketDataResolver
from backend.engine.private.market_data.tefas_metrics_models import TefasFundCurrentMetricsObservation
from backend.engine.private.market_data.tefas_models import TefasFundPriceObservation
from backend.engine.private.precious_metals.models import PreciousMetalMarketObservation
from backend.tests.invariants import static_guards as sg
from backend.tests.test_backtest_market_data_resolution_snapshot import (
    CASES,
    TRADE_DATE,
    BISTInstrumentQueryKey,
    bind_private_backtest_bist_eod_resolution,
    bist_case,
    bound,
    ctx,
    global_case,
    pm_case,
    tefas_metrics_case,
    tefas_price_case,
)
from backend.tests.test_market_data_resolver import create_mock_bist_obs, create_mock_bist_snapshot

UTC = timezone.utc
EXPECTED_TYPE = {K.BIST_EOD: BISTEODObservation, K.GLOBAL_EOD: GlobalEODObservation, K.TEFAS_FUND_PRICE: TefasFundPriceObservation,
                 K.TEFAS_CURRENT_METRICS: TefasFundCurrentMetricsObservation, K.PRECIOUS_METAL: PreciousMetalMarketObservation}
HASH_KEY = {K.BIST_EOD: "snapshot_hash", K.GLOBAL_EOD: "payload_hash", K.TEFAS_FUND_PRICE: "payload_hash", K.TEFAS_CURRENT_METRICS: "payload_hash",
            K.PRECIOUS_METAL: "payload_hash"}
DATE_KEY = {K.BIST_EOD: "trade_date", K.GLOBAL_EOD: "trade_date", K.TEFAS_FUND_PRICE: "trade_date", K.PRECIOUS_METAL: "effective_date"}
DECIMAL_KEY = {K.BIST_EOD: "close", K.GLOBAL_EOD: "close", K.TEFAS_FUND_PRICE: "unit_price", K.TEFAS_CURRENT_METRICS: "portfolio_size", K.PRECIOUS_METAL: "price"}
INT_KEY = {K.BIST_EOD: "trade_count", K.TEFAS_CURRENT_METRICS: "investor_count", K.PRECIOUS_METAL: "trade_count"}
INSTRUMENT_KINDS = (K.BIST_EOD, K.GLOBAL_EOD, K.TEFAS_FUND_PRICE, K.TEFAS_CURRENT_METRICS)


def selected(case):
    snapshot, key, snaps, kind = bound(case)
    assert snapshot.status is S.SELECTED
    return snapshot, kind


def rebuild(snapshot, edit) -> PrivateBacktestMarketDataResolutionSnapshot:
    """A new exact C2C1 envelope whose canonical JSON was edited; only the C2C1 envelope contract has to accept it."""
    payload = snapshot.resolution_payload()
    edit(payload)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return PrivateBacktestMarketDataResolutionSnapshot(market_context=snapshot.market_context, kind=snapshot.kind, query_key=snapshot.query_key, resolution_payload_json=text)


def wrap(snapshot) -> PrivateBacktestMarketDataSelectedObservation:
    return reconstruct_private_backtest_selected_observation(resolution_snapshot=snapshot)


def rejects(snapshot, edit) -> None:
    with pytest.raises(ValueError):
        wrap(rebuild(snapshot, edit))


all_cases = pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__)


# --- A, B, C: contract -----------------------------------------------------------------------------------------------

def test_wrapper_has_exactly_one_frozen_field_without_default() -> None:
    fs = dataclasses.fields(PrivateBacktestMarketDataSelectedObservation)
    assert [f.name for f in fs] == ["resolution_snapshot"]
    assert fs[0].default is dataclasses.MISSING and fs[0].default_factory is dataclasses.MISSING
    assert PrivateBacktestMarketDataSelectedObservation.__dataclass_params__.frozen is True
    snapshot, _ = selected(bist_case)
    with pytest.raises(dataclasses.FrozenInstanceError):
        wrap(snapshot).resolution_snapshot = snapshot  # type: ignore[misc]


def test_builder_is_keyword_only_exact_type_and_reconstruct_takes_no_arguments() -> None:
    params = inspect.signature(reconstruct_private_backtest_selected_observation).parameters
    assert list(params) == ["resolution_snapshot"]
    assert params["resolution_snapshot"].kind is inspect.Parameter.KEYWORD_ONLY and params["resolution_snapshot"].default is inspect.Parameter.empty
    assert list(inspect.signature(PrivateBacktestMarketDataSelectedObservation.reconstruct).parameters) == ["self"]
    snapshot, _ = selected(bist_case)
    with pytest.raises(TypeError):
        reconstruct_private_backtest_selected_observation(snapshot)  # type: ignore[misc]

    class Sub(PrivateBacktestMarketDataResolutionSnapshot):
        pass

    sub = Sub(market_context=snapshot.market_context, kind=snapshot.kind, query_key=snapshot.query_key, resolution_payload_json=snapshot.resolution_payload_json)
    for bad in (sub, object(), None, snapshot.resolution_payload_json):
        with pytest.raises(TypeError):
            wrap(bad)
        with pytest.raises(TypeError):
            PrivateBacktestMarketDataSelectedObservation(resolution_snapshot=bad)


# --- D: SELECTED only ------------------------------------------------------------------------------------------------

@all_cases
@pytest.mark.parametrize("status", [s for s in S if s is not S.SELECTED])
def test_every_non_selected_canonical_status_is_rejected(case, status) -> None:
    snapshot, _ = selected(case)
    rejects(snapshot, lambda p: p.__setitem__("status", status.value))


def test_real_resolver_non_selected_results_are_rejected() -> None:
    for case in CASES:
        empty, _, _, _ = bound(case, snapshots=())
        assert empty.status is not S.SELECTED
        with pytest.raises(ValueError):
            wrap(empty)
    inst = uuid4()
    key = BISTInstrumentQueryKey(instrument_id=inst, trade_date=TRADE_DATE, symbol="THYAO")
    from backend.tests.test_backtest_market_data_resolution_snapshot import T0
    a = create_mock_bist_snapshot(TRADE_DATE, T0, "ha", [create_mock_bist_obs("THYAO", inst, TRADE_DATE, Decimal("100.50"))])
    b = create_mock_bist_snapshot(TRADE_DATE, T0, "hb", [create_mock_bist_obs("THYAO", inst, TRADE_DATE, Decimal("101.50"))])
    conflict = bind_private_backtest_bist_eod_resolution(market_context=ctx(), query_key=key, snapshots=(a, b))
    assert conflict.status is S.SNAPSHOT_CONFLICT
    with pytest.raises(ValueError):
        wrap(conflict)
    from backend.engine.private.domain import AsOfMode
    for case in (bist_case, tefas_metrics_case):
        unavailable, _, _, _ = bound(case, ctx(mode=AsOfMode.SOURCE_AS_OF))
        assert unavailable.status is S.UNAVAILABLE_SOURCE_AS_OF
        with pytest.raises(ValueError):
            wrap(unavailable)


# --- E-I, AF: exact typed reconstruction and round trip --------------------------------------------------------------

@all_cases
def test_each_kind_reconstructs_its_exact_type_and_round_trips_the_stored_payload(case) -> None:
    snapshot, kind = selected(case)
    wrapper = wrap(snapshot)
    representative = wrapper.reconstruct()
    assert type(representative) is EXPECTED_TYPE[kind]
    stored = snapshot.selected_observation_payload()
    assert representative.to_dict() == stored
    assert str(representative.id) == snapshot.resolution_payload()["selected_observation_id"]
    assert wrapper.resolution_snapshot is snapshot


@all_cases
def test_payload_key_sets_are_exact_per_kind(case) -> None:
    snapshot, kind = selected(case)
    stored = snapshot.selected_observation_payload()
    assert set(wrap(snapshot).reconstruct().to_dict()) == set(stored)
    for key in list(stored):
        rejects(snapshot, lambda p, k=key: p["selected_observation"].pop(k))
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("unexpected_future_field", "x"))


# --- parsers ---------------------------------------------------------------------------------------------------------

@all_cases
def test_decimal_substitutions_are_rejected(case) -> None:
    snapshot, kind = selected(case)
    key = DECIMAL_KEY[kind]
    for bad in (100, True, "NaN", "Infinity", "-Infinity", " 1", "1 ", ["1"], {"v": "1"}):
        rejects(snapshot, lambda p, b=bad: p["selected_observation"].__setitem__(key, b))
    with pytest.raises(ValueError):                                              # the C2C1 envelope itself refuses a JSON float literal
        rebuild(snapshot, lambda p: p["selected_observation"].__setitem__(key, 1.5))
    for float_value in (1.5, float("nan")):
        with pytest.raises(ValueError):
            module_under_test._decimal(float_value)


@pytest.mark.parametrize("case", [bist_case, tefas_metrics_case, pm_case], ids=lambda c: c.__name__)
def test_bool_and_non_int_substituted_for_integers_are_rejected(case) -> None:
    snapshot, kind = selected(case)
    for bad in (True, False, "3", "3.0", [3]):
        rejects(snapshot, lambda p, b=bad: p["selected_observation"].__setitem__(INT_KEY[kind], b))


@all_cases
def test_uuid_date_datetime_and_enum_must_be_canonical(case) -> None:
    snapshot, kind = selected(case)
    stored = snapshot.selected_observation_payload()
    for bad in ("not-a-uuid", str(uuid4()).upper(), str(uuid4()).replace("-", ""), 7, None):
        rejects(snapshot, lambda p, b=bad: p["selected_observation"].__setitem__("id", b))
    for bad in ("2026-06-30T06:00:00", "2026-06-30", "", 5, "garbage"):                  # naive / date-only / malformed retrieved_at
        rejects(snapshot, lambda p, b=bad: p["selected_observation"].__setitem__("retrieved_at", b))
    if kind in DATE_KEY:
        for bad in ("2026-9-8", "20260908", "2026-02-30", 5, "2026-09-08T00:00:00"):
            rejects(snapshot, lambda p, b=bad: p["selected_observation"].__setitem__(DATE_KEY[kind], b))
    for bad in ("BOGUS", "Valid", "valid ", 1, None, ["valid"]):
        rejects(snapshot, lambda p, b=bad: p["selected_observation"].__setitem__("status", b))
    assert stored["status"] is not None


# --- S-U: SELECTED shape ---------------------------------------------------------------------------------------------

@all_cases
def test_selected_shape_fields_are_required_and_never_repaired(case) -> None:
    snapshot, _ = selected(case)
    rejects(snapshot, lambda p: p.__setitem__("selected_observation", None))
    for bad in (None, "", "not-a-uuid", 5):
        rejects(snapshot, lambda p, b=bad: p.__setitem__("selected_observation_id", b))
    for bad in (None, "", 5):
        rejects(snapshot, lambda p, b=bad: p.__setitem__("resolution_key", b))
    for bad in (None, "x", 5):
        rejects(snapshot, lambda p, b=bad: p.__setitem__("snapshot_id", b))
    for bad in ("2026-09-10T06:00:00", None, 5, "garbage"):
        rejects(snapshot, lambda p, b=bad: p.__setitem__("snapshot_retrieved_at", b))
    for bad in (None, "", 5):
        rejects(snapshot, lambda p, b=bad: p.__setitem__("snapshot_hash", b))


# --- V-X: identity ---------------------------------------------------------------------------------------------------

@all_cases
def test_selected_observation_id_must_match_the_representative(case) -> None:
    snapshot, _ = selected(case)
    rejects(snapshot, lambda p: p.__setitem__("selected_observation_id", str(uuid4())))


@pytest.mark.parametrize("case", [bist_case, global_case, tefas_price_case, tefas_metrics_case], ids=lambda c: c.__name__)
def test_instrument_identity_matches_query_and_top_level(case) -> None:
    snapshot, kind = selected(case)
    representative = wrap(snapshot).reconstruct()
    assert representative.instrument_id == snapshot.query_key.instrument_id == __import__("uuid").UUID(snapshot.resolution_payload()["canonical_instrument_id"])
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("instrument_id", str(uuid4())))
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("instrument_id", None))
    rejects(snapshot, lambda p: p.__setitem__("canonical_instrument_id", str(uuid4())))
    rejects(snapshot, lambda p: p.__setitem__("canonical_instrument_id", None))


def test_precious_metal_is_not_instrument_bound_and_invents_none() -> None:
    snapshot, _ = selected(pm_case)
    payload = snapshot.resolution_payload()
    assert payload["canonical_instrument_id"] is None and "instrument_id" not in payload["selected_observation"]
    representative = wrap(snapshot).reconstruct()
    assert not hasattr(representative, "instrument_id") and "instrument_id" not in representative.to_dict()
    rejects(snapshot, lambda p: p.__setitem__("canonical_instrument_id", str(uuid4())))
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("instrument_id", str(uuid4())))


# --- Y-AB: lineage, retrieval time, dates ----------------------------------------------------------------------------

@all_cases
def test_snapshot_id_and_hash_lineage_must_agree_with_the_top_level(case) -> None:
    snapshot, kind = selected(case)
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("snapshot_id", str(uuid4())))
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__(HASH_KEY[kind], "0" * 64))
    rejects(snapshot, lambda p: p.__setitem__("snapshot_id", str(uuid4())))
    rejects(snapshot, lambda p: p.__setitem__("snapshot_hash", "0" * 64))
    stored = snapshot.selected_observation_payload()
    assert stored["snapshot_id"] == snapshot.resolution_payload()["snapshot_id"] and stored[HASH_KEY[kind]] == snapshot.resolution_payload()["snapshot_hash"]


@all_cases
def test_retrieved_at_is_the_exact_same_instant_with_no_tolerance(case) -> None:
    snapshot, _ = selected(case)
    carried = snapshot.selected_observation_payload()["retrieved_at"]
    if carried is None:                                                          # an observation that carries no retrieved_at is not compared; one that does must match
        assert wrap(snapshot).reconstruct().retrieved_at is None
    stored = datetime.fromisoformat(carried or snapshot.resolution_payload()["snapshot_retrieved_at"])
    for delta in (timedelta(microseconds=1), -timedelta(microseconds=1), timedelta(hours=1)):
        rejects(snapshot, lambda p, d=delta: p["selected_observation"].__setitem__("retrieved_at", (stored + d).isoformat()))
    other_offset = stored.astimezone(timezone(timedelta(hours=3))).isoformat()
    assert other_offset != carried
    equivalent = wrap(rebuild(snapshot, lambda p: p["selected_observation"].__setitem__("retrieved_at", other_offset)))
    assert equivalent.reconstruct().to_dict()["retrieved_at"] == other_offset            # accepted only because it round-trips unchanged


@all_cases
def test_effective_date_consistency_with_query_and_top_level(case) -> None:
    snapshot, kind = selected(case)
    if kind is K.TEFAS_CURRENT_METRICS:
        assert snapshot.resolution_payload()["effective_date"] is None and snapshot.selected_observation_payload()["effective_date"] is None
        rejects(snapshot, lambda p: p.__setitem__("effective_date", "2026-09-08"))
        rejects(snapshot, lambda p: p["selected_observation"].__setitem__("effective_date", "2026-09-08"))
        return
    key = DATE_KEY[kind]
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__(key, "2026-09-07"))
    rejects(snapshot, lambda p: p.__setitem__("effective_date", "2026-09-07"))
    rejects(snapshot, lambda p: p.__setitem__("effective_date", None))


# --- AC-AE, AG: query semantics, provider, confidence ----------------------------------------------------------------

def test_global_provider_must_agree_with_query_and_top_level() -> None:
    snapshot, _ = selected(global_case)
    assert snapshot.resolution_payload()["provider"] == snapshot.selected_observation_payload()["provider"]
    rejects(snapshot, lambda p: p.__setitem__("provider", "OTHER"))
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("provider", "OTHER"))
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("provider", p["selected_observation"]["provider"].lower()))


def test_bist_tefas_and_precious_metal_provider_agreement() -> None:
    for case, key in ((bist_case, "source_provider"), (tefas_price_case, "provider"), (tefas_metrics_case, "provider"), (pm_case, "provider")):
        snapshot, _ = selected(case)
        top = snapshot.resolution_payload()
        assert top["provider"] == snapshot.selected_observation_payload()[key]
        rejects(snapshot, lambda p, k=key: p["selected_observation"].__setitem__(k, "OTHER"))
        rejects(snapshot, lambda p: p.__setitem__("provider", "OTHER"))
    snapshot, _ = selected(pm_case)
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("originating_source", "OTHER"))
    rejects(snapshot, lambda p: p.__setitem__("originating_source", "OTHER"))


def test_precious_metal_query_matches_is_the_closed_authority() -> None:
    snapshot, _ = selected(pm_case)
    representative = wrap(snapshot).reconstruct()
    assert snapshot.query_key.matches(representative)
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("price_currency", "USD"))
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("quantity_unit", "TROY_OUNCE" if p["selected_observation"]["quantity_unit"] != "TROY_OUNCE" else "KG"))
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("price_quantity", "2"))
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("fineness_per_mille", "999.9"))


@all_cases
def test_confidence_must_equal_the_top_level_confidence(case) -> None:
    snapshot, kind = selected(case)
    current = snapshot.resolution_payload()["confidence"]
    other = "NONE" if current != "NONE" else "LOW"
    rejects(snapshot, lambda p: p.__setitem__("confidence", other))
    field = "confidence" if kind is K.PRECIOUS_METAL else "confidence_level"
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__(field, other))


def stale_bist(confidence=None):
    from backend.tests.test_backtest_market_data_resolution_snapshot import T0
    inst = uuid4()
    obs = create_mock_bist_obs("THYAO", inst, TRADE_DATE, Decimal("100.50"))
    if confidence is not None:
        obs.confidence_level = confidence
    stale = create_mock_bist_snapshot(TRADE_DATE, T0, "hs", [obs], is_stale_discovery=True)
    key = BISTInstrumentQueryKey(instrument_id=inst, trade_date=TRADE_DATE, symbol="THYAO")
    return bind_private_backtest_bist_eod_resolution(market_context=ctx(), query_key=key, snapshots=(stale,))


def stale_precious_metal(confidence=None, stale=True):
    from backend.engine.private.domain import Currency
    from backend.engine.private.market_data.models import PreciousMetalSemanticKey
    from backend.engine.private.precious_metals.constants import PreciousMetalPriceType, PreciousMetalType, PreciousMetalUnit
    from backend.tests.test_backtest_market_data_resolution_snapshot import T0, bind_private_backtest_precious_metal_resolution
    from backend.tests.test_market_data_resolver import create_mock_pm_obs, create_mock_pm_snapshot
    obs = create_mock_pm_obs(PreciousMetalType.GOLD, TRADE_DATE, Decimal("7000000.00"), Currency.TRY, PreciousMetalUnit.KG, PreciousMetalPriceType.WEIGHTED_AVERAGE,
                             fineness_per_mille=Decimal("995.0"))
    if confidence is not None:
        obs.confidence = confidence
    snap = create_mock_pm_snapshot(TRADE_DATE, T0, "pms", [obs], is_stale_discovery=stale)
    key = PreciousMetalSemanticKey(metal=PreciousMetalType.GOLD, effective_date=TRADE_DATE, price_currency=Currency.TRY, quantity_unit=PreciousMetalUnit.KG,
                                   price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0"))
    return bind_private_backtest_precious_metal_resolution(market_context=ctx(), query_key=key, snapshots=(snap,))


@pytest.mark.parametrize("builder,observation_field", [(stale_bist, "confidence_level"), (stale_precious_metal, "confidence")], ids=["bist", "precious_metal"])
def test_real_resolver_stale_discovery_selection_reconstructs_with_two_confidence_layers(builder, observation_field) -> None:
    from backend.engine.private.domain import DataConfidenceLevel as L
    snapshot = builder()
    assert snapshot.status is S.SELECTED
    top, stored = snapshot.resolution_payload(), snapshot.selected_observation_payload()
    assert top["is_stale_discovery"] is True
    assert stored[observation_field] == L.HIGH.value and top["confidence"] == L.MEDIUM.value                 # the closed resolver's degradation
    assert any("DEGRADED_DISCOVERY" in d for d in top["diagnostics"])
    json_before = snapshot.resolution_payload_json
    representative = wrap(snapshot).reconstruct()                                                            # a valid stale SELECTED result must NOT fail
    assert getattr(representative, observation_field) is L.HIGH                                              # observation confidence retained, not degraded
    assert representative.to_dict() == stored and snapshot.resolution_payload()["confidence"] == L.MEDIUM.value
    assert snapshot.resolution_payload_json == json_before                                                   # nothing was recalculated or rewritten
    for observed, expected in ((L.MEDIUM, L.MEDIUM), (L.LOW, L.LOW), (L.NONE, L.NONE)):                      # only HIGH degrades; others are retained
        other = builder(observed)
        assert other.resolution_payload()["confidence"] == expected.value
        assert getattr(wrap(other).reconstruct(), observation_field) is observed


@pytest.mark.parametrize("builder", [stale_bist, stale_precious_metal], ids=["bist", "precious_metal"])
def test_impossible_or_tampered_confidence_relationships_are_rejected(builder) -> None:
    snapshot = builder()
    for bad in ("HIGH", "LOW", "NONE"):                                                                       # stale + HIGH observation can only be MEDIUM
        rejects(snapshot, lambda p, b=bad: p.__setitem__("confidence", b))
    rejects(snapshot, lambda p: p.__setitem__("is_stale_discovery", False))                                  # MEDIUM without a stale source is impossible
    rejects(snapshot, lambda p: p.__setitem__("is_stale_discovery", "yes"))
    rejects(snapshot, lambda p: p.__setitem__("is_stale_discovery", None))
    fresh = builder(stale=False) if builder is stale_precious_metal else selected(bist_case)[0]
    assert fresh.resolution_payload()["is_stale_discovery"] is False
    assert wrap(fresh).reconstruct().to_dict() == fresh.selected_observation_payload()                       # non-stale: layers must agree
    rejects(fresh, lambda p: p.__setitem__("confidence", "MEDIUM"))
    rejects(fresh, lambda p: p.__setitem__("is_stale_discovery", True))                                      # HIGH observation + stale flag demands MEDIUM, not HIGH


@pytest.mark.parametrize("case", [global_case, tefas_price_case, tefas_metrics_case], ids=lambda c: c.__name__)
def test_kinds_that_never_degrade_reject_a_stale_flag_or_a_confidence_difference(case) -> None:
    snapshot, _ = selected(case)
    assert snapshot.resolution_payload()["is_stale_discovery"] is False
    rejects(snapshot, lambda p: p.__setitem__("is_stale_discovery", True))
    rejects(snapshot, lambda p: p.__setitem__("confidence", "MEDIUM" if p["confidence"] != "MEDIUM" else "LOW"))


def _global_with_query_provider(query_provider):
    from backend.engine.private.market_data.models import GlobalEODQueryKey
    from backend.tests.test_backtest_market_data_resolution_snapshot import T0, bind_private_backtest_global_eod_resolution, make_global_obs, make_global_snapshot
    inst = uuid4()
    obs = make_global_obs(inst, "TIINGO", "AAPL", TRADE_DATE, Decimal("190.10"))
    snap = make_global_snapshot(inst, "TIINGO", "AAPL", T0, "g1", observations=[obs])
    key = GlobalEODQueryKey(instrument_id=inst, trade_date=TRADE_DATE, provider=query_provider, provider_symbol="AAPL")
    return bind_private_backtest_global_eod_resolution(market_context=ctx(), query_key=key, snapshots=(snap,))


@pytest.mark.parametrize("query_provider", ["TIINGO", "tiingo", " tiingo ", "Tiingo\t"], ids=["upper", "lower", "spaced", "mixed_tab"])
def test_global_provider_follows_the_closed_resolver_canonicalization_only(query_provider) -> None:
    snapshot = _global_with_query_provider(query_provider)
    assert snapshot.status is S.SELECTED and snapshot.query_key.provider == query_provider
    top, stored = snapshot.resolution_payload(), snapshot.selected_observation_payload()
    assert top["provider"] == stored["provider"] == query_provider.strip().upper() == "TIINGO"
    representative = wrap(snapshot).reconstruct()
    assert representative.provider == "TIINGO" and representative.to_dict() == stored
    rejects(snapshot, lambda p: p["selected_observation"].__setitem__("provider", "tiingo"))               # observation differs from the stored top-level provider
    rejects(snapshot, lambda p: p.__setitem__("provider", "tiingo"))                                        # top-level differs from the observation and from the canonical form
    rejects(snapshot, lambda p: (p.__setitem__("provider", "tiingo"), p["selected_observation"].__setitem__("provider", "tiingo")))  # agree with each other, not with strip().upper()
    rejects(snapshot, lambda p: (p.__setitem__("provider", "OTHER"), p["selected_observation"].__setitem__("provider", "OTHER")))      # no alias or substitution


def test_bist_raw_provider_symbol_is_canonicalized_to_the_serialized_value() -> None:
    inst = uuid4()
    from backend.tests.test_backtest_market_data_resolution_snapshot import T0
    obs = create_mock_bist_obs("THYAO", inst, TRADE_DATE, Decimal("100.50"))
    obs.raw_provider_symbol = None                                              # not recoverable from to_dict(): it serializes `raw_provider_symbol or symbol`
    snap = create_mock_bist_snapshot(TRADE_DATE, T0, "hr", [obs])
    key = BISTInstrumentQueryKey(instrument_id=inst, trade_date=TRADE_DATE, symbol="THYAO")
    snapshot = bind_private_backtest_bist_eod_resolution(market_context=ctx(), query_key=key, snapshots=(snap,))
    stored = snapshot.selected_observation_payload()
    assert stored["raw_provider_symbol"] == stored["symbol"] == "THYAO"
    representative = wrap(snapshot).reconstruct()
    assert representative.raw_provider_symbol == "THYAO" and representative.to_dict() == stored


# --- AH, AI: fresh objects and mutation isolation --------------------------------------------------------------------

@all_cases
def test_every_reconstruction_is_a_fresh_object_and_mutation_cannot_leak(case) -> None:
    snapshot, kind = selected(case)
    wrapper = wrap(snapshot)
    stored = snapshot.selected_observation_payload()
    json_before = snapshot.resolution_payload_json
    first, second = wrapper.reconstruct(), wrapper.reconstruct()
    assert first is not second and type(first) is type(second) is EXPECTED_TYPE[kind]
    assert first.to_dict() == second.to_dict() == stored
    first.diagnostics.append("MUTATED")
    first.status = None                                                          # the legacy models are mutable; nothing may reach the wrapper
    third = wrapper.reconstruct()
    assert third.to_dict() == stored and third is not first and third.diagnostics is not first.diagnostics
    assert wrapper.resolution_snapshot is snapshot and snapshot.resolution_payload_json == json_before
    assert snapshot.selected_observation_payload() == stored


def test_wrapper_retains_only_the_c2c1_snapshot_and_no_mutable_or_source_object() -> None:
    snapshot, _ = selected(bist_case)
    wrapper = wrap(snapshot)
    assert vars(wrapper) == {"resolution_snapshot": snapshot}
    assert {f.name for f in dataclasses.fields(PrivateBacktestMarketDataSelectedObservation)} == {"resolution_snapshot"}
    assert not any(name in dir(wrapper) for name in ("snapshots", "observation", "selected_observation", "source_snapshots", "result"))


# --- direct construction ---------------------------------------------------------------------------------------------

def test_direct_construction_validates_the_whole_reconstruction() -> None:
    snapshot, _ = selected(bist_case)
    assert PrivateBacktestMarketDataSelectedObservation(resolution_snapshot=snapshot) == wrap(snapshot)
    bad = rebuild(snapshot, lambda p: p["selected_observation"].__setitem__("close", 100))
    with pytest.raises(ValueError):
        PrivateBacktestMarketDataSelectedObservation(resolution_snapshot=bad)


# --- AJ-AO: no resolver, clock, uuid, fallback, valuation policy -----------------------------------------------------

@all_cases
def test_reconstruction_never_calls_the_resolver(case, monkeypatch) -> None:
    snapshot, _ = selected(case)

    def boom(*args, **kwargs):
        raise AssertionError("resolver replay")

    for name in dir(PointInTimeMarketDataResolver):
        if name.startswith("resolve_"):
            monkeypatch.setattr(PointInTimeMarketDataResolver, name, boom)
    assert wrap(snapshot).reconstruct().to_dict() == snapshot.selected_observation_payload()


@all_cases
def test_reconstruction_creates_no_identity_or_timestamp_of_its_own(case, monkeypatch) -> None:
    snapshot, _ = selected(case)
    import uuid as uuid_module
    monkeypatch.setattr(uuid_module, "uuid4", lambda: (_ for _ in ()).throw(AssertionError("uuid4")))
    stored = snapshot.selected_observation_payload()
    representative = wrap(snapshot).reconstruct()
    assert str(representative.id) == stored["id"] and representative.to_dict()["retrieved_at"] == stored["retrieved_at"]


def test_no_valuation_price_policy_and_tefas_metrics_price_is_not_promoted() -> None:
    snapshot, _ = selected(tefas_metrics_case)
    representative = wrap(snapshot).reconstruct()
    stored = snapshot.selected_observation_payload()
    assert (None if representative.reported_current_unit_price is None else str(representative.reported_current_unit_price)) == stored["reported_current_unit_price"]
    wrapper = wrap(snapshot)
    public = {name for name in dir(wrapper) if not name.startswith("_")}
    assert public == {"resolution_snapshot", "reconstruct"}
    for word in ("price", "value", "valuation", "mark", "close", "convert", "currency"):
        assert not any(word in name.lower() for name in public), word


# --- source-surface guards -------------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_market_data_selected_observation.py"


def _names() -> set:
    return ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
            | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})


def test_no_resolver_clock_random_uuid_hash_io_or_fallback() -> None:
    assert not _names() & {"PointInTimeMarketDataResolver", "resolve_bist_eod", "resolve_global_eod", "resolve_tefas_fund_price", "resolve_tefas_current_metrics",
                           "resolve_precious_metal", "uuid4", "uuid1", "now", "utcnow", "today", "time", "random", "secrets", "urandom", "sha256", "hashlib", "hmac",
                           "hash", "open", "client", "rpc", "table", "environ", "getenv", "sleep", "json", "loads", "dumps", "CURRENT_REPORTED", "MarketDataResolutionMode",
                           "SOURCE_AS_OF", "SYSTEM_AS_OF", "snapshots", "diagnostics_fallback"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await))]


def test_no_valuation_policy_portfolio_allocation_completeness_or_rebalance_dependency() -> None:
    assert not _names() & {"close", "adj_close", "unit_price", "reported_current_unit_price", "price", "price_quantity", "weighted_average", "valuation", "mark",
                           "LedgerProjectionView", "PortfolioTransaction", "RebalanceCurrentState", "build_cash_first_rebalance_plan", "build_band_aware_rebalance_plan",
                           "PrivateBacktestInputBundle", "PrivateBacktestInputCompletenessStatus", "PrivateBacktestInputRequirement", "convert", "fx_rate"}
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules <= {"backend.engine.private.backtest_market_data_resolution_snapshot", "backend.engine.private.bist.models", "backend.engine.private.domain",
                       "backend.engine.private.market_data.global_models", "backend.engine.private.market_data.models", "backend.engine.private.market_data.tefas_metrics_models",
                       "backend.engine.private.market_data.tefas_models", "backend.engine.private.precious_metals.constants",
                       "backend.engine.private.precious_metals.models"}
    for forbidden in ("portfolio", "allocation", "rebalance", "scheduler", "resolver", "completeness", "provider", "supabase", "repository"):
        assert not any(forbidden in m for m in modules), forbidden


def test_module_is_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_documentation_and_ci_wiring() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "PRIVATE_BACKTEST_MARKET_DATA_SELECTED_OBSERVATION.md").read_text(encoding="utf-8")
    for needle in ("previously deferred", "D3", "marked", "canonical typed representative", "not original object", "fresh", "SELECTED only", "round trip",
                   "raw_provider_symbol", "no resolver replay", "no fallback", "no valuation-price policy", "diagnostic", "not automatically a portfolio instrument",
                   "stale-discovery", "Observation-level confidence", "resolver-level confidence", "strip().upper()", "invents no new normalization", "D3A", "Red Team"):
        assert needle in doc, needle
    snapshot_doc = (root / "docs" / "PRIVATE_BACKTEST_MARKET_DATA_RESOLUTION_SNAPSHOT.md").read_text(encoding="utf-8")
    assert "C2C2 is implemented because the upcoming D3 marked-portfolio-state replay creates the first real typed selected-observation consumer" in snapshot_doc
    assert "deferred unless later decision replay actually needs it" not in snapshot_doc
    completeness = (root / "docs" / "PRIVATE_BACKTEST_INPUT_COMPLETENESS.md").read_text(encoding="utf-8")
    assert "D1 and D2 did not require C2C2" in completeness and "D3 portfolio marked-value boundary" in completeness and "C2E itself does not reconstruct observations" in completeness
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    architecture = ci.split("Phase 26 backtest architecture correctness", 1)[1].split("- name:", 1)[0]
    assert "backend/tests/test_backtest_market_data_selected_observation.py" in architecture
