"""
backend/engine/private/risk_evidence_kind_binding.py
====================================================
Declared risk-evidence kind binding for the Private Investment Decision Engine (Phase 15C.2).

Architectural Invariants:
    - Pure domain value object / axis-compatibility primitive.
    - Zero clock calls, zero network, zero persistence, zero UUID generation, zero hashing,
      zero filesystem, database, or cache access.
    - Stores exactly two fields: `kind` and `resolution`. The risk-axis context is DERIVED from the
      resolution branch (never stored, copied, or reconstructed).
    - Strict concrete-type validation, in this order:
      1. `type(kind) is RiskEvidenceKind`
      2. `type(resolution) is MissingRiskEvidence or RiskEvidenceContentMatch` (no subclasses)
      3. derive the existing context:
         * MissingRiskEvidence      -> `resolution.context`
         * RiskEvidenceContentMatch -> `resolution.pit_binding.context`
      4. `kind.axis is context.axis` (identity)
    - Metadata-only `RiskEvidencePITBinding` is rejected: the 15B.9 content stage must be completed first.
    - Preserves supplied objects by identity (`is`). Retains no raw content.
    - Error messages use static string literals to guarantee callback safety under adversarial inputs.
    - Means ONLY: "the caller declared this evidence resolution to belong to this kind, and that kind is
      compatible with the resolution's risk axis."
    - Does NOT decode content, validate payload schema, or verify that the bytes conform to the declared
      kind. Same-axis kind mislabeling is NOT detected here; a future canonical envelope/decoder must
      reject it.
    - Missing remains explicit absence. `missing_inputs` remains diagnostic metadata, unrelated to `kind`.
    - No score, level, weight, suitability, required risk, overall risk, or owner binding.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.domain import RiskEvidenceKind
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_content_match import RiskEvidenceContentMatch
from backend.engine.private.risk_evidence_resolution import RiskEvidenceContentResolution


@dataclass(frozen=True)
class RiskEvidenceKindBinding:
    """
    Declared kind bound to an explicit-missing or digest-matched PIT-admissible evidence resolution.
    """
    kind: RiskEvidenceKind
    resolution: RiskEvidenceContentResolution

    def __post_init__(self) -> None:
        if type(self.kind) is not RiskEvidenceKind:
            raise TypeError("kind must be an exact RiskEvidenceKind instance")

        resolution_type = type(self.resolution)
        if resolution_type is MissingRiskEvidence:
            context = self.resolution.context
        elif resolution_type is RiskEvidenceContentMatch:
            context = self.resolution.pit_binding.context
        else:
            raise TypeError(
                "resolution must be an exact MissingRiskEvidence or RiskEvidenceContentMatch instance"
            )

        if self.kind.axis is not context.axis:
            raise ValueError("risk evidence kind axis must match resolution context axis")
