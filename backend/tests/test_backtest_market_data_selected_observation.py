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


# --- R1: top-level SELECTED requires the closed resolver's final observation eligibility --------------------------------

def envelope(snapshot, edit) -> PrivateBacktestMarketDataResolutionSnapshot:
    """A directly constructed exact C2C1 envelope over edited canonical JSON; C2C1 (envelope authority) must accept it."""
    edited = rebuild(snapshot, edit)
    assert type(edited) is PrivateBacktestMarketDataResolutionSnapshot and edited.status is S.SELECTED
    return edited


def ineligible(snapshot, **fields) -> None:
    edited = envelope(snapshot, lambda p: p["selected_observation"].update(fields))
    with pytest.raises(ValueError):                                              # C2C2 rejects the impossible SELECTED semantics C2C1 deliberately does not judge
        wrap(edited)


def eligible(snapshot, **fields):
    edited = envelope(snapshot, lambda p: p["selected_observation"].update(fields))
    representative = wrap(edited).reconstruct()
    assert representative.to_dict() == edited.selected_observation_payload()
    return representative


NON_TEFAS_TYPE = None


def _non_tefas_instrument_type() -> str:
    from backend.engine.private.domain import InstrumentType
    return next(m.value for m in InstrumentType if m not in module_under_test._TEFAS_ALLOWED_INSTRUMENT_TYPES)


def test_the_hole_is_real_valid_status_enums_parse_but_are_not_selectable() -> None:
    parse = module_under_test._enum(__import__("backend.engine.private.bist.models", fromlist=["BISTObservationStatus"]).BISTObservationStatus)
    assert parse("invalid_observation") is not None                              # syntactically valid enum member: the parser alone accepts it
    snapshot, _ = selected(bist_case)
    ineligible(snapshot, status="invalid_observation")                           # ... yet it contradicts a top-level SELECTED and must fail


@pytest.mark.parametrize("case", [bist_case, global_case], ids=lambda c: c.__name__)
def test_bist_and_global_selected_require_a_valid_finite_close(case) -> None:
    snapshot, _ = selected(case)
    for status in ("invalid_observation", "unresolved_identity"):
        ineligible(snapshot, status=status)
    ineligible(snapshot, close=None)
    assert wrap(snapshot).reconstruct().close is not None                        # the valid finite SELECTED case stays accepted
    eligible(snapshot, close="0")                                                # the closed resolver requires finite, not positive: no extra policy
    eligible(snapshot, close="-1.5")


def test_global_adj_close_is_not_required() -> None:
    snapshot, _ = selected(global_case)
    eligible(snapshot, adj_close=None)


def test_tefas_price_selected_eligibility() -> None:
    snapshot, _ = selected(tefas_price_case)
    ineligible(snapshot, status="invalid_observation")
    ineligible(snapshot, status="rate_limited")
    ineligible(snapshot, unit_price=None)
    ineligible(snapshot, unit_price="0")
    ineligible(snapshot, unit_price="0.0")
    ineligible(snapshot, unit_price="-1.2345")
    ineligible(snapshot, currency=None)
    ineligible(snapshot, instrument_type=None)
    ineligible(snapshot, instrument_type=_non_tefas_instrument_type())
    assert wrap(snapshot).reconstruct().unit_price is not None


def test_tefas_allowed_instrument_types_equal_the_closed_resolver_constant_exactly() -> None:
    from backend.engine.private.market_data.resolver import TEFAS_RESOLVER_ALLOWED_INSTRUMENT_TYPES
    assert set(module_under_test._TEFAS_ALLOWED_INSTRUMENT_TYPES) == set(TEFAS_RESOLVER_ALLOWED_INSTRUMENT_TYPES)
    assert isinstance(module_under_test._TEFAS_ALLOWED_INSTRUMENT_TYPES, frozenset)
    for member in TEFAS_RESOLVER_ALLOWED_INSTRUMENT_TYPES:                      # every closed allowed type is accepted for fund prices
        eligible(selected(tefas_price_case)[0], instrument_type=member.value)


