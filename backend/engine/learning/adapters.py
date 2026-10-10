"""
backend/engine/learning/adapters.py
===================================
The only learning module that touches existing private evidence types (Phase 28B-0). It READS `RawProviderSnapshotRecord` and `TefasFundPriceObservation` and maps them
into learning contracts; it does not construct, mutate or re-define them, and adds no persistence.

The retained `raw_payload` is re-hashed with the closed storage authority's `compute_payload_hash` and must equal `payload_hash`. This establishes local content
consistency only, NOT provider authenticity.

Publication time is never imported: a provider-supplied `published_at` is not independently evidenced here, so publication stays UNKNOWN. The economic date stays the
observation's trade date; the retrieval instant is the stored `retrieved_at`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Tuple

from backend.engine.learning._checks import require_aware_datetime, require_enum, require_exact_type, require_sha256, utc
from backend.engine.learning.evidence_binding import (
    _ISSUER,
    BindingVerificationLevels,
    EvidenceBindingDeclaration,
    EvidenceBindingError,
    VerificationLevel,
    VerifiedEvidenceBinding,
)
from backend.engine.learning.observation_status import CapturedObservation, ObservationOrigin, ObservationStatus
from backend.engine.learning.source_authority import AvailabilityStatus, LicensingStatus, SourceAuthorityClass, SourceAuthorityRecord
from backend.engine.learning.temporal_provenance import TemporalProvenance
from backend.engine.learning.universe_coverage import LearningSubjectRef
from backend.engine.private.market_data.tefas_models import TefasFundPriceObservation, TefasObservationStatus
from backend.engine.private.storage_models import RawProviderSnapshotRecord, compute_payload_hash


def source_authority_from_raw_snapshot(
    record: RawProviderSnapshotRecord,
    *,
    source_id: str,
    source_reference: str,
    authority_class: SourceAuthorityClass,
    known_limitations: Tuple[str, ...],
    availability: AvailabilityStatus,
    licensing: LicensingStatus,
    licensing_evidence_sha256s: Tuple[str, ...] = (),
    document_version: "str | None" = None,
) -> SourceAuthorityRecord:
    require_exact_type("record", record, RawProviderSnapshotRecord)
    require_sha256("record.payload_hash", record.payload_hash)
    if compute_payload_hash(record.raw_payload) != record.payload_hash:
        raise ValueError("retained raw_payload does not match its payload_hash (local content inconsistency)")
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
        licensing_evidence_sha256s=licensing_evidence_sha256s,
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


def verify_evidence_binding(declaration: EvidenceBindingDeclaration, record: "RawProviderSnapshotRecord | None") -> VerifiedEvidenceBinding:
    """
    Bind a declaration to the retained record, failing closed. Establishes ONLY stored-content integrity, record-reference existence and source-identity binding:
    the retained payload must re-hash (closed `compute_payload_hash`) to the record's stored hash and to the declared hash, and the declared retrieval instant must be the
    retained record's own `retrieved_at` (never an economic date). It does not verify licensing, completeness, document assertions or publication time, and does not prove
    provider authenticity.
    """
    require_exact_type("declaration", declaration, EvidenceBindingDeclaration)
    if record is None:
        raise EvidenceBindingError("the referenced raw snapshot does not exist")
    if type(record) is not RawProviderSnapshotRecord:
        raise EvidenceBindingError(f"the retained record must be exactly a RawProviderSnapshotRecord, got {type(record).__name__}")
    src = declaration.source_authority
    if record.id != src.raw_snapshot_id:
        raise EvidenceBindingError("conflicting snapshot identity: the retained record is not the referenced snapshot")
    if record.provider != declaration.expected_provider or record.endpoint != declaration.expected_endpoint:
        raise EvidenceBindingError("source identity mismatch between the declaration and the retained record")
    if record.raw_payload is None:
        raise EvidenceBindingError("the retained record holds no payload this verifier can re-hash")
    try:
        require_sha256("record.payload_hash", record.payload_hash)
        require_aware_datetime("record.retrieved_at", record.retrieved_at)
    except (TypeError, ValueError) as exc:
        raise EvidenceBindingError(f"the retained record is malformed: {exc}") from exc
    if compute_payload_hash(record.raw_payload) != record.payload_hash:
        raise EvidenceBindingError("the retained payload does not re-hash to its stored hash")
    if src.content_sha256 != record.payload_hash:
        raise EvidenceBindingError("the declared content hash is not the retained record's hash")
    if utc(src.retrieved_at) != utc(record.retrieved_at):
        raise EvidenceBindingError("the declared retrieval instant is not the retained record's retrieved_at")
    levels = BindingVerificationLevels(
        stored_content_integrity=VerificationLevel.VERIFIED,
        record_reference_existence=VerificationLevel.VERIFIED,
        source_identity_binding=VerificationLevel.VERIFIED,
    )
    return VerifiedEvidenceBinding(declaration=declaration, snapshot_retrieved_at=record.retrieved_at, levels=levels, _issuer=_ISSUER)
