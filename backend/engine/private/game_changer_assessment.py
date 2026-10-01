"""
backend/engine/private/game_changer_assessment.py
=================================================
Explicit structured Game Changer assessment over one PIT-admissible event binding (Phase 23B).

Phase 23A is evidence: what event existed and whether it was knowable at the cutoff. Phase 23B validates and preserves an
explicit, caller-supplied, versioned structured assessment of that exact event: decision-review materiality, review urgency,
the supplied thesis-impact judgement, the affected analytical dimensions, the materiality basis and the assessment methodology
and provenance identity. It reads no news text, KAP body, PDF, headline or model output and it does not classify, predict, infer
or derive anything: the assessment is categorical and entirely explicit.

Claim limits:

    - `GameChangerMateriality` is an internal Sentinax decision-review classification. It is not a legal conclusion: not a
      statement that information is material under IFRS, inside information under MAR, material under securities law or required
      to be disclosed.
    - There is no numeric materiality: no score, probability, percentage, ratio, monetary threshold, market-cap fraction, price reaction or
      weighted sum. Materiality is contextual; the vocabulary is ordinal words only and carries no value, threshold or weight.
    - `GameChangerMaterialityBasis` records only whether the upstream assessment relied on the nature of the event, its magnitude
      or both; it holds no magnitude.
    - Materiality, urgency and thesis impact are orthogonal explicit fields. None implies another (CRITICAL is not IMMEDIATE, not
      INVALIDATED; UNCHANGED is not LOW). The Phase 23A event type, the source tier and the revision kind never default or
      determine any of them; a lower-tier source is not a less important event. Systemic events get no automatic assessment.
    - Materiality is not data confidence: no confidence, freshness or coverage score exists here; source quality and event
      importance are separate axes.
    - Thesis impact is not an instruction: WEAKENED is not SELL, STRENGTHENED is not BUY, INVALIDATED is not liquidation. Urgency is
      a review priority only: it schedules, notifies, freezes and quarantines nothing.
    - `impact_dimensions` are affected analytical channels only (no direction, magnitude or weight) and are non-empty, unique and in
      enum declaration order (representation only, validated and never sorted). OTHER is the explicit "none of the named" value and is
      mutually exclusive: (OTHER,) is valid, OTHER together with any named dimension is rejected, never dropped or repaired.
      There is deliberately no portfolio, rebalance or position dimension.

Provenance: `methodology_key` is a strict lowercase version identity (it proves no methodology quality) and
`assessment_provenance_sha256` is an opaque upstream reference to the assessment artifact (64 lowercase hex, nothing is hashed
here). It is distinct from the event's `content_sha256`; neither is required to equal the other and neither proves correctness. No
free-form rationale lives in this core object: explainable detail belongs to the upstream audit artifact the hash references.

Binding: the assessment requires the exact `GameChangerEventPITBinding` (never a raw event), retained by identity. Temporal
admissibility (SOURCE_AS_OF / SYSTEM_AS_OF) remains solely the Phase 23A authority and is not re-implemented here.

Revision lineage is NOT resolved here. A correction or withdrawal event may itself be assessed. Phase 23C MUST NOT naively
aggregate all historical assessments: before any quarantine or thesis-review gate it must resolve which revision-family state is
active at the applicable PIT cutoff, otherwise a withdrawn or corrected CRITICAL assessment could block new capital forever.

Out of scope: no sentiment, no LLM runtime, no quarantine or allocation consequence, no BUY / SELL / HOLD mapping, no expected-return
effect, no provider or KAP access, no scheduler.

Architectural Invariants:
    - Pure domain module: standard library plus the Phase 23A binding type. No arithmetic, numeric type, clock, randomness, network,
      database or persistence. Exact concrete types: subclasses and raw strings are rejected.
    - The assessment re-validates its complete contract on direct construction; the builder is keyword-only with no defaults.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from backend.engine.private.game_changer_event import GameChangerEventPITBinding

_METHODOLOGY_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

_ERR_BINDING = "binding must be an exact GameChangerEventPITBinding instance"
_ERR_MATERIALITY = "materiality must be an exact GameChangerMateriality instance"
_ERR_URGENCY = "urgency must be an exact GameChangerUrgency instance"
_ERR_THESIS = "thesis_impact must be an exact GameChangerThesisImpact instance"
_ERR_BASIS = "materiality_basis must be an exact GameChangerMaterialityBasis instance"
_ERR_DIMENSIONS_TYPE = "impact_dimensions must be a tuple of exact GameChangerImpactDimension instances"
_ERR_DIMENSIONS_VALUE = "impact_dimensions must be non-empty, unique and in canonical enum declaration sequence"
_ERR_DIMENSIONS_OTHER = "OTHER is exclusive: it must be the only impact dimension when present"
_ERR_KEY_TYPE = "methodology_key must be an exact str instance"
_ERR_KEY_VALUE = "methodology_key must be a canonical lowercase identifier of at most 128 characters"
_ERR_SHA_TYPE = "assessment_provenance_sha256 must be an exact str instance"
_ERR_SHA_VALUE = "assessment_provenance_sha256 must be exactly 64 lowercase hexadecimal characters"


class GameChangerMateriality(Enum):
    """Internal decision-review materiality vocabulary; ordinal words only, no value, threshold or weight."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class GameChangerUrgency(Enum):
    """Review priority only; schedules, notifies and freezes nothing."""
    ROUTINE = "routine"
    PROMPT = "prompt"
    IMMEDIATE = "immediate"


