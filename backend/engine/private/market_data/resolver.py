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
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

from backend.engine.private.bist.models import (
    BISTBulletinSnapshot,
    BISTEODObservation,
    BISTObservationStatus,
)
from backend.engine.private.domain import DataConfidenceLevel
from backend.engine.private.market_data.models import (
    BISTInstrumentQueryKey,
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
    MarketObservationResolutionResult,
    PreciousMetalSemanticKey,
)
from backend.engine.private.precious_metals.models import (
    PreciousMetalMarketObservation,
    PreciousMetalObservationStatus,
    PreciousMetalSnapshot,
)


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
