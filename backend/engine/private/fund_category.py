"""
backend/engine/private/fund_category.py
=======================================
PIT-safe TEFAS fund category authority for the Private Investment Decision Engine (Phase 16G).

Purpose:
    One authoritative boundary for the provider-reported TEFAS category label (`fonKategori`), so later
    peer-relative analytics never consume a raw category string directly.

Architectural Invariants:
    - No network, provider HTTP, database, filesystem, ambient clock, hashing implementation, or frontend. The
      existing current-metrics provider, resolver, and normalized observation (which intentionally excludes the
      category) are NOT modified; category authority is a SEPARATE derived layer over the selected raw snapshot.
    - Temporal authority is delegated entirely to
      `PointInTimeMarketDataResolver.resolve_tefas_current_metrics`: snapshot eligibility, conflicts,
      no-resurrection, the SYSTEM_AS_OF `retrieved_at <= as_of` cutoff, snapshot selection, and resolution identity
      are never re-implemented here. Any non-SELECTED underlying status is preserved exactly
      (`UNDERLYING_UNAVAILABLE`) without inspecting a payload; SOURCE_AS_OF stays unavailable (never downgraded).
    - The selected snapshot is recovered from the caller-supplied snapshots by exact id and must agree with the
      resolution on id, payload hash, retrieved-at, and instrument; otherwise `SELECTED_SNAPSHOT_UNAVAILABLE`
      (no other snapshot is ever substituted).
    - Before `fonKategori` is read, the existing `compute_payload_hash(raw_payload)` must equal both the snapshot's
      recorded hash and the resolution's snapshot hash (`PAYLOAD_INTEGRITY_FAILURE` otherwise), binding the label to
      the immutable raw payload.
    - The raw payload must be an exact `str` of JSON with no duplicate object keys, root dict, and `resultList` a
      list of exactly one dict row (`RAW_PAYLOAD_INVALID` otherwise). Only `row["fonKategori"]` is read: absent,
      None, or blank -> `CATEGORY_MISSING`; a non-str value -> `CATEGORY_INVALID`. No other field, fund name,
      code, or InstrumentType substitutes for it.
    - The canonical label is the raw string after outer `.strip()` ONLY: no case folding, accent removal, Turkish
      character mapping, synonym mapping, abbreviation, or taxonomy. InstrumentType is NOT category authority.
    - Missing remains unavailable: never "Unknown", "Other", an instrument type, or an empty string. A newer
      snapshot with a missing/invalid category never resurrects an older label.
    - `TefasFundCategoryLineage.observed_at` is the time Sentinax retrieved the selected snapshot. It is NOT the
      economic/legal effective date of the category, nor a TEFAS publication timestamp.
    - No peer groups, percentiles, ranks, persistence, category benchmarks, scores, or recommendations.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Union
from uuid import UUID

from backend.engine.private.market_data.models import (
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
    TefasFundCurrentMetricsQueryKey,
)
from backend.engine.private.market_data.resolver import PointInTimeMarketDataResolver
from backend.engine.private.market_data.tefas_metrics_models import TefasFundMetricsSnapshot
from backend.engine.private.storage_models import compute_payload_hash

_HEX_DIGITS = frozenset("0123456789abcdef")
_CATEGORY_KEY = "fonKategori"
_RESULT_LIST_KEY = "resultList"


class TefasFundCategoryUnavailableReason(Enum):
    UNDERLYING_UNAVAILABLE = "underlying_unavailable"
    SELECTED_SNAPSHOT_UNAVAILABLE = "selected_snapshot_unavailable"
    PAYLOAD_INTEGRITY_FAILURE = "payload_integrity_failure"
    RAW_PAYLOAD_INVALID = "raw_payload_invalid"
    CATEGORY_MISSING = "category_missing"
    CATEGORY_INVALID = "category_invalid"


def _is_lower_sha256_hex(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(c in _HEX_DIGITS for c in value)


def _is_aware_datetime(value: object) -> bool:
    if type(value) is not datetime or value.tzinfo is None:
        return False
    try:
        return type(value.utcoffset()) is timedelta
    except Exception:
        return False


@dataclass(frozen=True)
class TefasFundCategoryLineage:
    """Immutable lineage of the selected raw snapshot a category label was read from."""
    snapshot_id: UUID
    snapshot_hash: str
    observed_at: datetime
    metrics_resolution_key: str

    def __post_init__(self) -> None:
        if type(self.snapshot_id) is not UUID:
            raise TypeError("snapshot_id must be an exact UUID instance")
        if not _is_lower_sha256_hex(self.snapshot_hash):
            raise TypeError("snapshot_hash must be an exact lowercase 64-character SHA-256 hex string")
        if not _is_aware_datetime(self.observed_at):
            raise TypeError("observed_at must be an exact timezone-aware datetime")
        if not _is_lower_sha256_hex(self.metrics_resolution_key):
            raise TypeError("metrics_resolution_key must be an exact lowercase 64-character SHA-256 hex string")


def _validate_as_of(as_of: object) -> None:
    if as_of is not None and not _is_aware_datetime(as_of):
        raise TypeError("as_of must be None or an exact timezone-aware datetime")


@dataclass(frozen=True)
class TefasFundCategoryObservation:
    """Provider-reported category label read from the authoritative selected snapshot."""
    instrument_id: UUID
    category_label: str
    mode: MarketDataResolutionMode
    as_of: datetime | None
    lineage: TefasFundCategoryLineage

    def __post_init__(self) -> None:
        if type(self.instrument_id) is not UUID:
            raise TypeError("instrument_id must be an exact UUID instance")
        if type(self.category_label) is not str:
            raise TypeError("category_label must be an exact str")
        if len(self.category_label) == 0 or self.category_label != self.category_label.strip():
            raise ValueError("category_label must be non-empty and already stripped")
        if type(self.mode) is not MarketDataResolutionMode:
            raise TypeError("mode must be an exact MarketDataResolutionMode instance")
        _validate_as_of(self.as_of)
        if type(self.lineage) is not TefasFundCategoryLineage:
            raise TypeError("lineage must be an exact TefasFundCategoryLineage instance")

        if self.mode is MarketDataResolutionMode.CURRENT_REPORTED:
            if self.as_of is not None:
                raise ValueError("as_of must be None for CURRENT_REPORTED")
        elif self.mode is MarketDataResolutionMode.SYSTEM_AS_OF:
            if self.as_of is None:
                raise TypeError("as_of must be an exact timezone-aware datetime for SYSTEM_AS_OF")
            if not self.lineage.observed_at <= self.as_of:
                raise ValueError("a category snapshot observed after as_of cannot appear in a historical view")
        else:
            raise ValueError("a category observation cannot exist for SOURCE_AS_OF")


@dataclass(frozen=True)
class TefasFundCategoryUnavailable:
    """Explicit unavailability of a category label; never replaced by a generic label."""
    instrument_id: UUID
    mode: MarketDataResolutionMode
    as_of: datetime | None
    reason: TefasFundCategoryUnavailableReason
    underlying_status: MarketDataResolutionStatus
    lineage: TefasFundCategoryLineage | None

    def __post_init__(self) -> None:
        if type(self.instrument_id) is not UUID:
            raise TypeError("instrument_id must be an exact UUID instance")
        if type(self.mode) is not MarketDataResolutionMode:
            raise TypeError("mode must be an exact MarketDataResolutionMode instance")
        _validate_as_of(self.as_of)
        if type(self.reason) is not TefasFundCategoryUnavailableReason:
            raise TypeError("reason must be an exact TefasFundCategoryUnavailableReason instance")
        if type(self.underlying_status) is not MarketDataResolutionStatus:
            raise TypeError("underlying_status must be an exact MarketDataResolutionStatus instance")
        if self.lineage is not None and type(self.lineage) is not TefasFundCategoryLineage:
            raise TypeError("lineage must be None or an exact TefasFundCategoryLineage instance")

        selected = self.underlying_status is MarketDataResolutionStatus.SELECTED
        if self.reason is TefasFundCategoryUnavailableReason.UNDERLYING_UNAVAILABLE:
            if selected:
                raise ValueError("an unavailable underlying resolution cannot have a SELECTED status")
        elif not selected:
            raise ValueError("category-specific failures require a SELECTED underlying status")


TefasFundCategoryResolution = Union[TefasFundCategoryObservation, TefasFundCategoryUnavailable]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite constant")


def resolve_tefas_fund_category(
    *,
    query_key: TefasFundCurrentMetricsQueryKey,
    snapshots: Sequence[TefasFundMetricsSnapshot],
    mode: MarketDataResolutionMode,
    as_of: datetime | None,
) -> TefasFundCategoryResolution:
    """
    Resolve the provider-reported TEFAS category label through the authoritative current-metrics selection.
    """
    if type(query_key) is not TefasFundCurrentMetricsQueryKey:
        raise TypeError("query_key must be an exact TefasFundCurrentMetricsQueryKey instance")
    if type(mode) is not MarketDataResolutionMode:
        raise TypeError("mode must be an exact MarketDataResolutionMode instance")
    if (
        not isinstance(snapshots, Sequence)
        or isinstance(snapshots, (str, bytes, bytearray))
        or any(type(s) is not TefasFundMetricsSnapshot for s in snapshots)
    ):
        raise TypeError("snapshots must be a sequence of exact TefasFundMetricsSnapshot instances")
    _validate_as_of(as_of)
    if mode is MarketDataResolutionMode.CURRENT_REPORTED and as_of is not None:
        raise ValueError("as_of must be None for CURRENT_REPORTED")

    instrument_id = query_key.instrument_id
    reason_enum = TefasFundCategoryUnavailableReason

    def unavailable(reason, status, lineage=None) -> TefasFundCategoryUnavailable:
        return TefasFundCategoryUnavailable(
            instrument_id=instrument_id,
            mode=mode,
            as_of=as_of,
            reason=reason,
            underlying_status=status,
            lineage=lineage,
        )

    metrics = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(
        query_key, snapshots, mode=mode, as_of=as_of
    )
    if metrics.status is not MarketDataResolutionStatus.SELECTED:
        return unavailable(reason_enum.UNDERLYING_UNAVAILABLE, metrics.status)

    selected = MarketDataResolutionStatus.SELECTED
    snapshot_id = metrics.snapshot_id
    snapshot_hash = metrics.snapshot_hash
    retrieved_at = metrics.snapshot_retrieved_at
    resolution_key = metrics.resolution_key
    if (
        type(snapshot_id) is not UUID
        or not _is_lower_sha256_hex(snapshot_hash)
        or not _is_aware_datetime(retrieved_at)
        or not _is_lower_sha256_hex(resolution_key)
        or metrics.canonical_instrument_id != instrument_id
    ):
        return unavailable(reason_enum.SELECTED_SNAPSHOT_UNAVAILABLE, selected)

    matches = [s for s in snapshots if s.id == snapshot_id]
    if len(matches) != 1:
        return unavailable(reason_enum.SELECTED_SNAPSHOT_UNAVAILABLE, selected)
    snapshot = matches[0]
    if (
        snapshot.payload_hash != snapshot_hash
        or snapshot.retrieved_at != retrieved_at
        or snapshot.instrument_id != instrument_id
    ):
        return unavailable(reason_enum.SELECTED_SNAPSHOT_UNAVAILABLE, selected)

    lineage = TefasFundCategoryLineage(
        snapshot_id=snapshot_id,
        snapshot_hash=snapshot_hash,
        observed_at=retrieved_at,
        metrics_resolution_key=resolution_key,
    )

    raw_payload = snapshot.raw_payload
    try:
        computed_hash = compute_payload_hash(raw_payload)
    except Exception:
        return unavailable(reason_enum.PAYLOAD_INTEGRITY_FAILURE, selected, lineage)
    if computed_hash != snapshot.payload_hash or snapshot.payload_hash != snapshot_hash:
        return unavailable(reason_enum.PAYLOAD_INTEGRITY_FAILURE, selected, lineage)

    if type(raw_payload) is not str:
        return unavailable(reason_enum.RAW_PAYLOAD_INVALID, selected, lineage)
    try:
        document = json.loads(
            raw_payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        return unavailable(reason_enum.RAW_PAYLOAD_INVALID, selected, lineage)

    if type(document) is not dict:
        return unavailable(reason_enum.RAW_PAYLOAD_INVALID, selected, lineage)
    rows = document.get(_RESULT_LIST_KEY)
    if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
        return unavailable(reason_enum.RAW_PAYLOAD_INVALID, selected, lineage)
    row = rows[0]

    if _CATEGORY_KEY not in row or row[_CATEGORY_KEY] is None:
        return unavailable(reason_enum.CATEGORY_MISSING, selected, lineage)
    value = row[_CATEGORY_KEY]
    if type(value) is not str:
        return unavailable(reason_enum.CATEGORY_INVALID, selected, lineage)
    label = value.strip()
    if label == "":
        return unavailable(reason_enum.CATEGORY_MISSING, selected, lineage)

    return TefasFundCategoryObservation(
        instrument_id=instrument_id,
        category_label=label,
        mode=mode,
        as_of=as_of,
        lineage=lineage,
    )
