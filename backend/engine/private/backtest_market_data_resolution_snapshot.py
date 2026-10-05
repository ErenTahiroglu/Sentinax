"""
backend/engine/private/backtest_market_data_resolution_snapshot.py
==================================================================
Immutable historical market-data resolution snapshot for the Phase 26 backtest architecture (Phase 26C2C1). A NON-PURE adapter by classification: it calls the
closed point-in-time market-data resolver, so it is deliberately not in the PURE manifest. It stays deterministic and side-effect free: no I/O, clock,
randomness or hash.

The closed `PointInTimeMarketDataResolver` remains the only selection authority. Its `MarketObservationResolutionResult` and the source snapshot models are mutable
legacy objects, so a typed builder invokes the resolver with the replay context's exact historical mode and `as_of`, immediately converts the resolver's
audit-grade `to_dict()` into canonical JSON text and keeps nothing else: no result object, no source snapshot, no selected observation. The stored text is an
immutable evidence snapshot, not a new economic identity; no digest of it is created and the resolver's own `resolution_key` stays the closed authority.

Only the replay context's SOURCE_AS_OF / SYSTEM_AS_OF perspective can reach the resolver; there is no fallback to another perspective of any kind. Every
resolver status, including unavailable, missing and conflict outcomes, is preserved as captured provenance. This module makes no completeness judgment.

Direct construction validates the evidence envelope only; it cannot prove that the caller ran the resolver. The typed builders are the authoritative path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

from backend.engine.private.backtest_market_data_bridge import PrivateBacktestMarketDataContext
from backend.engine.private.bist.models import BISTBulletinSnapshot
from backend.engine.private.market_data.global_models import GlobalEODSnapshot
from backend.engine.private.market_data.models import (
    BISTInstrumentQueryKey,
    GlobalEODQueryKey,
    MarketDataResolutionStatus,
    MarketObservationResolutionResult,
    PreciousMetalSemanticKey,
    TefasFundCurrentMetricsQueryKey,
    TefasFundPriceQueryKey,
)
from backend.engine.private.market_data.resolver import PointInTimeMarketDataResolver
from backend.engine.private.market_data.tefas_metrics_models import TefasFundMetricsSnapshot
from backend.engine.private.market_data.tefas_models import TefasFundPriceSnapshot
from backend.engine.private.precious_metals.models import PreciousMetalSnapshot

_ERR_CONTEXT = "market_context must be an exact PrivateBacktestMarketDataContext instance"
_ERR_KIND = "kind must be an exact PrivateBacktestMarketDataKind member"
_ERR_QUERY = "query_key must be the exact query-key class of the market-data kind"
_ERR_SNAPSHOTS = "snapshots must be an exact tuple of the exact snapshot class of the market-data kind"
_ERR_JSON_TYPE = "resolution_payload_json must be an exact str"
_ERR_PAYLOAD = "resolution_payload_json must be a JSON object carrying the closed resolution result key set"
_ERR_FRONTIER = "resolution payload contradicts the replay context perspective or as_of"
_ERR_STATUS = "resolution payload status is not a canonical MarketDataResolutionStatus"
_ERR_SHAPE = "resolution payload selected_observation / resolution_key shape is invalid"
_ERR_RESULT = "market-data resolver returned something other than a MarketObservationResolutionResult"
_ERR_RESULT_FRONTIER = "market-data resolver result contradicts the replay context perspective or as_of"
_ERR_NOT_NATIVE = "market-data resolver audit payload is not JSON-native"
_ERR_DATE = "market-data resolver result effective_date contradicts the query key"
_ERR_INSTRUMENT = "market-data resolver result canonical_instrument_id contradicts the query key"

# Exact top-level key set of the closed MarketObservationResolutionResult.to_dict().
_PAYLOAD_KEYS = frozenset((
    "status", "resolution_mode", "as_of", "observation_type", "effective_date", "selected_observation_id", "selected_observation", "snapshot_id",
    "snapshot_hash", "snapshot_retrieved_at", "provider", "originating_source", "canonical_instrument_id", "semantic_key", "confidence",
    "is_stale_discovery", "diagnostics", "evaluation_snapshot_ids", "resolution_key",
))


class PrivateBacktestMarketDataKind(Enum):
    BIST_EOD = "bist_eod"
    GLOBAL_EOD = "global_eod"
    TEFAS_FUND_PRICE = "tefas_fund_price"
    TEFAS_CURRENT_METRICS = "tefas_current_metrics"
    PRECIOUS_METAL = "precious_metal"


_QUERY_CLASS = {
    PrivateBacktestMarketDataKind.BIST_EOD: BISTInstrumentQueryKey,
    PrivateBacktestMarketDataKind.GLOBAL_EOD: GlobalEODQueryKey,
    PrivateBacktestMarketDataKind.TEFAS_FUND_PRICE: TefasFundPriceQueryKey,
    PrivateBacktestMarketDataKind.TEFAS_CURRENT_METRICS: TefasFundCurrentMetricsQueryKey,
    PrivateBacktestMarketDataKind.PRECIOUS_METAL: PreciousMetalSemanticKey,
}
_SNAPSHOT_CLASS = {
    PrivateBacktestMarketDataKind.BIST_EOD: BISTBulletinSnapshot,
    PrivateBacktestMarketDataKind.GLOBAL_EOD: GlobalEODSnapshot,
    PrivateBacktestMarketDataKind.TEFAS_FUND_PRICE: TefasFundPriceSnapshot,
    PrivateBacktestMarketDataKind.TEFAS_CURRENT_METRICS: TefasFundMetricsSnapshot,
    PrivateBacktestMarketDataKind.PRECIOUS_METAL: PreciousMetalSnapshot,
}


def _reject_float(_text: str) -> Any:
    raise ValueError(_ERR_PAYLOAD)


def _reject_constant(_text: str) -> Any:
    raise ValueError(_ERR_PAYLOAD)


def _parse(text: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(text, parse_float=_reject_float, parse_constant=_reject_constant)
    except ValueError as exc:
        raise ValueError(_ERR_PAYLOAD) from exc
    if type(parsed) is not dict or set(parsed) != _PAYLOAD_KEYS:
        raise ValueError(_ERR_PAYLOAD)
    return parsed


def _is_json_native(value: Any) -> bool:
    kind = type(value)
    if value is None or kind in (str, int, bool):
        return True
    if kind is list:
        return all(_is_json_native(item) for item in value)
    if kind is dict:
        return all(type(key) is str and _is_json_native(item) for key, item in value.items())
    return False


def _same_instant(left: Any, right: datetime) -> bool:
    return type(left) is datetime and left.utcoffset() is not None and right.utcoffset() is not None and left == right


@dataclass(frozen=True)
class PrivateBacktestMarketDataResolutionSnapshot:
    """Canonical JSON audit capture of one closed-resolver result under the replay context's historical perspective."""
    market_context: PrivateBacktestMarketDataContext
    kind: PrivateBacktestMarketDataKind
    query_key: Any
    resolution_payload_json: str

    def __post_init__(self) -> None:
        if type(self.market_context) is not PrivateBacktestMarketDataContext:
            raise TypeError(_ERR_CONTEXT)
        if type(self.kind) is not PrivateBacktestMarketDataKind:
            raise TypeError(_ERR_KIND)
        if type(self.query_key) is not _QUERY_CLASS[self.kind]:
            raise TypeError(_ERR_QUERY)
        if type(self.resolution_payload_json) is not str:
            raise TypeError(_ERR_JSON_TYPE)
        payload = _parse(self.resolution_payload_json)
        if payload["resolution_mode"] != self.market_context.resolution_mode.value:
            raise ValueError(_ERR_FRONTIER)
        as_of_text = payload["as_of"]
        if type(as_of_text) is not str:
            raise ValueError(_ERR_FRONTIER)
        try:
            as_of = datetime.fromisoformat(as_of_text)
        except ValueError as exc:
            raise ValueError(_ERR_FRONTIER) from exc
        if not _same_instant(as_of, self.market_context.as_of):
            raise ValueError(_ERR_FRONTIER)
        try:
            MarketDataResolutionStatus(payload["status"])
        except ValueError as exc:
            raise ValueError(_ERR_STATUS) from exc
        if payload["selected_observation"] is not None and type(payload["selected_observation"]) is not dict:
            raise ValueError(_ERR_SHAPE)
        if payload["resolution_key"] is not None and type(payload["resolution_key"]) is not str:
            raise ValueError(_ERR_SHAPE)

    def resolution_payload(self) -> dict:
        """A freshly parsed copy of the stored audit payload."""
        return json.loads(self.resolution_payload_json)

    @property
    def status(self) -> MarketDataResolutionStatus:
        return MarketDataResolutionStatus(self.resolution_payload()["status"])

    def selected_observation_payload(self) -> Optional[dict]:
        """A freshly parsed copy of the selected observation's audit payload, or None."""
        return self.resolution_payload()["selected_observation"]

    @property
    def resolution_key(self) -> Optional[str]:
        return self.resolution_payload()["resolution_key"]


