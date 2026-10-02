"""
backend/tests/test_game_changer_gate.py
=======================================
Phase 23C2: deterministic thesis-review gate over one closed Phase 23C1 resolution. Quarantine means pausing NEW capital to the exact
affected instruments plus human thesis review: never a sale, a target-weight change or an execution. Systemic events never become a
portfolio-wide block.
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import game_changer_gate as module_under_test
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, SourceTier
from backend.engine.private.game_changer_assessment import (
    GameChangerImpactDimension,
    GameChangerMaterialityAssessment,
    GameChangerMaterialityBasis,
    GameChangerMateriality,
    GameChangerThesisImpact,
    GameChangerUrgency,
    build_game_changer_materiality_assessment,
)
from backend.engine.private.game_changer_event import (
    GameChangerEvent,
    GameChangerEventScope,
    GameChangerEventType,
    GameChangerRevisionKind,
    bind_game_changer_event_pit,
)
from backend.engine.private.game_changer_gate import (
    GameChangerDecisionGate,
    GameChangerInstrumentNewCapitalGate,
    GameChangerReviewReason,
    GameChangerReviewState,
    build_game_changer_decision_gate,
)
from backend.engine.private.game_changer_revision_family import (
    GameChangerRevisionFamilyCoverage,
    GameChangerRevisionFamilyResolution,
    resolve_game_changer_revision_family,
)
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
M, U, TI, RK = GameChangerMateriality, GameChangerUrgency, GameChangerThesisImpact, GameChangerRevisionKind
R, G, S = GameChangerReviewReason, GameChangerInstrumentNewCapitalGate, GameChangerReviewState
COMPLETE, INCOMPLETE = GameChangerRevisionFamilyCoverage.COMPLETE_AT_CUTOFF, GameChangerRevisionFamilyCoverage.INCOMPLETE_AT_CUTOFF
UA, UB = UUID(int=1), UUID(int=2)
CONTEXT = AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=datetime(2026, 9, 30, tzinfo=UTC))


def assess(key="A", kind=RK.ORIGINAL, revises=None, *, materiality=M.LOW, urgency=U.ROUTINE, thesis=TI.UNCHANGED, systemic=False,
           ids=(UA,)) -> GameChangerMaterialityAssessment:
    event = GameChangerEvent(
        source_key="kap", source_event_key=key, source_tier=SourceTier.TIER_1_REGULATORY,
        scope=GameChangerEventScope.SYSTEMIC if systemic else GameChangerEventScope.INSTRUMENT,
        event_type=GameChangerEventType.MACRO_SHOCK if systemic else GameChangerEventType.OPERATIONS,
        instrument_ids=() if systemic else tuple(ids), effective_date=None, published_at=datetime(2026, 9, 2, 1, tzinfo=UTC),
        observed_at=datetime(2026, 9, 2, 2, tzinfo=UTC), content_sha256="a" * 64, revision_kind=kind, revises_source_event_key=revises)
    return build_game_changer_materiality_assessment(
        binding=bind_game_changer_event_pit(event=event, context=CONTEXT), materiality=materiality, urgency=urgency, thesis_impact=thesis,
        impact_dimensions=(GameChangerImpactDimension.EARNINGS,), materiality_basis=GameChangerMaterialityBasis.NATURE,
        methodology_key="game_changer.materiality.v1", assessment_provenance_sha256="b" * 64)


def resolution(assessments, coverage=COMPLETE) -> GameChangerRevisionFamilyResolution:
    return resolve_game_changer_revision_family(assessments=tuple(assessments), coverage=coverage, coverage_provenance_sha256="c" * 64)


def gate(assessments, coverage=COMPLETE) -> GameChangerDecisionGate:
    return build_game_changer_decision_gate(resolution=resolution(assessments, coverage))


def single(**kwargs) -> GameChangerDecisionGate:
    return gate([assess(**kwargs)])


# --- enums / stored fields ---------------------------------------------------------------------------------------

def test_enums_and_stored_fields() -> None:
    assert [(m.name, m.value) for m in S] == [("NOT_REQUIRED", "not_required"), ("REQUIRED", "required")]
    assert [(m.name, m.value) for m in G] == [("OPEN", "open"), ("PAUSED_PENDING_EVIDENCE", "paused_pending_evidence"),
                                              ("QUARANTINED", "quarantined"), ("NOT_APPLICABLE_SYSTEMIC", "not_applicable_systemic")]
    assert [(m.name, m.value) for m in R] == [
        ("REVISION_COVERAGE_INCOMPLETE", "revision_coverage_incomplete"), ("MATERIALITY_HIGH_OR_CRITICAL", "materiality_high_or_critical"),
        ("URGENCY_PROMPT_OR_IMMEDIATE", "urgency_prompt_or_immediate"), ("THESIS_WEAKENED", "thesis_weakened"),
        ("THESIS_INVALIDATED", "thesis_invalidated"), ("THESIS_UNCERTAIN", "thesis_uncertain")]
    assert [f.name for f in dataclasses.fields(GameChangerDecisionGate)] == [
        "resolution", "review_state", "review_reasons", "instrument_new_capital_gate", "affected_instrument_ids"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in dataclasses.fields(GameChangerDecisionGate))
    result = single()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.review_state = S.REQUIRED  # type: ignore[misc]


def test_builder_has_exactly_one_keyword_only_input() -> None:
    res = resolution([assess()])
    with pytest.raises(TypeError):
        build_game_changer_decision_gate(res)  # type: ignore[misc]
    with pytest.raises(TypeError):
        build_game_changer_decision_gate(resolution=res, policy="x")  # type: ignore[call-arg]
    for bad in (None, object(), res.family, res.active_assessment):
        with pytest.raises(TypeError):
            build_game_changer_decision_gate(resolution=bad)  # type: ignore[arg-type]

    class Sub(GameChangerRevisionFamilyResolution):
        pass

    with pytest.raises(TypeError):
        build_game_changer_decision_gate(resolution=Sub(family=res.family, status=res.status, active_assessment=res.active_assessment))


def test_resolution_is_retained_by_identity() -> None:
    res = resolution([assess()])
    assert build_game_changer_decision_gate(resolution=res).resolution is res


# --- incomplete coverage -----------------------------------------------------------------------------------------

def test_incomplete_instrument_family_pauses_new_capital_without_reading_assessments() -> None:
    result = gate([assess(materiality=M.CRITICAL, thesis=TI.INVALIDATED, ids=(UA, UB))], INCOMPLETE)
    assert result.resolution.active_assessment is None
    assert result.review_state is S.REQUIRED and result.review_reasons == (R.REVISION_COVERAGE_INCOMPLETE,)
    assert result.instrument_new_capital_gate is G.PAUSED_PENDING_EVIDENCE and result.affected_instrument_ids == (UA, UB)
    benign = gate([assess(materiality=M.LOW, thesis=TI.UNCHANGED)], INCOMPLETE)
    assert (benign.review_reasons, benign.instrument_new_capital_gate) == (result.review_reasons, G.PAUSED_PENDING_EVIDENCE)   # fields are not read


def test_incomplete_systemic_family_requires_review_without_a_portfolio_block() -> None:
    result = gate([assess(systemic=True, materiality=M.CRITICAL, thesis=TI.INVALIDATED)], INCOMPLETE)
    assert result.review_state is S.REQUIRED and result.review_reasons == (R.REVISION_COVERAGE_INCOMPLETE,)
    assert result.instrument_new_capital_gate is G.NOT_APPLICABLE_SYSTEMIC and result.affected_instrument_ids == ()


def test_incomplete_family_with_missing_lineage_is_a_pause_not_an_error() -> None:
    orphan = assess("B", RK.CORRECTION, "MISSING-PARENT", materiality=M.CRITICAL)
    result = gate([orphan], INCOMPLETE)
    assert result.instrument_new_capital_gate is G.PAUSED_PENDING_EVIDENCE


# --- policy matrix -----------------------------------------------------------------------------------------------

def test_named_policy_matrix() -> None:
    cases = [
        (M.LOW, TI.UNCHANGED, U.ROUTINE, S.NOT_REQUIRED, (), G.OPEN),
        (M.LOW, TI.INVALIDATED, U.ROUTINE, S.REQUIRED, (R.THESIS_INVALIDATED,), G.QUARANTINED),
        (M.LOW, TI.UNCHANGED, U.IMMEDIATE, S.REQUIRED, (R.URGENCY_PROMPT_OR_IMMEDIATE,), G.OPEN),
        (M.HIGH, TI.WEAKENED, U.ROUTINE, S.REQUIRED, (R.MATERIALITY_HIGH_OR_CRITICAL, R.THESIS_WEAKENED), G.OPEN),
        (M.CRITICAL, TI.UNCHANGED, U.ROUTINE, S.REQUIRED, (R.MATERIALITY_HIGH_OR_CRITICAL,), G.OPEN),
        (M.CRITICAL, TI.STRENGTHENED, U.ROUTINE, S.REQUIRED, (R.MATERIALITY_HIGH_OR_CRITICAL,), G.OPEN),
        (M.CRITICAL, TI.WEAKENED, U.ROUTINE, S.REQUIRED, (R.MATERIALITY_HIGH_OR_CRITICAL, R.THESIS_WEAKENED), G.QUARANTINED),
        (M.CRITICAL, TI.UNCERTAIN, U.ROUTINE, S.REQUIRED, (R.MATERIALITY_HIGH_OR_CRITICAL, R.THESIS_UNCERTAIN), G.QUARANTINED),
        (M.HIGH, TI.UNCERTAIN, U.ROUTINE, S.REQUIRED, (R.MATERIALITY_HIGH_OR_CRITICAL, R.THESIS_UNCERTAIN), G.OPEN),
        (M.MEDIUM, TI.WEAKENED, U.ROUTINE, S.REQUIRED, (R.THESIS_WEAKENED,), G.OPEN),
        (M.LOW, TI.WEAKENED, U.ROUTINE, S.REQUIRED, (R.THESIS_WEAKENED,), G.OPEN),
        (M.LOW, TI.STRENGTHENED, U.ROUTINE, S.NOT_REQUIRED, (), G.OPEN),
        (M.CRITICAL, TI.INVALIDATED, U.IMMEDIATE, S.REQUIRED,
         (R.MATERIALITY_HIGH_OR_CRITICAL, R.URGENCY_PROMPT_OR_IMMEDIATE, R.THESIS_INVALIDATED), G.QUARANTINED),
        (M.MEDIUM, TI.UNCHANGED, U.PROMPT, S.REQUIRED, (R.URGENCY_PROMPT_OR_IMMEDIATE,), G.OPEN),
    ]
    for materiality, thesis, urgency, state, reasons, new_capital in cases:
        result = single(materiality=materiality, thesis=thesis, urgency=urgency, ids=(UA, UB))
        assert (result.review_state, result.review_reasons, result.instrument_new_capital_gate) == (state, reasons, new_capital), \
            (materiality, thesis, urgency)
        assert result.affected_instrument_ids == (UA, UB)


def test_exhaustive_materiality_by_thesis_matrix_for_routine_urgency() -> None:
    quarantined = {(m, TI.INVALIDATED) for m in M} | {(M.CRITICAL, TI.WEAKENED), (M.CRITICAL, TI.UNCERTAIN)}
    for materiality, thesis in itertools.product(M, TI):
        result = single(materiality=materiality, thesis=thesis)
        assert (result.instrument_new_capital_gate is G.QUARANTINED) == ((materiality, thesis) in quarantined), (materiality, thesis)
        expected = []
        if materiality in (M.HIGH, M.CRITICAL):
            expected.append(R.MATERIALITY_HIGH_OR_CRITICAL)
        expected.extend({TI.WEAKENED: [R.THESIS_WEAKENED], TI.INVALIDATED: [R.THESIS_INVALIDATED], TI.UNCERTAIN: [R.THESIS_UNCERTAIN]}.get(thesis, []))
        assert result.review_reasons == tuple(expected)
        assert result.review_state is (S.REQUIRED if expected else S.NOT_REQUIRED)
        assert result.instrument_new_capital_gate in (G.OPEN, G.QUARANTINED)


def test_urgency_changes_review_but_never_the_new_capital_gate() -> None:
    expectations = {U.ROUTINE: (S.NOT_REQUIRED, ()), U.PROMPT: (S.REQUIRED, (R.URGENCY_PROMPT_OR_IMMEDIATE,)),
                    U.IMMEDIATE: (S.REQUIRED, (R.URGENCY_PROMPT_OR_IMMEDIATE,))}
    for urgency, (state, reasons) in expectations.items():
        result = single(materiality=M.LOW, thesis=TI.UNCHANGED, urgency=urgency)
        assert (result.review_state, result.review_reasons, result.instrument_new_capital_gate) == (state, reasons, G.OPEN)
    for thesis, materiality in itertools.product(TI, M):
        gates = {single(materiality=materiality, thesis=thesis, urgency=u).instrument_new_capital_gate for u in U}
        assert len(gates) == 1


def test_materiality_alone_never_quarantines() -> None:
    for thesis in (TI.UNCHANGED, TI.STRENGTHENED):
        for urgency in U:
            assert single(materiality=M.CRITICAL, thesis=thesis, urgency=urgency).instrument_new_capital_gate is G.OPEN


# --- historical isolation / revision kinds -----------------------------------------------------------------------

def test_historical_critical_assessment_corrected_away_does_not_leak() -> None:
    a = assess("A", materiality=M.CRITICAL, thesis=TI.WEAKENED, urgency=U.IMMEDIATE)
    b = assess("B", RK.CORRECTION, "A", materiality=M.LOW, thesis=TI.UNCHANGED, urgency=U.ROUTINE)
    result = gate([a, b])
    assert result.resolution.active_assessment is b
    assert (result.instrument_new_capital_gate, result.review_state, result.review_reasons) == (G.OPEN, S.NOT_REQUIRED, ())


def test_active_critical_introduced_by_a_correction_quarantines() -> None:
    a = assess("A", materiality=M.LOW, thesis=TI.UNCHANGED)
    b = assess("B", RK.CORRECTION, "A", materiality=M.CRITICAL, thesis=TI.WEAKENED)
    result = gate([a, b])
    assert result.resolution.active_assessment is b
    assert (result.instrument_new_capital_gate, result.review_state) == (G.QUARANTINED, S.REQUIRED)
    assert result.review_reasons == (R.MATERIALITY_HIGH_OR_CRITICAL, R.THESIS_WEAKENED)


def test_withdrawal_is_evaluated_by_its_own_active_assessment() -> None:
    original = assess("A", materiality=M.CRITICAL, thesis=TI.INVALIDATED)
    benign = gate([original, assess("B", RK.WITHDRAWAL, "A", materiality=M.LOW, thesis=TI.UNCHANGED)])
    assert (benign.instrument_new_capital_gate, benign.review_state) == (G.OPEN, S.NOT_REQUIRED)
    harsh = gate([assess("A"), assess("B", RK.WITHDRAWAL, "A", materiality=M.CRITICAL, thesis=TI.UNCERTAIN)])
    assert (harsh.instrument_new_capital_gate, harsh.review_state) == (G.QUARANTINED, S.REQUIRED)
    for kind in RK:                                                                            # the revision kind itself drives nothing
        family = [assess("A")] if kind is RK.ORIGINAL else [assess("A"), assess("B", kind, "A")]
        assert gate(family).instrument_new_capital_gate is G.OPEN


# --- systemic ----------------------------------------------------------------------------------------------------

def test_systemic_families_never_quarantine_and_never_carry_instrument_ids() -> None:
    for materiality, thesis, urgency in itertools.product(M, TI, U):
        result = single(systemic=True, materiality=materiality, thesis=thesis, urgency=urgency)
        assert result.instrument_new_capital_gate is G.NOT_APPLICABLE_SYSTEMIC and result.affected_instrument_ids == ()
        reference = single(materiality=materiality, thesis=thesis, urgency=urgency)
        assert (result.review_state, result.review_reasons) == (reference.review_state, reference.review_reasons)
    critical = single(systemic=True, materiality=M.CRITICAL, thesis=TI.INVALIDATED)
    assert critical.review_state is S.REQUIRED and critical.instrument_new_capital_gate is not G.QUARANTINED


def test_affected_instrument_ids_come_only_from_the_resolution_authority() -> None:
    result = single(ids=(UA, UB), materiality=M.CRITICAL, thesis=TI.WEAKENED)
    event = result.resolution.active_assessment.binding.event
    assert result.affected_instrument_ids == (UA, UB) == event.instrument_ids
    with pytest.raises(TypeError):
        build_game_changer_decision_gate(resolution=result.resolution, affected_instrument_ids=(UB,))  # type: ignore[call-arg]


# --- forge resistance --------------------------------------------------------------------------------------------

def _forge(good: GameChangerDecisionGate, **changes) -> GameChangerDecisionGate:
    values = dict(resolution=good.resolution, review_state=good.review_state, review_reasons=good.review_reasons,
                  instrument_new_capital_gate=good.instrument_new_capital_gate, affected_instrument_ids=good.affected_instrument_ids)
    values.update(changes)
    return GameChangerDecisionGate(**values)


def test_direct_construction_rejects_forged_gates() -> None:
    critical = single(materiality=M.CRITICAL, thesis=TI.WEAKENED, ids=(UA, UB))
    assert _forge(critical) == critical
    benign = single(materiality=M.LOW, thesis=TI.UNCHANGED)
    incomplete = gate([assess(ids=(UA, UB))], INCOMPLETE)
    systemic = single(systemic=True, materiality=M.CRITICAL, thesis=TI.INVALIDATED)
    forged = [
        (critical, dict(instrument_new_capital_gate=G.OPEN)),
        (benign, dict(instrument_new_capital_gate=G.QUARANTINED)),
        (incomplete, dict(instrument_new_capital_gate=G.OPEN)),
        (incomplete, dict(instrument_new_capital_gate=G.QUARANTINED)),
        (systemic, dict(instrument_new_capital_gate=G.QUARANTINED)),
        (systemic, dict(instrument_new_capital_gate=G.OPEN)),
        (systemic, dict(affected_instrument_ids=(UA,))),
        (benign, dict(review_state=S.REQUIRED)),
        (critical, dict(review_state=S.NOT_REQUIRED)),
        (benign, dict(review_reasons=(R.THESIS_WEAKENED,))),
        (critical, dict(review_reasons=())),
        (critical, dict(review_reasons=(R.THESIS_WEAKENED, R.MATERIALITY_HIGH_OR_CRITICAL))),         # wrong order
        (critical, dict(review_reasons=(R.MATERIALITY_HIGH_OR_CRITICAL,))),
        (critical, dict(review_reasons=(R.MATERIALITY_HIGH_OR_CRITICAL, R.THESIS_WEAKENED, R.THESIS_UNCERTAIN))),
        (critical, dict(affected_instrument_ids=(UA,))),
        (critical, dict(affected_instrument_ids=(UB, UA))),
        (critical, dict(affected_instrument_ids=())),
    ]
    for good, changes in forged:
        with pytest.raises(ValueError):
            _forge(good, **changes)


def test_replacement_resolution_cannot_carry_a_gate_derived_from_another_resolution() -> None:
    quarantined = single(materiality=M.CRITICAL, thesis=TI.WEAKENED)
    open_resolution = resolution([assess(materiality=M.LOW)])
    clone = dataclasses.replace(open_resolution)
    assert clone == open_resolution and clone is not open_resolution
    with pytest.raises(ValueError):
        _forge(quarantined, resolution=open_resolution)
    with pytest.raises(ValueError):
        _forge(quarantined, resolution=clone)
    accepted = GameChangerDecisionGate(resolution=clone, review_state=S.NOT_REQUIRED, review_reasons=(),
                                       instrument_new_capital_gate=G.OPEN, affected_instrument_ids=(UA,))
    assert accepted.resolution is clone                                                         # retained exactly as supplied


def test_direct_construction_type_errors() -> None:
    good = single(materiality=M.CRITICAL, thesis=TI.WEAKENED)
    for field, bad in (("resolution", object()), ("review_state", "required"), ("review_state", None), ("review_reasons", list(good.review_reasons)),
                       ("review_reasons", ("thesis_weakened",)), ("review_reasons", None), ("instrument_new_capital_gate", "open"),
                       ("instrument_new_capital_gate", S.REQUIRED), ("affected_instrument_ids", [UA]), ("affected_instrument_ids", ("x",)),
                       ("affected_instrument_ids", None)):
        with pytest.raises(TypeError):
            _forge(good, **{field: bad})


def test_gate_is_deterministic() -> None:
    res = resolution([assess(materiality=M.CRITICAL, thesis=TI.UNCERTAIN)])
    assert build_game_changer_decision_gate(resolution=res) == build_game_changer_decision_gate(resolution=res)


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/game_changer_gate.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_and_the_closed_game_changer_types_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "dataclasses", "enum", "uuid", "backend.engine.private.game_changer_assessment",
                     "backend.engine.private.game_changer_event", "backend.engine.private.game_changer_revision_family"}
    assert imported["backend.engine.private.game_changer_assessment"] <= {"GameChangerMateriality", "GameChangerThesisImpact", "GameChangerUrgency"}
    assert imported["backend.engine.private.game_changer_event"] <= {"GameChangerEventScope"}
    assert imported["backend.engine.private.game_changer_revision_family"] <= {
        "GameChangerRevisionFamilyResolution", "GameChangerRevisionFamilyResolutionStatus"}
    for forbidden in ("provider", "kap", "sec", "requests", "httpx", "browser", "scraper", "openai", "gemini", "claude", "llm", "langchain",
                      "portfolio", "allocation", "rebalance", "database", "repository", "supabase", "numpy", "scipy", "pandas", "decimal",
                      "random", "datetime", "domain", "analysis_pit", "macro", "cash", "goal"):
        assert not any(forbidden in part for name in names for part in name.split(".")), forbidden


def test_no_historical_leakage_search_sorting_clock_or_arithmetic() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"max", "min", "sorted", "sort", "sum", "reversed", "now", "today", "utcnow", "float", "Decimal", "random"}
    assert not attrs & {"published_at", "observed_at", "effective_date", "content_sha256", "methodology_key", "assessment_provenance_sha256",
                        "impact_dimensions", "revises_source_event_key", "revision_kind", "source_event_key", "coverage"}
    parents = {id(child): parent for parent in ast.walk(_TREE) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Attribute) and node.attr == "assessments":
            parent = parents[id(node)]                                                          # only the anchor member [0] may be read
            assert isinstance(parent, ast.Subscript) and isinstance(parent.slice, ast.Constant) and parent.slice.value == 0
        assert not isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension, ast.While))       # no loops at all: no historical iteration
    arithmetic = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
    assert not [n for n in ast.walk(_TREE) if (isinstance(n, ast.BinOp) and isinstance(n.op, arithmetic)) or isinstance(n, ast.AugAssign)]
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) in (float, complex)]


_FRAGMENTS = ("provider", "scraper", "requests", "httpx", "browser", "openai", "gemini", "claude", "langchain", "langgraph", "sentiment",
              "score", "probab", "confidence", "severity", "rank", "predict", "classif", "portfolio", "allocation", "rebalance", "broker",
              "execution", "database", "repository", "supabase", "persist", "notif", "schedul", "target_weight", "weight", "sell", "exit",
              "liquidat", "dispos", "reduce", "latest", "newest", "highest", "worst")
_EXACT = {"kap", "sec", "llm", "tax", "order", "now", "today", "random", "buy", "hold", "trade"}


def test_no_sell_target_mutation_execution_score_or_llm_identifiers() -> None:
    identifiers: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
    for identifier in identifiers:
        lowered = identifier.lower()
        assert lowered not in _EXACT, identifier
        for fragment in _FRAGMENTS:
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    function = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "build_game_changer_decision_gate")
    assert not function.args.args and [a.arg for a in function.args.kwonlyargs] == ["resolution"]
    public = {n.lower() for n in dir(GameChangerDecisionGate) if not n.startswith("_")}
    assert not [n for n in public if any(w in n for w in ("weight", "sell", "order", "score", "action", "trade"))]


def test_documents_the_gate_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("pause NEW capital", "existing holdings", "systemic", "no sell", "terminal", "no target-weight", "23C1", "INVALIDATED",
                   "CRITICAL", "urgency", "not a recommendation", "no execution"):
        assert needle in doc, needle
