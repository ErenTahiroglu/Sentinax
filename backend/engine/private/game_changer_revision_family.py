"""
backend/engine/private/game_changer_revision_family.py
======================================================
Revision-family completeness and active-assessment resolution at one PIT frontier (Phase 23C1).

Phase 23A stores explicit revision lineage and Phase 23B assesses exactly one bound event; neither decides which assessment is
currently authoritative inside a revision family. This module answers only: given a caller-supplied family at one PIT frontier, is
coverage explicitly complete, and if so which exact Phase 23B assessment is the active terminal one? It decides nothing about
quarantine, new capital, alerts, allocation or trades (23C2 and later; no quarantine here), and it performs no discovery, storage
access, provider / KAP access or LLM call.

Authority rules:

    one PIT frontier   every assessment's binding context is the SAME OBJECT (identity, not equal values); PIT admissibility is not
                       re-run, Phase 23A remains its sole authority
    one family         identical source_key, source_tier, scope and instrument_ids across members; event_type, effective / published /
                       observed times, hashes, methodology and every assessed field may legitimately change between revisions
    unique keys        every source_event_key is unique inside a family
    coverage           COMPLETE_AT_CUTOFF is an explicit caller assertion that the supplied tuple is the full known chain at the cutoff;
                       INCOMPLETE_AT_CUTOFF states completeness cannot be proven; coverage is never inferred;
                       `coverage_provenance_sha256` is an opaque 64-lowercase-hex reference to the artifact behind that assertion
                       (nothing is hashed here, it proves no completeness and need not equal any event or assessment hash)

Resolution:

    INCOMPLETE_AT_CUTOFF   status INCOMPLETE_COVERAGE, active assessment None. The evidence tuple is preserved but may lack lineage
                           members (even a missing parent); no best-effort choice of the last, latest or most severe member exists.
    COMPLETE_AT_CUTOFF     the tuple itself must be one canonical root -> leaf linear chain in lineage order: first member an
                           ORIGINAL with no parent; every later member revises exactly the previous member's source_event_key and is
                           not an ORIGINAL. A missing root or parent, a branch, a skipped predecessor, a second ORIGINAL or a non-lineage
                           order is rejected: no branch resolution, no sorting, no repair, no transitive guessing.
                           status RESOLVED and the active assessment is `assessments[-1]` by object identity.

The terminal revision is active; earlier assessments are historical provenance only. The resolver must not aggregate: a CRITICAL
original followed by a LOW correction resolves to the correction alone, and a withdrawal is not a magic None: when the withdrawal
event is the terminal member its own assessment is the active one (the original is not revived and no concern is cleared). There is
no timestamp winner and no materiality, urgency or thesis-impact winner: revision edges are the only lineage authority, and the
supplied order is the caller's canonical representation.

Claim limit: given a non-empty caller-supplied assessment family at one exact PIT context and an explicit coverage assertion, this
module fails closed on incomplete coverage and, for a proven-complete unambiguous linear chain, identifies the terminal Phase 23B
assessment as the active revision-state assessment. It does not claim the provider family is objectively complete, that the latest
disclosure was independently discovered, that an assessment is correct or that any portfolio action is warranted.

Architectural Invariants:
    - Pure domain module: standard library plus the Phase 23A / 23B types. No clock, arithmetic, randomness, network, database or
      persistence; exact concrete types (subclasses and raw strings rejected).
    - One private validator is shared by the family and resolution constructors and the public function, so no forged family,
      status or active pair can be constructed directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from backend.engine.private.game_changer_assessment import GameChangerMaterialityAssessment
from backend.engine.private.game_changer_event import GameChangerRevisionKind

_SHA256 = re.compile(r"[0-9a-f]{64}")

_ERR_ASSESSMENTS_TYPE = "assessments must be a tuple of exact GameChangerMaterialityAssessment instances"
_ERR_ASSESSMENTS_EMPTY = "assessments must not be empty"
_ERR_COVERAGE_TYPE = "coverage must be an exact GameChangerRevisionFamilyCoverage instance"
_ERR_SHA_TYPE = "coverage_provenance_sha256 must be an exact str instance"
_ERR_SHA_VALUE = "coverage_provenance_sha256 must be exactly 64 lowercase hexadecimal characters"
_ERR_FRONTIER = "every assessment must share the same AnalysisPITContext object"
_ERR_IDENTITY = "family members must share source_key, source_tier, scope and instrument_ids"
_ERR_DUPLICATE_KEY = "source_event_key must be unique inside a revision family"
_ERR_CHAIN = "a complete family must be one linear root-to-leaf revision chain in lineage order"
_ERR_FAMILY_TYPE = "family must be an exact GameChangerRevisionFamily instance"
_ERR_STATUS_TYPE = "status must be an exact GameChangerRevisionFamilyResolutionStatus instance"
_ERR_ACTIVE_TYPE = "active_assessment must be None or an exact GameChangerMaterialityAssessment instance"
_ERR_RESOLUTION = "resolution must match the canonical family resolution exactly"


class GameChangerRevisionFamilyCoverage(Enum):
    COMPLETE_AT_CUTOFF = "complete_at_cutoff"
    INCOMPLETE_AT_CUTOFF = "incomplete_at_cutoff"


class GameChangerRevisionFamilyResolutionStatus(Enum):
    RESOLVED = "resolved"
    INCOMPLETE_COVERAGE = "incomplete_coverage"


def _validate_family(
    assessments: object,
    coverage: object,
    coverage_provenance_sha256: object,
) -> None:
    """Single canonical family validation shared by every public construction path."""
    if type(assessments) is not tuple or any(type(a) is not GameChangerMaterialityAssessment for a in assessments):
        raise TypeError(_ERR_ASSESSMENTS_TYPE)
    if not assessments:
        raise ValueError(_ERR_ASSESSMENTS_EMPTY)
    if type(coverage) is not GameChangerRevisionFamilyCoverage:
        raise TypeError(_ERR_COVERAGE_TYPE)
    if type(coverage_provenance_sha256) is not str:
        raise TypeError(_ERR_SHA_TYPE)
    if _SHA256.fullmatch(coverage_provenance_sha256) is None:
        raise ValueError(_ERR_SHA_VALUE)
    events = [a.binding.event for a in assessments]
    first = assessments[0]
    if any(a.binding.context is not first.binding.context for a in assessments):
        raise ValueError(_ERR_FRONTIER)
    anchor = events[0]
    if any((e.source_key, e.source_tier, e.scope, e.instrument_ids) != (anchor.source_key, anchor.source_tier, anchor.scope, anchor.instrument_ids)
           for e in events):
        raise ValueError(_ERR_IDENTITY)
    keys = [e.source_event_key for e in events]
    if len(set(keys)) != len(keys):
        raise ValueError(_ERR_DUPLICATE_KEY)
    if coverage is GameChangerRevisionFamilyCoverage.COMPLETE_AT_CUTOFF:
        if anchor.revision_kind is not GameChangerRevisionKind.ORIGINAL or anchor.revises_source_event_key is not None:
            raise ValueError(_ERR_CHAIN)
        for parent, child in zip(events, events[1:]):
            if child.revision_kind is GameChangerRevisionKind.ORIGINAL or child.revises_source_event_key != parent.source_event_key:
                raise ValueError(_ERR_CHAIN)


@dataclass(frozen=True)
class GameChangerRevisionFamily:
    """Caller-supplied revision family at one PIT frontier with an explicit coverage assertion; the tuple is kept as supplied."""
    assessments: tuple[GameChangerMaterialityAssessment, ...]
    coverage: GameChangerRevisionFamilyCoverage
    coverage_provenance_sha256: str

    def __post_init__(self) -> None:
        _validate_family(self.assessments, self.coverage, self.coverage_provenance_sha256)


def _canonical_resolution(
    family: GameChangerRevisionFamily,
) -> tuple[GameChangerRevisionFamilyResolutionStatus, GameChangerMaterialityAssessment | None]:
    if family.coverage is GameChangerRevisionFamilyCoverage.COMPLETE_AT_CUTOFF:
        return (GameChangerRevisionFamilyResolutionStatus.RESOLVED, family.assessments[-1])
    return (GameChangerRevisionFamilyResolutionStatus.INCOMPLETE_COVERAGE, None)


@dataclass(frozen=True)
class GameChangerRevisionFamilyResolution:
    """Resolution of a family: the terminal assessment when coverage is complete, otherwise no active assessment."""
    family: GameChangerRevisionFamily
    status: GameChangerRevisionFamilyResolutionStatus
    active_assessment: GameChangerMaterialityAssessment | None

    def __post_init__(self) -> None:
        if type(self.family) is not GameChangerRevisionFamily:
            raise TypeError(_ERR_FAMILY_TYPE)
        if type(self.status) is not GameChangerRevisionFamilyResolutionStatus:
            raise TypeError(_ERR_STATUS_TYPE)
        if self.active_assessment is not None and type(self.active_assessment) is not GameChangerMaterialityAssessment:
            raise TypeError(_ERR_ACTIVE_TYPE)
        status, active = _canonical_resolution(self.family)
        if self.status is not status or self.active_assessment is not active:
            raise ValueError(_ERR_RESOLUTION)


def resolve_game_changer_revision_family(
    *,
    assessments: tuple[GameChangerMaterialityAssessment, ...],
    coverage: GameChangerRevisionFamilyCoverage,
    coverage_provenance_sha256: str,
) -> GameChangerRevisionFamilyResolution:
    """Fail closed on incomplete coverage; for a complete linear chain the terminal assessment is the active one."""
    family = GameChangerRevisionFamily(
        assessments=assessments, coverage=coverage, coverage_provenance_sha256=coverage_provenance_sha256,
    )
    status, active = _canonical_resolution(family)
    return GameChangerRevisionFamilyResolution(family=family, status=status, active_assessment=active)
