"""
backend/engine/private/risk_evidence_availability.py
====================================================
Risk-evidence knowledge-availability reference foundation for the Private Investment Decision Engine (Phase 15B.5).

Architectural Invariants:
    - Pure domain value object / temporal availability reference primitive.
    - Zero clock calls (`datetime.now()`, `date.today()`), zero network, zero persistence.
    - Zero UUID generation, hashing, raw evidence storage, filesystem, or database access.
    - Strict concrete-type validation:
      * `type(provenance_ref) is RiskEvidenceProvenanceRef`
      * `type(available_at) is datetime` (rejects subclasses and non-datetimes)
    - Strict timezone-aware validation:
      * `available_at` must be timezone-aware with an exact `timedelta` offset strictly inside +-24h.
      * Pre-validates `available_at.astimezone(timezone.utc)` at construction.
    - Preserves caller-supplied objects by identity (`is`) without normalization or caching.
    - Error messages use static string literals to guarantee callback safety under adversarial inputs.
    - Does NOT claim evidence presence, completeness, verification, suitability, or PIT eligibility.
    - Does not compare available_at with PIT cutoff or compose with MissingRiskEvidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backend.engine.private.risk_evidence_provenance import RiskEvidenceProvenanceRef


@dataclass(frozen=True)
class RiskEvidenceAvailabilityRef:
    """
    Temporal availability reference binding a RiskEvidenceProvenanceRef with an earliest knowable instant.
    """
    provenance_ref: RiskEvidenceProvenanceRef
    available_at: datetime

    def __post_init__(self) -> None:
        if type(self.provenance_ref) is not RiskEvidenceProvenanceRef:
            raise TypeError("provenance_ref must be an exact RiskEvidenceProvenanceRef instance")

        if type(self.available_at) is not datetime:
            raise TypeError("available_at must be an exact timezone-aware datetime with a valid UTC offset")

        if self.available_at.tzinfo is None:
            raise TypeError("available_at must be an exact timezone-aware datetime with a valid UTC offset")

        try:
            offset = self.available_at.utcoffset()
            if type(offset) is not timedelta:
                raise TypeError("available_at must be an exact timezone-aware datetime with a valid UTC offset")
            if not (-timedelta(hours=24) < offset < timedelta(hours=24)):
                raise TypeError("available_at must be an exact timezone-aware datetime with a valid UTC offset")
            self.available_at.astimezone(timezone.utc)
        except Exception as e:
            raise TypeError(
                "available_at must be an exact timezone-aware datetime with a valid UTC offset"
            ) from e