def test_tefas_current_metrics_selected_eligibility() -> None:
    snapshot, _ = selected(tefas_metrics_case)
    from backend.engine.private.domain import Currency
    other_currency = next(c.value for c in Currency if c is not Currency.TRY)
    for fields in ({"status": "invalid_observation"}, {"portfolio_size": None}, {"portfolio_size": "-1"}, {"portfolio_size_currency": other_currency},
                   {"portfolio_size_currency": None}, {"instrument_type": None}, {"instrument_type": _non_tefas_instrument_type()},
                   {"outstanding_units": "-1"}, {"investor_count": -1}, {"retrieved_at": None}):
        ineligible(snapshot, **fields)
    stored = datetime.fromisoformat(snapshot.selected_observation_payload()["retrieved_at"])
    ineligible(snapshot, retrieved_at=(stored + timedelta(microseconds=1)).isoformat())
    for fields in ({"portfolio_size": "0"}, {"outstanding_units": "0"}, {"investor_count": 0}, {"outstanding_units": None}, {"investor_count": None},
                   {"outstanding_units": None, "investor_count": None, "reported_current_unit_price": None}):
        eligible(snapshot, **fields)                                              # zero and absent optional metrics stay eligible; the diagnostic price is never required


def test_precious_metal_selected_eligibility() -> None:
    snapshot, _ = selected(pm_case)
    ineligible(snapshot, status="INVALID_OBSERVATION")
    ineligible(snapshot, status="UNSUPPORTED_METAL")
    ineligible(snapshot, price=None)
    assert wrap(snapshot).reconstruct().price is not None
    eligible(snapshot, price="0")                                                  # no positivity rule exists in the closed resolver


@pytest.mark.parametrize("builder", [stale_bist, stale_precious_metal], ids=["bist", "precious_metal"])
def test_valid_stale_discovery_selection_stays_eligible(builder) -> None:
    snapshot = builder()
    assert snapshot.resolution_payload()["is_stale_discovery"] is True
    assert wrap(snapshot).reconstruct().to_dict() == snapshot.selected_observation_payload()


def test_eligibility_validation_does_not_replay_the_resolver_or_recompute_the_resolution_key(monkeypatch) -> None:
    snapshot, _ = selected(bist_case)
    for name in dir(PointInTimeMarketDataResolver):
        if name.startswith("resolve_"):
            monkeypatch.setattr(PointInTimeMarketDataResolver, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("resolver replay")))
    key_before = snapshot.resolution_key
    edited = envelope(snapshot, lambda p: p.__setitem__("resolution_key", "tampered-key-not-recomputed"))
    assert wrap(edited).reconstruct().to_dict() == edited.selected_observation_payload()      # claim limit: the key is not recomputed, the resolver run is not proven
    assert key_before != edited.resolution_key


# --- Phase 27 FIX B: temporal admission of a SELECTED envelope (the closed resolver's necessary conditions) -----------------------------------------------------------
# A directly constructed C2C1 envelope is an audit object. C2C2 is the first typed consumer, so it must refuse a SELECTED envelope that the closed resolver could never produce at the
# envelope's own mode/frontier: SOURCE_AS_OF never selects, and SYSTEM_AS_OF selects only snapshots with retrieved_at <= as_of (inclusive, exact instant).

from backend.engine.private.market_data.models import MarketDataResolutionMode  # noqa: E402
from backend.tests.test_backtest_market_data_resolution_snapshot import PLUS3, SO, SY, T0  # noqa: E402


def rebind(snapshot, context):
    """Direct C2C1 construction under another frontier: only the claimed top-level mode/as_of change; the stored selection and its retrieval instant are untouched."""
    def edit(payload):
        payload["resolution_mode"] = context.resolution_mode.value
        payload["as_of"] = context.as_of.isoformat()
    payload = snapshot.resolution_payload()
    edit(payload)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return PrivateBacktestMarketDataResolutionSnapshot(market_context=context, kind=snapshot.kind, query_key=snapshot.query_key, resolution_payload_json=text)