def _bind(
    kind: PrivateBacktestMarketDataKind,
    market_context: PrivateBacktestMarketDataContext,
    query_key: Any,
    snapshots: Any,
    resolver_method_name: str,
    effective_date: Callable[[Any], Optional[str]],
    has_instrument: bool,
) -> PrivateBacktestMarketDataResolutionSnapshot:
    if type(market_context) is not PrivateBacktestMarketDataContext:
        raise TypeError(_ERR_CONTEXT)
    if type(query_key) is not _QUERY_CLASS[kind]:
        raise TypeError(_ERR_QUERY)
    if type(snapshots) is not tuple or any(type(item) is not _SNAPSHOT_CLASS[kind] for item in snapshots):
        raise TypeError(_ERR_SNAPSHOTS)

    mode = market_context.resolution_mode
    as_of = market_context.as_of
    resolution = getattr(PointInTimeMarketDataResolver, resolver_method_name)(query_key, list(snapshots), mode=mode, as_of=as_of)

    if type(resolution) is not MarketObservationResolutionResult:
        raise RuntimeError(_ERR_RESULT)
    if resolution.resolution_mode is not mode or not _same_instant(resolution.as_of, as_of):
        raise RuntimeError(_ERR_RESULT_FRONTIER)

    payload = resolution.to_dict()
    if not _is_json_native(payload):
        raise RuntimeError(_ERR_NOT_NATIVE)
    if payload["effective_date"] != effective_date(query_key):
        raise RuntimeError(_ERR_DATE)
    if has_instrument and payload["canonical_instrument_id"] != str(query_key.instrument_id):
        raise RuntimeError(_ERR_INSTRUMENT)

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return PrivateBacktestMarketDataResolutionSnapshot(market_context=market_context, kind=kind, query_key=query_key, resolution_payload_json=text)


