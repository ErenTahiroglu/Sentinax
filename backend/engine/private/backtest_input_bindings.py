"""
backend/engine/private/backtest_input_bindings.py
=================================================
Self-validating bindings of already-resolved historical inputs to one Phase 26C1 analysis context (Phase 26C2A). Four existing input authorities are supported:
the candidate-universe resolution, the macro state input fact, the Game Changer revision-family resolution and the risk-evidence kind binding. Each binding
stores the exact `PrivateBacktestAnalysisContext` and the exact input (no copy, no rebuilding) and proves frontier ownership only:

    candidate universe   query.pit_context IS the analysis PIT object, and query.evaluation_date == replay evaluation_date
    macro fact           fact.mode IS the PIT mode, and fact.as_of is exactly the replay knowledge-cutoff instant (UTC instant equality, equivalent offsets are
                         valid; neither earlier nor later; no ordering of the fact's effective date)
    Game Changer family  the family's assessment PIT context IS the analysis PIT object (the closed family validator makes it one object for the whole family)
    risk evidence        the RiskAxisContext's temporal context (derived from the missing or digest-matched branch) IS the analysis temporal context

Missingness is preserved, never judged: NO_SNAPSHOT_AS_OF and the other candidate statuses, an UNAVAILABLE macro fact, INCOMPLETE_COVERAGE and missing risk
evidence are all valid bound inputs. There is no completeness or readiness verdict, no cross-category relation, no fetching or re-resolution, and no portfolio,
market-data or user-view binding (their own later checkpoints). Non-pure by classification only (it composes several closed domain subgraphs); it performs no
I/O, clock, hashing, randomness, loop or decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone

from backend.engine.private.allocation_candidate_universe import CandidateUniverseResolution
from backend.engine.private.backtest_analysis_context import PrivateBacktestAnalysisContext
from backend.engine.private.game_changer_revision_family import GameChangerRevisionFamilyResolution
from backend.engine.private.macro.state_inputs import MacroStateInputFact
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_content_match import RiskEvidenceContentMatch
from backend.engine.private.risk_evidence_kind_binding import RiskEvidenceKindBinding

_ERR_CONTEXT = "analysis_context must be an exact PrivateBacktestAnalysisContext instance"
_ERR_RESOLUTION = "resolution must be an exact CandidateUniverseResolution instance"
_ERR_CANDIDATE_PIT = "candidate universe query PIT context must be the very analysis PIT context object"
_ERR_CANDIDATE_DATE = "candidate universe query evaluation_date must equal the replay evaluation_date"
_ERR_FACT = "fact must be an exact MacroStateInputFact instance"
_ERR_MACRO_MODE = "macro fact mode must be the analysis PIT mode"
_ERR_MACRO_AS_OF = "macro fact as_of must be exactly the replay knowledge cutoff instant"
_ERR_FAMILY = "resolution must be an exact GameChangerRevisionFamilyResolution instance"
_ERR_FAMILY_PIT = "Game Changer family PIT context must be the very analysis PIT context object"
_ERR_EVIDENCE = "evidence must be an exact RiskEvidenceKindBinding instance"
_ERR_EVIDENCE_BRANCH = "risk evidence resolution must be an exact MissingRiskEvidence or RiskEvidenceContentMatch instance"
_ERR_RISK_TEMPORAL = "risk context temporal context must be the very analysis temporal context object"


def _check_context(analysis_context: object) -> None:
    if type(analysis_context) is not PrivateBacktestAnalysisContext:
        raise TypeError(_ERR_CONTEXT)


@dataclass(frozen=True)
class PrivateBacktestCandidateUniverseInputBinding:
    """A candidate-universe resolution proven to belong to the replay frontier; every canonical status stays valid."""
    analysis_context: PrivateBacktestAnalysisContext
    resolution: CandidateUniverseResolution

    def __post_init__(self) -> None:
        _check_context(self.analysis_context)
        if type(self.resolution) is not CandidateUniverseResolution:
            raise TypeError(_ERR_RESOLUTION)
        if self.resolution.query.pit_context is not self.analysis_context.pit_context:
            raise ValueError(_ERR_CANDIDATE_PIT)
        if self.resolution.query.evaluation_date != self.analysis_context.replay_point.evaluation_date:
            raise ValueError(_ERR_CANDIDATE_DATE)


@dataclass(frozen=True)
class PrivateBacktestMacroInputBinding:
    """A macro state input fact proven to sit exactly at the replay knowledge frontier and PIT mode; an UNAVAILABLE fact stays valid."""
    analysis_context: PrivateBacktestAnalysisContext
    fact: MacroStateInputFact

    def __post_init__(self) -> None:
        _check_context(self.analysis_context)
        if type(self.fact) is not MacroStateInputFact:
            raise TypeError(_ERR_FACT)
        if self.fact.mode is not self.analysis_context.pit_context.mode:
            raise ValueError(_ERR_MACRO_MODE)
        if self.fact.as_of.astimezone(timezone.utc) != self.analysis_context.replay_point.knowledge_cutoff_utc:
            raise ValueError(_ERR_MACRO_AS_OF)


@dataclass(frozen=True)
class PrivateBacktestGameChangerInputBinding:
    """A Game Changer revision-family resolution at the replay PIT frontier; INCOMPLETE_COVERAGE stays valid and invents no active assessment."""
    analysis_context: PrivateBacktestAnalysisContext
    resolution: GameChangerRevisionFamilyResolution

    def __post_init__(self) -> None:
        _check_context(self.analysis_context)
        if type(self.resolution) is not GameChangerRevisionFamilyResolution:
            raise TypeError(_ERR_FAMILY)
        if self.resolution.family.assessments[0].binding.context is not self.analysis_context.pit_context:
            raise ValueError(_ERR_FAMILY_PIT)


@dataclass(frozen=True)
class PrivateBacktestRiskEvidenceInputBinding:
    """Risk evidence (explicitly missing or digest-matched) whose risk context carries the exact analysis temporal context."""
    analysis_context: PrivateBacktestAnalysisContext
    evidence: RiskEvidenceKindBinding

    def __post_init__(self) -> None:
        _check_context(self.analysis_context)
        if type(self.evidence) is not RiskEvidenceKindBinding:
            raise TypeError(_ERR_EVIDENCE)
        resolution_type = type(self.evidence.resolution)
        if resolution_type is MissingRiskEvidence:
            risk_context = self.evidence.resolution.context
        elif resolution_type is RiskEvidenceContentMatch:
            risk_context = self.evidence.resolution.pit_binding.context
        else:
            raise TypeError(_ERR_EVIDENCE_BRANCH)
        if risk_context.temporal_context is not self.analysis_context.temporal_context:
            raise ValueError(_ERR_RISK_TEMPORAL)


def bind_private_backtest_candidate_universe_input(*, analysis_context: PrivateBacktestAnalysisContext,
                                                   resolution: CandidateUniverseResolution) -> PrivateBacktestCandidateUniverseInputBinding:
    return PrivateBacktestCandidateUniverseInputBinding(analysis_context=analysis_context, resolution=resolution)


def bind_private_backtest_macro_input(*, analysis_context: PrivateBacktestAnalysisContext, fact: MacroStateInputFact) -> PrivateBacktestMacroInputBinding:
    return PrivateBacktestMacroInputBinding(analysis_context=analysis_context, fact=fact)


def bind_private_backtest_game_changer_input(*, analysis_context: PrivateBacktestAnalysisContext,
                                             resolution: GameChangerRevisionFamilyResolution) -> PrivateBacktestGameChangerInputBinding:
    return PrivateBacktestGameChangerInputBinding(analysis_context=analysis_context, resolution=resolution)


def bind_private_backtest_risk_evidence_input(*, analysis_context: PrivateBacktestAnalysisContext,
                                              evidence: RiskEvidenceKindBinding) -> PrivateBacktestRiskEvidenceInputBinding:
    return PrivateBacktestRiskEvidenceInputBinding(analysis_context=analysis_context, evidence=evidence)