def temporal_rejects(envelope) -> None:
    with pytest.raises(ValueError, match="temporal frontier"):
        wrap(envelope)


@all_cases
def test_system_snapshot_strictly_before_the_cutoff_is_admitted(case) -> None:
    snapshot, kind = selected(case)
    assert snapshot.market_context.resolution_mode is MarketDataResolutionMode.SYSTEM_AS_OF
    wrap(snapshot)
    assert datetime.fromisoformat(snapshot.resolution_payload()["snapshot_retrieved_at"]) < snapshot.market_context.as_of


@all_cases
def test_system_snapshot_retrieved_exactly_at_the_cutoff_is_admitted(case) -> None:
    snapshot, key, snaps, kind = bound(case, ctx(cutoff=T0))
    assert snapshot.status is S.SELECTED and snapshot.market_context.as_of == T0
    wrap(snapshot)
    assert datetime.fromisoformat(snapshot.resolution_payload()["snapshot_retrieved_at"]) == snapshot.market_context.as_of


@all_cases
def test_exact_boundary_is_an_instant_comparison_across_equivalent_offsets(case) -> None:
    snapshot, key, snaps, kind = bound(case, ctx(cutoff=T0.astimezone(PLUS3)))          # same instant written at +03:00
    assert snapshot.market_context.as_of.utcoffset() == timedelta(hours=3) and snapshot.status is S.SELECTED
    wrap(snapshot)


@all_cases
def test_system_snapshot_retrieved_after_the_cutoff_is_rejected_even_by_one_microsecond(case) -> None:
    late, key, snaps, kind = bound(case, ctx(cutoff=T0 + timedelta(hours=1)))             # a genuine resolver SELECTED result at a later frontier
    assert late.status is S.SELECTED
    for early_cutoff in (T0 - timedelta(hours=1), T0 - timedelta(microseconds=1)):
        envelope = rebind(late, ctx(cutoff=early_cutoff))                                   # C2C1 still accepts the audit envelope
        assert envelope.status is S.SELECTED
        temporal_rejects(envelope)
    # the closed resolver itself selects nothing at that earlier frontier
    honest, _key, _snaps, _kind = bound(case, ctx(cutoff=T0 - timedelta(hours=1)))
    assert honest.status is not S.SELECTED


@all_cases
def test_source_as_of_selected_is_rejected_for_every_surface(case) -> None:
    system, key, snaps, kind = bound(case)
    honest, _k, _s, _kind = bound(case, ctx(mode=SO))
    assert honest.status is S.UNAVAILABLE_SOURCE_AS_OF                                       # the closed resolver never selects under SOURCE_AS_OF
    forged = rebind(system, ctx(mode=SO))
    assert forged.status is S.SELECTED and forged.market_context.resolution_mode is MarketDataResolutionMode.SOURCE_AS_OF
    temporal_rejects(forged)


@all_cases
def test_the_temporal_rule_is_additional_to_the_exact_retrieval_lineage(case) -> None:
    snapshot, kind = selected(case)
    if snapshot.selected_observation_payload()["retrieved_at"] is not None:        # (a TEFAS price observation may carry no retrieval instant of its own: the existing rule)
        rejects(snapshot, lambda p: p.__setitem__("snapshot_retrieved_at", (datetime.fromisoformat(p["snapshot_retrieved_at"]) - timedelta(hours=1)).isoformat()))   # earlier but different
    rejects(snapshot, lambda p: p.__setitem__("snapshot_retrieved_at", "2026-09-10T06:00:00"))                                                                       # naive text


def test_a_selected_envelope_that_fails_only_the_temporal_rule_does_not_get_a_shape_or_eligibility_message() -> None:
    late, key, snaps, kind = bound(bist_case, ctx(cutoff=T0 + timedelta(hours=1)))
    with pytest.raises(ValueError) as error:
        wrap(rebind(late, ctx(cutoff=T0 - timedelta(hours=1))))
    assert "temporal frontier" in str(error.value) and "eligib" not in str(error.value) and "payload" not in str(error.value)


