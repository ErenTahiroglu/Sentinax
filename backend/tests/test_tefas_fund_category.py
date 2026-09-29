"""
backend/tests/test_tefas_fund_category.py
=========================================
Tests for PIT-safe TEFAS fund category authority (Phase 16G).

Category authority is the `fonKategori` label of the raw payload of the snapshot selected by the existing
current-metrics resolver, bound to that snapshot by re-verifying the payload hash. Outer-strip canonicalization
only; no taxonomy, no InstrumentType mapping, no effective-date fabrication, no resurrection.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.engine.private import fund_category as module_under_test
from backend.engine.private.domain import Currency, DataConfidenceLevel, InstrumentType
from backend.engine.private.fund_category import (
    TefasFundCategoryLineage,
    TefasFundCategoryObservation,
    TefasFundCategoryUnavailable,
    TefasFundCategoryUnavailableReason,
    resolve_tefas_fund_category,
)
from backend.engine.private.market_data.models import (
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
    MarketObservationResolutionResult,
    TefasFundCurrentMetricsQueryKey,
)
from backend.engine.private.market_data.resolver import PointInTimeMarketDataResolver
from backend.engine.private.market_data.tefas_metrics_models import (
    TefasFundCurrentMetricsObservation,
    TefasFundMetricsSnapshot,
)
from backend.engine.private.market_data.tefas_models import TefasObservationStatus
from backend.engine.private.storage_models import compute_payload_hash

CR = MarketDataResolutionMode.CURRENT_REPORTED
SYS = MarketDataResolutionMode.SYSTEM_AS_OF
SRC = MarketDataResolutionMode.SOURCE_AS_OF
ST = MarketDataResolutionStatus
R = TefasFundCategoryUnavailableReason

_INST = UUID("11111111-1111-4111-8111-111111111111")
_T1 = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)
_T2 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
_QUERY = TefasFundCurrentMetricsQueryKey(instrument_id=_INST, provider_symbol="MAC")
_HEX64 = "a" * 64


def _payload(category=..., extra=None) -> str:
    row = {"fonKodu": "MAC", "sonFiyat": 0.76165, "kategoriDerece": 150, "kategoriFonSay": 199,
           "pazarPayi": 1.64, "fonUnvan": "ACME HISSE SENEDI FONU"}
    if category is not ...:
        row["fonKategori"] = category
    if extra:
        row.update(extra)
    return json.dumps({"errorCode": None, "errorMessage": None, "resultList": [row]}, ensure_ascii=False)


def _snap(retrieved_at: datetime, payload: str, instrument_type=InstrumentType.TEFAS_FUND,
          payload_hash=None, http_status: int = 200, amount: str = "1000000000.00") -> TefasFundMetricsSnapshot:
    sid = uuid4()
    p_hash = payload_hash or compute_payload_hash(payload)
    observation = TefasFundCurrentMetricsObservation(
        id=uuid4(), snapshot_id=sid, instrument_id=_INST, provider="TEFAS", provider_symbol="MAC",
        portfolio_size=Decimal(amount), portfolio_size_currency=Currency.TRY,
        outstanding_units=Decimal("10000000"), investor_count=10000,
        reported_current_unit_price=Decimal("100.00"), instrument_type=instrument_type, payload_hash=p_hash,
        retrieved_at=retrieved_at, status=TefasObservationStatus.VALID, confidence_level=DataConfidenceLevel.MEDIUM)
    return TefasFundMetricsSnapshot(
        id=sid, provider="TEFAS", provider_symbol="MAC", retrieved_at=retrieved_at, http_status=http_status,
        payload_hash=p_hash, raw_payload=payload, instrument_id=_INST, endpoint="FUND_CURRENT_METRICS",
        observation=observation)


def _resolve(snapshots, mode=CR, as_of=None, query=_QUERY):
    return resolve_tefas_fund_category(query_key=query, snapshots=snapshots, mode=mode, as_of=as_of)


def _label(snap_payload_category, **kw):
    result = _resolve([_snap(_T1, _payload(snap_payload_category), **kw)])
    assert type(result) is TefasFundCategoryObservation, result
    return result.category_label


def _unavailable(payload: str, **kw) -> TefasFundCategoryUnavailable:
    result = _resolve([_snap(_T1, payload, **kw)])
    assert type(result) is TefasFundCategoryUnavailable, result
    return result


class _Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")

    def __str__(self) -> str:
        raise RuntimeError("hostile str")


def _lineage(**overrides) -> TefasFundCategoryLineage:
    kwargs = dict(snapshot_id=uuid4(), snapshot_hash=_HEX64, observed_at=_T1, metrics_resolution_key="b" * 64)
    kwargs.update(overrides)
    return TefasFundCategoryLineage(**kwargs)


# --- structure ------------------------------------------------------------------------

def test_reason_enum_is_exact() -> None:
    assert [(m.name, m.value) for m in R] == [
        ("UNDERLYING_UNAVAILABLE", "underlying_unavailable"),
        ("SELECTED_SNAPSHOT_UNAVAILABLE", "selected_snapshot_unavailable"),
        ("PAYLOAD_INTEGRITY_FAILURE", "payload_integrity_failure"),
        ("RAW_PAYLOAD_INVALID", "raw_payload_invalid"),
        ("CATEGORY_MISSING", "category_missing"),
        ("CATEGORY_INVALID", "category_invalid"),
    ]
    for attr in ("score", "severity", "rank", "level"):
        assert not hasattr(R.CATEGORY_MISSING, attr)


def test_dataclass_fields_and_frozen() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundCategoryLineage)] == [
        "snapshot_id", "snapshot_hash", "observed_at", "metrics_resolution_key"]
    assert [f.name for f in dataclasses.fields(TefasFundCategoryObservation)] == [
        "instrument_id", "category_label", "mode", "as_of", "lineage"]
    assert [f.name for f in dataclasses.fields(TefasFundCategoryUnavailable)] == [
        "instrument_id", "mode", "as_of", "reason", "underlying_status", "lineage"]
    lineage = _lineage()
    with pytest.raises(dataclasses.FrozenInstanceError):
        lineage.snapshot_hash = _HEX64  # type: ignore[misc]
    for attr in ("effective_date", "published_at", "score", "rank", "peer_group"):
        assert not hasattr(lineage, attr)


# --- lineage validation / hostile values ---------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("snapshot_id", str(uuid4())), ("snapshot_id", None), ("snapshot_id", _Hostile()),
    ("snapshot_hash", "A" * 64), ("snapshot_hash", "a" * 63), ("snapshot_hash", "a" * 65), ("snapshot_hash", b"a" * 64),
    ("snapshot_hash", None), ("snapshot_hash", "g" * 64), ("snapshot_hash", _Hostile()),
    ("observed_at", datetime(2026, 8, 27, 10)), ("observed_at", "2026-08-27"), ("observed_at", None),
    ("observed_at", _Hostile()),
    ("metrics_resolution_key", "B" * 64), ("metrics_resolution_key", "b" * 10), ("metrics_resolution_key", None),
    ("metrics_resolution_key", _Hostile()),
])
def test_lineage_rejects_invalid_values(field, value) -> None:
    with pytest.raises((TypeError, ValueError)) as info:
        _lineage(**{field: value})
    assert "hostile" not in str(info.value)


def test_lineage_accepts_valid_hex_hashes() -> None:
    assert _lineage(snapshot_hash="0123456789abcdef" * 4).snapshot_hash == "0123456789abcdef" * 4


# --- observation validation -------------------------------------------------------------------

def _obs(**overrides) -> TefasFundCategoryObservation:
    kwargs = dict(instrument_id=_INST, category_label="Hisse Senedi Fonu", mode=CR, as_of=None, lineage=_lineage())
    kwargs.update(overrides)
    return TefasFundCategoryObservation(**kwargs)


@pytest.mark.parametrize("field,value", [
    ("instrument_id", str(_INST)), ("instrument_id", None),
    ("category_label", ""), ("category_label", "  X  "), ("category_label", " X"), ("category_label", "X "),
    ("category_label", None), ("category_label", 1), ("category_label", b"x"), ("category_label", "\n"),
    ("category_label", _Hostile()),
    ("mode", "CURRENT_REPORTED"), ("mode", None), ("mode", SRC),
    ("as_of", datetime(2026, 8, 27)), ("as_of", "x"),
    ("lineage", None), ("lineage", object()), ("lineage", _Hostile()),
])
def test_observation_rejects_invalid_values(field, value) -> None:
    with pytest.raises((TypeError, ValueError)) as info:
        _obs(**{field: value})
    assert "hostile" not in str(info.value)


def test_observation_mode_and_as_of_consistency() -> None:
    assert _obs(mode=CR, as_of=None)
    assert _obs(mode=SYS, as_of=_T2, lineage=_lineage(observed_at=_T1))
    assert _obs(mode=SYS, as_of=_T1, lineage=_lineage(observed_at=_T1))  # boundary is inclusive
    with pytest.raises(ValueError):
        _obs(mode=SYS, as_of=_T1, lineage=_lineage(observed_at=_T2))    # future snapshot in a historical view
    with pytest.raises((TypeError, ValueError)):
        _obs(mode=SYS, as_of=None)
    with pytest.raises((TypeError, ValueError)):
        _obs(mode=CR, as_of=_T2)


def test_subclass_lineage_rejected() -> None:
    class _Sub(TefasFundCategoryLineage):
        pass
    sub = _Sub(snapshot_id=uuid4(), snapshot_hash=_HEX64, observed_at=_T1, metrics_resolution_key="b" * 64)
    with pytest.raises(TypeError):
        _obs(lineage=sub)


# --- unavailable validation --------------------------------------------------------------------

def _una(**overrides) -> TefasFundCategoryUnavailable:
    kwargs = dict(instrument_id=_INST, mode=CR, as_of=None, reason=R.UNDERLYING_UNAVAILABLE,
                  underlying_status=ST.NO_SNAPSHOT, lineage=None)
    kwargs.update(overrides)
    return TefasFundCategoryUnavailable(**kwargs)


def test_unavailable_relationship_between_reason_and_underlying_status() -> None:
    assert _una()
    for status in ST:
        if status is ST.SELECTED:
            with pytest.raises(ValueError):
                _una(underlying_status=status)
        else:
            assert _una(underlying_status=status)
    for reason in R:
        if reason is R.UNDERLYING_UNAVAILABLE:
            continue
        assert _una(reason=reason, underlying_status=ST.SELECTED)
        with pytest.raises(ValueError):
            _una(reason=reason, underlying_status=ST.NO_SNAPSHOT)


@pytest.mark.parametrize("field,value", [
    ("instrument_id", "x"), ("mode", "CR"), ("as_of", datetime(2026, 8, 27)), ("reason", "category_missing"),
    ("underlying_status", "SELECTED"), ("lineage", object()), ("reason", None), ("underlying_status", None),
])
def test_unavailable_rejects_invalid_types(field, value) -> None:
    with pytest.raises((TypeError, ValueError)):
        _una(**{field: value})


def test_unavailable_allows_source_as_of_mode_and_optional_lineage() -> None:
    assert _una(mode=SRC, underlying_status=ST.UNAVAILABLE_SOURCE_AS_OF)
    assert _una(reason=R.CATEGORY_MISSING, underlying_status=ST.SELECTED, lineage=_lineage()).lineage is not None


# --- A/B/C: CURRENT_REPORTED, strip-only, no case normalization -------------------------------------------

def test_current_reported_selects_the_raw_category() -> None:
    snap = _snap(_T1, _payload("Hisse Senedi Fonu"))
    result = _resolve([snap])
    assert type(result) is TefasFundCategoryObservation
    assert result.category_label == "Hisse Senedi Fonu"
    assert result.instrument_id == _INST and result.mode is CR and result.as_of is None
    assert result.lineage.snapshot_id == snap.id
    assert result.lineage.snapshot_hash == snap.payload_hash == compute_payload_hash(snap.raw_payload)
    assert result.lineage.observed_at == snap.retrieved_at == _T1
    metrics = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(_QUERY, [snap], mode=CR, as_of=None)
    assert result.lineage.metrics_resolution_key == metrics.resolution_key


@pytest.mark.parametrize("raw,expected", [
    ("  Hisse Senedi Fonu  ", "Hisse Senedi Fonu"), ("\tDeğişken Fon\n", "Değişken Fon"),
    (" Borçlanma Araçları Fonu ", "Borçlanma Araçları Fonu"),
])
def test_outer_whitespace_is_stripped_only(raw: str, expected: str) -> None:
    assert _label(raw) == expected


def test_inner_whitespace_is_preserved() -> None:
    assert _label("Hisse   Senedi  Fonu") == "Hisse   Senedi  Fonu"


@pytest.mark.parametrize("a,b", [("Hisse Senedi Fonu", "hisse senedi fonu"), ("Değişken Fon", "Degisken Fon"),
                                 ("İstanbul", "istanbul"), ("Fon", "FON")])
def test_no_case_accent_or_turkish_normalization(a: str, b: str) -> None:
    assert _label(a) == a and _label(b) == b and _label(a) != _label(b)


# --- D..G: missing / null / blank / non-string -------------------------------------------------------------

def test_missing_key_is_category_missing_with_lineage() -> None:
    result = _unavailable(_payload())
    assert (result.reason, result.underlying_status) == (R.CATEGORY_MISSING, ST.SELECTED)
    assert result.lineage is not None and result.lineage.observed_at == _T1


@pytest.mark.parametrize("value", [None, "", "   ", "\n\t", " "])
def test_null_and_blank_categories_are_missing(value) -> None:
    assert _unavailable(_payload(value)).reason is R.CATEGORY_MISSING


@pytest.mark.parametrize("value", [1, 1.5, True, False, [], ["Hisse Senedi Fonu"], {}, {"a": 1}, 0])
def test_non_string_categories_are_invalid(value) -> None:
    result = _unavailable(_payload(value))
    assert result.reason is R.CATEGORY_INVALID and result.underlying_status is ST.SELECTED
    assert result.lineage is not None


def test_no_fallback_to_other_fields_or_generic_labels() -> None:
    result = _unavailable(_payload(extra={"fonKategori": None, "kategori": "Hisse Senedi Fonu",
                                          "category": "Hisse Senedi Fonu"}))
    assert result.reason is R.CATEGORY_MISSING
    for label in ("Unknown", "Other", "", "TEFAS_FUND"):
        assert not isinstance(result, TefasFundCategoryObservation) or result.category_label != label


# --- H..L: payload shape ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "{", "not json", "", "null", "[]", "42", '"x"', "true",
    json.dumps({"errorCode": None}),                                               # missing resultList
    json.dumps({"resultList": None}), json.dumps({"resultList": {}}), json.dumps({"resultList": "x"}),
    json.dumps({"resultList": []}),                                                # zero rows
    json.dumps({"resultList": [{"fonKategori": "A"}, {"fonKategori": "B"}]}),      # multiple rows
    json.dumps({"resultList": ["x"]}), json.dumps({"resultList": [None]}), json.dumps({"resultList": [[]]}),
    json.dumps([{"resultList": [{"fonKategori": "A"}]}]),                           # wrong root
    '{"resultList": [{"fonKategori": "A"}], "x": NaN}',
])
def test_malformed_or_wrongly_shaped_payloads_are_invalid(payload: str) -> None:
    result = _unavailable(payload)
    assert result.reason is R.RAW_PAYLOAD_INVALID and result.underlying_status is ST.SELECTED
    assert result.lineage is not None


@pytest.mark.parametrize("payload", [
    '{"resultList": [{"fonKategori": "A", "fonKategori": "B"}]}',
    '{"resultList": [{"fonKategori": "A"}], "resultList": [{"fonKategori": "B"}]}',
    '{"resultList": [{"fonKategori": "A", "x": 1, "x": 2}]}',
])
def test_duplicate_json_keys_are_rejected_not_last_wins(payload: str) -> None:
    assert _unavailable(payload).reason is R.RAW_PAYLOAD_INVALID


def test_non_str_raw_payload_is_invalid() -> None:
    raw = _payload("Hisse Senedi Fonu").encode("utf-8")
    snap = _snap(_T1, raw)  # type: ignore[arg-type]
    result = _resolve([snap])
    assert type(result) is TefasFundCategoryUnavailable and result.reason is R.RAW_PAYLOAD_INVALID


def test_deeply_nested_payload_is_invalid_not_a_crash() -> None:
    payload = "[" * 100000 + "]" * 100000
    assert _unavailable(payload).reason is R.RAW_PAYLOAD_INVALID


# --- M: payload hash binding ----------------------------------------------------------------------------------------

def test_payload_hash_mismatch_fails_closed_before_parsing() -> None:
    original = _payload("Hisse Senedi Fonu")
    snap = _snap(_T1, original)
    snap.raw_payload = _payload("Değişken Fon")  # tampered after the hash was recorded
    result = _resolve([snap])
    assert type(result) is TefasFundCategoryUnavailable
    assert result.reason is R.PAYLOAD_INTEGRITY_FAILURE and result.underlying_status is ST.SELECTED


def test_hash_mismatch_does_not_expose_the_tampered_label() -> None:
    snap = _snap(_T1, _payload("A"))
    snap.raw_payload = _payload("B")
    assert not isinstance(_resolve([snap]), TefasFundCategoryObservation)


def test_integrity_failure_when_recorded_hashes_disagree_with_content() -> None:
    payload = _payload("Hisse Senedi Fonu")
    snap = _snap(_T1, payload, payload_hash="c" * 64)  # a structurally valid but wrong digest
    result = _resolve([snap])
    assert result.reason is R.PAYLOAD_INTEGRITY_FAILURE


def test_malformed_recorded_hash_cannot_form_lineage() -> None:
    snap = _snap(_T1, _payload("Hisse Senedi Fonu"), payload_hash="hash_not_hex")
    result = _resolve([snap])
    assert type(result) is TefasFundCategoryUnavailable
    assert result.reason is R.SELECTED_SNAPSHOT_UNAVAILABLE and result.lineage is None


# --- N/O: selected snapshot recovery and lineage agreement ---------------------------------------------------

def _fake_selected(snapshot_id, snapshot_hash, retrieved_at, instrument=_INST, key="d" * 64, status=ST.SELECTED):
    return MarketObservationResolutionResult(
        status=status, resolution_mode=CR, as_of=None, observation_type="TEFAS_FUND_CURRENT_METRICS",
        effective_date=None, snapshot_id=snapshot_id, snapshot_hash=snapshot_hash,
        snapshot_retrieved_at=retrieved_at, canonical_instrument_id=instrument, resolution_key=key)


def _patch_resolver(monkeypatch, result) -> None:
    monkeypatch.setattr(PointInTimeMarketDataResolver, "resolve_tefas_current_metrics",
                        classmethod(lambda cls, *a, **k: result))


def test_selected_snapshot_not_found_in_supplied_snapshots(monkeypatch) -> None:
    snap = _snap(_T1, _payload("Hisse Senedi Fonu"))
    _patch_resolver(monkeypatch, _fake_selected(uuid4(), snap.payload_hash, _T1))
    result = _resolve([snap])
    assert result.reason is R.SELECTED_SNAPSHOT_UNAVAILABLE and result.lineage is None
    assert result.underlying_status is ST.SELECTED


def test_duplicate_snapshot_ids_are_ambiguous(monkeypatch) -> None:
    a = _snap(_T1, _payload("A"))
    b = _snap(_T1, _payload("B"))
    b.id = a.id
    _patch_resolver(monkeypatch, _fake_selected(a.id, a.payload_hash, _T1))
    assert _resolve([a, b]).reason is R.SELECTED_SNAPSHOT_UNAVAILABLE


@pytest.mark.parametrize("kind", ["hash", "retrieved_at", "snapshot_instrument", "result_instrument",
                                  "missing_key", "bad_key", "missing_hash", "missing_time", "missing_id"])
def test_selected_snapshot_lineage_mismatch(monkeypatch, kind: str) -> None:
    snap = _snap(_T1, _payload("Hisse Senedi Fonu"))
    args = dict(snapshot_id=snap.id, snapshot_hash=snap.payload_hash, retrieved_at=_T1, instrument=_INST, key="d" * 64)
    if kind == "hash":
        args["snapshot_hash"] = "e" * 64
    elif kind == "retrieved_at":
        args["retrieved_at"] = _T2
    elif kind == "snapshot_instrument":
        snap.instrument_id = uuid4()
    elif kind == "result_instrument":
        args["instrument"] = uuid4()
    elif kind == "missing_key":
        args["key"] = None
    elif kind == "bad_key":
        args["key"] = "not-hex"
    elif kind == "missing_hash":
        args["snapshot_hash"] = None
    elif kind == "missing_time":
        args["retrieved_at"] = None
    elif kind == "missing_id":
        args["snapshot_id"] = None
    _patch_resolver(monkeypatch, _fake_selected(**args))
    result = _resolve([snap])
    assert type(result) is TefasFundCategoryUnavailable
    assert result.reason is R.SELECTED_SNAPSHOT_UNAVAILABLE and result.lineage is None


def test_no_other_snapshot_is_substituted(monkeypatch) -> None:
    other = _snap(_T1, _payload("Değişken Fon"))
    _patch_resolver(monkeypatch, _fake_selected(uuid4(), other.payload_hash, _T1))
    assert not isinstance(_resolve([other]), TefasFundCategoryObservation)


# --- P: no-resurrection ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("newer_category,reason", [(..., R.CATEGORY_MISSING), (None, R.CATEGORY_MISSING),
                                                   ("  ", R.CATEGORY_MISSING), (7, R.CATEGORY_INVALID)])
def test_current_reported_no_resurrection(newer_category, reason) -> None:
    older = _snap(_T1, _payload("Hisse Senedi Fonu"))
    newer = _snap(_T2, _payload(newer_category))
    result = _resolve([older, newer])
    assert type(result) is TefasFundCategoryUnavailable and result.reason is reason
    assert result.lineage.snapshot_id == newer.id
    assert _resolve([older]).category_label == "Hisse Senedi Fonu"


def test_invalid_payload_in_newer_snapshot_blocks_older_label() -> None:
    older = _snap(_T1, _payload("Hisse Senedi Fonu"))
    newer = _snap(_T2, "{")
    assert _resolve([older, newer]).reason is R.RAW_PAYLOAD_INVALID


def test_current_reported_selects_the_latest_snapshot_category() -> None:
    older = _snap(_T1, _payload("Hisse Senedi Fonu"))
    newer = _snap(_T2, _payload("Değişken Fon"))
    result = _resolve([older, newer])
    assert result.category_label == "Değişken Fon" and result.lineage.observed_at == _T2


# --- Q/R: SYSTEM_AS_OF future isolation and historical change ----------------------------------------------------

def test_system_as_of_future_isolation_firewall() -> None:
    a = _snap(_T1, _payload("Hisse Senedi Fonu"))
    b = _snap(_T2, _payload("Değişken Fon"))
    cutoff = _T1 + timedelta(minutes=30)
    historical = _resolve([a, b], mode=SYS, as_of=cutoff)
    assert type(historical) is TefasFundCategoryObservation
    assert historical.category_label == "Hisse Senedi Fonu"
    assert historical.mode is SYS and historical.as_of == cutoff
    assert historical.lineage.observed_at == _T1 <= cutoff
    assert _resolve([a], mode=SYS, as_of=cutoff) == historical  # adding the future snapshot changes nothing


@pytest.mark.parametrize("future_payload", ["{", _payload(None), _payload(...), _payload(99), "", _payload("Z")])
def test_arbitrary_future_snapshots_do_not_alter_history(future_payload) -> None:
    a = _snap(_T1, _payload("Hisse Senedi Fonu"))
    future = _snap(_T2 + timedelta(days=3), future_payload)
    cutoff = _T1 + timedelta(hours=1)
    assert _resolve([a, future], mode=SYS, as_of=cutoff) == _resolve([a], mode=SYS, as_of=cutoff)


def test_system_as_of_historical_category_change() -> None:
    a = _snap(_T1, _payload("Hisse Senedi Fonu"))
    b = _snap(_T2, _payload("Değişken Fon"))
    before = _resolve([a, b], mode=SYS, as_of=_T1 + timedelta(minutes=1))
    after = _resolve([a, b], mode=SYS, as_of=_T2 + timedelta(minutes=1))
    assert (before.category_label, after.category_label) == ("Hisse Senedi Fonu", "Değişken Fon")
    assert before.lineage != after.lineage


def test_system_as_of_before_any_snapshot_is_unavailable() -> None:
    a = _snap(_T1, _payload("Hisse Senedi Fonu"))
    result = _resolve([a], mode=SYS, as_of=_T1 - timedelta(hours=1))
    assert (result.reason, result.underlying_status) == (R.UNDERLYING_UNAVAILABLE, ST.NO_SNAPSHOT_AS_OF) or \
        result.reason is R.UNDERLYING_UNAVAILABLE


def test_system_as_of_missing_as_of_propagates_invalid_temporal_lineage() -> None:
    result = _resolve([_snap(_T1, _payload("Hisse Senedi Fonu"))], mode=SYS, as_of=None)
    assert (result.reason, result.underlying_status) == (R.UNDERLYING_UNAVAILABLE, ST.INVALID_TEMPORAL_LINEAGE)


def test_naive_as_of_is_rejected_before_resolution() -> None:
    with pytest.raises(TypeError, match=r"^as_of must be None or an exact timezone-aware datetime$"):
        _resolve([], mode=SYS, as_of=datetime(2026, 8, 27, 12))


def test_current_reported_rejects_as_of() -> None:
    with pytest.raises(ValueError, match=r"^as_of must be None for CURRENT_REPORTED$"):
        _resolve([], mode=CR, as_of=_T2)


# --- S: SOURCE_AS_OF ---------------------------------------------------------------------------------------------------

def test_source_as_of_is_unavailable_and_never_downgraded() -> None:
    a = _snap(_T1, _payload("Hisse Senedi Fonu"))
    for as_of in (None, _T2):
        result = _resolve([a], mode=SRC, as_of=as_of)
        assert type(result) is TefasFundCategoryUnavailable
        assert (result.reason, result.underlying_status, result.mode) == (
            R.UNDERLYING_UNAVAILABLE, ST.UNAVAILABLE_SOURCE_AS_OF, SRC)
        assert result.lineage is None


# --- T/U/V: underlying status propagation --------------------------------------------------------------------------------

def test_underlying_no_snapshot_propagates() -> None:
    result = _resolve([])
    assert (result.reason, result.underlying_status, result.lineage) == (R.UNDERLYING_UNAVAILABLE, ST.NO_SNAPSHOT, None)


def test_underlying_other_instrument_propagates_no_snapshot_or_equivalent() -> None:
    other = TefasFundCurrentMetricsQueryKey(instrument_id=uuid4(), provider_symbol="MAC")
    result = _resolve([_snap(_T1, _payload("Hisse Senedi Fonu"))], query=other)
    assert result.reason is R.UNDERLYING_UNAVAILABLE and result.underlying_status is not ST.SELECTED


def test_underlying_no_eligible_observation_propagates_without_reading_payload(monkeypatch) -> None:
    snap = _snap(_T1, _payload("Hisse Senedi Fonu"), amount="-1")  # invalid AUM -> ineligible observation
    result = _resolve([snap])
    assert (result.reason, result.underlying_status) == (R.UNDERLYING_UNAVAILABLE, ST.NO_ELIGIBLE_OBSERVATION)
    assert result.lineage is None


def test_underlying_no_eligible_observation_blocks_an_older_valid_category() -> None:
    older = _snap(_T1, _payload("Hisse Senedi Fonu"))
    newer = _snap(_T2, _payload("Değişken Fon"), amount="-1")
    result = _resolve([older, newer])
    assert result.underlying_status is ST.NO_ELIGIBLE_OBSERVATION and result.reason is R.UNDERLYING_UNAVAILABLE


def test_underlying_snapshot_conflict_propagates() -> None:
    a = _snap(_T1, _payload("Hisse Senedi Fonu"))
    b = _snap(_T1, _payload("Değişken Fon"))
    result = _resolve([a, b])
    assert type(result) is TefasFundCategoryUnavailable
    assert result.reason is R.UNDERLYING_UNAVAILABLE
    assert result.underlying_status in (ST.SNAPSHOT_CONFLICT, ST.OBSERVATION_CONFLICT)


@pytest.mark.parametrize("status", [s for s in ST if s is not ST.SELECTED])
def test_every_underlying_non_selected_status_is_preserved_exactly(monkeypatch, status) -> None:
    fake = MarketObservationResolutionResult(
        status=status, resolution_mode=CR, as_of=None, observation_type="TEFAS_FUND_CURRENT_METRICS",
        effective_date=None)
    _patch_resolver(monkeypatch, fake)
    result = _resolve([_snap(_T1, _payload("Hisse Senedi Fonu"))])
    assert (result.reason, result.underlying_status) == (R.UNDERLYING_UNAVAILABLE, status)


def test_resolver_output_is_a_union_of_the_two_result_types() -> None:
    for snaps in ([], [_snap(_T1, _payload("X"))]):
        assert type(_resolve(snaps)) in (TefasFundCategoryObservation, TefasFundCategoryUnavailable)


# --- W: category-only change alters the authoritative lineage -----------------------------------------------------------

def test_category_only_payload_change_changes_lineage_and_resolution_key() -> None:
    a = _snap(_T1, _payload("Hisse Senedi Fonu"))
    b = _snap(_T1, _payload("Değişken Fon"))
    ra, rb = _resolve([a]), _resolve([b])
    assert ra.category_label != rb.category_label
    assert ra.lineage.snapshot_hash != rb.lineage.snapshot_hash
    assert ra.lineage.metrics_resolution_key != rb.lineage.metrics_resolution_key
    # the normalized current-metrics observation intentionally excludes the category
    assert a.observation.portfolio_size == b.observation.portfolio_size


def test_equivalent_payloads_give_uuid_independent_lineage_keys() -> None:
    payload = _payload("Hisse Senedi Fonu")
    x, y = _snap(_T1, payload), _snap(_T1, payload)
    assert x.id != y.id
    rx, ry = _resolve([x]), _resolve([y])
    assert rx.category_label == ry.category_label
    assert rx.lineage.snapshot_hash == ry.lineage.snapshot_hash
    assert rx.lineage.metrics_resolution_key == ry.lineage.metrics_resolution_key


# --- X: InstrumentType is not category authority ---------------------------------------------------------------------------

@pytest.mark.parametrize("instrument_type", [InstrumentType.TEFAS_FUND, InstrumentType.TEFAS_EQUITY,
                                             InstrumentType.TEFAS_MONEY_MARKET, InstrumentType.TEFAS_VARIABLE,
                                             InstrumentType.TEFAS_BALANCED])
def test_instrument_type_does_not_determine_the_label(instrument_type) -> None:
    assert _label("Değişken Fon", instrument_type=instrument_type) == "Değişken Fon"
    assert _label("Para Piyasası Fonu", instrument_type=instrument_type) == "Para Piyasası Fonu"
    assert _unavailable(_payload(), instrument_type=instrument_type).reason is R.CATEGORY_MISSING


def test_module_has_no_instrument_type_mapping() -> None:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "InstrumentType" not in used
    assert "backend.engine.private.domain" not in _imports()


# --- resolver input validation ----------------------------------------------------------------------------------------------

def test_resolver_arguments_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        resolve_tefas_fund_category(_QUERY, [], CR, None)  # type: ignore[misc]


@pytest.mark.parametrize("bad", [None, "x", object(), _Hostile(), (_INST,)])
def test_resolver_rejects_invalid_query_key(bad) -> None:
    with pytest.raises(TypeError):
        _resolve([], query=bad)


@pytest.mark.parametrize("bad", ["CURRENT_REPORTED", None, 1, _Hostile()])
def test_resolver_rejects_invalid_mode(bad) -> None:
    with pytest.raises(TypeError):
        _resolve([], mode=bad)


@pytest.mark.parametrize("bad", ["abc", b"abc", None, 1, [object()], [None]])
def test_resolver_rejects_invalid_snapshots(bad) -> None:
    with pytest.raises(TypeError):
        _resolve(bad)


# --- purity / scope -----------------------------------------------------------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_imports_are_pure_and_do_not_reimplement_hashing() -> None:
    imports = _imports()
    for banned in ("hashlib", "hmac", "pandas", "numpy", "scipy", "requests", "httpx", "os", "pathlib", "random",
                   "secrets", "socket", "sqlite3", "re", "unicodedata", "locale"):
        assert banned not in imports
    assert "backend.engine.private.storage_models" in imports
    assert "backend.engine.private.market_data.resolver" in imports


def test_no_normalization_or_peer_surface() -> None:
    source = Path(module_under_test.__file__).read_text(encoding="utf-8")
    for token in (".lower(", ".upper(", ".casefold(", "unicodedata", "translate(", "normalize("):
        assert token not in source
    for name in ("peer_group", "percentile", "rank", "score", "persistence", "category_average",
                 "normalize_category", "map_category"):
        assert not hasattr(module_under_test, name)


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_the_required_private_imports() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/fund_category.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert sg.scan_g1(source, rel) == []
    assert sg.scan_g2(source, rel) == []
    assert sg.scan_g4(source, rel) == []
    assert sg.scan_g5(source, rel) == []
    assert {v.node_kind for v in sg.scan_g3(source, rel)} == {
        "PrivateImport:backend.engine.private.market_data.models",
        "PrivateImport:backend.engine.private.market_data.resolver",
        "PrivateImport:backend.engine.private.market_data.tefas_metrics_models",
        "PrivateImport:backend.engine.private.storage_models",
    }
    assert rel not in sg.PURE_MANIFEST
