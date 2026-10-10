"""
backend/engine/learning/universe_coverage.py
============================================
Universe coverage evidence for learning-data capture (Phase 28B-0).

Coverage vocabulary: CURATED_PILOT, OBSERVED_LIST, SOURCE_CLAIMED_COMPLETE, UNKNOWN.
SOURCE_CLAIMED_COMPLETE requires an explicit `CompletenessAttestation` that cites a stored source document; it never arises from HTTP success, successful parsing or a
non-empty list (the `observed` factory can only produce OBSERVED_LIST). This is evidence about a captured list, NOT a candidate-selection, investment-universe or
membership authority; Phase 22 candidate snapshots are unrelated and untouched.

Identity: `fund_code` is a diagnostic provider token; `canonical_instrument_id` is the canonical identity when bound. Ambiguous code/UUID pairings are rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from backend.engine.learning._checks import (
    canonical_sha256,
    optional,
    require_canonical_token,
    require_enum,
    require_exact_type,
    require_nonblank_text,
    require_sha256,
    require_tuple_of,
)
from backend.engine.learning.temporal_provenance import TemporalProvenance

_FUND_CODE = re.compile(r"^[A-Z0-9]{2,12}$")


class CoverageClaim(Enum):
    CURATED_PILOT = "curated_pilot"
    OBSERVED_LIST = "observed_list"
    SOURCE_CLAIMED_COMPLETE = "source_claimed_complete"
    UNKNOWN = "unknown"


class SubjectIdentityStatus(Enum):
    FUND_CODE_ONLY = "fund_code_only"
    CANONICAL_BOUND = "canonical_bound"


@dataclass(frozen=True)
class LearningSubjectRef:
    fund_code: str
    canonical_instrument_id: Optional[UUID] = None

    def __post_init__(self) -> None:
        require_exact_type("fund_code", self.fund_code, str)
        if not _FUND_CODE.match(self.fund_code):
            raise ValueError(f"fund_code must be 2-12 uppercase letters/digits, got {self.fund_code!r}")
        if self.canonical_instrument_id is not None:
            require_exact_type("canonical_instrument_id", self.canonical_instrument_id, UUID)

    @property
    def identity_status(self) -> SubjectIdentityStatus:
        return SubjectIdentityStatus.CANONICAL_BOUND if self.canonical_instrument_id is not None else SubjectIdentityStatus.FUND_CODE_ONLY

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {"fund_code": self.fund_code, "canonical_instrument_id": str(self.canonical_instrument_id) if self.canonical_instrument_id else None}


def require_unambiguous_subjects(subjects: Tuple[LearningSubjectRef, ...]) -> None:
    """Each fund_code maps to at most one subject form, and each canonical UUID to at most one fund_code."""
    by_code: Dict[str, LearningSubjectRef] = {}
    by_uuid: Dict[UUID, str] = {}
    for s in subjects:
        prior = by_code.get(s.fund_code)
        if prior is not None and prior != s:
            raise ValueError(f"fund_code {s.fund_code!r} is bound inconsistently (different canonical identity or unbound vs bound)")
        by_code[s.fund_code] = s
        if s.canonical_instrument_id is not None:
            other = by_uuid.get(s.canonical_instrument_id)
            if other is not None and other != s.fund_code:
                raise ValueError(f"canonical instrument {s.canonical_instrument_id} is paired with two fund codes: {other!r}, {s.fund_code!r}")
            by_uuid[s.canonical_instrument_id] = s.fund_code


@dataclass(frozen=True)
class CompletenessAttestation:
    """Explicit upstream evidence that the source itself claims the list is complete: cites the stored source document and where it says so."""
    source_id: str
    source_content_sha256: str
    claim_reference: str

    def __post_init__(self) -> None:
        require_canonical_token("source_id", self.source_id)
        require_sha256("source_content_sha256", self.source_content_sha256)
        require_nonblank_text("claim_reference", self.claim_reference)

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {"source_id": self.source_id, "source_content_sha256": self.source_content_sha256, "claim_reference": self.claim_reference}


@dataclass(frozen=True)
class UniverseSnapshotEvidence:
    universe_key: str
    source_id: str
    coverage: CoverageClaim
    members: Tuple[LearningSubjectRef, ...]
    provenance: TemporalProvenance
    completeness_attestation: Optional[CompletenessAttestation] = None
    selection_rule_ref: Optional[str] = None

    economic_scope_note = "Learning-evidence snapshot only; not a candidate-selection, investment-universe or membership authority."

    def __post_init__(self) -> None:
        require_canonical_token("universe_key", self.universe_key)
        require_canonical_token("source_id", self.source_id)
        require_enum("coverage", self.coverage, CoverageClaim)
        require_tuple_of("members", self.members, LearningSubjectRef)
        require_exact_type("provenance", self.provenance, TemporalProvenance)
        optional(_require_attestation, "completeness_attestation", self.completeness_attestation)
        optional(require_canonical_token, "selection_rule_ref", self.selection_rule_ref)
        require_unambiguous_subjects(self.members)

        complete = self.coverage is CoverageClaim.SOURCE_CLAIMED_COMPLETE
        pilot = self.coverage is CoverageClaim.CURATED_PILOT
        if complete != (self.completeness_attestation is not None):
            raise ValueError("completeness_attestation is required for, and only valid with, SOURCE_CLAIMED_COMPLETE")
        if pilot != (self.selection_rule_ref is not None):
            raise ValueError("selection_rule_ref is required for, and only valid with, CURATED_PILOT")
        if (complete or pilot) and not self.members:
            raise ValueError("SOURCE_CLAIMED_COMPLETE and CURATED_PILOT snapshots must list members")

    @classmethod
    def observed(cls, *, universe_key: str, source_id: str, members: Tuple[LearningSubjectRef, ...], provenance: TemporalProvenance) -> "UniverseSnapshotEvidence":
        """A parsed list is only ever an OBSERVED_LIST; completeness cannot be asserted through this path."""
        return cls(universe_key=universe_key, source_id=source_id, coverage=CoverageClaim.OBSERVED_LIST, members=members, provenance=provenance)

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "universe_key": self.universe_key,
            "source_id": self.source_id,
            "coverage": self.coverage.value,
            "members": sorted((m.to_canonical_dict() for m in self.members), key=lambda d: d["fund_code"]),
            "provenance": self.provenance.to_canonical_dict(),
            "completeness_attestation": self.completeness_attestation.to_canonical_dict() if self.completeness_attestation else None,
            "selection_rule_ref": self.selection_rule_ref,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.to_canonical_dict())


def _require_attestation(name: str, value: object) -> CompletenessAttestation:
    require_exact_type(name, value, CompletenessAttestation)
    return value  # type: ignore[return-value]
