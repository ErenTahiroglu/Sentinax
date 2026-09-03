"""
backend/engine/private/risk_evidence_resolution.py
==================================================
Exclusive risk-evidence reference resolution for the Private Investment Decision Engine (Phase 15B.7).

Architectural Invariants:
    - Pure functional resolver / exclusive reference branching primitive.
    - Zero clock calls (`datetime.now()`, `date.today()`), zero network, zero persistence.
    - Zero UUID generation, hashing, raw evidence storage, filesystem, database, or cache access.
    - Strict concrete-type validation:
      * `type(context) is RiskAxisContext`
      * `availability_ref is None or type(availability_ref) is RiskEvidenceAvailabilityRef`
    - Exclusive two-branch contract:
      * Exactly one branch must be supplied:
        - Reference branch: `availability_ref is not None and missing_inputs is None`
        - Missing branch: `availability_ref is None and missing_inputs is not None`
      * Both branches supplied -> ValueError("exactly one risk evidence reference resolution branch must be supplied")
      * Neither branch supplied -> ValueError("exactly one risk evidence reference resolution branch must be supplied")
    - Propagates underlying primitive validation errors without catching, translating, or weakening:
      * Reference branch delegates directly to `RiskEvidencePITBinding(context=context, availability_ref=availability_ref)`
      * Missing branch delegates directly to `MissingRiskEvidence(context=context, missing_inputs=missing_inputs)`
    - Preserves caller-supplied objects by identity (`is`).
    - Error messages use static string literals to guarantee callback safety under adversarial inputs.
    - Does NOT convert missing evidence to zero/default/neutral. Does NOT promote reference metadata to verified/present evidence.
"""

from __future__ import annotations

from typing import Union

from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_availability import RiskEvidenceAvailabilityRef
from backend.engine.private.risk_evidence_pit_binding import RiskEvidencePITBinding

RiskEvidenceReferenceResolution = Union[MissingRiskEvidence, RiskEvidencePITBinding]


def resolve_risk_evidence_reference(
    *,
    context: RiskAxisContext,
    availability_ref: RiskEvidenceAvailabilityRef | None,
    missing_inputs: tuple[str, ...] | None,
) -> RiskEvidenceReferenceResolution:
    """
    Pure resolver forcing callers to choose exactly one explicit branch:
    1. Explicit missing evidence (MissingRiskEvidence); or
    2. A PIT-admissible evidence reference (RiskEvidencePITBinding).
    """
    if type(context) is not RiskAxisContext:
        raise TypeError("context must be an exact RiskAxisContext instance")

    if availability_ref is not None and type(availability_ref) is not RiskEvidenceAvailabilityRef:
        raise TypeError("availability_ref must be None or an exact RiskEvidenceAvailabilityRef instance")

    has_ref = availability_ref is not None
    has_missing = missing_inputs is not None

    if has_ref == has_missing:
        # Either both are supplied or neither is supplied
        raise ValueError("exactly one risk evidence reference resolution branch must be supplied")

    if has_ref:
        return RiskEvidencePITBinding(context=context, availability_ref=availability_ref)  # type: ignore[arg-type]

    return MissingRiskEvidence(context=context, missing_inputs=missing_inputs)  # type: ignore[arg-type]
