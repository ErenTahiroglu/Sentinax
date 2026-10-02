"""
backend/tests/test_backtest_analysis_context.py
===============================================
Phase 26C1: binding of a Phase 26A1 replay point and an explicit Horizon into the canonical AnalysisTemporalContext. The replay PIT context object is the
analysis PIT context object, the replay evaluation_date is the analysis horizon as_of_date, and the horizon is always explicit. Pure: no input resolution,
no completeness claim, no decision or execution.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.engine.private import backtest_analysis_context as module_under_test
from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.backtest_analysis_context import PrivateBacktestAnalysisContext, build_private_backtest_analysis_context
from backend.engine.private.backtest_replay_point import PrivateBacktestReplayPoint, build_private_backtest_replay_point
from backend.engine.private.domain import AsOfMode, Horizon, HorizonFamily, RiskAxis
from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence_availability import RiskEvidenceAvailabilityRef
from backend.engine.private.risk_evidence_pit_binding import RiskEvidencePITBinding
from backend.engine.private.risk_evidence_provenance import RiskEvidenceProvenanceRef
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
SO, SY = AsOfMode.SOURCE_AS_OF, AsOfMode.SYSTEM_AS_OF
CUTOFF = datetime(2026, 1, 10, 10, 0, tzinfo=PLUS3)             # 07:00 UTC
EVAL = date(2026, 1, 9)


def point(mode=SO, cutoff=CUTOFF, evaluation_date=EVAL) -> PrivateBacktestReplayPoint:
    return build_private_backtest_replay_point(pit_context=AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff), evaluation_date=evaluation_date)


def ctx(p=None, horizon=Horizon.ALLOCATION_12M) -> PrivateBacktestAnalysisContext:
    return build_private_backtest_analysis_context(replay_point=p or point(), horizon=horizon)


def forged_temporal(pit, as_of_date, horizon=Horizon.ALLOCATION_12M) -> AnalysisTemporalContext:
    return AnalysisTemporalContext(horizon_context=AnalysisHorizonContext(horizon=horizon, as_of_date=as_of_date), pit_context=pit)


# --- contract ------------------------------------------------------------------------------------------------------

def test_stored_shape_and_builder_signature() -> None:
    fields = dataclasses.fields(PrivateBacktestAnalysisContext)
    assert [f.name for f in fields] == ["replay_point", "temporal_context"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    assert PrivateBacktestAnalysisContext.__dataclass_params__.frozen is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx().temporal_context = None  # type: ignore[misc]
    parameters = list(inspect.signature(build_private_backtest_analysis_context).parameters.values())
    assert [p.name for p in parameters] == ["replay_point", "horizon"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)
    p = point()
    for extra in ("pit_context", "evaluation_date", "horizon_context", "temporal_context", "knowledge_cutoff"):
        with pytest.raises(TypeError):
            build_private_backtest_analysis_context(replay_point=p, horizon=Horizon.TACTICAL_1M, **{extra: 1})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_private_backtest_analysis_context(replay_point=p)  # type: ignore[call-arg]
    for derived in ("horizon", "horizon_context", "pit_context", "evaluation_date", "knowledge_cutoff"):
        assert derived not in {f.name for f in fields}


@pytest.mark.parametrize("horizon", list(Horizon))
def test_every_canonical_horizon_binds_explicitly(horizon: Horizon) -> None:
    p = point()
    result = build_private_backtest_analysis_context(replay_point=p, horizon=horizon)
    assert result.horizon is horizon and result.horizon_context.horizon is horizon
    assert result.horizon_context.as_of_date == p.evaluation_date == EVAL
    assert result.pit_context is p.pit_context is result.temporal_context.pit_context
    assert result.replay_point is p and result.horizon_context is result.temporal_context.horizon_context
    assert type(result.temporal_context) is AnalysisTemporalContext and type(result.horizon_context) is AnalysisHorizonContext
    assert result.horizon_context.family in set(HorizonFamily)


@pytest.mark.parametrize("mode", [SO, SY])
def test_both_pit_modes_pass_through_the_identical_pit_object(mode: AsOfMode) -> None:
    p = point(mode=mode)
    result = ctx(p)
    assert result.pit_context is p.pit_context and result.temporal_context.pit_context.mode is mode


def test_non_utc_frontier_is_not_normalized_or_rebuilt() -> None:
    p = point()
    result = ctx(p)
    assert result.pit_context.knowledge_cutoff is CUTOFF and result.temporal_context.pit_context.knowledge_cutoff.utcoffset() == timedelta(hours=3)
    assert result.pit_context.knowledge_cutoff_utc == datetime(2026, 1, 10, 7, 0, tzinfo=UTC)


def test_future_evaluation_date_is_accepted_and_is_the_horizon_as_of_date() -> None:
    p = point(cutoff=datetime(2026, 1, 10, 7, 0, tzinfo=UTC), evaluation_date=date(2026, 1, 20))
    assert p.evaluation_date > p.knowledge_cutoff.date()
    assert ctx(p).horizon_context.as_of_date == date(2026, 1, 20)
    earlier = point(evaluation_date=date(2025, 6, 1))
    assert ctx(earlier).horizon_context.as_of_date == date(2025, 6, 1) != earlier.knowledge_cutoff.date()          # never derived from the cutoff


def test_multiple_horizons_share_one_replay_point_and_pit_object() -> None:
    p = point()
    a, b, c = ctx(p, Horizon.TACTICAL_1M), ctx(p, Horizon.ALLOCATION_12M), ctx(p, Horizon.STRATEGIC_5Y)
    assert a.replay_point is b.replay_point is c.replay_point is p and a.pit_context is b.pit_context is c.pit_context
    assert a.horizon is not b.horizon and b.horizon is not c.horizon and a != b
    assert ctx(point(evaluation_date=date(2026, 3, 1)), Horizon.ALLOCATION_12M).horizon is Horizon.ALLOCATION_12M   # same horizon across different points


def test_equal_by_value_foreign_pit_context_is_rejected() -> None:
    p = point()
    pit_b = AnalysisPITContext(mode=p.pit_context.mode, knowledge_cutoff=p.pit_context.knowledge_cutoff)
    assert pit_b == p.pit_context and pit_b is not p.pit_context
    with pytest.raises(ValueError):
        PrivateBacktestAnalysisContext(replay_point=p, temporal_context=forged_temporal(pit_b, p.evaluation_date))
    PrivateBacktestAnalysisContext(replay_point=p, temporal_context=forged_temporal(p.pit_context, p.evaluation_date))
    other_mode = AnalysisPITContext(mode=SY if p.as_of_mode is SO else SO, knowledge_cutoff=p.knowledge_cutoff)
    with pytest.raises(ValueError):
        PrivateBacktestAnalysisContext(replay_point=p, temporal_context=forged_temporal(other_mode, p.evaluation_date))


def test_wrong_horizon_date_is_rejected_without_tolerance() -> None:
    p = point(evaluation_date=date(2026, 1, 20))
    for wrong in (date(2026, 1, 19), date(2026, 1, 21), p.knowledge_cutoff.date(), date(2025, 1, 20)):
        with pytest.raises(ValueError):
            PrivateBacktestAnalysisContext(replay_point=p, temporal_context=forged_temporal(p.pit_context, wrong))
    PrivateBacktestAnalysisContext(replay_point=p, temporal_context=forged_temporal(p.pit_context, date(2026, 1, 20), Horizon.STRATEGIC_3Y))


def test_exact_types_in_builder_and_direct_construction() -> None:
    class SubPoint(PrivateBacktestReplayPoint):
        pass

    class SubTemporal(AnalysisTemporalContext):
        pass

    p = point()
    sub_point = SubPoint(pit_context=p.pit_context, evaluation_date=p.evaluation_date)
    good_temporal = forged_temporal(p.pit_context, p.evaluation_date)
    sub_temporal = SubTemporal(horizon_context=good_temporal.horizon_context, pit_context=p.pit_context)
    for bad in (None, object(), sub_point, p.pit_context, "point"):
        with pytest.raises(TypeError):
            build_private_backtest_analysis_context(replay_point=bad, horizon=Horizon.TACTICAL_1M)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            PrivateBacktestAnalysisContext(replay_point=bad, temporal_context=good_temporal)  # type: ignore[arg-type]
    for bad in (None, object(), sub_temporal, p.pit_context, "temporal"):
        with pytest.raises(TypeError):
            PrivateBacktestAnalysisContext(replay_point=p, temporal_context=bad)  # type: ignore[arg-type]
    for bad in (None, "12M", 12, True, HorizonFamily.ALLOCATION if hasattr(HorizonFamily, "ALLOCATION") else list(HorizonFamily)[0], AsOfMode.SOURCE_AS_OF, object()):
        with pytest.raises(TypeError):
            build_private_backtest_analysis_context(replay_point=p, horizon=bad)  # type: ignore[arg-type]


# --- composition with the existing risk axes (test-only dependency) ------------------------------------------------

def test_risk_context_reuses_the_exact_temporal_context() -> None:
    result = ctx()
    risk = RiskAxisContext(axis=RiskAxis.TOLERANCE, temporal_context=result.temporal_context)
    assert risk.temporal_context is result.temporal_context and risk.temporal_context.pit_context is result.replay_point.pit_context


def test_risk_evidence_pit_semantics_compose_without_modification() -> None:
    result = ctx()
    risk = RiskAxisContext(axis=RiskAxis.TOLERANCE, temporal_context=result.temporal_context)
    provenance = RiskEvidenceProvenanceRef(source_key="survey_v1", content_sha256="0123456789abcdef" * 4)
    before = RiskEvidenceAvailabilityRef(provenance_ref=provenance, available_at=datetime(2026, 1, 10, 6, 59, tzinfo=UTC))
    at_cutoff = RiskEvidenceAvailabilityRef(provenance_ref=provenance, available_at=datetime(2026, 1, 10, 7, 0, tzinfo=UTC))
    after = RiskEvidenceAvailabilityRef(provenance_ref=provenance, available_at=datetime(2026, 1, 10, 7, 0, 0, 1, tzinfo=UTC))
    assert RiskEvidencePITBinding(context=risk, availability_ref=before).context is risk
    assert RiskEvidencePITBinding(context=risk, availability_ref=at_cutoff).availability_ref is at_cutoff
    with pytest.raises(ValueError):
        RiskEvidencePITBinding(context=risk, availability_ref=after)


# --- source-level invariants ---------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_analysis_context.py"


def test_module_is_registered_pure_and_clean_on_all_guards() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_only_the_four_allowed_pure_modules() -> None:
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules <= {"backend.engine.private.backtest_replay_point", "backend.engine.private.analysis_horizon", "backend.engine.private.analysis_context",
                       "backend.engine.private.domain", "backend.engine.private.analysis_pit"}
    assert {"backend.engine.private.backtest_replay_point", "backend.engine.private.analysis_horizon", "backend.engine.private.analysis_context",
            "backend.engine.private.domain"} <= modules
    for forbidden in ("backtest_replay_plan", "backtest_market_data_bridge", "market_data", "portfolio", "macro", "game_changer", "risk_context", "risk_evidence",
                      "scheduler"):
        assert not any(forbidden in m for m in modules), forbidden
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Import)]


def test_no_inference_clock_hash_random_io_or_loop() -> None:
    names = ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
             | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})
    assert not names & {"now", "utcnow", "today", "time", "uuid4", "random", "secrets", "urandom", "sha256", "hashlib", "open", "client", "rpc", "table",
                        "RiskAxisContext", "MarketDataResolutionMode", "knowledge_cutoff_utc", "knowledge_cutoff", "HistoricalInputBundle", "CompletenessStatus"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.For, ast.While, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await, ast.Try))]
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Attribute) and n.attr == "date" and isinstance(n.value, ast.Attribute)]      # no cutoff.date()


def test_documentation_states_the_binding() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PRIVATE_BACKTEST_ANALYSIS_CONTEXT.md").read_text(encoding="utf-8")
    for needle in ("knowledge frontier", "evaluation date", "horizon", "as_of_date", "never inferred", "multiple horizons", "completeness"):
        assert needle in doc, needle
