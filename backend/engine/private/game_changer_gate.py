"""
backend/engine/private/game_changer_gate.py
===========================================
Deterministic Game Changer thesis-review gate over one closed Phase 23C1 resolution (Phase 23C2).

Chain: 23A event evidence -> 23B explicit assessment -> 23C1 active revision authority -> 23C2 decision-support gate. The gate
answers two things only: does this revision-family state require human thesis review, and, for an instrument-scoped family, should
Sentinax pause NEW capital to those exact instruments. It is decision support and not a recommendation.

Product rule: quarantine means pause NEW capital to the explicitly affected instrument ids plus thesis review. The existing holdings are
untouched: no sell, no exit or liquidation, no target-weight mutation (no zero target, no weight delta), no rebalance or optimizer
call, no order, no execution, no persistence, no notification and no scheduler. OPEN means only that this gate imposes no new-capital
quarantine; it is not BUY, not approval and not a statement that the instrument is safe or attractive.

Policy (explicit architecture authority, categorical only: no score, probability, confidence or arithmetic; no policy inputs):

    Incomplete revision coverage (status INCOMPLETE_COVERAGE, no active assessment, no assessment field is read):
        review REQUIRED, reasons (REVISION_COVERAGE_INCOMPLETE,)
        instrument scope  -> PAUSED_PENDING_EVIDENCE (fail closed until the active revision state can be established)
        systemic scope    -> NOT_APPLICABLE_SYSTEMIC

    Resolved (status RESOLVED): ONLY the active terminal assessment is inspected; earlier family members are historical provenance
    and never leak into the gate (a CRITICAL original corrected to LOW is OPEN; a LOW original corrected to CRITICAL / WEAKENED is
    QUARANTINED; a terminal withdrawal is judged by its own assessment, the revision kind drives nothing). Review reasons, in enum
    declaration order: MATERIALITY_HIGH_OR_CRITICAL (materiality HIGH or CRITICAL), URGENCY_PROMPT_OR_IMMEDIATE (urgency PROMPT or
    IMMEDIATE), then one of THESIS_WEAKENED / THESIS_INVALIDATED / THESIS_UNCERTAIN (UNCHANGED and STRENGTHENED add none).
    No reasons -> NOT_REQUIRED, otherwise REQUIRED.

    Instrument new-capital gate (resolved, instrument scope) is QUARANTINED if
        A. the active thesis impact is INVALIDATED (any materiality: an invalidated thesis gets no new capital before reassessment), or
        B. the active materiality is CRITICAL and the thesis impact is WEAKENED or UNCERTAIN;
    otherwise OPEN. Urgency affects review only and materiality alone never quarantines (CRITICAL with UNCHANGED or STRENGTHENED is
    OPEN with review REQUIRED).

Systemic safety rule: a SYSTEMIC event carries no instrument ids and is NEVER read as "all instruments", the whole portfolio, all
sleeves or all candidate universes. Systemic families may require review but their gate is always NOT_APPLICABLE_SYSTEMIC and
`affected_instrument_ids` is (). Any macro or tactical consumption of systemic events is a separate, later layer.

`affected_instrument_ids` come only from the closed resolution authority (the active event, or the family anchor whose identity Phase
23C1 proved common to every member); they are never caller supplied, rebuilt or sorted here.

Claim limit: given one closed Phase 23C1 resolution, this module derives whether human thesis review is required and, for instrument-
scoped families, whether to temporarily pause new capital to those exact instruments. It does not claim that an asset should be sold,
that holdings should be reduced, that the event proves a loss, that Sentinax recommends buying, or that a systemic event blocks the
portfolio.

Architectural Invariants:
    - Pure domain module: standard library plus the closed Phase 23A / 23B / 23C1 types. No loop over the historical family, no
      search for a latest or most severe member, no sorting, no arithmetic, no clock, network, database or persistence.
    - One private derivation is shared by the builder and the constructor, so no forged gate can be constructed directly; the
      resolution is retained by identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from backend.engine.private.game_changer_assessment import (
    GameChangerMateriality,
    GameChangerThesisImpact,
    GameChangerUrgency,
)
from backend.engine.private.game_changer_event import GameChangerEventScope
from backend.engine.private.game_changer_revision_family import (
    GameChangerRevisionFamilyResolution,
    GameChangerRevisionFamilyResolutionStatus,
)

_ERR_RESOLUTION_TYPE = "resolution must be an exact GameChangerRevisionFamilyResolution instance"
_ERR_STATE_TYPE = "review_state must be an exact GameChangerReviewState instance"
_ERR_REASONS_TYPE = "review_reasons must be a tuple of exact GameChangerReviewReason instances"
_ERR_GATE_TYPE = "instrument_new_capital_gate must be an exact GameChangerInstrumentNewCapitalGate instance"
_ERR_IDS_TYPE = "affected_instrument_ids must be a tuple of exact UUID instances"
_ERR_MATCH = "gate must match the canonical gate derived from its resolution exactly"


class GameChangerReviewState(Enum):
    """Decision-support review state only; schedules and sends nothing."""
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"


class GameChangerInstrumentNewCapitalGate(Enum):
    """Pause of NEW capital to the exact affected instruments; never a sale, exit or target-weight change."""
    OPEN = "open"
    PAUSED_PENDING_EVIDENCE = "paused_pending_evidence"
    QUARANTINED = "quarantined"
    NOT_APPLICABLE_SYSTEMIC = "not_applicable_systemic"


class GameChangerReviewReason(Enum):
    """Explanation dimensions in canonical declaration order; no score and no priority weight."""
    REVISION_COVERAGE_INCOMPLETE = "revision_coverage_incomplete"
    MATERIALITY_HIGH_OR_CRITICAL = "materiality_high_or_critical"
    URGENCY_PROMPT_OR_IMMEDIATE = "urgency_prompt_or_immediate"
    THESIS_WEAKENED = "thesis_weakened"
    THESIS_INVALIDATED = "thesis_invalidated"
    THESIS_UNCERTAIN = "thesis_uncertain"


_THESIS_REASON = {
    GameChangerThesisImpact.WEAKENED: GameChangerReviewReason.THESIS_WEAKENED,
    GameChangerThesisImpact.INVALIDATED: GameChangerReviewReason.THESIS_INVALIDATED,
    GameChangerThesisImpact.UNCERTAIN: GameChangerReviewReason.THESIS_UNCERTAIN,
}
_HEAVY_MATERIALITY = frozenset({GameChangerMateriality.HIGH, GameChangerMateriality.CRITICAL})
_REVIEW_URGENCY = frozenset({GameChangerUrgency.PROMPT, GameChangerUrgency.IMMEDIATE})
_CRITICAL_ADVERSE_THESIS = frozenset({GameChangerThesisImpact.WEAKENED, GameChangerThesisImpact.UNCERTAIN})


def _derive(
    resolution: GameChangerRevisionFamilyResolution,
) -> tuple[GameChangerReviewState, tuple[GameChangerReviewReason, ...], GameChangerInstrumentNewCapitalGate, tuple[UUID, ...]]:
    """Single canonical gate derivation shared by the builder and the constructor verification."""
    reasons: list[GameChangerReviewReason] = []
    if resolution.status is GameChangerRevisionFamilyResolutionStatus.RESOLVED:
        active = resolution.active_assessment
        event = active.binding.event
        if active.materiality in _HEAVY_MATERIALITY:
            reasons.append(GameChangerReviewReason.MATERIALITY_HIGH_OR_CRITICAL)
        if active.urgency in _REVIEW_URGENCY:
            reasons.append(GameChangerReviewReason.URGENCY_PROMPT_OR_IMMEDIATE)
        if active.thesis_impact in _THESIS_REASON:
            reasons.append(_THESIS_REASON[active.thesis_impact])
        quarantine = active.thesis_impact is GameChangerThesisImpact.INVALIDATED or (
            active.materiality is GameChangerMateriality.CRITICAL and active.thesis_impact in _CRITICAL_ADVERSE_THESIS
        )
        instrument_gate = (
            GameChangerInstrumentNewCapitalGate.QUARANTINED if quarantine else GameChangerInstrumentNewCapitalGate.OPEN
        )
    else:
        event = resolution.family.assessments[0].binding.event
        reasons.append(GameChangerReviewReason.REVISION_COVERAGE_INCOMPLETE)
        instrument_gate = GameChangerInstrumentNewCapitalGate.PAUSED_PENDING_EVIDENCE
    if event.scope is GameChangerEventScope.SYSTEMIC:
        instrument_gate = GameChangerInstrumentNewCapitalGate.NOT_APPLICABLE_SYSTEMIC
    state = GameChangerReviewState.REQUIRED if reasons else GameChangerReviewState.NOT_REQUIRED
    return (state, tuple(reasons), instrument_gate, event.instrument_ids)


@dataclass(frozen=True)
class GameChangerDecisionGate:
    """Canonical review / new-capital gate retaining its Phase 23C1 resolution by identity."""
    resolution: GameChangerRevisionFamilyResolution
    review_state: GameChangerReviewState
    review_reasons: tuple[GameChangerReviewReason, ...]
    instrument_new_capital_gate: GameChangerInstrumentNewCapitalGate
    affected_instrument_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if type(self.resolution) is not GameChangerRevisionFamilyResolution:
            raise TypeError(_ERR_RESOLUTION_TYPE)
        if type(self.review_state) is not GameChangerReviewState:
            raise TypeError(_ERR_STATE_TYPE)
        if type(self.review_reasons) is not tuple or not set(map(type, self.review_reasons)) <= {GameChangerReviewReason}:
            raise TypeError(_ERR_REASONS_TYPE)
        if type(self.instrument_new_capital_gate) is not GameChangerInstrumentNewCapitalGate:
            raise TypeError(_ERR_GATE_TYPE)
        if type(self.affected_instrument_ids) is not tuple or not set(map(type, self.affected_instrument_ids)) <= {UUID}:
            raise TypeError(_ERR_IDS_TYPE)
        expected = _derive(self.resolution)
        supplied = (self.review_state, self.review_reasons, self.instrument_new_capital_gate, self.affected_instrument_ids)
        if supplied != expected:
            raise ValueError(_ERR_MATCH)


def build_game_changer_decision_gate(
    *,
    resolution: GameChangerRevisionFamilyResolution,
) -> GameChangerDecisionGate:
    """Derive the review state and instrument new-capital gate from one closed Phase 23C1 resolution; no policy inputs."""
    if type(resolution) is not GameChangerRevisionFamilyResolution:
        raise TypeError(_ERR_RESOLUTION_TYPE)
    state, reasons, instrument_gate, ids = _derive(resolution)
    return GameChangerDecisionGate(
        resolution=resolution,
        review_state=state,
        review_reasons=reasons,
        instrument_new_capital_gate=instrument_gate,
        affected_instrument_ids=ids,
    )
