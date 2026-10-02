"""
backend/tests/test_game_changer_extraction.py
=============================================
Phase 23D1: the safe structured-extraction boundary. A human or model extraction may supply only the Phase 23B categorical axes plus
provenance; it can never rewrite the Phase 23A event evidence, never exist before Sentinax observed the event or after the decision
cutoff, and only materializes into the closed Phase 23B assessment. No model runtime, no provider access, no portfolio consequence.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import game_changer_extraction as module_under_test
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, SourceTier
from backend.engine.private.game_changer_assessment import (
    GameChangerImpactDimension,
    GameChangerMaterialityAssessment,
    GameChangerMaterialityBasis,
    GameChangerMateriality,
    GameChangerThesisImpact,
    GameChangerUrgency,
)
from backend.engine.private.game_changer_event import (
    GameChangerEvent,
    GameChangerEventPITBinding,
    GameChangerEventScope,
    GameChangerEventType,
    GameChangerRevisionKind,
    bind_game_changer_event_pit,
)
from backend.engine.private.game_changer_extraction import (
    GameChangerExtractionMode,
    GameChangerStructuredAssessmentExtraction,
    build_game_changer_structured_assessment_extraction,
    materialize_game_changer_materiality_assessment,
)
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
M, U, TI, B, DIM = GameChangerMateriality, GameChangerUrgency, GameChangerThesisImpact, GameChangerMaterialityBasis, GameChangerImpactDimension
MODE = GameChangerExtractionMode
SOURCE_SHA, OUTPUT_SHA = "a" * 64, "b" * 64


def T(h=12, mi=0, s=0, us=0, tz=UTC) -> datetime:
    return datetime(2026, 9, 1, h, mi, s, us, tzinfo=tz)


def binding(cutoff=T(13), mode=AsOfMode.SOURCE_AS_OF, published=T(10), observed=T(12), content=SOURCE_SHA) -> GameChangerEventPITBinding:
    event = GameChangerEvent(
        source_key="kap", source_event_key="EVT-1", source_tier=SourceTier.TIER_1_REGULATORY, scope=GameChangerEventScope.INSTRUMENT,
        event_type=GameChangerEventType.OPERATIONS, instrument_ids=(UUID(int=1),), effective_date=None, published_at=published,
        observed_at=observed, content_sha256=content, revision_kind=GameChangerRevisionKind.ORIGINAL, revises_source_event_key=None)
    return bind_game_changer_event_pit(event=event, context=AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff))


def values(**changes):
    base = dict(binding=binding(), extraction_mode=MODE.MODEL, materiality=M.HIGH, urgency=U.PROMPT, thesis_impact=TI.WEAKENED,
                impact_dimensions=(DIM.EARNINGS, DIM.VALUATION), materiality_basis=B.NATURE_AND_MAGNITUDE,
                methodology_key="game_changer.materiality.v1", extractor_key="model.vendor.family", extractor_revision=3,
                extracted_at=T(12, 5), source_content_sha256=SOURCE_SHA, extraction_output_sha256=OUTPUT_SHA)
    base.update(changes)
    return base


def extraction(**changes) -> GameChangerStructuredAssessmentExtraction:
    return build_game_changer_structured_assessment_extraction(**values(**changes))


# --- enum / stored fields ----------------------------------------------------------------------------------------

def test_mode_enum_and_exact_stored_fields() -> None:
    assert [(m.name, m.value) for m in MODE] == [("HUMAN", "human"), ("MODEL", "model")]
    names = [f.name for f in dataclasses.fields(GameChangerStructuredAssessmentExtraction)]
    assert names == ["binding", "extraction_mode", "materiality", "urgency", "thesis_impact", "impact_dimensions", "materiality_basis",
                     "methodology_key", "extractor_key", "extractor_revision", "extracted_at", "source_content_sha256", "extraction_output_sha256"]
    fields = dataclasses.fields(GameChangerStructuredAssessmentExtraction)
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    with pytest.raises(dataclasses.FrozenInstanceError):
        extraction().materiality = M.LOW  # type: ignore[misc]


def test_no_event_evidence_prompt_text_or_confidence_fields() -> None:
    names = {f.name for f in dataclasses.fields(GameChangerStructuredAssessmentExtraction)}
    assert not names & {"event_type", "source_key", "source_event_key", "source_tier", "scope", "instrument_ids", "effective_date",
                        "published_at", "observed_at", "revision_kind", "revises_source_event_key", "content_sha256", "context"}
    assert not [n for n in names if any(w in n for w in ("prompt", "reason", "rationale", "confidence", "probab", "score", "headline", "body",
                                                         "text", "html", "pdf", "json", "bytes", "certainty"))]


def test_builder_and_materializer_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        build_game_changer_structured_assessment_extraction(*values().values())  # type: ignore[misc]
    with pytest.raises(TypeError):
        build_game_changer_structured_assessment_extraction(binding=binding())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        build_game_changer_structured_assessment_extraction(**values(), event_type="x")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        materialize_game_changer_materiality_assessment(extraction())  # type: ignore[misc]
    with pytest.raises(TypeError):
        materialize_game_changer_materiality_assessment(extraction=extraction(), materiality=M.LOW)  # type: ignore[call-arg]


# --- binding authority -------------------------------------------------------------------------------------------

def test_binding_must_be_an_exact_phase_23a_binding_retained_by_identity() -> None:
    bound = binding()
    assert extraction(binding=bound).binding is bound

    class SubBinding(GameChangerEventPITBinding):
        pass

    for bad in (bound.event, bound.context, None, object(), SubBinding(event=bound.event, context=bound.context)):
        with pytest.raises(TypeError):
            extraction(binding=bad)


def test_mode_and_categorical_fields_must_be_exact() -> None:
    for field, bads in (("extraction_mode", ["human", "model", None, 1, M.LOW]), ("materiality", ["high", None, 1, U.PROMPT]),
                        ("urgency", ["prompt", None, 1, M.HIGH]), ("thesis_impact", ["weakened", None, 1, M.LOW]),
                        ("materiality_basis", ["nature", None, 1, M.LOW])):
        for bad in bads:
            with pytest.raises(TypeError):
                extraction(**{field: bad})


def test_impact_dimensions_follow_the_closed_phase_23b_contract() -> None:
    assert extraction(impact_dimensions=(DIM.OTHER,)).impact_dimensions == (DIM.OTHER,)
    for bad in ([DIM.EARNINGS], None, "earnings", ("earnings",)):
        with pytest.raises(TypeError):
            extraction(impact_dimensions=bad)
    for bad in ((), (DIM.EARNINGS, DIM.EARNINGS), (DIM.VALUATION, DIM.EARNINGS), (DIM.EARNINGS, DIM.OTHER), tuple(DIM)):
        with pytest.raises(ValueError):
            extraction(impact_dimensions=bad)


# --- provenance --------------------------------------------------------------------------------------------------

def test_source_content_hash_must_equal_the_event_content_hash() -> None:
    assert extraction(binding=binding(content="a" * 64), source_content_sha256="a" * 64).source_content_sha256 == "a" * 64
    with pytest.raises(ValueError):
        extraction(binding=binding(content="a" * 64), source_content_sha256="b" * 64)           # never silently replaced
    for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", "a" * 64 + "\n"):
        with pytest.raises(ValueError):
            extraction(source_content_sha256=bad)
    for bad in (None, 1, b"a" * 64):
        with pytest.raises(TypeError):
            extraction(source_content_sha256=bad)


def test_output_hash_is_separate_and_unconstrained_relative_to_the_source_hash() -> None:
    assert extraction(extraction_output_sha256="c" * 64).extraction_output_sha256 == "c" * 64
    assert extraction(extraction_output_sha256=SOURCE_SHA).extraction_output_sha256 == SOURCE_SHA   # equality is also allowed
    for bad in ("B" * 64, "b" * 63, "b" * 65, "z" * 64, "", "b" * 64 + "\n"):
        with pytest.raises(ValueError):
            extraction(extraction_output_sha256=bad)
    for bad in (None, 1, b"b" * 64):
        with pytest.raises(TypeError):
            extraction(extraction_output_sha256=bad)


def test_methodology_and_extractor_identity() -> None:
    class SubStr(str):
        pass

    for ok in ("manual.review", "model.vendor.model_family", "x", "a" * 128, "0-a_b.c"):
        assert extraction(extractor_key=ok).extractor_key == ok
        assert extraction(methodology_key=ok).methodology_key == ok
    for field in ("extractor_key", "methodology_key"):
        for bad in ("", " x", "x ", "X", "a" * 129, "a b", "x\n", "-x", "https://x.y"):
            with pytest.raises(ValueError):
                extraction(**{field: bad})
        for bad in (None, 1, b"x", SubStr("x")):
            with pytest.raises(TypeError):
                extraction(**{field: bad})


def test_extractor_revision_is_an_exact_positive_int() -> None:
    for ok in (1, 2, 10 ** 12):
        assert extraction(extractor_revision=ok).extractor_revision == ok
    for bad in (0, -1):
        with pytest.raises(ValueError):
            extraction(extractor_revision=bad)
    for bad in (True, False, 1.0, "1", Decimal(1), None, [1]):
        with pytest.raises(TypeError):
            extraction(extractor_revision=bad)


# --- extraction time ---------------------------------------------------------------------------------------------

def test_extraction_time_window_has_inclusive_exact_bounds() -> None:
    observed, cutoff = T(12), T(13)
    for ok in (observed, T(12, 5), cutoff):
        assert extraction(binding=binding(cutoff=cutoff, observed=observed), extracted_at=ok).extracted_at == ok
    for bad in (observed - timedelta(microseconds=1), cutoff + timedelta(microseconds=1), T(11), T(14)):
        with pytest.raises(ValueError):
            extraction(binding=binding(cutoff=cutoff, observed=observed), extracted_at=bad)


def test_source_as_of_event_cannot_be_retroactively_extracted() -> None:
    early = binding(cutoff=T(11), published=T(10), observed=T(12))                         # market could know it; Sentinax had not
    assert early.event.observed_at > early.context.knowledge_cutoff
    for stamp in (T(11), T(12), T(12, 5), T(10)):
        with pytest.raises(ValueError):
            extraction(binding=early, extracted_at=stamp)
    current = binding(cutoff=T(13), published=T(10), observed=T(12))
    assert extraction(binding=current, extracted_at=T(12, 5)).extracted_at == T(12, 5)
    for mode in AsOfMode:
        assert extraction(binding=binding(mode=mode), extracted_at=T(12, 5)).binding.context.mode is mode


def test_timezone_hardening() -> None:
    plus9, minus5 = timezone(timedelta(hours=9)), timezone(timedelta(hours=-5))
    assert extraction(extracted_at=T(21, 0, tz=plus9)).extracted_at == T(12)                # equals observed_at as an instant
    assert extraction(extracted_at=T(8, 0, tz=minus5)).extracted_at == T(13)                # equals the cutoff as an instant
    with pytest.raises(ValueError):
        extraction(extracted_at=T(21, 0, 0, 0, tz=plus9) - timedelta(microseconds=1))
    with pytest.raises(ValueError):
        extraction(extracted_at=T(8, 0, 0, 1, tz=minus5))

    class Broken(tzinfo):
        def utcoffset(self, dt):
            raise RuntimeError("boom")

    class NoOffset(tzinfo):
        def utcoffset(self, dt):
            return None

    class SubDatetime(datetime):
        pass

    for bad in (datetime(2026, 9, 1, 12, 5), datetime(2026, 9, 1, 12, 5, tzinfo=Broken()), datetime(2026, 9, 1, 12, 5, tzinfo=NoOffset()),
                SubDatetime(2026, 9, 1, 12, 5, tzinfo=UTC), "2026-09-01T12:05:00Z", None, 1):
        with pytest.raises(TypeError):
            extraction(extracted_at=bad)


# --- materialization ---------------------------------------------------------------------------------------------

def test_materialization_preserves_every_field_and_the_binding_identity() -> None:
    source = extraction()
    assessment = materialize_game_changer_materiality_assessment(extraction=source)
    assert type(assessment) is GameChangerMaterialityAssessment
    assert assessment.binding is source.binding
    assert assessment.materiality is source.materiality and assessment.urgency is source.urgency
    assert assessment.thesis_impact is source.thesis_impact and assessment.materiality_basis is source.materiality_basis
    assert assessment.impact_dimensions == source.impact_dimensions
    assert assessment.methodology_key == source.methodology_key
    assert assessment.assessment_provenance_sha256 == source.extraction_output_sha256
    assert assessment.binding.event.content_sha256 == source.source_content_sha256
    with pytest.raises(TypeError):
        materialize_game_changer_materiality_assessment(extraction=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        materialize_game_changer_materiality_assessment(extraction=assessment)  # type: ignore[arg-type]


def test_human_and_model_extractions_materialize_equal_assessments() -> None:
    bound = binding()
    human = materialize_game_changer_materiality_assessment(extraction=extraction(binding=bound, extraction_mode=MODE.HUMAN))
    model = materialize_game_changer_materiality_assessment(extraction=extraction(binding=bound, extraction_mode=MODE.MODEL))
    assert human == model
    assert extraction(binding=bound, extraction_mode=MODE.HUMAN).extraction_mode is MODE.HUMAN
    assert extraction(binding=bound, extraction_mode=MODE.HUMAN) != extraction(binding=bound, extraction_mode=MODE.MODEL)   # audit provenance only


def test_every_categorical_combination_materializes_without_mode_influence() -> None:
    bound = binding()
    for materiality, urgency, thesis in ((M.LOW, U.ROUTINE, TI.UNCHANGED), (M.CRITICAL, U.IMMEDIATE, TI.INVALIDATED), (M.MEDIUM, U.PROMPT, TI.UNCERTAIN)):
        results = {materialize_game_changer_materiality_assessment(extraction=extraction(
            binding=bound, extraction_mode=mode, materiality=materiality, urgency=urgency, thesis_impact=thesis)) for mode in MODE}
        assert len(results) == 1


def test_direct_construction_enforces_the_full_contract() -> None:
    good = extraction()
    fields = {f.name: getattr(good, f.name) for f in dataclasses.fields(good)}
    assert GameChangerStructuredAssessmentExtraction(**fields) == good
    for change in (dict(extracted_at=T(14)), dict(extracted_at=T(11)), dict(source_content_sha256="b" * 64), dict(extractor_revision=0),
                   dict(extractor_revision=True), dict(impact_dimensions=(DIM.EARNINGS, DIM.OTHER)), dict(extractor_key="X")):
        with pytest.raises((ValueError, TypeError)):
            GameChangerStructuredAssessmentExtraction(**{**fields, **change})


def test_extraction_is_deterministic() -> None:
    bound = binding()
    assert extraction(binding=bound) == extraction(binding=bound)


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/game_changer_extraction.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_and_the_closed_23a_23b_surface_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "dataclasses", "datetime", "enum", "re", "backend.engine.private.game_changer_event",
                     "backend.engine.private.game_changer_assessment"}
    assert imported["backend.engine.private.game_changer_event"] == {"GameChangerEventPITBinding"}
    assert imported["backend.engine.private.game_changer_assessment"] <= {
        "GameChangerImpactDimension", "GameChangerMateriality", "GameChangerMaterialityAssessment", "GameChangerMaterialityBasis",
        "GameChangerThesisImpact", "GameChangerUrgency", "build_game_changer_materiality_assessment"}
    for forbidden in ("kap", "mkk", "sec", "provider", "requests", "httpx", "aiohttp", "browser", "selenium", "playwright", "socket", "urllib",
                      "database", "repository", "supabase", "openai", "anthropic", "claude", "gemini", "langchain", "langgraph", "transformers",
                      "torch", "numpy", "scipy", "pandas", "decimal", "random", "revision_family", "gate", "allocation", "rebalance",
                      "portfolio", "analysis_pit", "domain", "macro"):
        assert not any(forbidden in part for name in names for part in name.split(".")), forbidden


def test_no_clock_float_numeric_or_hashing_runtime() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "float", "Decimal", "random", "sha256", "hashlib", "hexdigest", "sorted", "sort", "max", "min", "sum"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) in (float, complex)]
    arithmetic = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
    assert not [n for n in ast.walk(_TREE) if (isinstance(n, ast.BinOp) and isinstance(n.op, arithmetic)) or isinstance(n, ast.AugAssign)]


_FRAGMENTS = ("quarantine", "new_capital", "review_state", "target_weight", "allocation", "rebalance", "portfolio", "execution", "kap", "mkk",
              "provider", "requests", "httpx", "aiohttp", "browser", "selenium", "playwright", "socket", "database", "repository", "supabase",
              "openai", "anthropic", "claude", "gemini", "langchain", "langgraph", "transformers", "torch", "prompt", "rationale",
              "reasoning", "confidence", "probab", "certainty", "score", "headline", "raw_text", "html", "pdf", "sentiment", "rank",
              "classif", "predict", "recommend", "broker", "schedul", "notif", "persist")
_EXACT = {"sell", "buy", "hold", "trade", "order", "sec", "llm", "tax", "now", "today", "random", "weight"}


def test_no_gate_portfolio_provider_model_sdk_or_text_surface() -> None:
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
    for name, inputs in (("build_game_changer_structured_assessment_extraction", 13), ("materialize_game_changer_materiality_assessment", 1)):
        function = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
        assert not function.args.args and len(function.args.kwonlyargs) == inputs
    members = {m.name for m in MODE}
    assert members == {"HUMAN", "MODEL"}                                                    # MODEL is the one explicitly allowed model word


def test_documents_the_extraction_boundary() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("HUMAN", "MODEL", "observed_at <= extracted_at <= knowledge_cutoff", "source_content_sha256", "extraction_output_sha256",
                   "methodology_key", "extractor_key", "no model runtime", "no confidence", "no raw text", "no provider", "no direct",
                   "23D2", "SOURCE_AS_OF"):
        assert needle in doc, needle
