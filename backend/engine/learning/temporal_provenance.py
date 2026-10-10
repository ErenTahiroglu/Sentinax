"""
backend/engine/learning/temporal_provenance.py
==============================================
Separate temporal axes of one piece of learning evidence (Phase 28B-0):

    economic_date          the economic/effective date the evidence is about (may be unknown for list-like evidence)
    publication_time       when the SOURCE independently evidenced publication; UNKNOWN unless stated by cited source evidence
    retrieved_at           when Sentinax retrieved the content (system time)
    capture_attempted_at   when the capture attempt started (<= retrieved_at)
    data_revision          source/data revision label, if any

A historical first-publication instant is NEVER inferred from retrieval, and an old economic date never becomes a retrieval or knowledge time. The only knowledge instant
this type exposes is `system_known_at_utc` (= retrieved_at). Non-authoritative for financial decisions.

Only instant-versus-instant ordering is enforced (capture attempt <= retrieval; evidenced publication <= retrieval). The economic date is deliberately NOT compared with any
UTC calendar date: its timezone and event semantics are source-specific (local-midnight observations, documents published ahead of a future effective date, historical
observations retrieved years later). Any source-specific rule (e.g. a price cannot precede its valuation day) belongs to a separately approved source authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, Optional

from backend.engine.learning._checks import (
    canonical_sha256,
    iso_utc,
    optional,
    require_aware_datetime,
    require_enum,
    require_exact_date,
    require_nonblank_text,
    require_sha256,
    utc,
)


class PublicationBasis(Enum):
    UNKNOWN = "unknown"
    SOURCE_DOCUMENT_STATED = "source_document_stated"


@dataclass(frozen=True)
class TemporalProvenance:
    economic_date: Optional[date]
    retrieved_at: datetime
    capture_attempted_at: datetime
    publication_time: Optional[datetime] = None
    publication_basis: PublicationBasis = PublicationBasis.UNKNOWN
    publication_evidence_sha256: Optional[str] = None
    data_revision: Optional[str] = None

    def __post_init__(self) -> None:
        optional(require_exact_date, "economic_date", self.economic_date)
        require_aware_datetime("retrieved_at", self.retrieved_at)
        require_aware_datetime("capture_attempted_at", self.capture_attempted_at)
        optional(require_aware_datetime, "publication_time", self.publication_time)
        require_enum("publication_basis", self.publication_basis, PublicationBasis)
        optional(require_sha256, "publication_evidence_sha256", self.publication_evidence_sha256)
        optional(require_nonblank_text, "data_revision", self.data_revision)

        stated = self.publication_basis is PublicationBasis.SOURCE_DOCUMENT_STATED
        if stated != (self.publication_time is not None) or stated != (self.publication_evidence_sha256 is not None):
            raise ValueError("publication_time and publication_evidence_sha256 are required together with SOURCE_DOCUMENT_STATED, and forbidden otherwise (unknown stays unknown)")
        if utc(self.capture_attempted_at) > utc(self.retrieved_at):
            raise ValueError("capture_attempted_at must not be after retrieved_at")
        if self.publication_time is not None and utc(self.publication_time) > utc(self.retrieved_at):
            raise ValueError("publication_time must not be after retrieved_at")

    @property
    def system_known_at_utc(self) -> datetime:
        """The earliest instant at which Sentinax itself held this content: the retrieval instant, never the economic date."""
        return utc(self.retrieved_at)

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "economic_date": self.economic_date.isoformat() if self.economic_date else None,
            "retrieved_at": iso_utc(self.retrieved_at),
            "capture_attempted_at": iso_utc(self.capture_attempted_at),
            "publication_time": iso_utc(self.publication_time) if self.publication_time else None,
            "publication_basis": self.publication_basis.value,
            "publication_evidence_sha256": self.publication_evidence_sha256,
            "data_revision": self.data_revision,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.to_canonical_dict())
