"""
backend/engine/learning/evidence_binding.py
===========================================
Pure evidence-binding contract (Phase 28B-1A): connects a Phase 28B-0 source-evidence declaration to ONE retained raw-provider snapshot.

Verification levels are separate claims, never one generic "verified" flag. Only three can ever be VERIFIED, and only together, by `adapters.verify_evidence_binding`:
stored-content integrity (the retained payload re-hashes to the declared hash), record-reference existence and source-identity binding. Document-assertion, licensing/access,
economic-completeness and publication-time-authority verification are constant UNVERIFIED here: no authority implementing them exists. A hash alone, a declaration, or an
attestation never produces VERIFIED, and nothing here authorizes capture.

Pure value objects: no database, network, clock, entropy or private-engine import (the private storage type is read only in `adapters.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict

from backend.engine.learning._checks import (
    canonical_sha256,
    iso_utc,
    require_enum,
    require_exact_type,
    require_nonblank_text,
    utc,
)
from backend.engine.learning.source_authority import SourceAuthorityRecord
from backend.engine.learning.temporal_provenance import TemporalProvenance


class EvidenceBindingError(ValueError):
    """The declared evidence cannot be bound to the retained record. Always fails closed."""


class VerificationLevel(Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class BindingVerificationLevels:
    stored_content_integrity: VerificationLevel
    record_reference_existence: VerificationLevel
    source_identity_binding: VerificationLevel
    document_assertion_verification: VerificationLevel = VerificationLevel.UNVERIFIED
    licensing_access_verification: VerificationLevel = VerificationLevel.UNVERIFIED
    economic_completeness_verification: VerificationLevel = VerificationLevel.UNVERIFIED
    publication_time_authority: VerificationLevel = VerificationLevel.UNVERIFIED

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            require_enum(name, getattr(self, name), VerificationLevel)
        integrity = {self.stored_content_integrity, self.record_reference_existence, self.source_identity_binding}
        if len(integrity) != 1:
            raise ValueError("the three integrity levels come from one verification and must be all VERIFIED or all UNVERIFIED")
        for name in ("document_assertion_verification", "licensing_access_verification", "economic_completeness_verification", "publication_time_authority"):
            if getattr(self, name) is not VerificationLevel.UNVERIFIED:
                raise ValueError(f"{name} cannot be VERIFIED: no independent authority implements it")


@dataclass(frozen=True)
class EvidenceBindingDeclaration:
    """A caller-supplied claim that `source_authority` describes the retained snapshot `source_authority.raw_snapshot_id`. Nothing here is verified."""
    source_authority: SourceAuthorityRecord
    provenance: TemporalProvenance
    expected_provider: str
    expected_endpoint: str
    revision: int

    def __post_init__(self) -> None:
        require_exact_type("source_authority", self.source_authority, SourceAuthorityRecord)
        require_exact_type("provenance", self.provenance, TemporalProvenance)
        require_nonblank_text("expected_provider", self.expected_provider)
        require_nonblank_text("expected_endpoint", self.expected_endpoint)
        if len(self.expected_provider) > 64 or len(self.expected_endpoint) > 255:
            raise ValueError("expected_provider is limited to 64 and expected_endpoint to 255 characters")
        require_exact_type("revision", self.revision, int)
        if self.revision < 1:
            raise ValueError("revision must be >= 1")
        if self.source_authority.raw_snapshot_id is None:
            raise ValueError("a binding requires source_authority.raw_snapshot_id")
        if utc(self.source_authority.retrieved_at) != utc(self.provenance.retrieved_at):
            raise ValueError("source_authority.retrieved_at and provenance.retrieved_at must be the same instant")

    def logical_key(self) -> tuple:
        return (self.source_authority.raw_snapshot_id, self.source_authority.source_id, self.revision)

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "source_authority": self.source_authority.to_canonical_dict(),
            "provenance": self.provenance.to_canonical_dict(),
            "expected_provider": self.expected_provider,
            "expected_endpoint": self.expected_endpoint,
            "revision": self.revision,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.to_canonical_dict())


_ISSUER = object()          # module-private token: only adapters.verify_evidence_binding passes it (guards against accidental construction, not against a determined caller)


@dataclass(frozen=True)
class VerifiedEvidenceBinding:
    declaration: EvidenceBindingDeclaration
    snapshot_retrieved_at: datetime
    levels: BindingVerificationLevels
    _issuer: object

    capture_authorized = False

    def __post_init__(self) -> None:
        if self._issuer is not _ISSUER:
            raise TypeError("VerifiedEvidenceBinding can only be issued by verify_evidence_binding")
        require_exact_type("declaration", self.declaration, EvidenceBindingDeclaration)
        require_exact_type("levels", self.levels, BindingVerificationLevels)
        if self.levels.stored_content_integrity is not VerificationLevel.VERIFIED:
            raise ValueError("a verified binding requires verified integrity levels")
        if utc(self.snapshot_retrieved_at) != utc(self.declaration.source_authority.retrieved_at):
            raise ValueError("snapshot_retrieved_at must equal the declared retrieval instant")

    def to_rpc_dict(self) -> Dict[str, Any]:
        s, p = self.declaration.source_authority, self.declaration.provenance
        return {
            "raw_snapshot_id": str(s.raw_snapshot_id),
            "source_id": s.source_id,
            "revision": self.declaration.revision,
            "expected_provider": self.declaration.expected_provider,
            "expected_endpoint": self.declaration.expected_endpoint,
            "content_sha256": s.content_sha256,
            "snapshot_retrieved_at": iso_utc(self.snapshot_retrieved_at),
            "capture_attempted_at": iso_utc(p.capture_attempted_at),
            "economic_date": p.economic_date.isoformat() if p.economic_date else None,
            "publication_time": iso_utc(p.publication_time) if p.publication_time else None,
            "publication_evidence_sha256": p.publication_evidence_sha256,
            "data_revision": p.data_revision,
            "document_version": s.document_version,
            "source_reference": s.source_reference,
            "authority_class": s.authority_class.value,
            "availability_declared": s.availability.value,
            "licensing_declared": s.licensing.value,
            "licensing_evidence_sha256s": sorted(s.licensing_evidence_sha256s),
            "known_limitations": sorted(s.known_limitations),
        }