def bind_private_backtest_bist_eod_resolution(
    *, market_context: PrivateBacktestMarketDataContext, query_key: BISTInstrumentQueryKey, snapshots: Tuple[BISTBulletinSnapshot, ...],
) -> PrivateBacktestMarketDataResolutionSnapshot:
    return _bind(PrivateBacktestMarketDataKind.BIST_EOD, market_context, query_key, snapshots, "resolve_bist_eod", lambda q: q.trade_date.isoformat(), True)


def bind_private_backtest_global_eod_resolution(
    *, market_context: PrivateBacktestMarketDataContext, query_key: GlobalEODQueryKey, snapshots: Tuple[GlobalEODSnapshot, ...],
) -> PrivateBacktestMarketDataResolutionSnapshot:
    return _bind(PrivateBacktestMarketDataKind.GLOBAL_EOD, market_context, query_key, snapshots, "resolve_global_eod", lambda q: q.trade_date.isoformat(), True)


def bind_private_backtest_tefas_fund_price_resolution(
    *, market_context: PrivateBacktestMarketDataContext, query_key: TefasFundPriceQueryKey, snapshots: Tuple[TefasFundPriceSnapshot, ...],
) -> PrivateBacktestMarketDataResolutionSnapshot:
    return _bind(PrivateBacktestMarketDataKind.TEFAS_FUND_PRICE, market_context, query_key, snapshots, "resolve_tefas_fund_price", lambda q: q.trade_date.isoformat(), True)


def bind_private_backtest_tefas_current_metrics_resolution(
    *, market_context: PrivateBacktestMarketDataContext, query_key: TefasFundCurrentMetricsQueryKey, snapshots: Tuple[TefasFundMetricsSnapshot, ...],
) -> PrivateBacktestMarketDataResolutionSnapshot:
    return _bind(PrivateBacktestMarketDataKind.TEFAS_CURRENT_METRICS, market_context, query_key, snapshots, "resolve_tefas_current_metrics", lambda q: None, True)


def bind_private_backtest_precious_metal_resolution(
    *, market_context: PrivateBacktestMarketDataContext, query_key: PreciousMetalSemanticKey, snapshots: Tuple[PreciousMetalSnapshot, ...],
) -> PrivateBacktestMarketDataResolutionSnapshot:
    return _bind(PrivateBacktestMarketDataKind.PRECIOUS_METAL, market_context, query_key, snapshots, "resolve_precious_metal", lambda q: q.effective_date.isoformat(), False)
