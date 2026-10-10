"""
backend/engine/learning/observation_status.py
=============================================
Captured-observation status for forward learning-data capture (Phase 28B-0).

The vocabulary deliberately has NO "missing", "absent" or zero member: NOT_OBSERVED_IN_RESPONSE only says a captured response did not contain the observation. It never
establishes an economic absence, a lifecycle event or a zero. Only OBSERVED_IN_RESPONSE carries a value (exact positive Decimal unit price). Origin (forward capture versus
retrospective rows inside the same response) is declared explicitly and never inferred. Non-authoritative for financial decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from backend.engine.learning._checks import (
    canonical_sha256,
    optional,
    require_canonical_token,
    require_enum,
    require_exact_date,
    require_exact_type,
    require_positive_finite_decimal,
    require_sha256,
    require_tuple_of,
    utc,
)
from backend.engine.learning.temporal_provenance import TemporalProvenance
from backend.engine.learning.universe_coverage import LearningSubjectRef, require_unambiguous_subjects


class ObservationStatus(Enum):
    OBSERVED_IN_RESPONSE = "observed_in_response"
    NOT_OBSERVED_IN_RESPONSE = "not_observed_in_response"
    EXPLICITLY_UNAVAILABLE_BY_SOURCE = "explicitly_unavailable_by_source"
    UNKNOWN = "unknown"
    RETRIEVAL_FAILED = "retrieval_failed"


class ObservationOrigin(Enum):
    FORWARD_CAPTURE = "forward_capture"
    RETROSPECTIVE_IN_RESPONSE = "retrospective_in_response"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CapturedObservation:
    subject: LearningSubjectRef
    economic_date: date
    status: ObservationStatus
    origin: ObservationOrigin
    provenance: TemporalProvenance
    observed_unit_price: Optional[Decimal] = None
    source_semantics_ref: Optional[str] = None
    failure_reason: Optional[str] = None
    evidence_sha256s: Tuple[str, ...] = ()

    # Absence from a captured response is never an economic or lifecycle finding.
    establishes_economic_absence = False
    establishes_lifecycle_event = False

    def __post_init__(self) -> None:
        require_exact_type("subject", self.subject, LearningSubjectRef)
        require_exact_date("economic_date", self.economic_date)
        require_enum("status", self.status, ObservationStatus)
        require_enum("origin", self.origin, ObservationOrigin)
        require_exact_type("provenance", self.provenance, TemporalProvenance)
        optional(require_canonical_token, "source_semantics_ref", self.source_semantics_ref)
        optional(require_canonical_token, "failure_reason", self.failure_reason)
        for h in require_tuple_of("evidence_sha256s", self.evidence_sha256s, str):
            require_sha256("evidence_sha256s item", h)

        if self.provenance.economic_date != self.economic_date:
            raise ValueError("economic_date must equal provenance.economic_date")
        if self.status is ObservationStatus.OBSERVED_IN_RESPONSE:
            if self.observed_unit_price is None:
                raise ValueError("OBSERVED_IN_RESPONSE requires observed_unit_price")
            require_positive_finite_decimal("observed_unit_price", self.observed_unit_price)
        elif self.observed_unit_price is not None:
            raise ValueError("only OBSERVED_IN_RESPONSE may carry a value; absence is never represented as zero or a price")
        if (self.status is ObservationStatus.EXPLICITLY_UNAVAILABLE_BY_SOURCE) != (self.source_semantics_ref is not None):
            raise ValueError("source_semantics_ref is required for, and only valid with, EXPLICITLY_UNAVAILABLE_BY_SOURCE")
        if (self.status is ObservationStatus.RETRIEVAL_FAILED) != (self.failure_reason is not None):
            raise ValueError("failure_reason is required for, and only valid with, RETRIEVAL_FAILED")
        if self.status is ObservationStatus.RETRIEVAL_FAILED and self.provenance.publication_time is not None:
            raise ValueError("a failed retrieval cannot carry publication evidence")

    def key(self) -> Tuple[str, date, object]:
        return (self.subject.fund_code, self.economic_date, utc(self.provenance.retrieved_at))

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject.to_canonical_dict(),
            "economic_date": self.economic_date.isoformat(),
            "status": self.status.value,
            "origin": self.origin.value,
            "provenance": self.provenance.to_canonical_dict(),
            "observed_unit_price": str(self.observed_unit_price) if self.observed_unit_price is not None else None,
            "source_semantics_ref": self.source_semantics_ref,
            "failure_reason": self.failure_reason,
            "evidence_sha256s": sorted(self.evidence_sha256s),
        }


@dataclass(frozen=True)
class CapturedObservationSet:
    observations: Tuple[CapturedObservation, ...]

    def __post_init__(self) -> None:
        require_tuple_of("observations", self.observations, CapturedObservation, unique=False)
        keys = [o.key() for o in self.observations]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate observation for the same fund_code, economic date and retrieval instant")
        require_unambiguous_subjects(tuple(dict.fromkeys(o.subject for o in self.observations)))

    def to_canonical_dict(self) -> Dict[str, Any]:
        rows = sorted((o.to_canonical_dict() for o in self.observations), key=lambda d: (d["subject"]["fund_code"], d["economic_date"], d["provenance"]["retrieved_at"]))
        return {"observations": rows}

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.to_canonical_dict())
