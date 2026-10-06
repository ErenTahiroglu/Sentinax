"""
backend/tests/test_backtest_game_changer_plan_admission.py
==========================================================
Phase 26D5B: historical Game Changer new-capital admission over a COMPLETED canonical D5A rebalance plan. Both BUY stages (CASH_FUNDED_BUY and SALE_FUNDED_BUY) deploy new capital to an
instrument, so a PAUSED_PENDING_EVIDENCE or QUARANTINED instrument-scoped gate blocks them; SELL is never blocked by the new-capital rule; review state alone, OPEN and SYSTEMIC never block. One
blocked BUY blocks the WHOLE plan; the plan is never filtered, rebuilt or mutated, and every output is a derived view. No Game Changer gate and no rebalance is recomputed.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from enum import Enum
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import backtest_game_changer_plan_admission as module_under_test
from backend.engine.private.allocation_rebalance import RebalanceTradeStage
from backend.engine.private.backtest_game_changer_plan_admission import (
    PrivateBacktestGameChangerPlanAdmission,
    PrivateBacktestNewCapitalAdmissionState,
    build_private_backtest_game_changer_plan_admission,
)
from backend.engine.private.backtest_game_changer_replay import PrivateBacktestGameChangerDecisionReplay, replay_private_backtest_game_changer_decision
from backend.engine.private.backtest_input_completeness import PrivateBacktestInputKind as K, PrivateBacktestInputRequirement as R
from backend.engine.private.backtest_rebalance_plan import PrivateBacktestBandAwareRebalanceReplay, replay_private_backtest_band_aware_rebalance
from backend.engine.private.game_changer_assessment import GameChangerMateriality as M, GameChangerThesisImpact as TI, GameChangerUrgency as U
from backend.engine.private.game_changer_gate import GameChangerInstrumentNewCapitalGate as G, GameChangerReviewState as RS
from backend.engine.private.game_changer_revision_family import GameChangerRevisionFamilyCoverage as FC
from backend.tests.invariants import static_guards as sg
from backend.tests.test_backtest_cross_universe_composition import (
    CASH_ONLY_SPEC,
    EQ,
    FD,
    HELD,
    I1,
    I2,
    I3,
    OTHER_OWNER,
    OWNER,
    analysis,
    authority,
    bind_portfolio,
    compose,
    coverage,
    make,
    pf_key,
    scenario,
)
from backend.tests.test_backtest_game_changer_replay import state as gc_state
from backend.tests.test_backtest_rebalance_plan import friction_for, policy_for

S = RebalanceTradeStage
SHAS = tuple(c * 64 for c in "abcdef")
ABSENT = UUID(int=0x7777)
MIXED_SPEC = ((EQ, "0.6", (I1, I2), ("0.25", "0.75")), (FD, "0.4", (I3,), ("1",)))        # held I1, I3: I1 sold, I2 bought (cash + sale funded), I3 bought (sale funded only)


# --- fixtures ---------------------------------------------------------------------------------------------------------------------------

def gate_binding(context, sha, ids, kind):
    ids = tuple(sorted(ids, key=str))
    if kind == "open":
        return gc_state(context, sha, ids=ids)
    if kind == "review_open":
        return gc_state(context, sha, ids=ids, urgency=U.IMMEDIATE)
    if kind == "quarantined":
        return gc_state(context, sha, ids=ids, materiality=M.CRITICAL, thesis=TI.WEAKENED)
    if kind == "paused":
        return gc_state(context, sha, coverage=FC.INCOMPLETE_AT_CUTOFF, ids=ids)
    if kind == "systemic":
        return gc_state(context, sha, systemic=True, materiality=M.CRITICAL, thesis=TI.INVALIDATED)
    raise AssertionError(kind)


def d1_for(s, *gates, owner=OWNER, binding=None, context=None) -> PrivateBacktestGameChangerDecisionReplay:
    context = context or s.context
    cov = coverage(binding or s.binding, owner)
    bindings = tuple(gate_binding(context, SHAS[index], ids, kind) for index, (ids, kind) in enumerate(gates))
    reqs = (R(K.PORTFOLIO_HISTORY, pf_key(cov)),) + tuple(R(K.GAME_CHANGER, b.resolution.family.coverage_provenance_sha256) for b in bindings)
    bundle = make(context, reqs, portfolio_history=cov, game_changers=bindings)
    return replay_private_backtest_game_changer_decision(input_bundle=bundle)


def d5a_for(s, auth, trigger="0", destination="0") -> PrivateBacktestBandAwareRebalanceReplay:
    composition = compose(s, auth)
    ids = composition.composition_plan.reconciled_instrument_ids
    return replay_private_backtest_band_aware_rebalance(cross_universe_composition=composition, policy=policy_for(ids, trigger, destination), friction=friction_for(ids))


def admit(d1, d5a) -> PrivateBacktestGameChangerPlanAdmission:
    return build_private_backtest_game_changer_plan_admission(game_changer_replay=d1, rebalance_replay=d5a)


def mixed(*gates, **kw):
    s = scenario(HELD, MIXED_SPEC)
    d5a = d5a_for(s, authority(zero=[I2]), **kw)
    return s, d5a, d1_for(s, *gates)


def cash_only(*gates):
    s = scenario((), CASH_ONLY_SPEC, investable="100")
    d5a = d5a_for(s, authority(zero=[I1, I2, I3]))
    return s, d5a, d1_for(s, *gates)


def stages(plan, instrument):
    return [t.stage for t in plan.trades if t.instrument_id == instrument]


NOT_BLOCKED, BLOCKED = PrivateBacktestNewCapitalAdmissionState.NOT_BLOCKED, PrivateBacktestNewCapitalAdmissionState.BLOCKED_NEW_CAPITAL


# --- fixtures sanity: the plan has the stage structure the semantics tests rely on --------------------------------------------------------

def test_the_fixture_plan_has_cash_funded_sale_funded_and_sell_stages() -> None:
    _, d5a, _ = mixed(((I1,), "open"))
    plan = d5a.rebalance_plan
    assert stages(plan, I1) == [S.SELL]
    assert stages(plan, I2) == [S.CASH_FUNDED_BUY, S.SALE_FUNDED_BUY]
    assert stages(plan, I3) == [S.SALE_FUNDED_BUY]
    _, cash_plan, _ = cash_only(((I1,), "open"))
    assert {t.stage for t in cash_plan.rebalance_plan.trades} == {S.CASH_FUNDED_BUY}


# --- A-I: contract, types, identities ------------------------------------------------------------------------------------------------------

def test_admission_enum_has_exactly_the_two_non_approval_members() -> None:
    assert issubclass(PrivateBacktestNewCapitalAdmissionState, Enum)
    assert {m.name: m.value for m in PrivateBacktestNewCapitalAdmissionState} == {"NOT_BLOCKED": "not_blocked", "BLOCKED_NEW_CAPITAL": "blocked_new_capital"}


def test_result_is_exactly_two_frozen_stored_fields_and_every_output_is_derived() -> None:
    fs = dataclasses.fields(PrivateBacktestGameChangerPlanAdmission)
    assert [f.name for f in fs] == ["game_changer_replay", "rebalance_replay"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestGameChangerPlanAdmission.__dataclass_params__.frozen is True
    for name in ("admission_state", "blocked_buy_trades", "blocked_instrument_ids", "blocking_gates"):
        assert isinstance(getattr(PrivateBacktestGameChangerPlanAdmission, name), property)
    _, d5a, d1 = mixed(((I2,), "quarantined"))
    admission = admit(d1, d5a)
    assert not {"admission_state", "blocked_buy_trades", "blocked_instrument_ids", "blocking_gates"} & set(vars(admission))
    with pytest.raises(dataclasses.FrozenInstanceError):
        admission.rebalance_replay = d5a  # type: ignore[misc]
    with pytest.raises(AttributeError):
        admission.admission_state = NOT_BLOCKED  # type: ignore[misc]


def test_builder_is_keyword_only_with_two_parameters_and_exact_types() -> None:
    params = inspect.signature(build_private_backtest_game_changer_plan_admission).parameters
    assert list(params) == ["game_changer_replay", "rebalance_replay"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in params.values())
    _, d5a, d1 = mixed(((I2,), "open"))
    with pytest.raises(TypeError):
        build_private_backtest_game_changer_plan_admission(d1, d5a)  # type: ignore[misc]

    class SubD1(PrivateBacktestGameChangerDecisionReplay):
        pass

    class SubD5(PrivateBacktestBandAwareRebalanceReplay):
        pass

    sub1 = SubD1(**{f.name: getattr(d1, f.name) for f in dataclasses.fields(d1)})
    sub5 = SubD5(**{f.name: getattr(d5a, f.name) for f in dataclasses.fields(d5a)})
    for bad in (sub1, object(), None, d5a):
        with pytest.raises(TypeError):
            admit(bad, d5a)
    for bad in (sub5, object(), None, d1):
        with pytest.raises(TypeError):
            admit(d1, bad)


def test_d1_and_d5a_are_retained_by_identity() -> None:
    _, d5a, d1 = mixed(((I2,), "open"))
    admission = admit(d1, d5a)
    assert admission.game_changer_replay is d1 and admission.rebalance_replay is d5a


# --- J-Q: shared replay and portfolio anchors -----------------------------------------------------------------------------------------------

def test_same_analysis_context_projection_binding_and_separate_wrappers_and_manifests_are_accepted() -> None:
    s, d5a, d1 = mixed(((I2,), "open"))
    d5_bundle = d5a.cross_universe_composition.candidate_eligibility.input_bundle
    assert d1.input_bundle.analysis_context is d5_bundle.analysis_context is s.context
    assert d1.input_bundle.portfolio_history is not d5_bundle.portfolio_history                                  # separate coverage wrappers ...
    assert d1.input_bundle.portfolio_history.projection_binding is d5_bundle.portfolio_history.projection_binding    # ... over the very same binding
    assert d1.input_bundle is not d5_bundle and d1.input_bundle.requirements != d5_bundle.requirements        # separate C2E manifests
    assert admit(d1, d5a).admission_state is NOT_BLOCKED


def test_different_and_equal_valued_cloned_analysis_contexts_are_rejected() -> None:
    s_a = scenario(HELD, MIXED_SPEC)
    s_b = scenario(HELD, MIXED_SPEC)
    d5a = d5a_for(s_a, authority(zero=[I2]))
    d1_b = d1_for(s_b, ((I2,), "quarantined"))
    assert s_a.context == s_b.context and s_a.context is not s_b.context
    with pytest.raises(ValueError):
        admit(d1_b, d5a)
    clone = type(s_a.context)(replay_point=s_a.context.replay_point, temporal_context=s_a.context.temporal_context)
    s_c = scenario(HELD, MIXED_SPEC, context=clone)
    assert clone == s_a.context and clone is not s_a.context
    with pytest.raises(ValueError):
        admit(d1_for(s_c, ((I2,), "quarantined")), d5a)


def test_equal_valued_separate_projection_binding_is_rejected_on_the_same_context() -> None:
    s = scenario(HELD, MIXED_SPEC)
    d5a = d5a_for(s, authority(zero=[I2]))
    other_binding = bind_portfolio(s.context, s.pf, tuple(s.txs))
    assert other_binding == s.binding and other_binding is not s.binding
    d1 = d1_for(s, ((I2,), "quarantined"), binding=other_binding)
    assert d1.input_bundle.analysis_context is s.context
    with pytest.raises(ValueError):
        admit(d1, d5a)


def test_owner_mismatch_over_the_same_projection_binding_is_rejected() -> None:
    s = scenario(HELD, MIXED_SPEC)
    d5a = d5a_for(s, authority(zero=[I2]))
    d1 = d1_for(s, ((I2,), "quarantined"), owner=OTHER_OWNER)
    assert d1.input_bundle.portfolio_history.projection_binding is s.binding
    with pytest.raises(ValueError):
        admit(d1, d5a)


def test_direct_construction_reproves_the_anchors_and_types() -> None:
    s_a, d5a, d1 = mixed(((I2,), "open"))
    assert PrivateBacktestGameChangerPlanAdmission(game_changer_replay=d1, rebalance_replay=d5a) == admit(d1, d5a)
    foreign_d1 = d1_for(scenario(HELD, MIXED_SPEC), ((I2,), "open"))
    with pytest.raises(ValueError):
        PrivateBacktestGameChangerPlanAdmission(game_changer_replay=foreign_d1, rebalance_replay=d5a)
    with pytest.raises(TypeError):
        PrivateBacktestGameChangerPlanAdmission(game_changer_replay=object(), rebalance_replay=d5a)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PrivateBacktestGameChangerPlanAdmission(game_changer_replay=d1, rebalance_replay=object())  # type: ignore[arg-type]


# --- R-Y: gate states against BUY stages ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("instrument,label", [(I2, "cash+sale funded"), (I3, "sale funded only")])
@pytest.mark.parametrize("kind", ["open", "review_open", "systemic"])
def test_open_review_required_and_systemic_gates_never_block_any_buy_stage(instrument, label, kind) -> None:
    _, d5a, d1 = mixed(((instrument,), kind))
    gate = d1.game_changer_gates[0]
    if kind == "systemic":
        assert gate.instrument_new_capital_gate is G.NOT_APPLICABLE_SYSTEMIC and gate.affected_instrument_ids == ()
    else:
        assert gate.instrument_new_capital_gate is G.OPEN
    if kind == "review_open":
        assert gate.review_state is RS.REQUIRED                                                            # review alone is never a capital block
    admission = admit(d1, d5a)
    assert admission.admission_state is NOT_BLOCKED and admission.blocked_buy_trades == () and admission.blocking_gates == ()


@pytest.mark.parametrize("kind,expected_gate", [("paused", G.PAUSED_PENDING_EVIDENCE), ("quarantined", G.QUARANTINED)])
def test_paused_and_quarantined_block_cash_funded_and_sale_funded_buys(kind, expected_gate) -> None:
    _, d5a, d1 = mixed(((I2,), kind))
    assert d1.game_changer_gates[0].instrument_new_capital_gate is expected_gate
    admission = admit(d1, d5a)
    assert admission.admission_state is BLOCKED
    assert [t.stage for t in admission.blocked_buy_trades] == [S.CASH_FUNDED_BUY, S.SALE_FUNDED_BUY]            # sale-funded capital cannot bypass the gate
    _, d5a3, d1_3 = mixed(((I3,), kind))
    admission3 = admit(d1_3, d5a3)
    assert admission3.admission_state is BLOCKED and [t.stage for t in admission3.blocked_buy_trades] == [S.SALE_FUNDED_BUY]
    _, cash_plan, cash_d1 = cash_only(((I1,), kind))
    cash_admission = admit(cash_d1, cash_plan)
    assert cash_admission.admission_state is BLOCKED and {t.stage for t in cash_admission.blocked_buy_trades} == {S.CASH_FUNDED_BUY}


# --- Z-AD: SELL-only, no-trade, non-traded, absent ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["paused", "quarantined"])
def test_a_sell_only_instrument_is_not_blocked_by_the_new_capital_gate(kind) -> None:
    _, d5a, d1 = mixed(((I1,), kind))                                                                     # I1 only has a SELL in this plan
    assert stages(d5a.rebalance_plan, I1) == [S.SELL]
    admission = admit(d1, d5a)
    assert admission.admission_state is NOT_BLOCKED and admission.blocked_buy_trades == () and admission.blocked_instrument_ids == ()
    assert admission.rebalance_replay.rebalance_plan.trades == d5a.rebalance_plan.trades                  # the sell exists only because Phase 21B produced it


def test_a_quarantined_gate_over_a_no_trade_plan_does_not_block() -> None:
    s = scenario(HELD, MIXED_SPEC)
    d5a = d5a_for(s, authority(zero=[I2]), trigger="1", destination="0")
    assert d5a.rebalance_plan.trades == ()
    d1 = d1_for(s, ((I1, I2, I3), "quarantined"))
    assert d1.game_changer_gates[0].instrument_new_capital_gate is G.QUARANTINED
    admission = admit(d1, d5a)
    assert admission.admission_state is NOT_BLOCKED and admission.blocked_buy_trades == ()


def test_a_quarantined_target_without_an_actual_buy_and_an_absent_instrument_do_not_block() -> None:
    s = scenario(HELD, MIXED_SPEC)
    d5a = d5a_for(s, authority(zero=[I2]))
    target_ids = d5a.rebalance_plan.target.instrument_ids
    assert I1 in target_ids and not any(t.instrument_id == I1 and t.stage is not S.SELL for t in d5a.rebalance_plan.trades)
    assert admit(d1_for(s, ((I1,), "quarantined")), d5a).admission_state is NOT_BLOCKED
    assert ABSENT not in target_ids and admit(d1_for(s, ((ABSENT,), "quarantined")), d5a).admission_state is NOT_BLOCKED
    assert admit(d1_for(s, ((ABSENT,), "paused")), d5a).blocking_gates == ()


# --- AE-AJ: blocked trades, instruments and gates ---------------------------------------------------------------------------------------------------

def test_both_buy_stages_of_one_instrument_are_blocked_once_per_trade_and_the_id_once() -> None:
    _, d5a, d1 = mixed(((I2,), "quarantined"))
    admission = admit(d1, d5a)
    plan_trades = d5a.rebalance_plan.trades
    assert len(admission.blocked_buy_trades) == 2 and admission.blocked_instrument_ids == (I2,)
    assert all(any(blocked is trade for trade in plan_trades) for blocked in admission.blocked_buy_trades)  # the exact plan trade objects, never clones
    assert [plan_trades.index(t) for t in admission.blocked_buy_trades] == sorted(plan_trades.index(t) for t in admission.blocked_buy_trades)


def test_blocked_trades_follow_plan_order_and_instrument_ids_first_occurrence_order() -> None:
    _, d5a, d1 = mixed(((I3, I2), "quarantined"))
    admission = admit(d1, d5a)
    plan_trades = d5a.rebalance_plan.trades
    expected = tuple(t for t in plan_trades if t.stage is not S.SELL)
    assert admission.blocked_buy_trades == expected and all(a is b for a, b in zip(admission.blocked_buy_trades, expected))
    first_seen = []
    for trade in expected:
        if trade.instrument_id not in first_seen:
            first_seen.append(trade.instrument_id)
    assert admission.blocked_instrument_ids == tuple(first_seen) and len(set(admission.blocked_instrument_ids)) == len(admission.blocked_instrument_ids)


def test_blocking_gates_are_the_exact_d1_gates_in_d1_order_and_only_those_hitting_a_buy() -> None:
    _, d5a, d1 = mixed(((I3,), "paused"), ((I2,), "quarantined"), ((I1,), "quarantined"), ((I2,), "open"), ((ABSENT,), "quarantined"))
    gates = d1.game_changer_gates
    assert len(gates) == 5
    admission = admit(d1, d5a)
    assert len(admission.blocking_gates) == 2 and all(any(b is g for g in gates) for b in admission.blocking_gates)
    assert admission.blocking_gates == (gates[0], gates[1])                                              # D1 order; I1 (sell-only) / OPEN / absent gates excluded
    indices = [next(i for i, g in enumerate(gates) if g is b) for b in admission.blocking_gates]
    assert indices == sorted(indices)


# --- AK-AO: overlap -------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kinds,blocked", [(("open", "quarantined"), True), (("open", "paused"), True), (("quarantined", "paused"), True), (("open", "open"), False)])
def test_overlapping_gates_any_blocking_family_blocks_without_precedence(kinds, blocked) -> None:
    _, d5a, d1 = mixed(((I2,), kinds[0]), ((I2,), kinds[1]))
    admission = admit(d1, d5a)
    assert (admission.admission_state is BLOCKED) is blocked
    if blocked:
        assert len(admission.blocked_buy_trades) == 2                                                    # a trade hit by several gates appears once
    assert len({id(t) for t in admission.blocked_buy_trades}) == len(admission.blocked_buy_trades)


def test_multiple_blocking_gates_are_all_listed_and_never_collapsed_to_one_severity() -> None:
    _, d5a, d1 = mixed(((I2,), "quarantined"), ((I2, I3), "paused"))
    admission = admit(d1, d5a)
    assert len(admission.blocking_gates) == 2
    assert {g.instrument_new_capital_gate for g in admission.blocking_gates} == {G.QUARANTINED, G.PAUSED_PENDING_EVIDENCE}
    assert len(admission.blocked_buy_trades) == 3 and set(admission.blocked_instrument_ids) == {I2, I3}


# --- AP-AU: whole-plan block, immutability, derived views -----------------------------------------------------------------------------------------

def test_one_blocked_buy_blocks_the_whole_plan_which_stays_unchanged_with_no_executable_subset() -> None:
    _, d5a, d1 = mixed(((I3,), "quarantined"))
    plan_before, trades_before = d5a.rebalance_plan, tuple(d5a.rebalance_plan.trades)
    admission = admit(d1, d5a)
    assert admission.admission_state is BLOCKED
    assert len(admission.blocked_buy_trades) < len(plan_before.trades)                                    # other trades exist (a SELL and unblocked BUYs) ...
    assert admission.rebalance_replay.rebalance_plan is plan_before and tuple(plan_before.trades) == trades_before   # ... but the plan is retained untouched
    public = {n for n in dir(admission) if not n.startswith("_")}
    assert public == {"game_changer_replay", "rebalance_replay", "admission_state", "blocked_buy_trades", "blocked_instrument_ids", "blocking_gates"}
    for word in ("allowed", "filtered", "safe", "executable", "approved", "subset", "unblocked", "replacement", "repair"):
        assert not any(word in n.lower() for n in public), word


def test_derived_properties_are_deterministic_and_never_stored() -> None:
    _, d5a, d1 = mixed(((I2,), "quarantined"))
    admission = admit(d1, d5a)
    assert admission.blocked_buy_trades == admission.blocked_buy_trades and admission.blocking_gates == admission.blocking_gates
    assert admission.admission_state is admission.admission_state and admission == admit(d1, d5a)
    assert not {"admission_state", "blocked_buy_trades", "blocking_gates", "blocked_instrument_ids"} & set(vars(admission))


# --- AV-AZ: nothing is recomputed or mutated ------------------------------------------------------------------------------------------------------------

def test_no_gate_recomputation_and_no_rebalance_rebuild(monkeypatch) -> None:
    _, d5a, d1 = mixed(((I2,), "quarantined"))

    def boom(*args, **kwargs):
        raise AssertionError("recomputation")

    monkeypatch.setattr("backend.engine.private.game_changer_gate.build_game_changer_decision_gate", boom)
    monkeypatch.setattr("backend.engine.private.allocation_rebalance_policy.build_band_aware_rebalance_plan", boom)
    monkeypatch.setattr("backend.engine.private.allocation_rebalance.build_cash_first_rebalance_plan", boom)
    monkeypatch.setattr("backend.engine.private.allocation_universe_composition.build_cross_asset_composition_plan", boom)
    admission = admit(d1, d5a)
    assert admission.admission_state is BLOCKED and len(admission.blocked_buy_trades) == 2 and admission.blocked_instrument_ids == (I2,)


def test_target_sleeves_candidates_and_plan_are_untouched() -> None:
    s, d5a, d1 = mixed(((I2,), "quarantined"))
    composition = d5a.cross_universe_composition
    sleeves, target, state, plan = d5a.cross_universe_composition.candidate_eligibility.sleeves, composition.composition_plan.rebalance_target, \
        composition.composition_plan.rebalance_state, d5a.rebalance_plan
    admission = admit(d1, d5a)
    assert admission.rebalance_replay.cross_universe_composition.candidate_eligibility.sleeves is sleeves
    assert admission.rebalance_replay.cross_universe_composition.composition_plan.rebalance_target is target
    assert admission.rebalance_replay.cross_universe_composition.composition_plan.rebalance_state is state
    assert admission.rebalance_replay.rebalance_plan is plan and plan.target is target and plan.state is state
    assert I2 in target.instrument_ids and target.weights[target.instrument_ids.index(I2)] > 0              # the quarantined instrument keeps its target weight


# --- scope guards ------------------------------------------------------------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_TREE = ast.parse(_PATH.read_text(encoding="utf-8"))
_REL = "backend/engine/private/backtest_game_changer_plan_admission.py"


def _names() -> set:
    return ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
            | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})


def test_no_recomputation_plan_mutation_or_other_decision_surface() -> None:
    assert not _names() & {"build_game_changer_decision_gate", "build_band_aware_rebalance_plan", "build_cash_first_rebalance_plan", "build_cross_asset_composition_plan",
                           "RebalanceBandPolicy", "RebalanceFrictionProfile", "CrossUniverseAuthority", "CrossAssetSleeve", "BandAwareRebalancePlan", "CashFirstRebalancePlan",
                           "CashBucket", "CashPurpose", "PointInTimeMarketDataResolver", "optimize_minimum_cvar_portfolio", "build_bayesian_expected_return_posterior",
                           "scheduler", "dispatcher", "repository", "supabase", "float", "round", "quantize", "replace"}
    assert not _names() & {"review_state", "review_reasons", "materiality", "urgency", "thesis_impact", "resolution", "notional", "weights", "current_values",
                           "investable_cash", "market_data", "marked_positions", "cash_projection", "allocations"}                  # review state and amounts are never consulted
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.BinOp, ast.AugAssign))]


def test_buy_stages_are_the_two_explicit_members_and_imports_are_minimal() -> None:
    names = _names()
    assert {"CASH_FUNDED_BUY", "SALE_FUNDED_BUY"} <= names and "SELL" not in names
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules <= {"backend.engine.private.allocation_rebalance", "backend.engine.private.backtest_game_changer_replay",
                       "backend.engine.private.backtest_rebalance_plan", "backend.engine.private.game_changer_gate"}
    assert not any(isinstance(n, ast.Compare) and any(isinstance(op, (ast.NotEq, ast.IsNot)) for op in n.ops) and "stage" in ast.dump(n) for n in ast.walk(_TREE))  # no open-ended "is not SELL"


def test_no_clock_random_hash_io_execution_or_persistence() -> None:
    assert not _names() & {"now", "utcnow", "today", "time", "uuid4", "uuid1", "random", "secrets", "urandom", "sha256", "hashlib", "hmac", "hash", "open",
                           "client", "rpc", "table", "environ", "getenv", "sleep", "datetime", "json", "loads", "dumps", "order", "execute", "persist"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await))]


def test_module_is_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_documentation_and_ci_wiring() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "PRIVATE_BACKTEST_GAME_CHANGER_PLAN_ADMISSION.md").read_text(encoding="utf-8")
    for needle in ("admission", "CASH_FUNDED_BUY", "SALE_FUNDED_BUY", "SELL", "not a sell recommendation", "review alone", "SYSTEMIC", "any blocking family",
                   "no severity aggregation", "whole plan", "no partial execution", "NOT_BLOCKED", "not approval", "counterfactual", "no execution", "separate C2E manifests",
                   "projection binding", "analysis context"):
        assert needle in doc, needle
    plan_doc = (root / "docs" / "PRIVATE_BACKTEST_REBALANCE_PLAN.md").read_text(encoding="utf-8")
    assert "D5B evaluates the completed canonical plan without mutating it" in plan_doc
    assert "both CASH_FUNDED_BUY and SALE_FUNDED_BUY are treated as new-capital deployment for instrument-scoped Phase 23 admission" in plan_doc
    d1_doc = (root / "docs" / "PRIVATE_BACKTEST_GAME_CHANGER_REPLAY.md").read_text(encoding="utf-8")
    assert "D5B later consumes the exact D1 gates only after proving D1 and D5A share the same analysis context and exact portfolio projection binding" in d1_doc
    assert "does not change D1 gate semantics" in d1_doc
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    architecture = ci.split("Phase 26 backtest architecture correctness", 1)[1].split("- name:", 1)[0]
    assert "backend/tests/test_backtest_game_changer_plan_admission.py" in architecture
