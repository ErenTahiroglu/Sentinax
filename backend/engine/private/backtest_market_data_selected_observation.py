"""
backend/engine/private/backtest_market_data_selected_observation.py
===================================================================
Strict typed reconstruction of the SELECTED observation stored in a Phase 26C2C1 market-data resolution snapshot (Phase 26C2C2). The immutable C2C1 canonical JSON remains the
authority. This module reconstructs a CANONICAL TYPED REPRESENTATIVE of that audit payload; it does not resurrect the original mutable object (identity is gone, and
`BISTEODObservation.to_dict()` even serializes `raw_provider_symbol or symbol`, so an original None cannot be told from a value equal to the symbol: the representative takes the
serialized value, a representational canonicalization and not an economic change).

The wrapper retains only the C2C1 snapshot. No mutable legacy observation is stored: `reconstruct()` returns a FRESH object every time, so mutating one can never reach the wrapper,
the C2C1 JSON or a later reconstruction. Direct construction runs the full reconstruction, so malformed selected JSON cannot be wrapped.

Admission is SELECTED only; every other resolver state fails closed (never None, never an empty observation). Each of the five C2C1 kinds has an exact key set (no missing, no extra
key: schema drift fails closed and demands an explicit checkpoint). Values are parsed by explicit parsers only (canonical UUID text, ISO date, timezone-aware ISO datetime, Decimal from
its exact finite string, exact int that is not bool, exact enum, exact list), every constructor field is supplied from stored evidence so no default factory can mint an id or a
timestamp, and the representative's `to_dict()` must equal the stored payload exactly (the primary proof; no normalization afterwards). It must also agree with the top-level
resolution result: selected id; instrument identity (with the query key and canonical instrument id; none is invented for the instrument-free precious-metal reference); snapshot id
and hash lineage; retrieval instant (same UTC instant, no tolerance, and an equivalent offset only when the stored payload round-trips unchanged); effective date (None for current
metrics); query semantics (the closed `PreciousMetalSemanticKey.matches`; the Global provider by the closed resolver's own query rule, `provider.strip().upper()`, and nothing else); provider; and
confidence. Confidence has two layers: the observation's own and the closed resolver's. For BIST and precious metal the resolver degrades HIGH to MEDIUM when the source snapshot is a
stale discovery, otherwise the two agree; the other kinds never degrade. C2C2 validates that stored relationship and never promotes, degrades or recomputes either value.

Top-level SELECTED also requires the closed resolver's final observation-eligibility conditions per surface (valid observation status and a finite price field; TEFAS price additionally positive
with a currency and an allowed TEFAS instrument type; TEFAS metrics a finite non-negative TRY portfolio size, an allowed type, optional non-negative units and count and an exact retrieval lineage),
validated without running the resolver. The resolution key is NOT recomputed, so this still does not prove the resolver was actually called: the claim is only that the immutable C2C1
SELECTED envelope holds a strictly typed representative whose stored semantics are compatible with a closed resolver SELECTED outcome.

No resolver replay, no source snapshots, no fallback of any kind and no valuation arithmetic. Which field is a portfolio valuation price (and TEFAS `reported_current_unit_price`,
which stays diagnostic) is deliberately NOT decided here: that policy belongs to a later portfolio valuation adapter. A precious-metal observation is a dimensioned market reference,
not automatically a portfolio instrument. Non-pure by dependency composition; no I/O, clock, randomness, UUID or hash generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Union
from uuid import UUID

from backend.engine.private.backtest_market_data_resolution_snapshot import PrivateBacktestMarketDataKind, PrivateBacktestMarketDataResolutionSnapshot
from backend.engine.private.bist.models import BISTEODObservation, BISTObservationStatus
from backend.engine.private.domain import AssetClass, Currency, DataConfidenceLevel, InstrumentType, SourceTier
from backend.engine.private.market_data.global_models import GlobalEODObservation, GlobalObservationStatus
from backend.engine.private.market_data.models import MarketDataResolutionStatus
from backend.engine.private.market_data.tefas_metrics_models import TefasFundCurrentMetricsObservation
from backend.engine.private.market_data.tefas_models import TefasFundPriceObservation, TefasObservationStatus
from backend.engine.private.precious_metals.constants import PreciousMetalMarket, PreciousMetalPriceType, PreciousMetalType, PreciousMetalUnit
from backend.engine.private.precious_metals.models import PreciousMetalMarketObservation, PreciousMetalObservationStatus

SelectedObservation = Union[
    BISTEODObservation, GlobalEODObservation, TefasFundPriceObservation, TefasFundCurrentMetricsObservation, PreciousMetalMarketObservation,
]

_ERR_SNAPSHOT = "resolution_snapshot must be an exact PrivateBacktestMarketDataResolutionSnapshot instance"
_ERR_NOT_SELECTED = "only a SELECTED market-data resolution can be reconstructed"
_ERR_SHAPE = "selected observation payload must be an exact dict with exactly the closed key set of its kind"
_ERR_TOP = "top-level SELECTED resolution fields are missing or malformed"
_ERR_VALUE = "selected observation payload holds a malformed value"
_ERR_ROUND_TRIP = "reconstructed observation does not round-trip to the stored selected observation payload exactly"
_ERR_CONSISTENCY = "reconstructed observation contradicts the top-level resolution result or the query key"
_ERR_INELIGIBLE = "reconstructed observation is not eligible for a SELECTED result under the closed resolver's final observation-eligibility conditions"


def _fail(message: str) -> ValueError:
    return ValueError(message)


# --- explicit strict parsers (JSON-native stored value -> typed constructor input) ---------------------------------------

def _str(value: object) -> str:
    if type(value) is not str:
        raise _fail(_ERR_VALUE)
    return value


def _uuid(value: object) -> UUID:
    text = _str(value)
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise _fail(_ERR_VALUE) from error
    if str(parsed) != text:
        raise _fail(_ERR_VALUE)
    return parsed


def _date(value: object) -> date:
    text = _str(value)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise _fail(_ERR_VALUE) from error
    if parsed.isoformat() != text:
        raise _fail(_ERR_VALUE)
    return parsed


def _datetime(value: object) -> datetime:
    text = _str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise _fail(_ERR_VALUE) from error
    if parsed.utcoffset() is None or parsed.isoformat() != text:
        raise _fail(_ERR_VALUE)
    return parsed


def _decimal(value: object) -> Decimal:
    if type(value) is not str:
        raise _fail(_ERR_VALUE)
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise _fail(_ERR_VALUE) from error
    if not parsed.is_finite() or str(parsed) != value:
        raise _fail(_ERR_VALUE)
    return parsed


def _int(value: object) -> int:
    if type(value) is not int:
        raise _fail(_ERR_VALUE)
    return value


def _none(value: object) -> None:
    if value is not None:
        raise _fail(_ERR_VALUE)
    return None


def _diagnostics(value: object) -> list:
    if type(value) is not list or not all(type(item) is str for item in value):
        raise _fail(_ERR_VALUE)
    return list(value)


def _optional(parser):
    def parse(value: object):
        return None if value is None else parser(value)
    return parse


def _enum(enum_class):
    def parse(value: object):
        text = _str(value)
        try:
            return enum_class(text)
        except ValueError as error:
            raise _fail(_ERR_VALUE) from error
    return parse


_OPT_DECIMAL, _OPT_STR, _OPT_UUID, _OPT_DATETIME = _optional(_decimal), _optional(_str), _optional(_uuid), _optional(_datetime)
_OPT_INT, _OPT_DATE = _optional(_int), _optional(_date)
_CONFIDENCE, _CURRENCY_OPT, _INSTRUMENT_TYPE_OPT = _enum(DataConfidenceLevel), _optional(_enum(Currency)), _optional(_enum(InstrumentType))

# Exact selected-observation key sets: the closed `to_dict()` contract of each observation class, in constructor-keyword form.
_SPECS = {
    PrivateBacktestMarketDataKind.BIST_EOD: (BISTEODObservation, (
        ("id", _uuid), ("symbol", _str), ("raw_provider_symbol", _str), ("trade_date", _date), ("open", _OPT_DECIMAL), ("high", _OPT_DECIMAL), ("low", _OPT_DECIMAL),
        ("close", _OPT_DECIMAL), ("previous_close", _OPT_DECIMAL), ("weighted_average", _OPT_DECIMAL), ("volume", _OPT_DECIMAL), ("turnover", _OPT_DECIMAL),
        ("trade_count", _OPT_INT), ("currency", _enum(Currency)), ("market_segment", _OPT_STR), ("instrument_name", _OPT_STR), ("instrument_id", _OPT_UUID),
        ("asset_class", _optional(_enum(AssetClass))), ("instrument_type", _INSTRUMENT_TYPE_OPT), ("status", _enum(BISTObservationStatus)), ("source_provider", _str),
        ("snapshot_id", _OPT_UUID), ("snapshot_hash", _OPT_STR), ("retrieved_at", _OPT_DATETIME), ("source_as_of", _OPT_DATETIME), ("confidence_level", _CONFIDENCE),
        ("source_tier", _enum(SourceTier)), ("diagnostics", _diagnostics))),
    PrivateBacktestMarketDataKind.GLOBAL_EOD: (GlobalEODObservation, (
        ("id", _uuid), ("instrument_id", _OPT_UUID), ("provider_symbol", _str), ("exchange", _OPT_STR), ("trade_date", _date), ("open", _OPT_DECIMAL), ("high", _OPT_DECIMAL),
        ("low", _OPT_DECIMAL), ("close", _OPT_DECIMAL), ("volume", _OPT_DECIMAL), ("adj_open", _OPT_DECIMAL), ("adj_high", _OPT_DECIMAL), ("adj_low", _OPT_DECIMAL),
        ("adj_close", _OPT_DECIMAL), ("adj_volume", _OPT_DECIMAL), ("div_cash", _OPT_DECIMAL), ("split_factor", _OPT_DECIMAL), ("currency", _CURRENCY_OPT),
        ("instrument_type", _INSTRUMENT_TYPE_OPT), ("provider", _str), ("snapshot_id", _OPT_UUID), ("payload_hash", _OPT_STR), ("retrieved_at", _OPT_DATETIME),
        ("published_at", _OPT_DATETIME), ("status", _enum(GlobalObservationStatus)), ("confidence_level", _CONFIDENCE), ("diagnostics", _diagnostics))),
    PrivateBacktestMarketDataKind.TEFAS_FUND_PRICE: (TefasFundPriceObservation, (
        ("id", _uuid), ("instrument_id", _OPT_UUID), ("provider_symbol", _str), ("trade_date", _date), ("unit_price", _OPT_DECIMAL), ("currency", _CURRENCY_OPT),
        ("instrument_type", _INSTRUMENT_TYPE_OPT), ("provider", _str), ("snapshot_id", _OPT_UUID), ("payload_hash", _OPT_STR), ("retrieved_at", _OPT_DATETIME),
        ("published_at", _OPT_DATETIME), ("status", _enum(TefasObservationStatus)), ("confidence_level", _CONFIDENCE), ("diagnostics", _diagnostics))),
    PrivateBacktestMarketDataKind.TEFAS_CURRENT_METRICS: (TefasFundCurrentMetricsObservation, (
        ("id", _uuid), ("instrument_id", _OPT_UUID), ("provider_symbol", _str), ("portfolio_size", _OPT_DECIMAL), ("portfolio_size_currency", _CURRENCY_OPT),
        ("outstanding_units", _OPT_DECIMAL), ("investor_count", _OPT_INT), ("reported_current_unit_price", _OPT_DECIMAL), ("instrument_type", _INSTRUMENT_TYPE_OPT),
        ("provider", _str), ("snapshot_id", _OPT_UUID), ("payload_hash", _OPT_STR), ("retrieved_at", _OPT_DATETIME), ("published_at", _none), ("effective_date", _none),
        ("status", _enum(TefasObservationStatus)), ("confidence_level", _CONFIDENCE), ("diagnostics", _diagnostics))),
    PrivateBacktestMarketDataKind.PRECIOUS_METAL: (PreciousMetalMarketObservation, (
        ("id", _uuid), ("metal", _enum(PreciousMetalType)), ("market", _enum(PreciousMetalMarket)), ("effective_date", _date), ("price", _OPT_DECIMAL),
        ("price_currency", _enum(Currency)), ("price_quantity", _decimal), ("quantity_unit", _enum(PreciousMetalUnit)), ("price_type", _enum(PreciousMetalPriceType)),
        ("purity", _OPT_DECIMAL), ("raw_purity_value", _OPT_DECIMAL), ("raw_purity_text", _OPT_STR), ("purity_scale", _OPT_STR), ("fineness_per_mille", _OPT_DECIMAL),
        ("raw_value_date_text", _OPT_STR), ("value_date", _OPT_DATE), ("settlement_term", _OPT_STR), ("volume", _OPT_DECIMAL), ("turnover", _OPT_DECIMAL),
        ("trade_count", _OPT_INT), ("provider", _str), ("originating_source", _str), ("raw_symbol", _OPT_STR), ("snapshot_id", _OPT_UUID), ("payload_hash", _OPT_STR),
        ("retrieved_at", _datetime), ("published_at", _OPT_DATETIME), ("status", _enum(PreciousMetalObservationStatus)), ("confidence", _CONFIDENCE),
        ("diagnostics", _diagnostics))),
}

# Attribute names that carry each cross-layer field of the representative (the closed observation contracts differ per kind).
_HASH_ATTRIBUTE = {
    PrivateBacktestMarketDataKind.BIST_EOD: "snapshot_hash", PrivateBacktestMarketDataKind.GLOBAL_EOD: "payload_hash",
    PrivateBacktestMarketDataKind.TEFAS_FUND_PRICE: "payload_hash", PrivateBacktestMarketDataKind.TEFAS_CURRENT_METRICS: "payload_hash",
    PrivateBacktestMarketDataKind.PRECIOUS_METAL: "payload_hash",
}
_DATE_ATTRIBUTE = {
    PrivateBacktestMarketDataKind.BIST_EOD: "trade_date", PrivateBacktestMarketDataKind.GLOBAL_EOD: "trade_date",
    PrivateBacktestMarketDataKind.TEFAS_FUND_PRICE: "trade_date", PrivateBacktestMarketDataKind.TEFAS_CURRENT_METRICS: "effective_date",
    PrivateBacktestMarketDataKind.PRECIOUS_METAL: "effective_date",
}
_PROVIDER_ATTRIBUTE = {
    PrivateBacktestMarketDataKind.BIST_EOD: "source_provider", PrivateBacktestMarketDataKind.GLOBAL_EOD: "provider",
    PrivateBacktestMarketDataKind.TEFAS_FUND_PRICE: "provider", PrivateBacktestMarketDataKind.TEFAS_CURRENT_METRICS: "provider",
    PrivateBacktestMarketDataKind.PRECIOUS_METAL: "provider",
}
# The closed resolver's TEFAS_RESOLVER_ALLOWED_INSTRUMENT_TYPES, restated locally so this module never imports the resolver (a test asserts exact equality).
_TEFAS_ALLOWED_INSTRUMENT_TYPES = frozenset({
    InstrumentType.TEFAS_FUND, InstrumentType.TEFAS_EQUITY, InstrumentType.TEFAS_MONEY_MARKET, InstrumentType.TEFAS_VARIABLE, InstrumentType.TEFAS_BALANCED,
})
_ZERO = Decimal("0")
_STALE_DEGRADING_KINDS = frozenset({PrivateBacktestMarketDataKind.BIST_EOD, PrivateBacktestMarketDataKind.PRECIOUS_METAL})
_INSTRUMENT_KINDS = frozenset({
    PrivateBacktestMarketDataKind.BIST_EOD, PrivateBacktestMarketDataKind.GLOBAL_EOD, PrivateBacktestMarketDataKind.TEFAS_FUND_PRICE,
    PrivateBacktestMarketDataKind.TEFAS_CURRENT_METRICS,
})


def _check(condition: bool) -> None:
    if not condition:
        raise _fail(_ERR_CONSISTENCY)


def _finite(value: object) -> bool:
    return value is not None and value.is_finite()


def _check_eligibility(kind: PrivateBacktestMarketDataKind, representative: SelectedObservation, snapshot_retrieved_at: datetime) -> None:
    """Validate (never resolve) that the representative could satisfy the closed resolver's FINAL observation-eligibility conditions for a SELECTED result.

    A syntactically valid enum member is not enough: a top-level SELECTED over an INVALID_OBSERVATION or a missing price is an impossible state that the C2C1 envelope
    deliberately does not judge. Only presence, finiteness, sign and membership are checked; there is no arithmetic and no choice of a valuation field.
    """
    if kind is PrivateBacktestMarketDataKind.BIST_EOD:
        eligible = representative.status is BISTObservationStatus.VALID and _finite(representative.close)
    elif kind is PrivateBacktestMarketDataKind.GLOBAL_EOD:
        eligible = representative.status is GlobalObservationStatus.VALID and _finite(representative.close)
    elif kind is PrivateBacktestMarketDataKind.TEFAS_FUND_PRICE:
        eligible = (
            representative.status is TefasObservationStatus.VALID and _finite(representative.unit_price) and representative.unit_price > _ZERO
            and representative.currency is not None and representative.instrument_type in _TEFAS_ALLOWED_INSTRUMENT_TYPES
        )
    elif kind is PrivateBacktestMarketDataKind.TEFAS_CURRENT_METRICS:
        eligible = (
            representative.status is TefasObservationStatus.VALID
            and _finite(representative.portfolio_size) and representative.portfolio_size >= _ZERO
            and representative.portfolio_size_currency is Currency.TRY
            and representative.instrument_type in _TEFAS_ALLOWED_INSTRUMENT_TYPES
            and (representative.outstanding_units is None or (_finite(representative.outstanding_units) and representative.outstanding_units >= _ZERO))
            and (representative.investor_count is None or representative.investor_count >= 0)
            and representative.retrieved_at is not None and representative.retrieved_at == snapshot_retrieved_at
        )
    else:
        eligible = representative.status is PreciousMetalObservationStatus.VALID and _finite(representative.price)
    if not eligible:
        raise _fail(_ERR_INELIGIBLE)


def _reconstruct(snapshot: object) -> SelectedObservation:
    """The single reconstruction path: admission, strict parse, exact round trip, then cross-layer consistency. Returns a fresh object."""
    if type(snapshot) is not PrivateBacktestMarketDataResolutionSnapshot:
        raise TypeError(_ERR_SNAPSHOT)
    top = snapshot.resolution_payload()
    if top["status"] != MarketDataResolutionStatus.SELECTED.value:
        raise _fail(_ERR_NOT_SELECTED)
    stored = top["selected_observation"]
    kind = snapshot.kind
    observation_class, spec = _SPECS[kind]
    if type(stored) is not dict or set(stored) != {key for key, _parser in spec}:
        raise _fail(_ERR_SHAPE)
    if type(top["resolution_key"]) is not str or top["resolution_key"] == "":
        raise _fail(_ERR_TOP)
    selected_id, snapshot_id = _uuid(top["selected_observation_id"]), _uuid(top["snapshot_id"])
    snapshot_hash, retrieved_at = _str(top["snapshot_hash"]), _datetime(top["snapshot_retrieved_at"])
    if snapshot_hash == "":
        raise _fail(_ERR_TOP)

    representative = observation_class(**{key: parser(stored[key]) for key, parser in spec})
    if representative.to_dict() != stored:
        raise _fail(_ERR_ROUND_TRIP)

    _check_eligibility(kind, representative, retrieved_at)
    query = snapshot.query_key
    _check(representative.id == selected_id)
    _check(representative.snapshot_id == snapshot_id and getattr(representative, _HASH_ATTRIBUTE[kind]) == snapshot_hash)
    _check(representative.retrieved_at is None or representative.retrieved_at == retrieved_at)
    effective = getattr(representative, _DATE_ATTRIBUTE[kind])
    if kind is PrivateBacktestMarketDataKind.TEFAS_CURRENT_METRICS:
        _check(effective is None and top["effective_date"] is None)
    else:
        _check(effective == query.effective_date if kind is PrivateBacktestMarketDataKind.PRECIOUS_METAL else effective == query.trade_date)
        _check(top["effective_date"] == effective.isoformat())
    if kind in _INSTRUMENT_KINDS:
        _check(representative.instrument_id == query.instrument_id and top["canonical_instrument_id"] == str(query.instrument_id))
    else:
        _check(top["canonical_instrument_id"] is None)
        _check(query.matches(representative))
        _check(top["originating_source"] == representative.originating_source)
    _check(top["provider"] == getattr(representative, _PROVIDER_ATTRIBUTE[kind]))
    if kind is PrivateBacktestMarketDataKind.GLOBAL_EOD:
        _check(top["provider"] == query.provider.strip().upper())
    # Two distinct confidence layers: the observation's own and the closed resolver's. Only BIST and precious metal can degrade (HIGH to MEDIUM) for a stale-discovery
    # snapshot; the other kinds never do. This validates the stored relationship; it never promotes, degrades or rewrites either value.
    confidence = representative.confidence if kind is PrivateBacktestMarketDataKind.PRECIOUS_METAL else representative.confidence_level
    stale = top["is_stale_discovery"]
    if type(stale) is not bool:
        raise _fail(_ERR_TOP)
    if kind in _STALE_DEGRADING_KINDS:
        expected = DataConfidenceLevel.MEDIUM if stale and confidence is DataConfidenceLevel.HIGH else confidence
    else:
        _check(stale is False)
        expected = confidence
    _check(top["confidence"] == expected.value)
    return representative


@dataclass(frozen=True)
class PrivateBacktestMarketDataSelectedObservation:
    """The C2C1 snapshot whose SELECTED observation payload has been proven strictly reconstructable; nothing derived is stored."""
    resolution_snapshot: PrivateBacktestMarketDataResolutionSnapshot

    def __post_init__(self) -> None:
        _reconstruct(self.resolution_snapshot)

    def reconstruct(self) -> SelectedObservation:
        """A FRESH canonical typed representative of the stored selected-observation audit payload (never the original object, never a stored instance)."""
        return _reconstruct(self.resolution_snapshot)


def reconstruct_private_backtest_selected_observation(
    *, resolution_snapshot: PrivateBacktestMarketDataResolutionSnapshot,
) -> PrivateBacktestMarketDataSelectedObservation:
    """Wrap a SELECTED C2C1 snapshot after proving strict typed reconstruction, exact round trip and top-level consistency."""
    return PrivateBacktestMarketDataSelectedObservation(resolution_snapshot=resolution_snapshot)
