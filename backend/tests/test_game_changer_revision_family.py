"""
backend/tests/test_game_changer_revision_family.py
==================================================
Phase 23C1: revision-family completeness and active-assessment resolution at ONE PIT frontier. The active assessment is the terminal
member of an explicitly complete, linear, caller-ordered revision chain. No aggregation, no timestamp or severity winner, no
quarantine, no allocation consequence.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import game_changer_revision_family as module_under_test
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
from backend.engine.private.game_changer_revision_family import (
    GameChangerRevisionFamily,
    GameChangerRevisionFamilyCoverage,
    GameChangerRevisionFamilyResolution,
    GameChangerRevisionFamilyResolutionStatus,
    resolve_game_changer_revision_family,
)
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
M, U, TI = GameChangerMateriality, GameChangerUrgency, GameChangerThesisImpact
RK, ET = GameChangerRevisionKind, GameChangerEventType
COMPLETE, INCOMPLETE = GameChangerRevisionFamilyCoverage.COMPLETE_AT_CUTOFF, GameChangerRevisionFamilyCoverage.INCOMPLETE_AT_CUTOFF
RESOLVED, NO_COVERAGE = GameChangerRevisionFamilyResolutionStatus.RESOLVED, GameChangerRevisionFamilyResolutionStatus.INCOMPLETE_COVERAGE
COVERAGE_SHA = "c" * 64
UA, UB = UUID(int=1), UUID(int=2)
CONTEXT = AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=datetime(2026, 9, 30, tzinfo=UTC))


def T(day=1, hour=0) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=UTC)


def assessment(key, kind=RK.ORIGINAL, revises=None, *, context=CONTEXT, materiality=M.MEDIUM, urgency=U.ROUTINE, thesis=TI.UNCHANGED,
               event_type=ET.FINANCIAL_REPORT, source_key="kap", tier=SourceTier.TIER_1_REGULATORY, systemic=False, ids=(UA,),
               published=None, observed=None) -> GameChangerMaterialityAssessment:
    event = GameChangerEvent(
        source_key=source_key, source_event_key=key, source_tier=tier,
        scope=GameChangerEventScope.SYSTEMIC if systemic else GameChangerEventScope.INSTRUMENT,
        event_type=ET.MACRO_SHOCK if systemic else event_type, instrument_ids=() if systemic else tuple(ids), effective_date=None,
        published_at=published or T(2, 1), observed_at=observed or T(2, 2), content_sha256="a" * 64, revision_kind=kind,
        revises_source_event_key=revises,
    )
    bound = bind_game_changer_event_pit(event=event, context=context)
    return build_game_changer_materiality_assessment(
        binding=bound, materiality=materiality, urgency=urgency, thesis_impact=thesis,
        impact_dimensions=(GameChangerImpactDimension.EARNINGS,), materiality_basis=GameChangerMaterialityBasis.NATURE,
        methodology_key="game_changer.materiality.v1", assessment_provenance_sha256="b" * 64)


def resolve(assessments, coverage=COMPLETE, sha=COVERAGE_SHA) -> GameChangerRevisionFamilyResolution:
    return resolve_game_changer_revision_family(assessments=tuple(assessments), coverage=coverage, coverage_provenance_sha256=sha)


def chain3():
    a = assessment("A")
    b = assessment("B", RK.UPDATE, "A", event_type=ET.GUIDANCE)
    c = assessment("C", RK.CORRECTION, "B", event_type=ET.OPERATIONS)
    return a, b, c


# --- enums / stored fields ---------------------------------------------------------------------------------------

def test_enums_and_stored_fields() -> None:
    assert [(m.name, m.value) for m in GameChangerRevisionFamilyCoverage] == [("COMPLETE_AT_CUTOFF", "complete_at_cutoff"),
                                                                              ("INCOMPLETE_AT_CUTOFF", "incomplete_at_cutoff")]
    assert [(m.name, m.value) for m in GameChangerRevisionFamilyResolutionStatus] == [("RESOLVED", "resolved"),
                                                                                      ("INCOMPLETE_COVERAGE", "incomplete_coverage")]
    assert [f.name for f in dataclasses.fields(GameChangerRevisionFamily)] == ["assessments", "coverage", "coverage_provenance_sha256"]
    assert [f.name for f in dataclasses.fields(GameChangerRevisionFamilyResolution)] == ["family", "status", "active_assessment"]
    for cls in (GameChangerRevisionFamily, GameChangerRevisionFamilyResolution):
        assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in dataclasses.fields(cls))
    resolved = resolve([assessment("A")])
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.status = NO_COVERAGE  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.family.coverage = INCOMPLETE  # type: ignore[misc]


def test_function_is_keyword_only_without_defaults() -> None:
    a = assessment("A")
    with pytest.raises(TypeError):
        resolve_game_changer_revision_family((a,), COMPLETE, COVERAGE_SHA)  # type: ignore[misc]
    with pytest.raises(TypeError):
        resolve_game_changer_revision_family(assessments=(a,), coverage=COMPLETE)  # type: ignore[call-arg]


# --- resolution of complete families -----------------------------------------------------------------------------

def test_single_original_resolves_to_itself() -> None:
    a = assessment("A")
    result = resolve([a])
    assert result.status is RESOLVED and result.active_assessment is a


def test_multi_revision_chain_activates_the_terminal_assessment() -> None:
    a, b, c = chain3()
    supplied = (a, b, c)
    result = resolve_game_changer_revision_family(assessments=supplied, coverage=COMPLETE, coverage_provenance_sha256=COVERAGE_SHA)
    assert result.status is RESOLVED and result.active_assessment is c
    assert result.family.assessments is supplied and all(x is y for x, y in zip(result.family.assessments, (a, b, c)))
    assert result.active_assessment is result.family.assessments[-1]


def test_old_critical_assessment_does_not_leak_through_a_correction() -> None:
    a = assessment("A", materiality=M.CRITICAL, thesis=TI.WEAKENED, urgency=U.IMMEDIATE)
    b = assessment("B", RK.CORRECTION, "A", materiality=M.LOW, thesis=TI.UNCHANGED)
    result = resolve([a, b])
    assert result.active_assessment is b and result.active_assessment.materiality is M.LOW and result.active_assessment is not a
    public = {n.lower() for n in dir(result) + dir(result.family) if not n.startswith("_")}
    assert not [n for n in public if any(w in n for w in ("max", "historical", "aggregate", "severity", "worst", "highest"))]


def test_withdrawal_is_an_active_terminal_event_assessment() -> None:
    a = assessment("A", materiality=M.CRITICAL)
    b = assessment("B", RK.WITHDRAWAL, "A", materiality=M.MEDIUM, thesis=TI.UNCERTAIN)
    result = resolve([a, b])
    assert result.status is RESOLVED and result.active_assessment is b                    # not None, not A: the withdrawal is its own event


def test_event_type_may_change_inside_a_family_and_timestamps_do_not_select() -> None:
    a = assessment("A", published=T(10, 1), observed=T(10, 2), event_type=ET.FINANCIAL_REPORT)
    b = assessment("B", RK.UPDATE, "A", published=T(3, 1), observed=T(3, 2), event_type=ET.LEGAL_REGULATORY)     # earlier timestamps than A
    c = assessment("C", RK.CORRECTION, "B", published=T(20, 1), observed=T(20, 2), event_type=ET.OTHER)
    assert resolve([a, b, c]).active_assessment is c
    assert resolve([a, b]).active_assessment is b                                         # B is terminal although A is later in time
    for x, y in ((a, b), (b, c)):
        assert x.binding.event.event_type is not y.binding.event.event_type


def test_systemic_family_resolves() -> None:
    a = assessment("A", systemic=True)
    b = assessment("B", RK.UPDATE, "A", systemic=True)
    assert resolve([a, b]).active_assessment is b


# --- incomplete coverage -----------------------------------------------------------------------------------------

def test_incomplete_coverage_fails_closed_without_best_effort() -> None:
    critical = assessment("A", materiality=M.CRITICAL, urgency=U.IMMEDIATE, thesis=TI.INVALIDATED)
    result = resolve([critical], INCOMPLETE)
    assert result.status is NO_COVERAGE and result.active_assessment is None
    a, b, c = chain3()
    multi = resolve([a, b, c], INCOMPLETE)
    assert multi.status is NO_COVERAGE and multi.active_assessment is None                 # no "latest", "last" or "highest" choice
    assert multi.family.assessments == (a, b, c)                                           # evidence is preserved


def test_incomplete_family_may_miss_lineage_members() -> None:
    orphan = assessment("B", RK.CORRECTION, "MISSING-PARENT")
    result = resolve([orphan], INCOMPLETE)
    assert result.status is NO_COVERAGE and result.active_assessment is None
    skipped = resolve([assessment("A"), assessment("C", RK.CORRECTION, "B")], INCOMPLETE)   # missing B, branch-free anyway
    assert skipped.active_assessment is None
    branched = resolve([assessment("A"), assessment("B", RK.UPDATE, "A"), assessment("C", RK.CORRECTION, "A")], INCOMPLETE)
    assert branched.status is NO_COVERAGE and branched.active_assessment is None            # incomplete is never adjudicated


def test_missing_parent_under_complete_coverage_fails() -> None:
    orphan = assessment("B", RK.CORRECTION, "MISSING-PARENT")
    with pytest.raises(ValueError):
        resolve([orphan], COMPLETE)                                                         # no root exists


# --- complete chain contract -------------------------------------------------------------------------------------

def test_branches_are_rejected_without_a_tie_break() -> None:
    a = assessment("A")
    b = assessment("B", RK.UPDATE, "A", published=T(3, 1), observed=T(3, 2))
    c = assessment("C", RK.CORRECTION, "A", published=T(9, 1), observed=T(9, 2))
    for order in ((a, b, c), (a, c, b)):
        with pytest.raises(ValueError):
            resolve(order)


def test_skipped_predecessor_is_rejected() -> None:
    a, b = assessment("A"), assessment("B", RK.UPDATE, "A")
    c = assessment("C", RK.CORRECTION, "A")
    with pytest.raises(ValueError):
        resolve([a, b, c])
    with pytest.raises(ValueError):
        resolve([a, assessment("C2", RK.CORRECTION, "B")])                                  # parent B absent from the family


def test_non_lineage_order_is_rejected_never_sorted() -> None:
    a, b, c = chain3()
    for order in ((c, a, b), (c, b, a), (b, a, c), (a, c, b), (b, c, a)):
        with pytest.raises(ValueError):
            resolve(order)
    assert resolve([a, b, c]).active_assessment is c


def test_first_member_must_be_an_original_root() -> None:
    with pytest.raises(ValueError):
        resolve([assessment("B", RK.UPDATE, "A")])
    with pytest.raises(ValueError):
        resolve([assessment("B", RK.WITHDRAWAL, "A"), assessment("C", RK.UPDATE, "B")])
    with pytest.raises(ValueError):
        resolve([assessment("A"), assessment("B")])                                         # a second ORIGINAL is not a revision


def test_duplicate_event_keys_are_rejected() -> None:
    a = assessment("A")
    twin = assessment("A", RK.ORIGINAL, published=T(5, 1), observed=T(5, 2))
    with pytest.raises(ValueError):
        resolve([a, twin])
    with pytest.raises(ValueError):
        resolve([a, assessment("B", RK.UPDATE, "A"), assessment("B", RK.CORRECTION, "A")], INCOMPLETE)     # also under incomplete coverage


# --- one frontier / one family identity --------------------------------------------------------------------------

def test_one_pit_frontier_by_object_identity() -> None:
    equal_but_distinct = AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=datetime(2026, 9, 30, tzinfo=UTC))
    assert equal_but_distinct == CONTEXT and equal_but_distinct is not CONTEXT
    a = assessment("A")
    b = assessment("B", RK.UPDATE, "A", context=equal_but_distinct)
    for coverage in (COMPLETE, INCOMPLETE):
        with pytest.raises(ValueError):
            resolve([a, b], coverage)
    shared = assessment("B", RK.UPDATE, "A", context=CONTEXT)
    assert resolve([a, shared]).active_assessment is shared
    other_mode = AnalysisPITContext(mode=AsOfMode.SYSTEM_AS_OF, knowledge_cutoff=datetime(2026, 9, 30, tzinfo=UTC))
    with pytest.raises(ValueError):
        resolve([a, assessment("B", RK.UPDATE, "A", context=other_mode)])


def test_family_identity_mismatches_are_rejected_independently() -> None:
    a = assessment("A")
    mismatches = {
        "source_key": assessment("B", RK.UPDATE, "A", source_key="other-source"),
        "source_tier": assessment("B", RK.UPDATE, "A", tier=SourceTier.TIER_2_EXCHANGE),
        "instrument_ids": assessment("B", RK.UPDATE, "A", ids=(UB,)),
    }
    for name, other in mismatches.items():
        for coverage in (COMPLETE, INCOMPLETE):
            with pytest.raises(ValueError):
                resolve([a, other], coverage)
    systemic_a = assessment("A", systemic=True)
    with pytest.raises(ValueError):                                                         # scope mismatch
        resolve([systemic_a, assessment("B", RK.UPDATE, "A")])
    with pytest.raises(ValueError):
        resolve([a, assessment("B", RK.UPDATE, "A", systemic=True)])
    assert resolve([a, assessment("B", RK.UPDATE, "A", ids=(UA,))]).status is RESOLVED


# --- type contracts ----------------------------------------------------------------------------------------------

def test_input_type_strictness() -> None:
    a = assessment("A")

    class SubAssessment(GameChangerMaterialityAssessment):
        pass

    sub = SubAssessment(**{f.name: getattr(a, f.name) for f in dataclasses.fields(a)})

    class SubTuple(tuple):
        pass

    class SubStr(str):
        pass

    for bad in ([a], None, (a, object()), (sub,), SubTuple((a,)), "x", a):
        with pytest.raises(TypeError):
            resolve_game_changer_revision_family(assessments=bad, coverage=COMPLETE, coverage_provenance_sha256=COVERAGE_SHA)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        resolve([])                                                                         # non-empty
    for bad in ("complete_at_cutoff", None, 1, GameChangerRevisionFamilyResolutionStatus.RESOLVED):
        with pytest.raises(TypeError):
            resolve_game_changer_revision_family(assessments=(a,), coverage=bad, coverage_provenance_sha256=COVERAGE_SHA)  # type: ignore[arg-type]
    for bad in (None, 1, b"c" * 64, SubStr("c" * 64)):
        with pytest.raises(TypeError):
            resolve([a], COMPLETE, bad)
    for bad in ("C" * 64, "c" * 63, "c" * 65, "g" * 64, "", "c" * 64 + "\n"):
        with pytest.raises(ValueError):
            resolve([a], COMPLETE, bad)
    other_provenance = resolve([a], COMPLETE, "0" * 64)                                     # unrelated to event / assessment hashes
    assert other_provenance.family.coverage_provenance_sha256 == "0" * 64


# --- forge resistance --------------------------------------------------------------------------------------------

def test_direct_resolution_construction_rejects_forged_status_and_active_pairs() -> None:
    a, b, c = chain3()
    complete = GameChangerRevisionFamily(assessments=(a, b, c), coverage=COMPLETE, coverage_provenance_sha256=COVERAGE_SHA)
    incomplete = GameChangerRevisionFamily(assessments=(a, b, c), coverage=INCOMPLETE, coverage_provenance_sha256=COVERAGE_SHA)
    assert GameChangerRevisionFamilyResolution(family=complete, status=RESOLVED, active_assessment=c).active_assessment is c
    assert GameChangerRevisionFamilyResolution(family=incomplete, status=NO_COVERAGE, active_assessment=None).active_assessment is None
    clone = dataclasses.replace(c)
    assert clone == c and clone is not c
    forged = [
        (complete, NO_COVERAGE, c), (complete, NO_COVERAGE, None), (complete, RESOLVED, None), (complete, RESOLVED, a),
        (complete, RESOLVED, b), (complete, RESOLVED, clone),
        (incomplete, RESOLVED, c), (incomplete, RESOLVED, None), (incomplete, NO_COVERAGE, c), (incomplete, NO_COVERAGE, a),
    ]
    for family, status, active in forged:
        with pytest.raises(ValueError):
            GameChangerRevisionFamilyResolution(family=family, status=status, active_assessment=active)
    for family, status, active in ((object(), RESOLVED, c), (complete, "resolved", c), (complete, RESOLVED, object()), (complete, None, c)):
        with pytest.raises(TypeError):
            GameChangerRevisionFamilyResolution(family=family, status=status, active_assessment=active)  # type: ignore[arg-type]


def test_direct_family_construction_enforces_the_complete_chain_contract() -> None:
    a, b, c = chain3()
    with pytest.raises(ValueError):
        GameChangerRevisionFamily(assessments=(c, a, b), coverage=COMPLETE, coverage_provenance_sha256=COVERAGE_SHA)
    assert GameChangerRevisionFamily(assessments=(c, a, b), coverage=INCOMPLETE, coverage_provenance_sha256=COVERAGE_SHA).assessments == (c, a, b)


def test_resolution_is_deterministic() -> None:
    a, b, c = chain3()
    assert resolve([a, b, c]) == resolve([a, b, c])


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/game_changer_revision_family.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_and_the_23a_23b_types_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "dataclasses", "enum", "re", "backend.engine.private.game_changer_assessment",
                     "backend.engine.private.game_changer_event"}
    assert imported["backend.engine.private.game_changer_assessment"] == {"GameChangerMaterialityAssessment"}
    assert imported["backend.engine.private.game_changer_event"] <= {"GameChangerEventScope", "GameChangerRevisionKind"}
    for forbidden in ("provider", "kap", "sec", "requests", "httpx", "browser", "scraper", "openai", "gemini", "claude", "llm", "langchain",
                      "langgraph", "portfolio", "allocation", "rebalance", "database", "repository", "supabase", "numpy", "scipy",
                      "pandas", "decimal", "random", "datetime", "domain", "analysis_pit", "macro", "cash", "goal"):
        assert not any(forbidden in part for name in names for part in name.split(".")), forbidden


def test_no_winner_logic_sorting_clock_or_numeric_runtime() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"max", "min", "sorted", "sort", "sum", "reversed", "now", "today", "utcnow", "float", "Decimal", "random"}
    assert not attrs & {"published_at", "observed_at", "effective_date", "materiality", "urgency", "thesis_impact", "impact_dimensions",
                        "content_sha256", "assessment_provenance_sha256", "methodology_key"}           # lineage edges are the only authority
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) in (float, complex)]
    arithmetic = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)       # `X | None` annotations are not arithmetic
    assert not [n for n in ast.walk(_TREE) if (isinstance(n, ast.BinOp) and isinstance(n.op, arithmetic)) or isinstance(n, ast.AugAssign)]


_FRAGMENTS = ("provider", "scraper", "requests", "httpx", "browser", "openai", "gemini", "claude", "langchain", "langgraph", "sentiment",
              "score", "rank", "recommend", "predict", "classif", "portfolio", "allocation", "rebalance", "quarantine", "broker",
              "execution", "database", "repository", "supabase", "clock", "severity", "latest", "newest", "highest", "worst", "alert",
              "capital", "eligible", "block", "pause", "liquidat")
_EXACT = {"kap", "sec", "llm", "tax", "order", "now", "today", "random", "buy", "sell", "hold", "trade", "weight"}


def test_no_quarantine_portfolio_llm_or_trade_identifiers() -> None:
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
    function = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "resolve_game_changer_revision_family")
    assert not function.args.args and [a.arg for a in function.args.kwonlyargs] == ["assessments", "coverage", "coverage_provenance_sha256"]


def test_documents_the_family_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("one PIT frontier", "COMPLETE_AT_CUTOFF", "INCOMPLETE_AT_CUTOFF", "linear", "no branch", "no sorting", "terminal",
                   "withdrawal", "no timestamp", "no quarantine", "23C2", "coverage_provenance_sha256", "not aggregate"):
        assert needle in doc, needle