def test_a_top_level_as_of_mismatch_is_still_rejected_by_c2c1_itself() -> None:
    snapshot, kind = selected(bist_case)
    payload = snapshot.resolution_payload()
    payload["as_of"] = (snapshot.market_context.as_of + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        PrivateBacktestMarketDataResolutionSnapshot(market_context=snapshot.market_context, kind=snapshot.kind, query_key=snapshot.query_key,
                                                    resolution_payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")))


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
                           "hash", "open", "client", "rpc", "table", "environ", "getenv", "sleep", "json", "loads", "dumps", "CURRENT_REPORTED", "SOURCE_AS_OF", "snapshots", "diagnostics_fallback"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await))]


def test_temporal_admission_uses_the_canonical_enum_member_only_with_no_clock_tolerance_or_other_mode() -> None:
    names = _names()
    assert "MarketDataResolutionMode" in names and "SYSTEM_AS_OF" in names
    assert not names & {"CURRENT_REPORTED", "SOURCE_AS_OF", "timedelta", "timezone", "astimezone", "replace", "timestamp", "total_seconds", "float", "round"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in {"SYSTEM_AS_OF", "SOURCE_AS_OF", "CURRENT_REPORTED"}]      # no raw mode strings
    import re
    assert re.search(r"context\.resolution_mode is not MarketDataResolutionMode\.SYSTEM_AS_OF", _SOURCE)          # exact enum-member identity
    assert re.search(r"snapshot_retrieved_at > context\.as_of", _SOURCE)                                           # inclusive boundary: only strictly-after is rejected


def test_no_valuation_policy_portfolio_allocation_completeness_or_rebalance_dependency() -> None:
    assert not _names() & {"adj_close", "reported_current_unit_price", "price_quantity", "weighted_average", "valuation", "mark",
                           "LedgerProjectionView", "PortfolioTransaction", "RebalanceCurrentState", "build_cash_first_rebalance_plan", "build_band_aware_rebalance_plan",
                           "PrivateBacktestInputBundle", "PrivateBacktestInputCompletenessStatus", "PrivateBacktestInputRequirement", "convert", "fx_rate"}
    # The raw price fields are read ONLY by the eligibility validator, and only for presence / finiteness / sign: no arithmetic, no field choice for valuation.
    for function in (n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)):
        reads = {n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)} & {"close", "unit_price", "price"}
        assert not reads or function.name == "_check_eligibility", (function.name, reads)
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.BinOp, ast.AugAssign))]
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
                   "stale-discovery", "payload type validity is insufficient", "surface-specific observation eligibility", "without running the resolver", "resolution key", "R1", "Observation-level confidence", "resolver-level confidence", "strip().upper()", "invents no new normalization", "D3A", "Red Team", "temporal compatibility", "retrieved_at <= as_of", "exact boundary", "SOURCE_AS_OF SELECTED", "Phase 27 FIX B", "does not prove the resolver actually ran"):
        assert needle in doc, needle
    snapshot_doc = (root / "docs" / "PRIVATE_BACKTEST_MARKET_DATA_RESOLUTION_SNAPSHOT.md").read_text(encoding="utf-8")
    assert "C2C2 is implemented because the upcoming D3 marked-portfolio-state replay creates the first real typed selected-observation consumer" in snapshot_doc
    assert "deferred unless later decision replay actually needs it" not in snapshot_doc
    completeness = (root / "docs" / "PRIVATE_BACKTEST_INPUT_COMPLETENESS.md").read_text(encoding="utf-8")
    assert "D1 and D2 did not require C2C2" in completeness and "D3 portfolio marked-value boundary" in completeness and "C2E itself does not reconstruct observations" in completeness
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    architecture = ci.split("Phase 26 backtest architecture correctness", 1)[1].split("- name:", 1)[0]
    assert "backend/tests/test_backtest_market_data_selected_observation.py" in architecture
    assert "backend/tests/test_backtest_market_data_temporal_admission.py" in architecture
    assert "audit-envelope authority" in snapshot_doc and "Economic consumers must pass through C2C2" in snapshot_doc
