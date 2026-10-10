"""
backend/engine/learning/source_authority.py
===========================================
Authority record for one stored source document or provider response (Phase 28B-0).

A `content_sha256` proves stored-content identity only: it is not publisher authenticity and not a historical publication time. Licensing and availability are
caller-supplied, UNVERIFIED declarations. A declared permission or prohibition must cite licensing/access evidence hashes, but even then this record never authorizes
capture: `capture_authorized` is constant False in Phase 28B-0. A future source-access gate (Phase 28B-1) must verify the referenced stored evidence independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from backend.engine.learning._checks import (
    canonical_sha256,
    iso_utc,
    optional,
    require_aware_datetime,
    require_canonical_token,
    require_enum,
    require_exact_type,
    require_nonblank_text,
    require_sha256,
    require_tuple_of,
)


class SourceAuthorityClass(Enum):
    PRIMARY_OFFICIAL_DOCUMENT = "primary_official_document"
    OFFICIAL_PUBLIC_PROVIDER_SURFACE = "official_public_provider_surface"
    ACADEMIC_PRIMARY = "academic_primary"
    SECONDARY_REPORTING = "secondary_reporting"
    UNCLASSIFIED = "unclassified"


class AvailabilityStatus(Enum):
    PUBLICLY_AVAILABLE = "publicly_available"
    ACCESS_RESTRICTED = "access_restricted"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class LicensingStatus(Enum):
    """Caller DECLARATIONS about licensing; never verified here and never authorization."""
    UNRESOLVED = "unresolved"
    PERMISSION_DECLARED = "permission_declared"
    PROHIBITION_DECLARED = "prohibition_declared"


@dataclass(frozen=True)
class SourceAuthorityRecord:
    """
    Immutable source authority. `content_sha256` proves stored-content identity only; it is not publisher authenticity and does not prove historical publication time.
    """
    source_id: str
    source_reference: str
    document_version: Optional[str]
    retrieved_at: datetime
    content_sha256: str
    authority_class: SourceAuthorityClass
    known_limitations: Tuple[str, ...]
    availability: AvailabilityStatus
    licensing: LicensingStatus
    raw_snapshot_id: Optional[UUID] = None
    licensing_evidence_sha256s: Tuple[str, ...] = ()

    capture_authorized = False          # constant: no live capture is authorized by any record in Phase 28B-0
    licensing_verified = False          # constant: declarations are not independently verified here

    def __post_init__(self) -> None:
        require_canonical_token("source_id", self.source_id)
        require_nonblank_text("source_reference", self.source_reference)
        optional(require_nonblank_text, "document_version", self.document_version)
        require_aware_datetime("retrieved_at", self.retrieved_at)
        require_sha256("content_sha256", self.content_sha256)
        require_enum("authority_class", self.authority_class, SourceAuthorityClass)
        for item in require_tuple_of("known_limitations", self.known_limitations, str):
            require_nonblank_text("known_limitations item", item)
        require_enum("availability", self.availability, AvailabilityStatus)
        require_enum("licensing", self.licensing, LicensingStatus)
        if self.raw_snapshot_id is not None:
            require_exact_type("raw_snapshot_id", self.raw_snapshot_id, UUID)
        for h in require_tuple_of("licensing_evidence_sha256s", self.licensing_evidence_sha256s, str):
            require_sha256("licensing_evidence_sha256s item", h)
        if (self.licensing is not LicensingStatus.UNRESOLVED) != bool(self.licensing_evidence_sha256s):
            raise ValueError("a declared licensing permission/prohibition must cite licensing evidence hashes, and unresolved licensing must cite none")

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_reference": self.source_reference,
            "document_version": self.document_version,
            "retrieved_at": iso_utc(self.retrieved_at),
            "content_sha256": self.content_sha256,
            "authority_class": self.authority_class.value,
            "known_limitations": sorted(self.known_limitations),
            "availability": self.availability.value,
            "licensing": self.licensing.value,
            "raw_snapshot_id": str(self.raw_snapshot_id) if self.raw_snapshot_id else None,
            "licensing_evidence_sha256s": sorted(self.licensing_evidence_sha256s),
            "capture_authorized": False,
            "licensing_verified": False,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.to_canonical_dict())