class GameChangerThesisImpact(Enum):
    """Explicitly supplied judgement about the investment thesis; never a BUY / SELL / HOLD instruction."""
    UNCHANGED = "unchanged"
    STRENGTHENED = "strengthened"
    WEAKENED = "weakened"
    INVALIDATED = "invalidated"
    UNCERTAIN = "uncertain"


class GameChangerMaterialityBasis(Enum):
    """What kind of consideration the upstream assessment relied on; holds no magnitude."""
    NATURE = "nature"
    MAGNITUDE = "magnitude"
    NATURE_AND_MAGNITUDE = "nature_and_magnitude"


class GameChangerImpactDimension(Enum):
    """Affected analytical channels only; no direction, magnitude, weight or portfolio consequence."""
    EARNINGS = "earnings"
    CASH_FLOW = "cash_flow"
    BALANCE_SHEET = "balance_sheet"
    VALUATION = "valuation"
    OPERATIONS = "operations"
    FINANCING_LIQUIDITY = "financing_liquidity"
    GOVERNANCE = "governance"
    LEGAL_REGULATORY = "legal_regulatory"
    OWNERSHIP_CONTROL = "ownership_control"
    CAPITAL_STRUCTURE = "capital_structure"
    MACRO_EXPOSURE = "macro_exposure"
    OTHER = "other"


_DECLARATION_POSITION = {member: position for position, member in enumerate(GameChangerImpactDimension)}


@dataclass(frozen=True)
class GameChangerMaterialityAssessment:
    """Explicit structured assessment of one PIT-admissible event; retains the binding by identity."""
    binding: GameChangerEventPITBinding
    materiality: GameChangerMateriality
    urgency: GameChangerUrgency
    thesis_impact: GameChangerThesisImpact
    impact_dimensions: tuple[GameChangerImpactDimension, ...]
    materiality_basis: GameChangerMaterialityBasis
    methodology_key: str
    assessment_provenance_sha256: str

    def __post_init__(self) -> None:
        if type(self.binding) is not GameChangerEventPITBinding:
            raise TypeError(_ERR_BINDING)
        if type(self.materiality) is not GameChangerMateriality:
            raise TypeError(_ERR_MATERIALITY)
        if type(self.urgency) is not GameChangerUrgency:
            raise TypeError(_ERR_URGENCY)
        if type(self.thesis_impact) is not GameChangerThesisImpact:
            raise TypeError(_ERR_THESIS)
        if type(self.impact_dimensions) is not tuple or any(type(d) is not GameChangerImpactDimension for d in self.impact_dimensions):
            raise TypeError(_ERR_DIMENSIONS_TYPE)
        positions = [_DECLARATION_POSITION[d] for d in self.impact_dimensions]
        if not positions or any(left >= right for left, right in zip(positions, positions[1:])):
            raise ValueError(_ERR_DIMENSIONS_VALUE)
        if GameChangerImpactDimension.OTHER in self.impact_dimensions and self.impact_dimensions != (GameChangerImpactDimension.OTHER,):
            raise ValueError(_ERR_DIMENSIONS_OTHER)
        if type(self.materiality_basis) is not GameChangerMaterialityBasis:
            raise TypeError(_ERR_BASIS)
        if type(self.methodology_key) is not str:
            raise TypeError(_ERR_KEY_TYPE)
        if _METHODOLOGY_KEY.fullmatch(self.methodology_key) is None:
            raise ValueError(_ERR_KEY_VALUE)
        if type(self.assessment_provenance_sha256) is not str:
            raise TypeError(_ERR_SHA_TYPE)
        if _SHA256.fullmatch(self.assessment_provenance_sha256) is None:
            raise ValueError(_ERR_SHA_VALUE)


def build_game_changer_materiality_assessment(
    *,
    binding: GameChangerEventPITBinding,
    materiality: GameChangerMateriality,
    urgency: GameChangerUrgency,
    thesis_impact: GameChangerThesisImpact,
    impact_dimensions: tuple[GameChangerImpactDimension, ...],
    materiality_basis: GameChangerMaterialityBasis,
    methodology_key: str,
    assessment_provenance_sha256: str,
) -> GameChangerMaterialityAssessment:
    """Validate and preserve an explicit structured assessment; nothing is classified, derived or defaulted."""
    return GameChangerMaterialityAssessment(
        binding=binding,
        materiality=materiality,
        urgency=urgency,
        thesis_impact=thesis_impact,
        impact_dimensions=impact_dimensions,
        materiality_basis=materiality_basis,
        methodology_key=methodology_key,
        assessment_provenance_sha256=assessment_provenance_sha256,
    )
