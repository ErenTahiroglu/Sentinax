"""
backend/tests/test_game_changer_assessment.py
=============================================
Phase 23B: explicit structured Game Changer assessment over one already PIT-admissible Phase 23A binding. Categorical only: no
score, no sentiment, no classification algorithm, no LLM, no quarantine, no portfolio consequence, no revision resolution.
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import game_changer_assessment as module_under_test
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
    GameChangerEventPITBinding,
    GameChangerEventScope,
    GameChangerEventType,
    GameChangerRevisionKind,
    bind_game_changer_event_pit,
)
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
M, U, TI, B, DIM = GameChangerMateriality, GameChangerUrgency, GameChangerThesisImpact, GameChangerMaterialityBasis, GameChangerImpactDimension
EVENT_SHA, ASSESSMENT_SHA = "a" * 64, "b" * 64
KEY = "game_changer.materiality.v1"


def T(h=12) -> datetime:
    return datetime(2026, 9, 1, h, tzinfo=UTC)


def binding(event_type=GameChangerEventType.FINANCIAL_REPORT, tier=SourceTier.TIER_1_REGULATORY, kind=GameChangerRevisionKind.ORIGINAL,
            systemic=False, content=EVENT_SHA) -> GameChangerEventPITBinding:
    event = GameChangerEvent(
        source_key="kap", source_event_key="EVT-2", source_tier=tier,
        scope=GameChangerEventScope.SYSTEMIC if systemic else GameChangerEventScope.INSTRUMENT,
        event_type=GameChangerEventType.MACRO_SHOCK if systemic else event_type,
        instrument_ids=() if systemic else (UUID(int=1),), effective_date=None, published_at=T(10), observed_at=T(11),
        content_sha256=content, revision_kind=kind, revises_source_event_key=None if kind is GameChangerRevisionKind.ORIGINAL else "EVT-1",
    )
    return bind_game_changer_event_pit(event=event, context=AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=T(12)))


_DEFAULT = object()


def assess(bound=_DEFAULT, **changes) -> GameChangerMaterialityAssessment:
    values = dict(binding=binding() if bound is _DEFAULT else bound, materiality=M.HIGH, urgency=U.PROMPT, thesis_impact=TI.WEAKENED,
                  impact_dimensions=(DIM.EARNINGS, DIM.VALUATION), materiality_basis=B.NATURE_AND_MAGNITUDE, methodology_key=KEY,
                  assessment_provenance_sha256=ASSESSMENT_SHA)
    values.update(changes)
    return build_game_changer_materiality_assessment(**values)


# --- enums / stored fields ---------------------------------------------------------------------------------------

def test_enum_members_and_values() -> None:
    assert [(m.name, m.value) for m in M] == [("LOW", "low"), ("MEDIUM", "medium"), ("HIGH", "high"), ("CRITICAL", "critical")]
    assert [(m.name, m.value) for m in U] == [("ROUTINE", "routine"), ("PROMPT", "prompt"), ("IMMEDIATE", "immediate")]
    assert [m.value for m in TI] == ["unchanged", "strengthened", "weakened", "invalidated", "uncertain"]
    assert [m.value for m in B] == ["nature", "magnitude", "nature_and_magnitude"]
    assert [m.value for m in DIM] == ["earnings", "cash_flow", "balance_sheet", "valuation", "operations", "financing_liquidity", "governance",
                                      "legal_regulatory", "ownership_control", "capital_structure", "macro_exposure", "other"]
    assert not {m.name for m in DIM} & {"PORTFOLIO_IMPACT", "REBALANCE_IMPACT", "POSITION_IMPACT"}
    for enum in (M, U, TI, B, DIM):
        assert all(type(m.value) is str for m in enum)                                      # vocabulary only, no numeric value


def test_stored_fields_no_defaults_frozen_and_identity() -> None:
    assert [f.name for f in dataclasses.fields(GameChangerMaterialityAssessment)] == [
        "binding", "materiality", "urgency", "thesis_impact", "impact_dimensions", "materiality_basis", "methodology_key",
        "assessment_provenance_sha256"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in dataclasses.fields(GameChangerMaterialityAssessment))
    bound = binding()
    assessment = assess(bound)
    assert assessment.binding is bound                                                        # retained, never reconstructed
    with pytest.raises(dataclasses.FrozenInstanceError):
        assessment.materiality = M.LOW  # type: ignore[misc]
    with pytest.raises(TypeError):
        GameChangerMaterialityAssessment(binding=bound)  # type: ignore[call-arg]


def test_builder_is_keyword_only() -> None:
    bound = binding()
    with pytest.raises(TypeError):
        build_game_changer_materiality_assessment(bound, M.LOW, U.ROUTINE, TI.UNCHANGED, (DIM.OTHER,), B.NATURE, KEY, ASSESSMENT_SHA)  # type: ignore[misc]
    with pytest.raises(TypeError):
        build_game_changer_materiality_assessment(binding=bound, materiality=M.LOW)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        assess(rationale="text")                                                              # no free-form field


# --- binding authority -------------------------------------------------------------------------------------------

def test_binding_must_be_an_exact_pit_binding() -> None:
    bound = binding()

    class SubBinding(GameChangerEventPITBinding):
        pass

    sub = SubBinding(event=bound.event, context=bound.context)
    for bad in (bound.event, bound.context, None, object(), sub):                              # a raw event cannot be assessed
        with pytest.raises(TypeError):
            assess(bad)
        with pytest.raises(TypeError):
            GameChangerMaterialityAssessment(binding=bad, materiality=M.LOW, urgency=U.ROUTINE, thesis_impact=TI.UNCHANGED,  # type: ignore[arg-type]
                                             impact_dimensions=(DIM.OTHER,), materiality_basis=B.NATURE, methodology_key=KEY,
                                             assessment_provenance_sha256=ASSESSMENT_SHA)


# --- enum exactness ----------------------------------------------------------------------------------------------

def test_enum_fields_are_exact() -> None:
    for field, bads in (("materiality", ["high", 1, None, U.PROMPT]), ("urgency", ["prompt", 1, None, M.HIGH]),
                        ("thesis_impact", ["weakened", None, 1, M.LOW]), ("materiality_basis", ["nature", None, 1, M.LOW])):
        for bad in bads:
            with pytest.raises(TypeError):
                assess(**{field: bad})


# --- impact dimensions -------------------------------------------------------------------------------------------

def test_impact_dimension_contract() -> None:
    assert assess(impact_dimensions=(DIM.OTHER,)).impact_dimensions == (DIM.OTHER,)
    assert assess(impact_dimensions=tuple(DIM)).impact_dimensions == tuple(DIM)
    for bad in ([DIM.EARNINGS], None, "earnings", (DIM.EARNINGS, "valuation"), ("earnings",), (M.LOW,), (None,)):
        with pytest.raises(TypeError):
            assess(impact_dimensions=bad)
    with pytest.raises(ValueError):
        assess(impact_dimensions=())                                                          # empty never means unknown
    with pytest.raises(ValueError):
        assess(impact_dimensions=(DIM.EARNINGS, DIM.EARNINGS))
    with pytest.raises(ValueError):
        assess(impact_dimensions=(DIM.VALUATION, DIM.EARNINGS))                               # never silently sorted
    assert assess(impact_dimensions=(DIM.GOVERNANCE, DIM.OTHER)).impact_dimensions == (DIM.GOVERNANCE, DIM.OTHER)


# --- methodology key / provenance --------------------------------------------------------------------------------

def test_methodology_key_contract() -> None:
    class SubStr(str):
        pass

    for ok in (KEY, "x", "a" * 128, "0-a_b.c", "other.method.v2"):
        assert assess(methodology_key=ok).methodology_key == ok
    for bad in ("", " x", "x ", "X", "a" * 129, "a b", "x\n", "-x", "_x", "https://x.y/v1"):
        with pytest.raises(ValueError):
            assess(methodology_key=bad)
    for bad in (None, 1, b"x", SubStr("x")):
        with pytest.raises(TypeError):
            assess(methodology_key=bad)


def test_assessment_provenance_hash_contract() -> None:
    class SubStr(str):
        pass

    for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", "a" * 64 + "\n", " " + "a" * 63):
        with pytest.raises(ValueError):
            assess(assessment_provenance_sha256=bad)
    for bad in (None, 1, b"a" * 64, SubStr("a" * 64)):
        with pytest.raises(TypeError):
            assess(assessment_provenance_sha256=bad)
    assert assess(assessment_provenance_sha256="0123456789abcdef" * 4).assessment_provenance_sha256 == "0123456789abcdef" * 4


def test_event_content_hash_and_assessment_provenance_are_separate_and_unconstrained() -> None:
    bound = binding(content="a" * 64)
    assessment = assess(bound, assessment_provenance_sha256="b" * 64)
    assert assessment.binding.event.content_sha256 != assessment.assessment_provenance_sha256
    same = assess(bound, assessment_provenance_sha256="a" * 64)                              # equality is allowed, never required
    assert same.assessment_provenance_sha256 == bound.event.content_sha256


# --- orthogonality -----------------------------------------------------------------------------------------------

def test_event_type_does_not_determine_materiality() -> None:
    for event_type in (GameChangerEventType.LEGAL_REGULATORY, GameChangerEventType.M_AND_A, GameChangerEventType.FINANCIAL_REPORT):
        bound = binding(event_type=event_type)
        assert {assess(bound, materiality=m).materiality for m in M} == set(M)


def test_materiality_urgency_and_thesis_are_independent() -> None:
    combos = {(m, u, t) for m in M for u in U for t in TI}
    built = {(a.materiality, a.urgency, a.thesis_impact) for a in (assess(materiality=m, urgency=u, thesis_impact=t) for m, u, t in combos)}
    assert built == combos and len(built) == 4 * 3 * 5
    low_but_invalidated = assess(materiality=M.LOW, urgency=U.ROUTINE, thesis_impact=TI.INVALIDATED)
    critical_but_unchanged = assess(materiality=M.CRITICAL, urgency=U.ROUTINE, thesis_impact=TI.UNCHANGED)
    assert (low_but_invalidated.materiality, low_but_invalidated.thesis_impact) == (M.LOW, TI.INVALIDATED)
    assert (critical_but_unchanged.urgency, critical_but_unchanged.thesis_impact) == (U.ROUTINE, TI.UNCHANGED)
    for basis in B:
        assert assess(materiality_basis=basis).materiality_basis is basis


def test_source_tier_does_not_alter_the_assessment() -> None:
    results = {tier: assess(binding(tier=tier)) for tier in SourceTier}
    for tier, assessment in results.items():
        assert assessment.binding.event.source_tier is tier
        assert (assessment.materiality, assessment.urgency, assessment.thesis_impact, assessment.impact_dimensions) == \
               (M.HIGH, U.PROMPT, TI.WEAKENED, (DIM.EARNINGS, DIM.VALUATION))
    for tier in SourceTier:
        for materiality in M:
            assert assess(binding(tier=tier), materiality=materiality, urgency=U.ROUTINE).materiality is materiality


def test_revision_kind_does_not_imply_a_severity() -> None:
    for kind in GameChangerRevisionKind:
        bound = binding(kind=kind)
        for materiality, urgency in itertools.product((M.LOW, M.CRITICAL), (U.ROUTINE, U.IMMEDIATE)):
            assessment = assess(bound, materiality=materiality, urgency=urgency)
            assert (assessment.materiality, assessment.urgency) == (materiality, urgency)
    for kind in (GameChangerRevisionKind.CORRECTION, GameChangerRevisionKind.WITHDRAWAL):
        assessment = assess(binding(kind=kind), materiality=M.LOW)
        assert assessment.binding.event.revision_kind is kind
    public = {n for n in dir(GameChangerMaterialityAssessment) if not n.startswith("_")}
    assert not [n for n in public if any(w in n for w in ("active", "supersed", "resolved", "family", "latest"))]


def test_systemic_event_has_no_automatic_assessment_defaults() -> None:
    systemic = binding(systemic=True)
    low = assess(systemic, materiality=M.LOW, urgency=U.ROUTINE, impact_dimensions=(DIM.OTHER,), thesis_impact=TI.UNCHANGED)
    assert (low.materiality, low.urgency, low.impact_dimensions) == (M.LOW, U.ROUTINE, (DIM.OTHER,))
    assert assess(systemic, materiality=M.CRITICAL, urgency=U.IMMEDIATE, impact_dimensions=(DIM.MACRO_EXPOSURE,)).impact_dimensions == (DIM.MACRO_EXPOSURE,)
    assert assess(systemic, impact_dimensions=(DIM.EARNINGS,)).impact_dimensions == (DIM.EARNINGS,)            # not forced to MACRO_EXPOSURE


def test_public_object_exposes_no_numeric_or_consequence_surface() -> None:
    assessment = assess()
    public = {n.lower() for n in dir(assessment) if not n.startswith("_")}
    banned = ("score", "probability", "confidence", "numeric", "weight", "threshold", "action", "quarantine", "freeze", "buy", "sell",
              "hold", "return", "alpha", "rationale", "summary", "explanation", "sentiment")
    assert not [n for n in public if any(b in n for b in banned)]
    assert public == {"binding", "materiality", "urgency", "thesis_impact", "impact_dimensions", "materiality_basis", "methodology_key",
                      "assessment_provenance_sha256"}


def test_equal_inputs_build_equal_assessments() -> None:
    bound = binding()
    assert assess(bound) == assess(bound)
    assert assess(bound) != assess(bound, urgency=U.IMMEDIATE)


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/game_changer_assessment.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_and_the_23a_binding_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "dataclasses", "enum", "re", "backend.engine.private.game_changer_event"}
    assert imported["backend.engine.private.game_changer_event"] == {"GameChangerEventPITBinding"}
    for forbidden in ("provider", "kap", "sec", "requests", "httpx", "browser", "scraper", "openai", "gemini", "claude", "llm", "langchain",
                      "langgraph", "prompt", "portfolio", "allocation", "rebalance", "tax", "database", "repository", "supabase", "numpy",
                      "scipy", "pandas", "decimal", "random", "datetime", "domain", "macro", "buffett", "confidence"):
        assert not any(forbidden in part for name in names for part in name.split(".")), forbidden


def test_no_clock_float_numeric_or_classification_runtime() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "float", "Decimal", "random", "getcontext", "sorted", "sort", "min", "max", "sum"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) in (float, complex)]
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.BinOp, ast.AugAssign))]                      # no arithmetic at all


_FRAGMENTS = ("provider", "scraper", "requests", "httpx", "browser", "openai", "gemini", "claude", "langchain", "langgraph", "prompt",
              "sentiment", "polarity", "bullish", "bearish", "score", "rank", "recommend", "predict", "classif", "portfolio",
              "allocation", "rebalance", "quarantine", "broker", "execution", "database", "repository", "supabase", "clock",
              "probab", "threshold", "classify", "infer", "severity")
_EXACT = {"kap", "sec", "llm", "tax", "order", "now", "today", "random", "buy", "sell", "hold", "trade", "weight", "alpha"}


def test_no_scoring_sentiment_llm_or_portfolio_identifiers() -> None:
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
    for identifier in identifiers - {"PROMPT"}:                              # PROMPT is the spec-mandated urgency member, not an LLM prompt
        lowered = identifier.lower()
        assert lowered not in _EXACT, identifier
        for fragment in _FRAGMENTS:
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    function = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "build_game_changer_materiality_assessment")
    assert not function.args.args and [a.arg for a in function.args.kwonlyargs] == [
        "binding", "materiality", "urgency", "thesis_impact", "impact_dimensions", "materiality_basis", "methodology_key",
        "assessment_provenance_sha256"]


def test_documents_the_assessment_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("categorical", "internal", "not a legal", "no numeric", "orthogonal", "methodology_key", "assessment_provenance_sha256",
                   "no sentiment", "no LLM", "no quarantine", "23C", "revision", "not data confidence"):
        assert needle in doc, needle
