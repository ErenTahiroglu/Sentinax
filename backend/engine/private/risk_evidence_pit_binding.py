"""
backend/engine/private/risk_evidence_pit_binding.py
===================================================
Fail-closed risk-evidence PIT binding for the Private Investment Decision Engine (Phase 15B.6).

Architectural Invariants:
    - Pure domain value object / PIT temporal binding primitive.
    - Zero clock calls (`datetime.now()`, `date.today()`), zero network, zero persistence.
    - Zero UUID generation, hashing, raw evidence storage, filesystem, database, or cache access.
    - Strict concrete-type validation:
      * `type(context) is RiskAxisContext`
      * `type(availability_ref) is RiskEvidenceAvailabilityRef`
    - Preserves both supplied objects by identity (`is`); does NOT store normalized UTC values.
    - Fail-closed UTC instant comparison:
      * Converts both `availability_ref.available_at` and `context.temporal_context.pit_context.knowledge_cutoff`
        to UTC independently.
      * Requires `available_at_utc <= knowledge_cutoff_utc`.
      * Raises `ValueError("risk evidence availability exceeds the analysis knowledge cutoff")` if lookahead detected.
      * Catches ordinary conversion/comparison exceptions and raises:
        `TypeError("risk evidence PIT instants must remain valid for UTC comparison")`.
    - Error messages use static string literals to guarantee callback safety under adversarial inputs.
    - Proves only temporal admissibility; carries NO claims of evidence presence, authenticity, completeness,
      sufficiency, verification, or suitability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone

from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence_availability import RiskEvidenceAvailabilityRef


@dataclass(frozen=True)
class RiskEvidencePITBinding:
    """
    Temporal binding proving that a RiskEvidenceAvailabilityRef is temporally admissible
    for a given RiskAxisContext analysis knowledge cutoff.
    """
    context: RiskAxisContext
    availability_ref: RiskEvidenceAvailabilityRef

    def __post_init__(self) -> None:
        if type(self.context) is not RiskAxisContext:
            raise TypeError("context must be an exact RiskAxisContext instance")

        if type(self.availability_ref) is not RiskEvidenceAvailabilityRef:
            raise TypeError("availability_ref must be an exact RiskEvidenceAvailabilityRef instance")

        try:
            available_at_utc = self.availability_ref.available_at.astimezone(timezone.utc)
            cutoff_utc = self.context.temporal_context.pit_context.knowledge_cutoff.astimezone(timezone.utc)
            is_valid = (available_at_utc <= cutoff_utc)
        except Exception as e:
            raise TypeError("risk evidence PIT instants must remain valid for UTC comparison") from e

        if not is_valid:
            raise ValueError("risk evidence availability exceeds the analysis knowledge cutoff")
