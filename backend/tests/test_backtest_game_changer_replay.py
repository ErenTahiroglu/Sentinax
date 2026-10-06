"""
backend/tests/test_backtest_game_changer_replay.py
==================================================
Phase 26D1: deterministic historical Game Changer decision replay. A COMPLETE Phase 26C2E bundle whose declared contract is exactly one portfolio-history slot plus
one or more Game Changer slots is replayed through the closed Phase 23C2 gate, once per historical family, in the bundle's canonical order with resolution identity
preserved. No second policy, no other decision engine, no market data, no sale / exit / target weight / order, no aggregation.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
from pathlib import Path

import pytest

from backend.engine.private import backtest_game_changer_replay as module_under_test
from backend.engine.private.backtest_game_changer_replay import (
    PrivateBacktestGameChangerDecisionReplay,
    replay_private_backtest_game_changer_decision,
)
from backend.engine.private.backtest_input_bindings import bind_private_backtest_game_changer_input
from backend.engine.private.backtest_input_completeness import (
    PrivateBacktestInputBundle,
    PrivateBacktestInputCompletenessStatus,
    PrivateBacktestInputKind as K,
    PrivateBacktestInputRequirement as R,
)
from backend.engine.private.game_changer_assessment import GameChangerMateriality as M, GameChangerThesisImpact as TI, GameChangerUrgency as U
from backend.engine.private.game_changer_event import GameChangerRevisionKind as RK
from backend.engine.private.game_changer_gate import (
    GameChangerDecisionGate,
    GameChangerInstrumentNewCapitalGate as G,
    GameChangerReviewReason,
    GameChangerReviewState as S,
    build_game_changer_decision_gate,
)
from backend.engine.private.game_changer_revision_family import GameChangerRevisionFamilyCoverage as FC
from backend.tests.invariants import static_guards as sg
from backend.tests.test_backtest_input_bindings import analysis, gc_assessment, gc_resolve
from backend.tests.test_backtest_input_completeness import cand, macro, make, market, only_portfolio, risk, views

SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64


def family(context, sha, *assessments, coverage=FC.COMPLETE_AT_CUTOFF):
    resolution = gc_resolve(list(assessments) or [gc_assessment("EVT-1", context=context.pit_context)], coverage=coverage, sha=sha)
    return bind_private_backtest_game_changer_input(analysis_context=context, resolution=resolution)


def state(context, sha, coverage=FC.COMPLETE_AT_CUTOFF, **assessment):
    return family(context, sha, gc_assessment("EVT-1", context=context.pit_context, **assessment), coverage=coverage)


def d1_bundle(context, *bindings):
    pf, preq = only_portfolio(context)
    reqs = (preq,) + tuple(R(K.GAME_CHANGER, b.resolution.family.coverage_provenance_sha256) for b in bindings)
    return make(context, reqs, portfolio_history=pf, game_changers=tuple(bindings))


def replay(bundle):
    return replay_private_backtest_game_changer_decision(input_bundle=bundle)


def direct(gates, bundle):
    return PrivateBacktestGameChangerDecisionReplay(input_bundle=bundle, game_changer_gates=gates)


# --- A, B, C: contract -----------------------------------------------------------------------------------------------

def test_result_is_exactly_two_frozen_fields_without_defaults() -> None:
    fs = dataclasses.fields(PrivateBacktestGameChangerDecisionReplay)
    assert [f.name for f in fs] == ["input_bundle", "game_changer_gates"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestGameChangerDecisionReplay.__dataclass_params__.frozen is True
    context = analysis()
    result = replay(d1_bundle(context, state(context, SHA_A)))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.game_changer_gates = ()  # type: ignore[misc]


def test_builder_signature_is_exactly_keyword_only_input_bundle() -> None:
    params = inspect.signature(replay_private_backtest_game_changer_decision).parameters
    assert list(params) == ["input_bundle"]
    assert params["input_bundle"].kind is inspect.Parameter.KEYWORD_ONLY and params["input_bundle"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        replay_private_backtest_game_changer_decision(d1_bundle((c := analysis()), state(c, SHA_A)))  # type: ignore[misc]


def test_input_bundle_subclass_and_non_bundle_are_rejected() -> None:
    context = analysis()
    bundle = d1_bundle(context, state(context, SHA_A))

    class Sub(PrivateBacktestInputBundle):
        pass

    sub = Sub(**{f.name: getattr(bundle, f.name) for f in dataclasses.fields(bundle)})
    for bad in (sub, object(), None, bundle.analysis_context):
        with pytest.raises(TypeError):
            replay(bad)


# --- D, E: COMPLETE admission ----------------------------------------------------------------------------------------

def test_incomplete_bundle_is_rejected_not_partially_replayed() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    binding = state(context, SHA_A)
    incomplete = make(context, (preq, R(K.GAME_CHANGER, SHA_A), R(K.GAME_CHANGER, SHA_B)), portfolio_history=pf, game_changers=(binding,))
    assert incomplete.status is PrivateBacktestInputCompletenessStatus.INCOMPLETE
    with pytest.raises(ValueError):
        replay(incomplete)
    no_portfolio = make(context, (preq, R(K.GAME_CHANGER, SHA_A)), portfolio_history=None, game_changers=(binding,))
    with pytest.raises(ValueError):
        replay(no_portfolio)


def test_complete_status_with_a_nonempty_missing_tuple_cannot_be_forged_through_d1() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    binding = state(context, SHA_A)
    incomplete = make(context, (preq, R(K.GAME_CHANGER, SHA_A), R(K.GAME_CHANGER, SHA_B)), portfolio_history=pf, game_changers=(binding,))
    with pytest.raises(ValueError):                                              # C2E itself refuses the forged field combination
        dataclasses.replace(incomplete, status=PrivateBacktestInputCompletenessStatus.COMPLETE)
    forged = copy.copy(incomplete)                                                # bypass C2E validation entirely
    object.__setattr__(forged, "status", PrivateBacktestInputCompletenessStatus.COMPLETE)
    assert forged.status is PrivateBacktestInputCompletenessStatus.COMPLETE and forged.missing_requirements != ()
    with pytest.raises(ValueError):
        replay(forged)


# --- F-N: exact D1 requirement surface -------------------------------------------------------------------------------

def test_portfolio_plus_game_changer_manifest_is_accepted() -> None:
    context = analysis()
    result = replay(d1_bundle(context, state(context, SHA_A)))
    assert len(result.game_changer_gates) == 1
    context = analysis()
    many = replay(d1_bundle(context, state(context, SHA_A), state(context, SHA_B), state(context, SHA_C)))
    assert len(many.game_changer_gates) == 3


@pytest.mark.parametrize("kind", [K.CANDIDATE_UNIVERSE, K.MACRO, K.RISK_EVIDENCE, K.MARKET_DATA, K.USER_VIEWS])
def test_any_other_requirement_kind_is_rejected_even_when_complete(kind) -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    binding = state(context, SHA_A)
    extra = {
        K.CANDIDATE_UNIVERSE: (R(K.CANDIDATE_UNIVERSE, "x"), "candidate_universes", None),
        K.MACRO: (R(K.MACRO, "TR_FX_USDTRY"), "macro_inputs", macro),
        K.RISK_EVIDENCE: (R(K.RISK_EVIDENCE, "cash_balance"), "risk_evidence", risk),
        K.MARKET_DATA: (None, "market_data", None),
        K.USER_VIEWS: (R(K.USER_VIEWS, "user_views"), "user_views", None),
    }[kind]
    requirement, field, factory = extra
    if kind is K.CANDIDATE_UNIVERSE:
        evidence = cand(context)
        requirement = R(K.CANDIDATE_UNIVERSE, f"{evidence.resolution.query.source_key}|{evidence.resolution.query.universe_key}|equity")
        value = (evidence,)
    elif kind is K.MARKET_DATA:
        snapshot, key, _, _ = market(context)
        requirement = R(K.MARKET_DATA, f"{snapshot.kind.value}|{key.to_string()}")
        value = (snapshot,)
    elif kind is K.USER_VIEWS:
        value = views(context)
    else:
        value = (factory(context),)
    bundle = make(context, (preq, R(K.GAME_CHANGER, SHA_A), requirement), portfolio_history=pf, game_changers=(binding,), **{field: value})
    assert bundle.status is PrivateBacktestInputCompletenessStatus.COMPLETE
    with pytest.raises(ValueError):
        replay(bundle)


def test_zero_game_changer_requirements_are_rejected_and_not_read_as_no_events() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    bundle = make(context, (preq,), portfolio_history=pf)
    assert bundle.status is PrivateBacktestInputCompletenessStatus.COMPLETE and bundle.game_changers == ()
    with pytest.raises(ValueError):
        replay(bundle)


# --- O-Q: gate derivation, order, identity ---------------------------------------------------------------------------

def test_exactly_one_closed_gate_is_built_per_binding(monkeypatch) -> None:
    context = analysis()
    bundle = d1_bundle(context, state(context, SHA_A), state(context, SHA_B))
    calls = []
    real = module_under_test.build_game_changer_decision_gate

    def spy(*, resolution):
        calls.append(resolution)
        return real(resolution=resolution)

    monkeypatch.setattr(module_under_test, "build_game_changer_decision_gate", spy)
    result = replay(bundle)
    assert len(calls) == 2 and all(a is b.resolution for a, b in zip(calls, bundle.game_changers))
    assert len(result.game_changer_gates) == 2


def test_gate_order_and_resolution_identity_follow_the_canonical_c2e_order() -> None:
    context = analysis()
    a, b, c = state(context, SHA_A), state(context, SHA_B), state(context, SHA_C)
    for supplied in ((c, a, b), (b, c, a), (a, b, c)):
        bundle = d1_bundle(context, *supplied)
        result = replay(bundle)
        assert [g.resolution.family.coverage_provenance_sha256 for g in result.game_changer_gates] == [SHA_A, SHA_B, SHA_C]
        assert all(gate.resolution is binding.resolution for gate, binding in zip(result.game_changer_gates, bundle.game_changers))
        assert result.input_bundle is bundle


def test_gates_are_not_sorted_by_severity_or_anything_else() -> None:
    context = analysis()
    quarantined = state(context, SHA_A, materiality=M.CRITICAL, thesis=TI.INVALIDATED)
    open_gate = state(context, SHA_B)
    result = replay(d1_bundle(context, open_gate, quarantined))
    assert [g.instrument_new_capital_gate for g in result.game_changer_gates] == [G.QUARANTINED, G.OPEN]
    result = replay(d1_bundle(context, state(context, SHA_A), state(context, SHA_B, materiality=M.CRITICAL, thesis=TI.INVALIDATED)))
    assert [g.instrument_new_capital_gate for g in result.game_changer_gates] == [G.OPEN, G.QUARANTINED]


# --- R-U: direct construction ----------------------------------------------------------------------------------------

def test_direct_construction_accepts_the_builder_composition() -> None:
    context = analysis()
    bundle = d1_bundle(context, state(context, SHA_A), state(context, SHA_B))
    built = replay(bundle)
    assert direct(built.game_changer_gates, bundle) == built
    independent = tuple(build_game_changer_decision_gate(resolution=b.resolution) for b in bundle.game_changers)
    assert direct(independent, bundle) == built


def test_direct_construction_rejects_reordered_missing_extra_and_foreign_gates() -> None:
    context = analysis()
    bundle = d1_bundle(context, state(context, SHA_A), state(context, SHA_B))
    first, second = replay(bundle).game_changer_gates
    with pytest.raises(ValueError):
        direct((second, first), bundle)
    with pytest.raises(ValueError):
        direct((first,), bundle)
    with pytest.raises(ValueError):
        direct((), bundle)
    with pytest.raises(ValueError):
        direct((first, second, second), bundle)
    foreign = build_game_changer_decision_gate(resolution=state(context, SHA_C).resolution)
    with pytest.raises(ValueError):
        direct((first, foreign), bundle)
    equal_but_not_identical = build_game_changer_decision_gate(resolution=copy.deepcopy(first.resolution))
    assert equal_but_not_identical == first and equal_but_not_identical.resolution is not first.resolution
    with pytest.raises(ValueError):
        direct((equal_but_not_identical, second), bundle)


def test_direct_construction_enforces_types_and_the_d1_admission_rules() -> None:
    context = analysis()
    bundle = d1_bundle(context, state(context, SHA_A))
    gate = replay(bundle).game_changer_gates[0]

    class SubGate(GameChangerDecisionGate):
        pass

    sub = SubGate(**{f.name: getattr(gate, f.name) for f in dataclasses.fields(gate)})
    for bad in ([gate], (gate, object()), (sub,), iter((gate,)), None):
        with pytest.raises(TypeError):
            direct(bad, bundle)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        direct((gate,), object())  # type: ignore[arg-type]
    pf, preq = only_portfolio(context)
    with pytest.raises(ValueError):
        direct((), make(context, (preq,), portfolio_history=pf))
    with pytest.raises(ValueError):
        direct((gate,), make(context, (preq, R(K.GAME_CHANGER, SHA_A), R(K.GAME_CHANGER, SHA_B)), portfolio_history=pf, game_changers=(bundle.game_changers[0],)))


# --- V-X: incomplete revision coverage is a real replay state --------------------------------------------------------

def test_incomplete_coverage_family_is_replayed_through_the_closed_fail_closed_gate() -> None:
    context = analysis()
    binding = state(context, SHA_A, coverage=FC.INCOMPLETE_AT_CUTOFF, materiality=M.CRITICAL, thesis=TI.INVALIDATED)
    bundle = d1_bundle(context, binding)
    assert bundle.status is PrivateBacktestInputCompletenessStatus.COMPLETE
    gate = replay(bundle).game_changer_gates[0]
    assert gate.instrument_new_capital_gate is G.PAUSED_PENDING_EVIDENCE
    assert gate.review_state is S.REQUIRED and gate.review_reasons == (GameChangerReviewReason.REVISION_COVERAGE_INCOMPLETE,)
    assert gate == build_game_changer_decision_gate(resolution=binding.resolution)


# --- Y-AC: resolved families delegate to Phase 23C2 ------------------------------------------------------------------

def test_resolved_open_and_quarantined_match_the_direct_phase_23_builder() -> None:
    context = analysis()
    open_binding = state(context, SHA_A)
    quarantined = state(context, SHA_B, materiality=M.CRITICAL, thesis=TI.WEAKENED)
    invalidated = state(context, SHA_C, materiality=M.LOW, thesis=TI.INVALIDATED)
    result = replay(d1_bundle(context, open_binding, quarantined, invalidated))
    gates = {g.resolution.family.coverage_provenance_sha256: g for g in result.game_changer_gates}
    assert gates[SHA_A].instrument_new_capital_gate is G.OPEN and gates[SHA_A].review_state is S.NOT_REQUIRED
    assert gates[SHA_B].instrument_new_capital_gate is G.QUARANTINED and gates[SHA_B].review_state is S.REQUIRED
    assert gates[SHA_C].instrument_new_capital_gate is G.QUARANTINED
    for sha, binding in ((SHA_A, open_binding), (SHA_B, quarantined), (SHA_C, invalidated)):
        assert gates[sha] == build_game_changer_decision_gate(resolution=binding.resolution)


def test_urgency_requires_review_without_creating_a_quarantine() -> None:
    context = analysis()
    binding = state(context, SHA_A, urgency=U.IMMEDIATE)
    gate = replay(d1_bundle(context, binding)).game_changer_gates[0]
    assert gate.review_state is S.REQUIRED and gate.instrument_new_capital_gate is G.OPEN


def test_systemic_event_stays_not_applicable_and_never_expands_affected_ids() -> None:
    context = analysis()
    complete = state(context, SHA_A, systemic=True, materiality=M.CRITICAL, thesis=TI.INVALIDATED)
    incomplete = state(context, SHA_B, coverage=FC.INCOMPLETE_AT_CUTOFF, systemic=True, materiality=M.CRITICAL, thesis=TI.INVALIDATED)
    result = replay(d1_bundle(context, complete, incomplete))
    assert [g.instrument_new_capital_gate for g in result.game_changer_gates] == [G.NOT_APPLICABLE_SYSTEMIC, G.NOT_APPLICABLE_SYSTEMIC]
    assert all(g.affected_instrument_ids == () for g in result.game_changer_gates)


def test_an_earlier_revision_cannot_leak_around_the_terminal_authority() -> None:
    context = analysis()
    critical = gc_assessment("EVT-A", context=context.pit_context, materiality=M.CRITICAL, thesis=TI.INVALIDATED)
    corrected = gc_assessment("EVT-B", context=context.pit_context, kind=RK.CORRECTION, revises="EVT-A", materiality=M.LOW, thesis=TI.UNCHANGED)
    binding = family(context, SHA_A, critical, corrected)
    gate = replay(d1_bundle(context, binding)).game_changer_gates[0]
    assert gate.instrument_new_capital_gate is G.OPEN and gate.review_state is S.NOT_REQUIRED
    worsened = family(context, SHA_B, gc_assessment("EVT-A", context=context.pit_context),
                      gc_assessment("EVT-B", context=context.pit_context, kind=RK.CORRECTION, revises="EVT-A", materiality=M.CRITICAL, thesis=TI.WEAKENED))
    assert replay(d1_bundle(context, worsened)).game_changer_gates[0].instrument_new_capital_gate is G.QUARANTINED


# --- AD-AI, AN: no mutation, no portfolio / decision semantics, determinism ------------------------------------------

def test_replay_mutates_neither_the_bundle_nor_the_portfolio_history_and_is_deterministic() -> None:
    context = analysis()
    bundle = d1_bundle(context, state(context, SHA_A), state(context, SHA_B, materiality=M.CRITICAL, thesis=TI.INVALIDATED))
    snapshot = {f.name: getattr(bundle, f.name) for f in dataclasses.fields(bundle)}
    projection = bundle.portfolio_history.projection_binding.projection
    transactions = bundle.portfolio_history.projection_binding.transactions
    first, second = replay(bundle), replay(bundle)
    assert first == second and first.game_changer_gates == second.game_changer_gates
    assert all(getattr(bundle, name) is value for name, value in snapshot.items())
    assert bundle.portfolio_history.projection_binding.projection is projection and bundle.portfolio_history.projection_binding.transactions is transactions
    assert {f.name for f in dataclasses.fields(first)} == {"input_bundle", "game_changer_gates"}


def test_result_carries_no_trade_weight_rebalance_or_aggregate_surface() -> None:
    context = analysis()
    result = replay(d1_bundle(context, state(context, SHA_A, materiality=M.CRITICAL, thesis=TI.INVALIDATED)))
    surface = {n.lower() for n in dir(result) if not n.startswith("_")} | {f.name.lower() for f in dataclasses.fields(result.game_changer_gates[0])}
    for word in ("weight", "rebalance", "sell", "exit", "order", "execut", "trade", "score", "severity", "aggregate", "overall", "max", "average", "decision_id", "hash"):
        assert not any(word in name for name in surface), word


# --- AJ-AM, AO: source-surface guards --------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_TREE = ast.parse(_PATH.read_text(encoding="utf-8"))
_REL = "backend/engine/private/backtest_game_changer_replay.py"


def _names() -> set:
    return ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
            | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})


def test_only_the_game_changer_gate_builder_is_an_economic_decision_builder() -> None:
    forbidden = {"PointInTimeMarketDataResolver", "PrivateBacktestMarketDataResolutionSnapshot", "build_user_return_view_set", "build_bayesian_expected_return_posterior",
                 "optimize_minimum_cvar_portfolio", "build_cross_asset_composition_plan", "build_cash_first_rebalance_plan", "build_band_aware_rebalance_plan",
                 "UserReturnViewSet", "scheduler", "dispatcher", "selected_observation", "selected_observation_payload", "resolution_payload", "resolution_payload_json",
                 "loads", "json", "BISTBulletinSnapshot", "GlobalEODSnapshot", "TefasFundPriceSnapshot", "PreciousMetalSnapshot", "LedgerProjectionView",
                 "build_ledger_projection_view", "PortfolioTransaction", "target_weight", "rebalance", "sell", "execute", "optimizer"}
    assert not _names() & forbidden
    builders = {name for name in _names() if name.startswith("build_") or name.startswith("resolve_") or name.startswith("optimize_")}
    assert builders == {"build_game_changer_decision_gate"}


def test_imports_exclude_market_data_and_every_other_decision_module() -> None:
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules <= {"backend.engine.private.backtest_input_completeness", "backend.engine.private.game_changer_gate"}
    imported_plain = {a.name for n in ast.walk(_TREE) if isinstance(n, ast.Import) for a in n.names}
    assert not any(any(token in m for token in ("market_data", "backtest_market", "allocation", "rebalance", "cvar", "scheduler", "bist", "tefas", "precious", "fund_"))
                   for m in modules | imported_plain)


def test_completeness_reads_only_the_bundle_and_no_assessment_status_or_aggregation() -> None:
    assert not _names() & {"active_assessment", "materiality", "urgency", "thesis_impact", "review_state", "review_reasons", "instrument_new_capital_gate",
                           "affected_instrument_ids", "instrument_ids", "family", "assessments", "transactions", "projection", "known_transactions",
                           "max", "min", "sum", "sorted", "reversed", "mean", "statistics", "score"}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Attribute) and node.attr == "status":
            assert (isinstance(node.value, ast.Name) and node.value.id == "input_bundle") or (
                isinstance(node.value, ast.Attribute) and node.value.attr == "input_bundle"), ast.dump(node)


def test_no_clock_random_hash_io_or_generated_identity() -> None:
    assert not _names() & {"now", "utcnow", "today", "time", "uuid4", "uuid1", "UUID", "random", "secrets", "urandom", "sha256", "hashlib", "hmac", "hash",
                           "open", "client", "rpc", "table", "environ", "getenv", "sleep", "datetime", "date", "global"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await, ast.Try))]


def test_d1_has_no_c2c2_dependency_and_module_is_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST
    source = _PATH.read_text(encoding="utf-8")
    assert "backtest_market_data_selected_observation" not in source and "selected_observation" not in _names()
    assert "c2c2" not in source.lower().replace("c2c2 remains deferred", "")


def test_documentation_and_ci_wiring() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "PRIVATE_BACKTEST_GAME_CHANGER_REPLAY.md").read_text(encoding="utf-8")
    for needle in ("first Phase 26D", "COMPLETE", "no Game Changer events", "NOT_APPLICABLE_SYSTEMIC", "not a sale", "INCOMPLETE_COVERAGE", "C2C2 remains deferred",
                   "market-data", "Red-Team", "object identity", "no rebalance"):
        assert needle in doc, needle
    completeness = (root / "docs" / "PRIVATE_BACKTEST_INPUT_COMPLETENESS.md").read_text(encoding="utf-8")
    assert "Phase 26D1 consumes COMPLETE C2E bundles" in completeness and "C2C2 remains deferred because D1 consumes no typed market observation" in completeness
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    architecture = ci.split("Phase 26 backtest architecture correctness", 1)[1].split("- name:", 1)[0]
    assert "backend/tests/test_backtest_game_changer_replay.py" in architecture
