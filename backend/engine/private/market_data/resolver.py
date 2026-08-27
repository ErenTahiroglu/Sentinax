"""
backend/engine/private/market_data/resolver.py
==============================================
Point-in-Time Market Data Observation Resolver for BIST Equity, ALTIN.S1, and BIST KMTP Precious Metals.

Core Architectural Invariants:
    - Pure functional and deterministic: Zero network, zero datetime.now() / date.today() in authority logic.
    - Full snapshot semantics: Latest eligible successful full snapshot supersedes previous snapshots.
    - Zero resurrection: Missing/invalid row in authoritative snapshot is never backfilled from older snapshot.
    - SYSTEM_AS_OF: Filters snapshot.retrieved_at <= as_of BEFORE conflict checks and observation evaluation.
    - Future isolation: Future snapshot conflicts or corruptions never contaminate historical SYSTEM_AS_OF resolution.
    - Successful snapshots only: HTTP transport failures (e.g. 500) never supersede valid historical snapshots.
    - Logical snapshot deduplication: Same (trade_date, retrieved_at, payload_hash) deduplicates deterministically.
    - Logical observation fingerprinting: Multiple matches with differing attributes fail closed as OBSERVATION_CONFLICT even if prices match.
    - UUID-independent resolution_key: Economic inputs and logical fingerprints determine resolution authority.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from backend.engine.private.bist.models import (
    BISTBulletinSnapshot,
    BISTEODObservation,
    BISTObservationStatus,
)
from backend.engine.private.domain import Currency, DataConfidenceLevel, InstrumentType
from backend.engine.private.market_data.global_models import (
    GlobalEODObservation,
    GlobalEODSnapshot,
    GlobalObservationStatus,
)
from backend.engine.private.market_data.models import (
    BISTInstrumentQueryKey,
    GlobalEODQueryKey,
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
    MarketObservationResolutionResult,
    PreciousMetalSemanticKey,
    TefasFundCurrentMetricsQueryKey,
    TefasFundPriceQueryKey,
)
from backend.engine.private.market_data.tefas_models import (
    TefasFundPriceObservation,
    TefasFundPriceSnapshot,
    TefasObservationStatus,
)
from backend.engine.private.market_data.tefas_metrics_models import (
    TefasFundCurrentMetricsObservation,
    TefasFundMetricsSnapshot,
)
from backend.engine.private.precious_metals.models import (
    PreciousMetalMarketObservation,
    PreciousMetalObservationStatus,
    PreciousMetalSnapshot,
)

TEFAS_PROVIDER_NAME: str = "TEFAS"

TEFAS_RESOLVER_ALLOWED_INSTRUMENT_TYPES: Set[InstrumentType] = {
    InstrumentType.TEFAS_FUND,
    InstrumentType.TEFAS_EQUITY,
    InstrumentType.TEFAS_MONEY_MARKET,
    InstrumentType.TEFAS_VARIABLE,
    InstrumentType.TEFAS_BALANCED,
}


def _tefas_observation_fingerprint(obs: TefasFundPriceObservation) -> str:
    """Computes a deterministic, UUID-independent economic fingerprint of a TEFAS fund price observation."""
    return (
        f"{obs.instrument_id}:{obs.provider}:{obs.provider_symbol.strip().upper() if obs.provider_symbol else 'None'}:"
        f"{obs.trade_date.isoformat()}:{obs.unit_price}:{obs.currency.value if obs.currency else 'None'}:"
        f"{obs.instrument_type.value if obs.instrument_type else 'None'}:{obs.status.value}"
    )


def _tefas_current_metrics_observation_fingerprint(obs: TefasFundCurrentMetricsObservation) -> str:
    """Computes a deterministic, UUID-independent economic fingerprint of a TEFAS fund current metrics observation."""
    inst_str = str(obs.instrument_id) if obs.instrument_id else "None"
    prov_str = (obs.provider or "").strip().upper()
    sym_str = (obs.provider_symbol or "").strip().upper()
    psize_str = str(obs.portfolio_size) if obs.portfolio_size is not None else "None"
    curr_str = obs.portfolio_size_currency.value if obs.portfolio_size_currency else "None"
    units_str = str(obs.outstanding_units) if obs.outstanding_units is not None else "None"
    inv_str = str(obs.investor_count) if obs.investor_count is not None else "None"
    price_str = str(obs.reported_current_unit_price) if obs.reported_current_unit_price is not None else "None"
    type_str = obs.instrument_type.value if obs.instrument_type else "None"
    status_str = obs.status.value if hasattr(obs.status, "value") else str(obs.status)

    return f"{inst_str}:{prov_str}:{sym_str}:{psize_str}:{curr_str}:{units_str}:{inv_str}:{price_str}:{type_str}:{status_str}"


def _tefas_snapshot_covers_target_date(snap: TefasFundPriceSnapshot, target: date) -> bool:
    """
    Determines whether a TEFAS snapshot has authority over target trade_date.
    Coverage is established ONLY through:
    A) two-sided snapshot.trade_date_range: range_start <= target <= range_end
    OR
    B) snapshot contains an observation with trade_date == target.

    Do NOT derive target range from period_months, retrieved_at - 60 months, or calendar arithmetic.
    """
    r_start, r_end = snap.trade_date_range
    if r_start is not None and r_end is not None:
        if r_start <= target <= r_end:
            return True

    if any(o.trade_date == target for o in snap.observations):
        return True

    return False


def _global_observation_fingerprint(obs: GlobalEODObservation) -> str:
    """Computes a deterministic, UUID-independent fingerprint of a Global EOD observation."""
    return (
        f"{obs.instrument_id}:{obs.provider}:{obs.provider_symbol}:{obs.trade_date.isoformat()}:"
        f"{obs.currency.value if obs.currency else 'None'}:{obs.exchange}:{obs.instrument_type.value if obs.instrument_type else 'None'}:"
        f"{obs.open}:{obs.high}:{obs.low}:{obs.close}:{obs.volume}:"
        f"{obs.adj_open}:{obs.adj_high}:{obs.adj_low}:{obs.adj_close}:{obs.adj_volume}:"
        f"{obs.div_cash}:{obs.split_factor}:{obs.status.value}"
    )


def _snapshot_covers_target_date(snap: GlobalEODSnapshot, target: date) -> bool:
    """
    Determines whether a per-instrument snapshot has authority over target trade_date.
    Snapshot covers target ONLY if ONE of:
    A) start_date AND end_date both exist: start_date <= target <= end_date
    OR
    B) trade_date_range start AND end both exist: range_start <= target <= range_end
    OR
    C) Snapshot contains an observation with: trade_date == target.

    One-sided request/range boundary ALONE is insufficient evidence for authority.
    """
    # A) Explicit requested two-sided bounds
    if snap.start_date is not None and snap.end_date is not None:
        if snap.start_date <= target <= snap.end_date:
            return True

    # B) trade_date_range two-sided boundaries
    r_start, r_end = snap.trade_date_range
    if r_start is not None and r_end is not None:
        if r_start <= target <= r_end:
            return True

    # C) Contains exact observation matching target
    if any(o.trade_date == target for o in snap.observations):
        return True

    return False


def _bist_observation_fingerprint(obs: BISTEODObservation) -> str:
    """Computes a deterministic, UUID-independent fingerprint of a BIST EOD observation."""
    return (
        f"{obs.instrument_id}:{obs.trade_date.isoformat()}:{obs.symbol}:{obs.raw_provider_symbol}:"
        f"{obs.open}:{obs.high}:{obs.low}:{obs.close}:{obs.previous_close}:{obs.weighted_average}:"
        f"{obs.volume}:{obs.turnover}:{obs.trade_count}:{obs.currency.value if obs.currency else 'None'}:"
        f"{obs.market_segment}:{obs.instrument_type.value if obs.instrument_type else 'None'}:"
        f"{obs.status.value}:{obs.snapshot_hash}"
    )


def _precious_observation_fingerprint(obs: PreciousMetalMarketObservation) -> str:
    """Computes a deterministic, UUID-independent fingerprint of a Precious Metal observation."""
    return (
        f"{obs.metal.value}:{obs.effective_date.isoformat()}:{obs.price}:{obs.price_currency.value}:"
        f"{obs.quantity_unit.value}:{obs.price_type.value}:{obs.price_quantity}:{obs.fineness_per_mille}:"
        f"{obs.settlement_term}:{obs.value_date.isoformat() if obs.value_date else 'None'}:"
        f"{obs.raw_value_date_text}:{obs.market.value if obs.market else 'None'}:{obs.provider}:"
        f"{obs.originating_source}:{obs.status.value}:{obs.payload_hash}"
    )


class PointInTimeMarketDataResolver:
    """
    Institutional Point-in-Time Market Observation Resolver.
    """

    @classmethod
    def resolve_bist_eod(
        cls,
        query_key: BISTInstrumentQueryKey,
        snapshots: List[BISTBulletinSnapshot],
        mode: MarketDataResolutionMode = MarketDataResolutionMode.CURRENT_REPORTED,
        as_of: Optional[datetime] = None,
    ) -> MarketObservationResolutionResult:
        """
        Resolves the canonical BIST EOD observation for an instrument_id on a trade_date.
        Handles equities, funds, and commodity certificates (ALTIN.S1) deterministically.
        """
        obs_type = "BIST_EOD_PRICE"
        target_date = query_key.trade_date
        inst_id = query_key.instrument_id

        # 1. Mode Validation
        if mode == MarketDataResolutionMode.SOURCE_AS_OF:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.UNAVAILABLE_SOURCE_AS_OF,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                canonical_instrument_id=inst_id,
                diagnostics=["SOURCE_AS_OF unavailable: Exchange public bulletin feed does not provide exact historical first-publication timestamps."],
            )

        if mode == MarketDataResolutionMode.SYSTEM_AS_OF:
            if as_of is None or as_of.tzinfo is None:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    diagnostics=["SYSTEM_AS_OF resolution requires a timezone-aware as_of timestamp."],
                )

        # 2. Filter Snapshots by Target Trade Date
        date_snaps = [s for s in snapshots if s.trade_date == target_date]
        if not date_snaps:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_SNAPSHOT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                canonical_instrument_id=inst_id,
                diagnostics=[f"No BIST snapshot found for trade_date {target_date.isoformat()}."],
            )

        # 3. Validate Snapshot retrieved_at Timezone Awareness
        for s in date_snaps:
            if s.retrieved_at is None or s.retrieved_at.tzinfo is None:
                eval_ids = sorted([str(snap.id) for snap in date_snaps])
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"Snapshot {s.id} contains naive or missing retrieved_at timestamp."],
                )

        # 4. Retain Successful Full Snapshots Only (HTTP 200 with non-empty payload_hash)
        successful_snaps = [s for s in date_snaps if s.http_status == 200 and s.payload_hash]
        if not successful_snaps:
            eval_ids = sorted([str(s.id) for s in date_snaps])
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.INVALID_SNAPSHOT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                canonical_instrument_id=inst_id,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=["No successful (HTTP 200) full BIST snapshot available for trade date."],
            )

        # 5. SYSTEM_AS_OF Temporal Filtering BEFORE Conflict & Deduplication
        if mode == MarketDataResolutionMode.SYSTEM_AS_OF:
            assert as_of is not None
            eligible_snaps = [s for s in successful_snaps if s.retrieved_at <= as_of]
            if not eligible_snaps:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.NO_SNAPSHOT_AS_OF,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    evaluation_snapshot_ids=[],
                    diagnostics=[f"NO_SNAPSHOT_AS_OF: No BIST snapshot retrieved at or before as_of {as_of.isoformat()}."],
                )
        else:
            eligible_snaps = successful_snaps

        eval_ids = sorted([str(s.id) for s in eligible_snaps])

        # 6. Detect Snapshot Conflicts on ELIGIBLE Snapshots Only
        time_hash_map: Dict[datetime, set] = {}
        for s in eligible_snaps:
            time_hash_map.setdefault(s.retrieved_at, set()).add(s.payload_hash)

        for ret_time, hashes in time_hash_map.items():
            if len(hashes) > 1:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.SNAPSHOT_CONFLICT,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"SNAPSHOT_CONFLICT: Multiple differing payload hashes at identical retrieved_at {ret_time.isoformat()}."],
                )

        # 7. Deduplicate Logical Snapshots: (trade_date, retrieved_at, payload_hash)
        # Select deterministic representative (min UUID string) for each logical snapshot group
        grouped_snaps: Dict[Tuple[Any, datetime, str], List[BISTBulletinSnapshot]] = {}
        for s in eligible_snaps:
            key = (s.trade_date, s.retrieved_at, s.payload_hash)
            grouped_snaps.setdefault(key, []).append(s)

        unique_snaps: List[BISTBulletinSnapshot] = [
            min(group, key=lambda snap: str(snap.id)) for group in grouped_snaps.values()
        ]

        # 8. Select Authoritative Snapshot (Latest aware retrieved_at in eligible set)
        unique_snaps.sort(key=lambda s: (s.retrieved_at, str(s.id)))
        auth_snap = unique_snaps[-1]

        # 9. Validate Observation Lineage within Authoritative Snapshot
        for obs in auth_snap.observations:
            if obs.snapshot_id is not None and obs.snapshot_id != auth_snap.id:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation {obs.id} snapshot_id {obs.snapshot_id} mismatches containing snapshot {auth_snap.id}."],
                )
            if obs.snapshot_hash is not None and obs.snapshot_hash != auth_snap.payload_hash:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation {obs.id} snapshot_hash {obs.snapshot_hash} mismatches containing snapshot payload_hash {auth_snap.payload_hash}."],
                )
            if obs.trade_date != auth_snap.trade_date:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation trade_date {obs.trade_date} mismatches containing snapshot trade_date {auth_snap.trade_date}."],
                )

        # 10. Match Observations for Target Instrument ID
        valid_matches: List[BISTEODObservation] = []
        unresolved_matches: List[BISTEODObservation] = []

        for obs in auth_snap.observations:
            if obs.instrument_id == inst_id:
                if obs.status == BISTObservationStatus.VALID and obs.close is not None and obs.close.is_finite():
                    valid_matches.append(obs)
                elif obs.status == BISTObservationStatus.UNRESOLVED_IDENTITY:
                    unresolved_matches.append(obs)
            elif query_key.symbol and (obs.symbol == query_key.symbol or obs.raw_provider_symbol == query_key.symbol):
                if obs.status == BISTObservationStatus.UNRESOLVED_IDENTITY or obs.instrument_id is None:
                    unresolved_matches.append(obs)

        if not valid_matches:
            status = MarketDataResolutionStatus.UNRESOLVED_IDENTITY if unresolved_matches else MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION
            diag = ["Target instrument identity was unresolved in authoritative snapshot."] if unresolved_matches else ["Target instrument not found or not valid in authoritative snapshot."]
            return MarketObservationResolutionResult(
                status=status,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                provider="BIST_EOD",
                originating_source="BIST",
                canonical_instrument_id=inst_id,
                is_stale_discovery=auth_snap.is_stale_discovery,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=diag,
            )

        # 11. Logical Observation Fingerprint Conflict / Deduplication
        fingerprints = {_bist_observation_fingerprint(o) for o in valid_matches}
        if len(fingerprints) > 1:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.OBSERVATION_CONFLICT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                provider="BIST_EOD",
                originating_source="BIST",
                canonical_instrument_id=inst_id,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"OBSERVATION_CONFLICT: Multiple valid observations with differing fingerprints for instrument {inst_id}."],
            )

        # Identical logical fingerprints -> exact duplicates, choose deterministic representative
        selected_obs = min(valid_matches, key=lambda o: str(o.id))
        obs_fp = _bist_observation_fingerprint(selected_obs)

        # 12. Compute Deterministic, UUID-Independent Resolution Key & Confidence
        res_key_raw = f"{mode.value}:{as_of.isoformat() if as_of else 'NONE'}:BIST_EOD:{target_date.isoformat()}:{inst_id}:{auth_snap.payload_hash}:{obs_fp}"
        resolution_key = hashlib.sha256(res_key_raw.encode("utf-8")).hexdigest()

        confidence = selected_obs.confidence_level
        diags = list(selected_obs.diagnostics)
        if auth_snap.is_stale_discovery:
            diags.append("DEGRADED_DISCOVERY: Snapshot resolved from stale cached directory manifest.")
            if confidence == DataConfidenceLevel.HIGH:
                confidence = DataConfidenceLevel.MEDIUM

        return MarketObservationResolutionResult(
            status=MarketDataResolutionStatus.SELECTED,
            resolution_mode=mode,
            as_of=as_of,
            observation_type=obs_type,
            effective_date=target_date,
            selected_observation=selected_obs,
            selected_observation_id=selected_obs.id,
            snapshot_id=auth_snap.id,
            snapshot_hash=auth_snap.payload_hash,
            snapshot_retrieved_at=auth_snap.retrieved_at,
            provider="BIST_EOD",
            originating_source="BIST",
            canonical_instrument_id=inst_id,
            confidence=confidence,
            is_stale_discovery=auth_snap.is_stale_discovery,
            diagnostics=diags,
            evaluation_snapshot_ids=eval_ids,
            resolution_key=resolution_key,
        )

    @classmethod
    def resolve_precious_metal(
        cls,
        semantic_key: PreciousMetalSemanticKey,
        snapshots: List[PreciousMetalSnapshot],
        mode: MarketDataResolutionMode = MarketDataResolutionMode.CURRENT_REPORTED,
        as_of: Optional[datetime] = None,
    ) -> MarketObservationResolutionResult:
        """
        Resolves the canonical Precious Metal market reference observation matching semantic_key.
        """
        obs_type = "PRECIOUS_METAL_MARKET_REFERENCE"
        target_date = semantic_key.effective_date
        sem_str = semantic_key.to_string()

        # 1. Mode Validation
        if mode == MarketDataResolutionMode.SOURCE_AS_OF:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.UNAVAILABLE_SOURCE_AS_OF,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                semantic_key=sem_str,
                diagnostics=["SOURCE_AS_OF unavailable: Exchange public bulletin feed does not provide exact historical first-publication timestamps."],
            )

        if mode == MarketDataResolutionMode.SYSTEM_AS_OF:
            if as_of is None or as_of.tzinfo is None:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    semantic_key=sem_str,
                    diagnostics=["SYSTEM_AS_OF resolution requires a timezone-aware as_of timestamp."],
                )

        # 2. Filter Snapshots by Target Trade Date
        date_snaps = [s for s in snapshots if s.trade_date == target_date]
        if not date_snaps:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_SNAPSHOT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                semantic_key=sem_str,
                diagnostics=[f"No KMTP snapshot found for trade_date {target_date.isoformat()}."],
            )

        # 3. Validate Snapshot retrieved_at Timezone Awareness
        for s in date_snaps:
            if s.retrieved_at is None or s.retrieved_at.tzinfo is None:
                eval_ids = sorted([str(snap.id) for snap in date_snaps])
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"Snapshot {s.id} contains naive or missing retrieved_at timestamp."],
                )

        # 4. Retain Successful Full Snapshots Only (HTTP 200 with non-empty payload_hash)
        successful_snaps = [s for s in date_snaps if s.http_status == 200 and s.payload_hash]
        if not successful_snaps:
            eval_ids = sorted([str(s.id) for s in date_snaps])
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.INVALID_SNAPSHOT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=["No successful (HTTP 200) full KMTP snapshot available for trade date."],
            )

        # 5. SYSTEM_AS_OF Temporal Filtering BEFORE Conflict & Deduplication
        if mode == MarketDataResolutionMode.SYSTEM_AS_OF:
            assert as_of is not None
            eligible_snaps = [s for s in successful_snaps if s.retrieved_at <= as_of]
            if not eligible_snaps:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.NO_SNAPSHOT_AS_OF,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=[],
                    diagnostics=[f"NO_SNAPSHOT_AS_OF: No KMTP snapshot retrieved at or before as_of {as_of.isoformat()}."],
                )
        else:
            eligible_snaps = successful_snaps

        eval_ids = sorted([str(s.id) for s in eligible_snaps])

        # 6. Detect Snapshot Conflicts on ELIGIBLE Snapshots Only
        time_hash_map: Dict[datetime, set] = {}
        for s in eligible_snaps:
            time_hash_map.setdefault(s.retrieved_at, set()).add(s.payload_hash)

        for ret_time, hashes in time_hash_map.items():
            if len(hashes) > 1:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.SNAPSHOT_CONFLICT,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"SNAPSHOT_CONFLICT: Multiple differing payload hashes at identical retrieved_at {ret_time.isoformat()}."],
                )

        # 7. Deduplicate Logical Snapshots: (trade_date, retrieved_at, payload_hash)
        grouped_snaps: Dict[Tuple[Any, datetime, str], List[PreciousMetalSnapshot]] = {}
        for s in eligible_snaps:
            key = (s.trade_date, s.retrieved_at, s.payload_hash)
            grouped_snaps.setdefault(key, []).append(s)

        unique_snaps: List[PreciousMetalSnapshot] = [
            min(group, key=lambda snap: str(snap.id)) for group in grouped_snaps.values()
        ]

        # 8. Select Authoritative Snapshot (Latest aware retrieved_at in eligible set)
        unique_snaps.sort(key=lambda s: (s.retrieved_at, str(s.id)))
        auth_snap = unique_snaps[-1]

        # 9. Validate Observation Lineage within Authoritative Snapshot
        for obs in auth_snap.observations:
            if obs.snapshot_id is not None and obs.snapshot_id != auth_snap.id:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    semantic_key=sem_str,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation {obs.id} snapshot_id {obs.snapshot_id} mismatches containing snapshot {auth_snap.id}."],
                )
            if obs.payload_hash is not None and obs.payload_hash != auth_snap.payload_hash:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    semantic_key=sem_str,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation {obs.id} payload_hash {obs.payload_hash} mismatches containing snapshot payload_hash {auth_snap.payload_hash}."],
                )
            if obs.effective_date != auth_snap.trade_date:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    semantic_key=sem_str,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation effective_date {obs.effective_date} mismatches containing snapshot trade_date {auth_snap.trade_date}."],
                )

        # 10. Match Observations against Semantic Key
        valid_matches: List[PreciousMetalMarketObservation] = []
        for obs in auth_snap.observations:
            if semantic_key.matches(obs):
                if obs.status == PreciousMetalObservationStatus.VALID and obs.price is not None and obs.price.is_finite():
                    valid_matches.append(obs)

        if not valid_matches:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                provider="BIST_KMTP",
                originating_source="BIST",
                semantic_key=sem_str,
                is_stale_discovery=auth_snap.is_stale_discovery,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=["Target precious metal semantic key not found or not valid in authoritative snapshot."],
            )

        # 11. Logical Observation Fingerprint Conflict / Deduplication
        fingerprints = {_precious_observation_fingerprint(o) for o in valid_matches}
        if len(fingerprints) > 1:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.OBSERVATION_CONFLICT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                provider="BIST_KMTP",
                originating_source="BIST",
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"OBSERVATION_CONFLICT: Multiple valid observations with differing fingerprints for semantic key."],
            )

        # Identical logical fingerprints -> exact duplicates, choose deterministic representative
        selected_obs = min(valid_matches, key=lambda o: str(o.id))
        obs_fp = _precious_observation_fingerprint(selected_obs)

        # 12. Compute Deterministic, UUID-Independent Resolution Key & Confidence
        res_key_raw = f"{mode.value}:{as_of.isoformat() if as_of else 'NONE'}:BIST_KMTP:{target_date.isoformat()}:{sem_str}:{auth_snap.payload_hash}:{obs_fp}"
        resolution_key = hashlib.sha256(res_key_raw.encode("utf-8")).hexdigest()

        confidence = selected_obs.confidence
        diags = list(selected_obs.diagnostics)
        if auth_snap.is_stale_discovery:
            diags.append("DEGRADED_DISCOVERY: Snapshot resolved from stale cached directory manifest.")
            if confidence == DataConfidenceLevel.HIGH:
                confidence = DataConfidenceLevel.MEDIUM

        return MarketObservationResolutionResult(
            status=MarketDataResolutionStatus.SELECTED,
            resolution_mode=mode,
            as_of=as_of,
            observation_type=obs_type,
            effective_date=target_date,
            selected_observation=selected_obs,
            selected_observation_id=selected_obs.id,
            snapshot_id=auth_snap.id,
            snapshot_hash=auth_snap.payload_hash,
            snapshot_retrieved_at=auth_snap.retrieved_at,
            provider="BIST_KMTP",
            originating_source="BIST",
            semantic_key=sem_str,
            confidence=confidence,
            is_stale_discovery=auth_snap.is_stale_discovery,
            diagnostics=diags,
            evaluation_snapshot_ids=eval_ids,
            resolution_key=resolution_key,
        )

    @classmethod
    def resolve_global_eod(
        cls,
        query_key: GlobalEODQueryKey,
        snapshots: Sequence[GlobalEODSnapshot],
        mode: MarketDataResolutionMode = MarketDataResolutionMode.CURRENT_REPORTED,
        as_of: Optional[datetime] = None,
    ) -> MarketObservationResolutionResult:
        """
        Resolves the canonical Global (US/European) EOD observation for an instrument_id on a trade_date.
        Provider is explicit (ALPHA_VANTAGE, TIINGO, MARKETSTACK) with per-instrument coverage semantics.
        """
        obs_type = "GLOBAL_EOD_PRICE"
        target_date = query_key.trade_date
        inst_id = query_key.instrument_id
        provider_name = query_key.provider.strip().upper()
        sem_str = query_key.to_string()

        # 1. Mode Validation
        if mode == MarketDataResolutionMode.SOURCE_AS_OF:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.UNAVAILABLE_SOURCE_AS_OF,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                diagnostics=["SOURCE_AS_OF unavailable: Global EOD providers do not provide reliable historical first-publication timestamps."],
            )

        if mode == MarketDataResolutionMode.SYSTEM_AS_OF:
            if as_of is None or as_of.tzinfo is None:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    diagnostics=["SYSTEM_AS_OF resolution requires a timezone-aware as_of timestamp."],
                )

        # 2. Filter Snapshots by Provider and Canonical Instrument ID
        relevant_snaps = [
            s for s in snapshots
            if s.provider.strip().upper() == provider_name and s.instrument_id == inst_id
        ]
        if not relevant_snaps:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_SNAPSHOT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                diagnostics=[f"No {provider_name} snapshots found for canonical instrument {inst_id}."],
            )

        # 3. Discard Failed / Non-Authoritative Transport Snapshots (HTTP != 200 or missing payload_hash)
        successful_snaps = [s for s in relevant_snaps if s.http_status == 200 and s.payload_hash]
        if not successful_snaps:
            eval_ids = sorted([str(s.id) for s in relevant_snaps])
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.INVALID_SNAPSHOT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"No successful HTTP 200 snapshots available for {provider_name}:{inst_id}."],
            )

        # 4. Filter by Target-Date Coverage (Non-covering snapshots ignored and cannot poison resolution)
        covering_snaps = [s for s in successful_snaps if _snapshot_covers_target_date(s, target_date)]
        if not covering_snaps:
            eval_ids = sorted([str(s.id) for s in successful_snaps])
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_SNAPSHOT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"No {provider_name} snapshots cover target trade_date {target_date.isoformat()} for instrument {inst_id}."],
            )

        # 5. Validate retrieved_at Timezone Awareness on Successful Covering Snapshots ONLY
        for s in covering_snaps:
            if s.retrieved_at is None or s.retrieved_at.tzinfo is None:
                eval_ids = sorted([str(snap.id) for snap in covering_snaps])
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"Covering snapshot {s.id} contains naive or missing retrieved_at timestamp."],
                )

        # 6. SYSTEM_AS_OF Temporal Filtering BEFORE conflict checks & authority selection
        if mode == MarketDataResolutionMode.SYSTEM_AS_OF:
            assert as_of is not None
            temporal_snaps = [s for s in covering_snaps if s.retrieved_at <= as_of]
            if not temporal_snaps:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.NO_SNAPSHOT_AS_OF,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    diagnostics=[f"No {provider_name} snapshots retrieved as of {as_of.isoformat()} for instrument {inst_id}."],
                )
        else:
            temporal_snaps = covering_snaps

        eval_ids = sorted([str(s.id) for s in temporal_snaps])

        # 7. Deduplicate Logical Snapshots Deterministically
        logical_groups: Dict[Tuple[Any, ...], List[GlobalEODSnapshot]] = {}
        for s in temporal_snaps:
            log_key = (
                s.provider.strip().upper(),
                s.instrument_id,
                s.provider_symbol.strip().upper() if s.provider_symbol else None,
                s.retrieved_at,
                s.payload_hash,
                s.start_date,
                s.end_date,
                s.trade_date_range,
                s.endpoint,
                s.output_size,
            )
            logical_groups.setdefault(log_key, []).append(s)

        deduped_snaps: List[GlobalEODSnapshot] = [grp[0] for grp in logical_groups.values()]

        # 8. Check Conflicts at Latest retrieved_at Frontier
        max_retrieved = max(s.retrieved_at for s in deduped_snaps)
        frontier_snaps = [s for s in deduped_snaps if s.retrieved_at == max_retrieved]

        auth_snap: GlobalEODSnapshot
        if len(frontier_snaps) > 1:
            # Check if same scope but different payload_hash
            scopes = {
                (s.start_date, s.end_date, s.trade_date_range, s.endpoint, s.output_size)
                for s in frontier_snaps
            }
            if len(scopes) == 1:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.SNAPSHOT_CONFLICT,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"SNAPSHOT_CONFLICT: Multiple distinct snapshots at identical retrieved_at {max_retrieved.isoformat()}."],
                )
            else:
                obs_fingerprints_by_snap: Dict[str, Set[str]] = {}
                for s in frontier_snaps:
                    target_obs = [
                        o for o in s.observations
                        if o.trade_date == target_date and o.instrument_id == inst_id
                    ]
                    fps = {_global_observation_fingerprint(o) for o in target_obs}
                    obs_fingerprints_by_snap[str(s.id)] = fps

                all_fps = set()
                for fps in obs_fingerprints_by_snap.values():
                    all_fps.update(fps)

                if len(all_fps) != 1 or any(len(fps) == 0 for fps in obs_fingerprints_by_snap.values()):
                    return MarketObservationResolutionResult(
                        status=MarketDataResolutionStatus.SNAPSHOT_CONFLICT,
                        resolution_mode=mode,
                        as_of=as_of,
                        observation_type=obs_type,
                        effective_date=target_date,
                        canonical_instrument_id=inst_id,
                        provider=provider_name,
                        originating_source=provider_name,
                        semantic_key=sem_str,
                        evaluation_snapshot_ids=eval_ids,
                        diagnostics=["SNAPSHOT_CONFLICT: Differing scopes at same retrieved_at produce conflicting target observations."],
                    )
                def _stable_scope_key(s: GlobalEODSnapshot):
                    return (
                        str(s.payload_hash),
                        str(s.start_date),
                        str(s.end_date),
                        str(s.trade_date_range),
                        str(s.endpoint),
                        str(s.output_size),
                    )
                auth_snap = min(frontier_snaps, key=_stable_scope_key)
        else:
            auth_snap = frontier_snaps[0]

        # 9. Extract Target Observations from Authoritative Snapshot & Lineage Checks
        matching_obs = [
            o for o in auth_snap.observations
            if o.trade_date == target_date and o.instrument_id == inst_id
        ]

        # Lineage checks: provenance failures return INVALID_TEMPORAL_LINEAGE
        for o in matching_obs:
            if o.provider.strip().upper() != auth_snap.provider.strip().upper():
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation provider '{o.provider}' mismatches snapshot provider '{auth_snap.provider}'."],
                )
            if o.snapshot_id and o.snapshot_id != auth_snap.id:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation snapshot_id '{o.snapshot_id}' mismatches snapshot id '{auth_snap.id}'."],
                )
            if o.payload_hash and o.payload_hash != auth_snap.payload_hash:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=["INVALID_TEMPORAL_LINEAGE: Observation payload_hash mismatches snapshot payload_hash."],
                )

        if not matching_obs:
            # No-resurrection: The authoritative snapshot covered this date, but target row was absent/not provided.
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"Authoritative snapshot {auth_snap.id} ({auth_snap.payload_hash[:8]}) covers {target_date.isoformat()} but contains no matching observation."],
            )

        # 10. Deduplicate and Validate Target Observations
        obs_fps = {_global_observation_fingerprint(o) for o in matching_obs}
        if len(obs_fps) > 1:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.OBSERVATION_CONFLICT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"OBSERVATION_CONFLICT: Multiple valid observations with differing fingerprints for {target_date.isoformat()} in snapshot {auth_snap.id}."],
            )

        selected_obs = matching_obs[0]

        # 11. Validate Selected Observation Eligibility
        if (
            selected_obs.status != GlobalObservationStatus.VALID
            or selected_obs.close is None
            or not selected_obs.close.is_finite()
            or selected_obs.instrument_id != inst_id
            or selected_obs.trade_date != target_date
        ):
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"Matching observation in snapshot {auth_snap.id} is invalid: status={selected_obs.status.value}, close={selected_obs.close}."],
            )

        # 12. Construct Deterministic Resolution Key and Return SELECTED Result
        obs_fp = _global_observation_fingerprint(selected_obs)
        res_key_raw = (
            f"GLOBAL_EOD:{mode.value}:{as_of.isoformat() if as_of else 'CURRENT'}:"
            f"{provider_name}:{inst_id}:{target_date.isoformat()}:{auth_snap.payload_hash}:"
            f"{auth_snap.start_date}:{auth_snap.end_date}:{auth_snap.trade_date_range}:{obs_fp}"
        )
        resolution_key = hashlib.sha256(res_key_raw.encode("utf-8")).hexdigest()

        confidence = selected_obs.confidence_level
        diags = list(selected_obs.diagnostics)

        return MarketObservationResolutionResult(
            status=MarketDataResolutionStatus.SELECTED,
            resolution_mode=mode,
            as_of=as_of,
            observation_type=obs_type,
            effective_date=target_date,
            selected_observation=selected_obs,
            selected_observation_id=selected_obs.id,
            snapshot_id=auth_snap.id,
            snapshot_hash=auth_snap.payload_hash,
            snapshot_retrieved_at=auth_snap.retrieved_at,
            provider=provider_name,
            originating_source=provider_name,
            canonical_instrument_id=inst_id,
            semantic_key=sem_str,
            confidence=confidence,
            is_stale_discovery=False,
            diagnostics=diags,
            evaluation_snapshot_ids=eval_ids,
            resolution_key=resolution_key,
        )

    @classmethod
    def resolve_tefas_fund_price(
        cls,
        query_key: TefasFundPriceQueryKey,
        snapshots: Sequence[TefasFundPriceSnapshot],
        mode: MarketDataResolutionMode = MarketDataResolutionMode.CURRENT_REPORTED,
        as_of: Optional[datetime] = None,
    ) -> MarketObservationResolutionResult:
        """
        Resolves the Point-in-Time TEFAS Turkish Investment Fund daily price observation for an instrument_id on a trade_date.
        Provider is fixed to 'TEFAS'.
        """
        obs_type = "TEFAS_FUND_PRICE"
        target_date = query_key.trade_date
        inst_id = query_key.instrument_id
        provider_name = "TEFAS"
        sem_str = query_key.to_string()

        # 1. Mode Validation
        if mode == MarketDataResolutionMode.SOURCE_AS_OF:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.UNAVAILABLE_SOURCE_AS_OF,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                diagnostics=["SOURCE_AS_OF unavailable: TEFAS does not provide reliable historical first-publication timestamps."],
            )

        if mode == MarketDataResolutionMode.SYSTEM_AS_OF:
            if as_of is None or as_of.tzinfo is None:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    diagnostics=["SYSTEM_AS_OF resolution requires a timezone-aware as_of timestamp."],
                )

        # 2. Filter Snapshots by Provider and Canonical Instrument ID
        relevant_snaps = [
            s for s in snapshots
            if s.provider.strip().upper() == provider_name and s.instrument_id == inst_id
        ]
        if not relevant_snaps:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_SNAPSHOT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                diagnostics=[f"No TEFAS snapshots found for canonical instrument {inst_id}."],
            )

        # 3. Discard Failed / Non-Authoritative Transport Snapshots (HTTP != 200 or missing payload_hash)
        successful_snaps = [s for s in relevant_snaps if s.http_status == 200 and s.payload_hash]
        if not successful_snaps:
            eval_ids = sorted([str(s.id) for s in relevant_snaps])
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.INVALID_SNAPSHOT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"No successful HTTP 200 snapshots available for TEFAS:{inst_id}."],
            )

        # 4. Filter by Target-Date Coverage (Non-covering snapshots ignored and cannot poison resolution)
        covering_snaps = [s for s in successful_snaps if _tefas_snapshot_covers_target_date(s, target_date)]
        if not covering_snaps:
            eval_ids = sorted([str(s.id) for s in successful_snaps])
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_SNAPSHOT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"No TEFAS snapshots cover target trade_date {target_date.isoformat()} for instrument {inst_id}."],
            )

        # 5. Validate retrieved_at Timezone Awareness on Successful Covering Snapshots ONLY
        for s in covering_snaps:
            if s.retrieved_at is None or s.retrieved_at.tzinfo is None:
                eval_ids = sorted([str(snap.id) for snap in covering_snaps])
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"Covering snapshot {s.id} contains naive or missing retrieved_at timestamp."],
                )

        # 6. SYSTEM_AS_OF Temporal Filtering BEFORE conflict checks & authority selection
        if mode == MarketDataResolutionMode.SYSTEM_AS_OF:
            assert as_of is not None
            temporal_snaps = [s for s in covering_snaps if s.retrieved_at <= as_of]
            if not temporal_snaps:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.NO_SNAPSHOT_AS_OF,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    diagnostics=[f"No TEFAS snapshots retrieved as of {as_of.isoformat()} for instrument {inst_id}."],
                )
        else:
            temporal_snaps = covering_snaps

        eval_ids = sorted([str(s.id) for s in temporal_snaps])

        # 7. Deduplicate Logical Snapshots Deterministically
        logical_groups: Dict[Tuple[Any, ...], List[TefasFundPriceSnapshot]] = {}
        for s in temporal_snaps:
            log_key = (
                s.provider.strip().upper(),
                s.instrument_id,
                s.provider_symbol.strip().upper() if s.provider_symbol else None,
                s.retrieved_at,
                s.payload_hash,
                s.period_months,
                s.trade_date_range,
                s.endpoint,
                s.parser_version,
            )
            logical_groups.setdefault(log_key, []).append(s)

        deduped_snaps: List[TefasFundPriceSnapshot] = [grp[0] for grp in logical_groups.values()]

        # 8. Check Conflicts at Latest retrieved_at Frontier
        max_retrieved = max(s.retrieved_at for s in deduped_snaps)
        frontier_snaps = [s for s in deduped_snaps if s.retrieved_at == max_retrieved]

        auth_snap: TefasFundPriceSnapshot
        if len(frontier_snaps) > 1:
            # Check if same scope but different payload_hash
            scopes = {
                (s.period_months, s.trade_date_range, s.endpoint, s.parser_version)
                for s in frontier_snaps
            }
            if len(scopes) == 1:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.SNAPSHOT_CONFLICT,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"SNAPSHOT_CONFLICT: Multiple distinct snapshots at identical retrieved_at {max_retrieved.isoformat()}."],
                )
            else:
                obs_fingerprints_by_snap: Dict[str, Set[str]] = {}
                for s in frontier_snaps:
                    target_obs = [
                        o for o in s.observations
                        if o.trade_date == target_date and o.instrument_id == inst_id
                    ]
                    fps = {_tefas_observation_fingerprint(o) for o in target_obs}
                    obs_fingerprints_by_snap[str(s.id)] = fps

                all_fps = set()
                for fps in obs_fingerprints_by_snap.values():
                    all_fps.update(fps)

                if len(all_fps) != 1 or any(len(fps) == 0 for fps in obs_fingerprints_by_snap.values()):
                    return MarketObservationResolutionResult(
                        status=MarketDataResolutionStatus.SNAPSHOT_CONFLICT,
                        resolution_mode=mode,
                        as_of=as_of,
                        observation_type=obs_type,
                        effective_date=target_date,
                        canonical_instrument_id=inst_id,
                        provider=provider_name,
                        originating_source=provider_name,
                        semantic_key=sem_str,
                        evaluation_snapshot_ids=eval_ids,
                        diagnostics=["SNAPSHOT_CONFLICT: Differing scopes at same retrieved_at produce conflicting target observations."],
                    )
                def _stable_tefas_scope_key(s: TefasFundPriceSnapshot):
                    return (
                        str(s.payload_hash),
                        int(s.period_months or 0),
                        str(s.trade_date_range),
                        str(s.endpoint),
                        str(s.provider_symbol),
                    )
                auth_snap = min(frontier_snaps, key=_stable_tefas_scope_key)
        else:
            auth_snap = frontier_snaps[0]

        # 9. Extract Target Observations from Authoritative Snapshot & Lineage Checks
        matching_obs = [
            o for o in auth_snap.observations
            if o.trade_date == target_date and o.instrument_id == inst_id
        ]

        # Lineage checks: provenance failures return INVALID_TEMPORAL_LINEAGE
        for o in matching_obs:
            if o.provider.strip().upper() != auth_snap.provider.strip().upper():
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation provider '{o.provider}' mismatches snapshot provider '{auth_snap.provider}'."],
                )
            if o.snapshot_id and o.snapshot_id != auth_snap.id:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation snapshot_id '{o.snapshot_id}' mismatches snapshot id '{auth_snap.id}'."],
                )
            if o.payload_hash and o.payload_hash != auth_snap.payload_hash:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=target_date,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=["INVALID_TEMPORAL_LINEAGE: Observation payload_hash mismatches snapshot payload_hash."],
                )
            if auth_snap.provider_symbol and o.provider_symbol:
                if o.provider_symbol.strip().upper() != auth_snap.provider_symbol.strip().upper():
                    return MarketObservationResolutionResult(
                        status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                        resolution_mode=mode,
                        as_of=as_of,
                        observation_type=obs_type,
                        effective_date=target_date,
                        snapshot_id=auth_snap.id,
                        snapshot_hash=auth_snap.payload_hash,
                        snapshot_retrieved_at=auth_snap.retrieved_at,
                        canonical_instrument_id=inst_id,
                        provider=provider_name,
                        originating_source=provider_name,
                        semantic_key=sem_str,
                        evaluation_snapshot_ids=eval_ids,
                        diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation provider_symbol '{o.provider_symbol}' mismatches snapshot symbol '{auth_snap.provider_symbol}'."],
                    )

        if not matching_obs:
            # No-resurrection: The authoritative snapshot covered this date, but target row was absent/not provided.
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"Authoritative snapshot {auth_snap.id} ({auth_snap.payload_hash[:8]}) covers {target_date.isoformat()} but contains no matching observation."],
            )

        # 10. Deduplicate and Validate Target Observations
        obs_fps = {_tefas_observation_fingerprint(o) for o in matching_obs}
        if len(obs_fps) > 1:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.OBSERVATION_CONFLICT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"OBSERVATION_CONFLICT: Multiple valid observations with differing fingerprints for {target_date.isoformat()} in snapshot {auth_snap.id}."],
            )

        selected_obs = matching_obs[0]

        # 11. Validate Selected Observation Eligibility
        if (
            selected_obs.status != TefasObservationStatus.VALID
            or selected_obs.unit_price is None
            or not selected_obs.unit_price.is_finite()
            or selected_obs.unit_price <= Decimal("0")
            or selected_obs.instrument_id != inst_id
            or selected_obs.trade_date != target_date
            or selected_obs.currency is None
            or selected_obs.instrument_type not in TEFAS_RESOLVER_ALLOWED_INSTRUMENT_TYPES
        ):
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=target_date,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"Matching observation in snapshot {auth_snap.id} is invalid: status={selected_obs.status.value}, unit_price={selected_obs.unit_price}."],
            )

        # 12. Construct Deterministic Resolution Key and Return SELECTED Result
        obs_fp = _tefas_observation_fingerprint(selected_obs)
        res_key_raw = (
            f"TEFAS_FUND_PRICE:{mode.value}:{as_of.isoformat() if as_of else 'CURRENT'}:"
            f"{provider_name}:{inst_id}:{target_date.isoformat()}:{auth_snap.payload_hash}:"
            f"{auth_snap.period_months}:{auth_snap.trade_date_range}:{obs_fp}"
        )
        resolution_key = hashlib.sha256(res_key_raw.encode("utf-8")).hexdigest()

        confidence = selected_obs.confidence_level
        diags = list(selected_obs.diagnostics)

        return MarketObservationResolutionResult(
            status=MarketDataResolutionStatus.SELECTED,
            resolution_mode=mode,
            as_of=as_of,
            observation_type=obs_type,
            effective_date=target_date,
            selected_observation=selected_obs,
            selected_observation_id=selected_obs.id,
            snapshot_id=auth_snap.id,
            snapshot_hash=auth_snap.payload_hash,
            snapshot_retrieved_at=auth_snap.retrieved_at,
            provider=provider_name,
            originating_source=provider_name,
            canonical_instrument_id=inst_id,
            semantic_key=sem_str,
            confidence=confidence,
            is_stale_discovery=False,
            diagnostics=diags,
            evaluation_snapshot_ids=eval_ids,
            resolution_key=resolution_key,
        )

    @classmethod
    def resolve_tefas_current_metrics(
        cls,
        query_key: TefasFundCurrentMetricsQueryKey,
        snapshots: Sequence[TefasFundMetricsSnapshot],
        mode: MarketDataResolutionMode = MarketDataResolutionMode.CURRENT_REPORTED,
        as_of: Optional[datetime] = None,
    ) -> MarketObservationResolutionResult:
        """
        Deterministically resolves a Point-in-Time TEFAS Turkish Investment Fund current valuation and size metrics observation.

        Resolution Invariants:
            - SOURCE_AS_OF: Always returns UNAVAILABLE_SOURCE_AS_OF (TEFAS current endpoint provides no economic date).
            - CURRENT_REPORTED: Selects latest authoritative snapshot by retrieved_at knowledge time.
            - SYSTEM_AS_OF: Enforces retrieved_at <= as_of temporal cutoff; requires timezone-aware as_of.
            - Authority is canonical instrument_id; provider is fixed to 'TEFAS'; provider_symbol is diagnostic only.
            - HTTP-200 snapshots with invalid/missing observation block resurrection of older valid snapshots (fail-closed).
            - HTTP failures (403, 429, 500, timeout) are excluded and do not supersede older valid HTTP-200 snapshots.
            - Fresh PARTIAL metrics beat older COMPLETE metrics (no field resurrection).
            - Try-only safety: Only canonical Currency.TRY observations are eligible.
            - Exact Decimal preserved; zero-value AUM/units/investors remain valid non-negative states.
            - Deterministic, UUID-independent resolution key computed via SHA-256.
        """
        obs_type = "TEFAS_FUND_CURRENT_METRICS"
        inst_id = query_key.instrument_id
        provider_name = TEFAS_PROVIDER_NAME
        sem_str = query_key.to_string()

        # 1. Mode Validation & Temporal Contract Checks
        if mode == MarketDataResolutionMode.SOURCE_AS_OF:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.UNAVAILABLE_SOURCE_AS_OF,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=None,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                diagnostics=["SOURCE_AS_OF is unavailable: TEFAS fonBilgiGetir provides no publication or economic effective date."],
            )

        if mode == MarketDataResolutionMode.SYSTEM_AS_OF:
            if as_of is None or as_of.tzinfo is None:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=None,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    diagnostics=["SYSTEM_AS_OF requires a timezone-aware as_of datetime."],
                )

        if not snapshots:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_SNAPSHOT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=None,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                diagnostics=["No TEFAS current metrics snapshots provided to resolver."],
            )

        # 2. Scope & Candidate Filtering
        candidate_snaps: List[TefasFundMetricsSnapshot] = []
        for s in snapshots:
            if (s.provider or "").strip().upper() != provider_name:
                continue
            if (s.endpoint or "").strip().upper() != "FUND_CURRENT_METRICS":
                continue
            if s.instrument_id != inst_id:
                continue
            if s.http_status != 200:
                continue
            if not s.payload_hash or not str(s.payload_hash).strip():
                continue
            candidate_snaps.append(s)

        if not candidate_snaps:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_SNAPSHOT,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=None,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                diagnostics=["No HTTP 200 TEFAS current metrics snapshot found for instrument."],
            )

        # 3. Check retrieved_at timezone-awareness on candidate snapshots
        for s in candidate_snaps:
            if s.retrieved_at is None or s.retrieved_at.tzinfo is None:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=None,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    diagnostics=[f"Snapshot {s.id} has naive or missing retrieved_at timestamp."],
                )

        # 4. SYSTEM_AS_OF Temporal Filtering BEFORE conflict checks & authority selection
        if mode == MarketDataResolutionMode.SYSTEM_AS_OF:
            assert as_of is not None
            temporal_snaps = [s for s in candidate_snaps if s.retrieved_at <= as_of]
            if not temporal_snaps:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.NO_SNAPSHOT_AS_OF,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=None,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    diagnostics=[f"No TEFAS current metrics snapshots retrieved as of {as_of.isoformat()} for instrument {inst_id}."],
                )
        else:
            temporal_snaps = candidate_snaps

        eval_ids = sorted([str(s.id) for s in temporal_snaps])

        # 5. Deduplicate Logical Snapshots Deterministically
        logical_groups: Dict[Tuple[Any, ...], List[TefasFundMetricsSnapshot]] = {}
        for s in temporal_snaps:
            log_key = (
                s.provider.strip().upper(),
                s.instrument_id,
                s.provider_symbol.strip().upper() if s.provider_symbol else None,
                s.retrieved_at,
                s.payload_hash,
                s.endpoint,
                s.parser_version,
            )
            logical_groups.setdefault(log_key, []).append(s)

        deduped_snaps: List[TefasFundMetricsSnapshot] = [grp[0] for grp in logical_groups.values()]

        # 6. Check Conflicts at Latest retrieved_at Frontier
        max_retrieved = max(s.retrieved_at for s in deduped_snaps)
        frontier_snaps = [s for s in deduped_snaps if s.retrieved_at == max_retrieved]

        auth_snap: TefasFundMetricsSnapshot
        if len(frontier_snaps) > 1:
            hashes = {s.payload_hash for s in frontier_snaps}
            if len(hashes) > 1:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.SNAPSHOT_CONFLICT,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=None,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"SNAPSHOT_CONFLICT: Multiple differing payload hashes at identical retrieved_at {max_retrieved.isoformat()}."],
                )
            obs_fps = set()
            for s in frontier_snaps:
                if s.observation:
                    obs_fps.add(_tefas_current_metrics_observation_fingerprint(s.observation))
                else:
                    obs_fps.add("NO_OBSERVATION")
            if len(obs_fps) > 1:
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.SNAPSHOT_CONFLICT,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=None,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=["SNAPSHOT_CONFLICT: Differing observations at identical retrieved_at."],
                )
            auth_snap = min(frontier_snaps, key=lambda s: (s.payload_hash, str(s.provider_symbol)))
        else:
            auth_snap = frontier_snaps[0]

        # 7. Authoritative Snapshot Observation & Lineage Evaluation
        obs = auth_snap.observation

        if obs is None:
            res_key_raw = (
                f"TEFAS_FUND_CURRENT_METRICS:{mode.value}:{as_of.isoformat() if as_of else 'CURRENT'}:"
                f"{provider_name}:{inst_id}:{auth_snap.retrieved_at.isoformat()}:{auth_snap.payload_hash}:NO_OBSERVATION"
            )
            resolution_key = hashlib.sha256(res_key_raw.encode("utf-8")).hexdigest()
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=None,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                resolution_key=resolution_key,
                diagnostics=["Latest authoritative HTTP-200 snapshot contains no usable observation (fail-closed)."],
            )

        # Lineage Checks
        if (obs.provider or "").strip().upper() != auth_snap.provider.strip().upper():
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=None,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation provider '{obs.provider}' mismatches snapshot provider '{auth_snap.provider}'."],
            )
        if obs.snapshot_id and obs.snapshot_id != auth_snap.id:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=None,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation snapshot_id '{obs.snapshot_id}' mismatches snapshot id '{auth_snap.id}'."],
            )
        if obs.payload_hash and obs.payload_hash != auth_snap.payload_hash:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=None,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=["INVALID_TEMPORAL_LINEAGE: Observation payload_hash mismatches snapshot payload_hash."],
            )
        if obs.instrument_id != inst_id:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=None,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation instrument_id '{obs.instrument_id}' mismatches query '{inst_id}'."],
            )
        if auth_snap.provider_symbol and obs.provider_symbol:
            if obs.provider_symbol.strip().upper() != auth_snap.provider_symbol.strip().upper():
                return MarketObservationResolutionResult(
                    status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                    resolution_mode=mode,
                    as_of=as_of,
                    observation_type=obs_type,
                    effective_date=None,
                    snapshot_id=auth_snap.id,
                    snapshot_hash=auth_snap.payload_hash,
                    snapshot_retrieved_at=auth_snap.retrieved_at,
                    canonical_instrument_id=inst_id,
                    provider=provider_name,
                    originating_source=provider_name,
                    semantic_key=sem_str,
                    evaluation_snapshot_ids=eval_ids,
                    diagnostics=[f"INVALID_TEMPORAL_LINEAGE: Observation provider_symbol '{obs.provider_symbol}' mismatches snapshot symbol '{auth_snap.provider_symbol}'."],
                )
        if obs.retrieved_at is None or obs.retrieved_at.tzinfo is None or obs.retrieved_at != auth_snap.retrieved_at:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=None,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=["INVALID_TEMPORAL_LINEAGE: Observation retrieved_at is naive or mismatches snapshot retrieved_at."],
            )
        if obs.effective_date is not None:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=None,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=["INVALID_TEMPORAL_LINEAGE: Current metrics observation has fabricated effective_date."],
            )
        if obs.published_at is not None:
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=None,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                diagnostics=["INVALID_TEMPORAL_LINEAGE: Current metrics observation has fabricated published_at."],
            )

        # 8. Check Observation Eligibility
        is_units_valid = True
        if obs.outstanding_units is not None:
            if isinstance(obs.outstanding_units, bool):
                is_units_valid = False
            else:
                try:
                    u_dec = Decimal(obs.outstanding_units)
                    is_units_valid = u_dec.is_finite() and u_dec >= Decimal("0")
                except Exception:
                    is_units_valid = False

        is_investor_valid = True
        if obs.investor_count is not None:
            if isinstance(obs.investor_count, bool) or not isinstance(obs.investor_count, int) or obs.investor_count < 0:
                is_investor_valid = False

        if (
            obs.status != TefasObservationStatus.VALID
            or obs.portfolio_size is None
            or not obs.portfolio_size.is_finite()
            or obs.portfolio_size < Decimal("0")
            or obs.portfolio_size_currency != Currency.TRY
            or (obs.instrument_type and obs.instrument_type not in TEFAS_RESOLVER_ALLOWED_INSTRUMENT_TYPES)
            or not is_units_valid
            or not is_investor_valid
        ):
            res_key_raw = (
                f"TEFAS_FUND_CURRENT_METRICS:{mode.value}:{as_of.isoformat() if as_of else 'CURRENT'}:"
                f"{provider_name}:{inst_id}:{auth_snap.retrieved_at.isoformat()}:{auth_snap.payload_hash}:INELIGIBLE"
            )
            resolution_key = hashlib.sha256(res_key_raw.encode("utf-8")).hexdigest()
            return MarketObservationResolutionResult(
                status=MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION,
                resolution_mode=mode,
                as_of=as_of,
                observation_type=obs_type,
                effective_date=None,
                snapshot_id=auth_snap.id,
                snapshot_hash=auth_snap.payload_hash,
                snapshot_retrieved_at=auth_snap.retrieved_at,
                canonical_instrument_id=inst_id,
                provider=provider_name,
                originating_source=provider_name,
                semantic_key=sem_str,
                evaluation_snapshot_ids=eval_ids,
                resolution_key=resolution_key,
                diagnostics=[f"Observation in latest authoritative snapshot {auth_snap.id} is invalid/ineligible (fail-closed)."],
            )

        # 9. Compute Deterministic Resolution Key and Return SELECTED Result
        obs_fp = _tefas_current_metrics_observation_fingerprint(obs)
        res_key_raw = (
            f"TEFAS_FUND_CURRENT_METRICS:{mode.value}:{as_of.isoformat() if as_of else 'CURRENT'}:"
            f"{provider_name}:{inst_id}:{auth_snap.retrieved_at.isoformat()}:{auth_snap.payload_hash}:{obs_fp}"
        )
        resolution_key = hashlib.sha256(res_key_raw.encode("utf-8")).hexdigest()

        confidence = obs.confidence_level
        diags = list(obs.diagnostics)

        return MarketObservationResolutionResult(
            status=MarketDataResolutionStatus.SELECTED,
            resolution_mode=mode,
            as_of=as_of,
            observation_type=obs_type,
            effective_date=None,
            selected_observation=obs,
            selected_observation_id=obs.id,
            snapshot_id=auth_snap.id,
            snapshot_hash=auth_snap.payload_hash,
            snapshot_retrieved_at=auth_snap.retrieved_at,
            provider=provider_name,
            originating_source=provider_name,
            canonical_instrument_id=inst_id,
            semantic_key=sem_str,
            confidence=confidence,
            is_stale_discovery=False,
            diagnostics=diags,
            evaluation_snapshot_ids=eval_ids,
            resolution_key=resolution_key,
        )

