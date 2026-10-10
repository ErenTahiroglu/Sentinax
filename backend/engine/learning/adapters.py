"""
backend/engine/learning/adapters.py
===================================
The only learning module that touches existing private evidence types (Phase 28B-0). It READS `RawProviderSnapshotRecord` and `TefasFundPriceObservation` and maps them
into learning contracts; it does not construct, mutate or re-define them, and adds no persistence.

Publication time is never imported: a provider-supplied `published_at` is not independently evidenced here, so publication stays UNKNOWN. The economic date stays the
observation's trade date; the retrieval instant is the stored `retrieved_at`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Tuple

from backend.engine.learning._checks import require_enum, require_exact_type, require_sha256
from backend.engine.learning.observation_status import CapturedObservation, ObservationOrigin, ObservationStatus
from backend.engine.learning.source_authority import AvailabilityStatus, LicensingStatus, SourceAuthorityClass, SourceAuthorityRecord
from backend.engine.learning.temporal_provenance import TemporalProvenance
from backend.engine.learning.universe_coverage import LearningSubjectRef
from backend.engine.private.market_data.tefas_models import TefasFundPriceObservation, TefasObservationStatus
from backend.engine.private.storage_models import RawProviderSnapshotRecord


def source_authority_from_raw_snapshot(
    record: RawProviderSnapshotRecord,
    *,
    source_id: str,
    source_reference: str,
    authority_class: SourceAuthorityClass,
    known_limitations: Tuple[str, ...],
    availability: AvailabilityStatus,
    licensing: LicensingStatus,
    document_version: "str | None" = None,
) -> SourceAuthorityRecord:
    require_exact_type("record", record, RawProviderSnapshotRecord)
    require_sha256("record.payload_hash", record.payload_hash)
    return SourceAuthorityRecord(
        source_id=source_id,
        source_reference=source_reference,
        document_version=document_version,
        retrieved_at=record.retrieved_at,
        content_sha256=record.payload_hash,
        authority_class=authority_class,
        known_limitations=known_limitations,
        availability=availability,
        licensing=licensing,
        raw_snapshot_id=record.id,
    )


def captured_observation_from_tefas_price(
    observation: TefasFundPriceObservation,
    *,
    capture_attempted_at: datetime,
    origin: ObservationOrigin,
) -> CapturedObservation:
    require_exact_type("observation", observation, TefasFundPriceObservation)
    require_enum("origin", origin, ObservationOrigin)
    if observation.status is not TefasObservationStatus.VALID:
        raise ValueError("only VALID TEFAS observations map to OBSERVED_IN_RESPONSE; other statuses must be recorded explicitly by the caller")
    if observation.unit_price is None:
        raise ValueError("a TEFAS observation without a unit price is not an observed value")
    if observation.retrieved_at is None:
        raise ValueError("retrieved_at is required; it is never defaulted from the clock")
    provenance = TemporalProvenance(
        economic_date=observation.trade_date,
        retrieved_at=observation.retrieved_at,
        capture_attempted_at=capture_attempted_at,
    )
    return CapturedObservation(
        subject=LearningSubjectRef(fund_code=observation.provider_symbol, canonical_instrument_id=observation.instrument_id),
        economic_date=observation.trade_date,
        status=ObservationStatus.OBSERVED_IN_RESPONSE,
        origin=origin,
        provenance=provenance,
        observed_unit_price=observation.unit_price,
    )
