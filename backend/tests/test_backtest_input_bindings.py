"""
backend/tests/test_backtest_input_bindings.py
=============================================
Phase 26C2A: self-validating bindings of already-resolved historical inputs (candidate universe, macro fact, Game Changer revision family, risk evidence) to the
exact Phase 26C1 analysis context. Frontier ownership only: missing, unavailable, conflicting and incomplete inputs stay valid and explicit; no completeness
verdict, no fetching, no re-resolution.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from backend.engine.private import backtest_input_bindings as module_under_test
from backend.engine.private.allocation_candidate_universe import (
    CandidateUniverseQuery,
    CandidateUniverseResolution,
    CandidateUniverseResolutionStatus,
    resolve_candidate_universe,
)
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.backtest_analysis_context import PrivateBacktestAnalysisContext, build_private_backtest_analysis_context
from backend.engine.private.backtest_input_bindings import (
    PrivateBacktestCandidateUniverseInputBinding,
    PrivateBacktestGameChangerInputBinding,
    PrivateBacktestMacroInputBinding,
    PrivateBacktestRiskEvidenceInputBinding,
    bind_private_backtest_candidate_universe_input,
    bind_private_backtest_game_changer_input,
    bind_private_backtest_macro_input,
    bind_private_backtest_risk_evidence_input,
)
from backend.engine.private.backtest_replay_point import build_private_backtest_replay_point
from backend.engine.private.domain import AsOfMode, AssetClass, DataStatus, Horizon, RiskAxis, RiskEvidenceKind
from backend.engine.private.game_changer_revision_family import GameChangerRevisionFamilyCoverage, GameChangerRevisionFamilyResolution, GameChangerRevisionFamilyResolutionStatus
from backend.engine.private.macro.state_inputs import MacroStateInputFact
from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_availability import RiskEvidenceAvailabilityRef
from backend.engine.private.risk_evidence_content_match import RiskEvidenceContentMatch
from backend.engine.private.risk_evidence_kind_binding import RiskEvidenceKindBinding
from backend.engine.private.risk_evidence_pit_binding import RiskEvidencePITBinding
from backend.engine.private.risk_evidence_provenance import RiskEvidenceProvenanceRef
from backend.tests.invariants import static_guards as sg
from backend.tests.test_allocation_candidate_universe import SRC, UA, UNI, snap
from backend.tests.test_game_changer_revision_family import assessment as _gc_assessment
from backend.tests.test_game_changer_revision_family import resolve as gc_resolve
from backend.tests.test_macro_state_inputs import _fact as macro_fact

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
SO, SY = AsOfMode.SOURCE_AS_OF, AsOfMode.SYSTEM_AS_OF
CUTOFF = datetime(2026, 6, 30, 15, 0, tzinfo=PLUS3)            # 12:00 UTC
EVAL = date(2026, 6, 29)
CS = CandidateUniverseResolutionStatus


def analysis(mode=SY, cutoff=CUTOFF, evaluation_date=EVAL, horizon=Horizon.ALLOCATION_12M) -> PrivateBacktestAnalysisContext:
    point = build_private_backtest_replay_point(pit_context=AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff), evaluation_date=evaluation_date)
    return build_private_backtest_analysis_context(replay_point=point, horizon=horizon)


def equal_pit(context: PrivateBacktestAnalysisContext) -> AnalysisPITContext:
    return AnalysisPITContext(mode=context.pit_context.mode, knowledge_cutoff=context.pit_context.knowledge_cutoff)


def candidate(context, snapshots=(), evaluation_date=None, pit=None) -> CandidateUniverseResolution:
    query = CandidateUniverseQuery(source_key=SRC, universe_key=UNI, asset_class=AssetClass.EQUITY,
                                   evaluation_date=evaluation_date or context.replay_point.evaluation_date, pit_context=pit or context.pit_context)
    return resolve_candidate_universe(query=query, snapshots=tuple(snapshots))


def gc_assessment(key, *, context, **kw):
    return _gc_assessment(key, context=context, published=datetime(2026, 6, 1, tzinfo=UTC), observed=datetime(2026, 6, 2, tzinfo=UTC), **kw)


KNOWN = snap([UA], effective_from=date(2026, 1, 1), published=datetime(2026, 1, 1, tzinfo=UTC), observed=datetime(2026, 1, 2, tzinfo=UTC))


# --- shapes / signatures -------------------------------------------------------------------------------------------

BINDINGS = [
    (PrivateBacktestCandidateUniverseInputBinding, "resolution", bind_private_backtest_candidate_universe_input),
    (PrivateBacktestMacroInputBinding, "fact", bind_private_backtest_macro_input),
    (PrivateBacktestGameChangerInputBinding, "resolution", bind_private_backtest_game_changer_input),
    (PrivateBacktestRiskEvidenceInputBinding, "evidence", bind_private_backtest_risk_evidence_input),
]


@pytest.mark.parametrize("cls,field,builder", BINDINGS)
def test_dataclass_shape_and_builder_signature(cls, field, builder) -> None:
    fields = dataclasses.fields(cls)
    assert [f.name for f in fields] == ["analysis_context", field]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    assert cls.__dataclass_params__.frozen is True
    parameters = list(inspect.signature(builder).parameters.values())
    assert [p.name for p in parameters] == ["analysis_context", field]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)


def test_builders_reject_unrelated_keywords() -> None:
    context = analysis()
    resolution = candidate(context, (KNOWN,))
    for extra in ("replay_point", "pit_context", "evaluation_date", "knowledge_cutoff", "completeness"):
        with pytest.raises(TypeError):
            bind_private_backtest_candidate_universe_input(analysis_context=context, resolution=resolution, **{extra: 1})  # type: ignore[arg-type]


# --- candidate universe --------------------------------------------------------------------------------------------

def test_candidate_binding_preserves_identity_and_enforces_pit_object_and_date() -> None:
    context = analysis()
    resolution = candidate(context, (KNOWN,))
    binding = bind_private_backtest_candidate_universe_input(analysis_context=context, resolution=resolution)
    assert binding.analysis_context is context and binding.resolution is resolution and resolution.status is CS.SELECTED
    assert PrivateBacktestCandidateUniverseInputBinding(analysis_context=context, resolution=resolution) == binding
    foreign = candidate(context, (KNOWN,), pit=equal_pit(context))                     # equal by value, a different object
    assert foreign.query.pit_context == context.pit_context and foreign.query.pit_context is not context.pit_context
    with pytest.raises(ValueError):
        bind_private_backtest_candidate_universe_input(analysis_context=context, resolution=foreign)
    with pytest.raises(ValueError):
        PrivateBacktestCandidateUniverseInputBinding(analysis_context=context, resolution=foreign)
    for wrong in (date(2026, 6, 28), date(2026, 6, 30), context.replay_point.knowledge_cutoff.date()):
        with pytest.raises(ValueError):
            bind_private_backtest_candidate_universe_input(analysis_context=context, resolution=candidate(context, (KNOWN,), evaluation_date=wrong))


def test_candidate_missingness_and_conflict_statuses_are_not_reinterpreted() -> None:
    context = analysis()
    late = snap([UA], effective_from=date(2026, 1, 1), published=datetime(2026, 9, 1, tzinfo=UTC), observed=datetime(2026, 9, 2, tzinfo=UTC), sha="b" * 64)
    clash_a = snap([UA], effective_from=date(2026, 1, 1), published=datetime(2026, 1, 1, tzinfo=UTC), observed=datetime(2026, 1, 2, tzinfo=UTC), sha="c" * 64)
    clash_b = snap([UA, UA.__class__(int=9)], effective_from=date(2026, 1, 1), published=datetime(2026, 1, 1, tzinfo=UTC), observed=datetime(2026, 1, 2, tzinfo=UTC), sha="d" * 64)
    future_only = snap([UA], effective_from=date(2027, 1, 1), published=datetime(2026, 1, 1, tzinfo=UTC), observed=datetime(2026, 1, 2, tzinfo=UTC), sha="e" * 64)
    other_source = snap([UA], source="other-source")
    cases = {CS.SELECTED: (KNOWN,), CS.NO_SNAPSHOT_AS_OF: (late,), CS.FRONTIER_CONFLICT: (clash_a, clash_b), CS.NO_EFFECTIVE_SNAPSHOT: (future_only,),
             CS.NO_SOURCE_SNAPSHOT: (other_source,)}
    for status, snapshots in cases.items():
        resolution = candidate(context, snapshots)
        assert resolution.status is status
        assert bind_private_backtest_candidate_universe_input(analysis_context=context, resolution=resolution).resolution.status is status


# --- macro ---------------------------------------------------------------------------------------------------------

def macro_for(mode, as_of, **over) -> MacroStateInputFact:
    base = dict(mode=mode, as_of=as_of, published_at=as_of - timedelta(days=1), observed_at=as_of - timedelta(days=1), ingested_at=as_of - timedelta(days=1))
    base.update(over)
    return macro_fact(**base)


@pytest.mark.parametrize("mode", [SO, SY])
def test_macro_binding_both_modes_and_exact_instant(mode: AsOfMode) -> None:
    context = analysis(mode=mode)
    fact = macro_for(mode, datetime(2026, 6, 30, 12, 0, tzinfo=UTC))                 # equivalent offset to the 15:00+03:00 cutoff
    binding = bind_private_backtest_macro_input(analysis_context=context, fact=fact)
    assert binding.fact is fact and binding.analysis_context is context and fact.as_of is not context.replay_point.knowledge_cutoff
    assert PrivateBacktestMacroInputBinding(analysis_context=context, fact=fact).fact is fact
    same_instant_other_offset = macro_for(mode, datetime(2026, 6, 30, 15, 0, tzinfo=PLUS3))
    assert bind_private_backtest_macro_input(analysis_context=context, fact=same_instant_other_offset).fact is same_instant_other_offset


@pytest.mark.parametrize("mode", [SO, SY])
def test_macro_as_of_must_be_the_exact_replay_frontier(mode: AsOfMode) -> None:
    context = analysis(mode=mode)
    for delta in (timedelta(microseconds=1), -timedelta(microseconds=1), timedelta(days=-1), timedelta(days=1)):
        fact = macro_for(mode, datetime(2026, 6, 30, 12, 0, tzinfo=UTC) + delta, published_at=datetime(2026, 6, 1, tzinfo=UTC),
                         observed_at=datetime(2026, 6, 1, tzinfo=UTC), ingested_at=datetime(2026, 6, 1, tzinfo=UTC))
        with pytest.raises(ValueError):
            bind_private_backtest_macro_input(analysis_context=context, fact=fact)
        with pytest.raises(ValueError):
            PrivateBacktestMacroInputBinding(analysis_context=context, fact=fact)


def test_macro_mode_mismatch_is_rejected_at_the_same_instant() -> None:
    for replay_mode, fact_mode in ((SO, SY), (SY, SO)):
        with pytest.raises(ValueError):
            bind_private_backtest_macro_input(analysis_context=analysis(mode=replay_mode), fact=macro_for(fact_mode, datetime(2026, 6, 30, 12, 0, tzinfo=UTC)))


def test_macro_unavailable_fact_binds_and_future_effective_date_is_not_rejected() -> None:
    context = analysis()
    unavailable = macro_for(SY, datetime(2026, 6, 30, 12, 0, tzinfo=UTC), data_status=DataStatus.UNAVAILABLE, value=None)
    assert bind_private_backtest_macro_input(analysis_context=context, fact=unavailable).fact.value is None
    future_effective = macro_for(SY, datetime(2026, 6, 30, 12, 0, tzinfo=UTC), effective_date=date(2026, 12, 31))
    assert bind_private_backtest_macro_input(analysis_context=context, fact=future_effective).fact.effective_date > context.replay_point.evaluation_date
    old_effective = macro_for(SY, datetime(2026, 6, 30, 12, 0, tzinfo=UTC), effective_date=date(2020, 1, 1))
    assert bind_private_backtest_macro_input(analysis_context=context, fact=old_effective).fact is old_effective


# --- Game Changer --------------------------------------------------------------------------------------------------

def gc_context_pit(context: PrivateBacktestAnalysisContext):
    return context.pit_context


def test_game_changer_resolved_and_incomplete_families_bind_by_pit_object_identity() -> None:
    context = analysis(mode=SO)
    pit = context.pit_context
    complete = gc_resolve([gc_assessment("EVT-1", context=pit)])
    incomplete = gc_resolve([gc_assessment("EVT-1", context=pit)], coverage=GameChangerRevisionFamilyCoverage.INCOMPLETE_AT_CUTOFF)
    assert complete.status is GameChangerRevisionFamilyResolutionStatus.RESOLVED and incomplete.status is GameChangerRevisionFamilyResolutionStatus.INCOMPLETE_COVERAGE
    for resolution in (complete, incomplete):
        binding = bind_private_backtest_game_changer_input(analysis_context=context, resolution=resolution)
        assert binding.resolution is resolution and binding.analysis_context is context
        assert PrivateBacktestGameChangerInputBinding(analysis_context=context, resolution=resolution).resolution is resolution
    assert incomplete.active_assessment is None                                       # no active assessment is invented


def test_game_changer_foreign_pit_context_is_rejected() -> None:
    context = analysis(mode=SO)
    foreign = gc_resolve([gc_assessment("EVT-1", context=equal_pit(context))])
    assert foreign.family.assessments[0].binding.context == context.pit_context
    with pytest.raises(ValueError):
        bind_private_backtest_game_changer_input(analysis_context=context, resolution=foreign)
    with pytest.raises(ValueError):
        PrivateBacktestGameChangerInputBinding(analysis_context=context, resolution=foreign)


# --- risk evidence -------------------------------------------------------------------------------------------------

KIND = RiskEvidenceKind.CASH_BALANCE


def risk_context_for(context, temporal=None, axis=None) -> RiskAxisContext:
    return RiskAxisContext(axis=axis or KIND.axis, temporal_context=temporal or context.temporal_context)


def missing_binding(context, temporal=None) -> RiskEvidenceKindBinding:
    return RiskEvidenceKindBinding(kind=KIND, resolution=MissingRiskEvidence(context=risk_context_for(context, temporal), missing_inputs=("cash_balance",)))


def match_binding(context, temporal=None, content=b"evidence") -> RiskEvidenceKindBinding:
    import hashlib
    ref = RiskEvidenceAvailabilityRef(provenance_ref=RiskEvidenceProvenanceRef(source_key="test.source", content_sha256=hashlib.sha256(content).hexdigest()),
                                      available_at=datetime(2026, 6, 30, 11, 0, tzinfo=UTC))
    pit_binding = RiskEvidencePITBinding(context=risk_context_for(context, temporal), availability_ref=ref)
    return RiskEvidenceKindBinding(kind=KIND, resolution=RiskEvidenceContentMatch(pit_binding=pit_binding, content=content))


def foreign_temporal(context):
    from backend.engine.private.analysis_context import AnalysisTemporalContext
    from backend.engine.private.analysis_horizon import AnalysisHorizonContext
    temporal = AnalysisTemporalContext(horizon_context=AnalysisHorizonContext(horizon=context.horizon, as_of_date=context.replay_point.evaluation_date),
                                       pit_context=context.pit_context)
    assert temporal == context.temporal_context and temporal is not context.temporal_context
    return temporal


def test_risk_missing_and_content_match_branches_bind_with_the_exact_temporal_context() -> None:
    context = analysis()
    for evidence in (missing_binding(context), match_binding(context)):
        binding = bind_private_backtest_risk_evidence_input(analysis_context=context, evidence=evidence)
        assert binding.evidence is evidence and binding.analysis_context is context
        assert PrivateBacktestRiskEvidenceInputBinding(analysis_context=context, evidence=evidence).evidence is evidence
    assert "content" not in {f.name for f in dataclasses.fields(PrivateBacktestRiskEvidenceInputBinding)}


def test_risk_equal_value_foreign_temporal_context_is_rejected() -> None:
    context = analysis()
    for evidence in (missing_binding(context, foreign_temporal(context)), match_binding(context, foreign_temporal(context))):
        with pytest.raises(ValueError):
            bind_private_backtest_risk_evidence_input(analysis_context=context, evidence=evidence)
        with pytest.raises(ValueError):
            PrivateBacktestRiskEvidenceInputBinding(analysis_context=context, evidence=evidence)
    other = analysis(horizon=Horizon.TACTICAL_1M)                                   # same replay point values but a distinct context object
    with pytest.raises(ValueError):
        bind_private_backtest_risk_evidence_input(analysis_context=context, evidence=missing_binding(other))


# --- missingness is not failure / types ----------------------------------------------------------------------------

def test_all_missingness_states_coexist_as_bound_inputs() -> None:
    context = analysis()
    late = snap([UA], effective_from=date(2026, 1, 1), published=datetime(2026, 9, 1, tzinfo=UTC), observed=datetime(2026, 9, 2, tzinfo=UTC), sha="b" * 64)
    resolution = candidate(context, (late,))
    unavailable = macro_for(SY, datetime(2026, 6, 30, 12, 0, tzinfo=UTC), data_status=DataStatus.UNAVAILABLE, value=None)
    incomplete = gc_resolve([gc_assessment("EVT-1", context=context.pit_context)], coverage=GameChangerRevisionFamilyCoverage.INCOMPLETE_AT_CUTOFF)
    bound = [
        bind_private_backtest_candidate_universe_input(analysis_context=context, resolution=resolution),
        bind_private_backtest_macro_input(analysis_context=context, fact=unavailable),
        bind_private_backtest_game_changer_input(analysis_context=context, resolution=incomplete),
        bind_private_backtest_risk_evidence_input(analysis_context=context, evidence=missing_binding(context)),
    ]
    assert all(b.analysis_context is context for b in bound)
    assert resolution.status is CS.NO_SNAPSHOT_AS_OF and unavailable.data_status is DataStatus.UNAVAILABLE


def test_exact_types_everywhere() -> None:
    class SubContext(PrivateBacktestAnalysisContext):
        pass

    context = analysis()
    sub = SubContext(replay_point=context.replay_point, temporal_context=context.temporal_context)
    resolution = candidate(context, (KNOWN,))
    fact = macro_for(SY, datetime(2026, 6, 30, 12, 0, tzinfo=UTC))
    family = gc_resolve([gc_assessment("EVT-1", context=context.pit_context)])
    risk = missing_binding(context)
    cases = [(bind_private_backtest_candidate_universe_input, "resolution", resolution, PrivateBacktestCandidateUniverseInputBinding),
             (bind_private_backtest_macro_input, "fact", fact, PrivateBacktestMacroInputBinding),
             (bind_private_backtest_game_changer_input, "resolution", family, PrivateBacktestGameChangerInputBinding),
             (bind_private_backtest_risk_evidence_input, "evidence", risk, PrivateBacktestRiskEvidenceInputBinding)]
    for builder, field, good, cls in cases:
        for bad_context in (None, object(), sub, context.temporal_context, "ctx"):
            with pytest.raises(TypeError):
                builder(analysis_context=bad_context, **{field: good})
            with pytest.raises(TypeError):
                cls(analysis_context=bad_context, **{field: good})
        for bad_input in (None, object(), "x", context.temporal_context):
            with pytest.raises(TypeError):
                builder(analysis_context=context, **{field: bad_input})
            with pytest.raises(TypeError):
                cls(analysis_context=context, **{field: bad_input})
    class SubResolution(CandidateUniverseResolution):
        pass

    class SubFact(MacroStateInputFact):
        pass

    class SubFamily(GameChangerRevisionFamilyResolution):
        pass

    class SubRisk(RiskEvidenceKindBinding):
        pass

    for builder, field, good, sub_type in ((bind_private_backtest_candidate_universe_input, "resolution", resolution, SubResolution),
                                           (bind_private_backtest_macro_input, "fact", fact, SubFact),
                                           (bind_private_backtest_game_changer_input, "resolution", family, SubFamily),
                                           (bind_private_backtest_risk_evidence_input, "evidence", risk, SubRisk)):
        clone = object.__new__(sub_type)
        for f in dataclasses.fields(good):
            object.__setattr__(clone, f.name, getattr(good, f.name))
        with pytest.raises(TypeError):
            builder(analysis_context=context, **{field: clone})


# --- source-level invariants ---------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_input_bindings.py"


def test_module_is_deliberately_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_imports_are_only_the_required_domain_input_modules() -> None:
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules <= {"backend.engine.private.backtest_analysis_context", "backend.engine.private.allocation_candidate_universe",
                       "backend.engine.private.macro.state_inputs", "backend.engine.private.game_changer_revision_family",
                       "backend.engine.private.risk_evidence_kind_binding", "backend.engine.private.risk_evidence",
                       "backend.engine.private.risk_evidence_content_match"}
    for forbidden in ("portfolio", "market_data", "backtest_market_data_bridge", "allocation_user_view", "scheduler", "repository", "provider", "supabase",
                      "backtest_replay_plan"):
        assert not any(forbidden in m for m in modules), forbidden
    names = ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
             | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})
    assert not names & {"LedgerProjectionView", "Portfolio", "PortfolioTransaction", "MarketObservationResolutionResult", "MarketDataResolutionMode",
                        "UserReturnView", "UserReturnViewSet", "BayesianExpectedReturnPosterior"}


def test_no_completeness_clock_hash_random_io_loop_or_decision() -> None:
    names = ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
             | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})
    assert not names & {"now", "utcnow", "today", "time", "uuid4", "random", "secrets", "urandom", "sha256", "hashlib", "hmac", "open", "client", "rpc", "table",
                        "sorted", "COMPLETE", "INCOMPLETE", "READY", "NOT_READY", "missing_categories", "required_inputs", "rebalance", "optimizer"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.For, ast.While, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await, ast.Try))]


def test_documentation_states_what_is_and_is_not_proven() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PRIVATE_BACKTEST_INPUT_BINDINGS.md").read_text(encoding="utf-8")
    for needle in ("frontier", "completeness", "object identity", "exact as_of", "missing", "portfolio", "market data", "user view", "C2B", "C2C", "C2D", "C2E"):
        assert needle in doc, needle
