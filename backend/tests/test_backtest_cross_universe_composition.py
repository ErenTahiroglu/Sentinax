"""
backend/tests/test_backtest_cross_universe_composition.py
=========================================================
Phase 26D4B: historical cross-universe target / state composition. D2 candidate-eligible explicit sleeves + the D3C held-universe (or cash-only) Phase 21 current state + an EXPLICIT
`CrossUniverseAuthority` go through the closed Phase 22 `build_cross_asset_composition_plan`. Both sides must share the very analysis context object and the very portfolio projection
binding (separate coverage wrappers over it are fine). The authority is never derived, an exit authorization is not a trade, and no rebalance, valuation, cash or Game Changer logic exists here.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from collections import namedtuple
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import backtest_cross_universe_composition as module_under_test
from backend.engine.private.allocation_rebalance import RebalanceCurrentState
from backend.engine.private.allocation_universe_composition import (
    CrossAssetCompositionPlan,
    CrossAssetSleeve,
    CrossUniverseAuthority,
    build_cross_asset_composition_plan,
)
from backend.engine.private.backtest_candidate_eligibility_replay import (
    PrivateBacktestCandidateEligibilityReplay,
    replay_private_backtest_candidate_eligibility,
)
from backend.engine.private.backtest_cross_universe_composition import (
    PrivateBacktestCrossUniverseComposition,
    build_private_backtest_cross_universe_composition,
)
from backend.engine.private.backtest_input_completeness import PrivateBacktestInputKind as K, PrivateBacktestInputRequirement as R
from backend.engine.private.backtest_portfolio_history_coverage import PrivateBacktestPortfolioHistoryCoverageRepository
from backend.engine.private.backtest_rebalance_current_state import PrivateBacktestRebalanceCurrentState, build_private_backtest_rebalance_current_state
from backend.engine.private.domain import AssetClass, Currency
from backend.tests.invariants import static_guards as sg
from backend.tests.test_backtest_candidate_eligibility_replay import req as candidate_req
from backend.tests.test_backtest_candidate_eligibility_replay import universe as candidate_universe
from backend.tests.test_backtest_input_bindings import analysis
from backend.tests.test_backtest_input_completeness import make, pf_key
from backend.tests.test_backtest_marked_holdings import A1, I1, I2, I3, OWNER, bist_snap, build, bundle_for, buy, deposit, global_snap, tefas_snap
from backend.tests.test_backtest_portfolio_history_coverage import FakeClient, row_for
from backend.tests.test_backtest_portfolio_projection import bind as bind_portfolio
from backend.tests.test_backtest_rebalance_current_state import selection_for
from backend.tests.test_portfolio_projection import _make_portfolio

D = Decimal
TRY, USD = Currency.TRY, Currency.USD
EQ, FD = AssetClass.EQUITY, AssetClass.FUND
OTHER_OWNER = UUID(int=78)
PRICERS = {I1: bist_snap, I2: global_snap, I3: tefas_snap}
Scenario = namedtuple("Scenario", "context pf txs binding d2 d3c selection state sleeves")


# --- fixtures: one replay context, one portfolio projection binding, two separate C2E bundles ---------------------------------

def ledger(held, cash_try="1000"):
    pf = _make_portfolio(owner_id=OWNER)
    txs = [deposit(pf, A1, "1000000000", 0, USD), deposit(pf, A1, cash_try, 1, TRY)]
    txs += [buy(pf, A1, inst, qty, 2 + index) for index, (inst, qty) in enumerate(held)]
    return pf, txs


def coverage(binding, owner=OWNER):
    return PrivateBacktestPortfolioHistoryCoverageRepository(client=FakeClient([row_for(binding, owner)]), owner_id=owner).verify_projection_history(projection_binding=binding)


def mk_sleeve(asset, weight, ids, weights) -> CrossAssetSleeve:
    return CrossAssetSleeve(asset_class=asset, target_weight=D(weight), instrument_ids=tuple(ids), instrument_weights=tuple(D(w) for w in weights))


def make_d3c(context, binding, held, owner=OWNER, investable="100"):
    snaps = [PRICERS[inst](context, inst=inst) for inst, _ in held]
    bundle = bundle_for(context, coverage(binding, owner), *snaps)
    state = build(bundle, TRY)
    selection = selection_for(state, investable)
    return build_private_backtest_rebalance_current_state(investable_cash_selection=selection), selection, state


def make_d2(context, binding, sleeve_spec, owner=OWNER):
    sleeves = tuple(mk_sleeve(*spec) for spec in sleeve_spec)
    bindings = tuple(candidate_universe(context, sleeve.asset_class, sleeve.instrument_ids) for sleeve in sleeves)
    cov = coverage(binding, owner)
    bundle = make(context, (R(K.PORTFOLIO_HISTORY, pf_key(cov)),) + tuple(candidate_req(b) for b in bindings), portfolio_history=cov, candidate_universes=bindings)
    return replay_private_backtest_candidate_eligibility(input_bundle=bundle, sleeves=sleeves), sleeves


def scenario(held, sleeve_spec, *, investable="100", context=None, cash_try="1000") -> Scenario:
    context = context or analysis()
    pf, txs = ledger(held, cash_try)
    binding = bind_portfolio(context, pf, tuple(txs))
    d3c, selection, state = make_d3c(context, binding, held, investable=investable)
    d2, sleeves = make_d2(context, binding, sleeve_spec)
    return Scenario(context, pf, txs, binding, d2, d3c, selection, state, sleeves)


def authority(zero=(), exits=()) -> CrossUniverseAuthority:
    return CrossUniverseAuthority(confirmed_zero_current_value_instrument_ids=tuple(zero), authorized_exit_instrument_ids=tuple(exits))


def compose(s, auth):
    return build_private_backtest_cross_universe_composition(candidate_eligibility=s.d2, rebalance_current_state=s.d3c, authority=auth)


HELD = ((I1, "10"), (I3, "5"))
MATCHING = ((EQ, "0.6", (I1,), ("1",)), (FD, "0.4", (I3,), ("1",)))
TARGET_ONLY = ((EQ, "0.6", (I1, I2), ("0.7", "0.3")), (FD, "0.4", (I3,), ("1",)))
CURRENT_ONLY = ((EQ, "1", (I1,), ("1",)),)
BOTH = ((EQ, "1", (I1, I2), ("0.5", "0.5")),)
CASH_ONLY_SPEC = ((EQ, "0.6", (I1, I2), ("0.7", "0.3")), (FD, "0.4", (I3,), ("1",)))


# --- A-H: contracts and exact types ---------------------------------------------------------------------------------------

def test_result_is_exactly_three_frozen_fields_without_defaults() -> None:
    fs = dataclasses.fields(PrivateBacktestCrossUniverseComposition)
    assert [f.name for f in fs] == ["candidate_eligibility", "rebalance_current_state", "composition_plan"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestCrossUniverseComposition.__dataclass_params__.frozen is True
    assert not {"authority", "sleeves", "target", "rebalance_state", "analysis_context", "portfolio_history", "instrument_ids", "investable_cash", "currency"} & {f.name for f in fs}


def test_builder_is_keyword_only_with_three_parameters_and_no_defaults() -> None:
    params = inspect.signature(build_private_backtest_cross_universe_composition).parameters
    assert list(params) == ["candidate_eligibility", "rebalance_current_state", "authority"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in params.values())
    s = scenario(HELD, MATCHING)
    with pytest.raises(TypeError):
        build_private_backtest_cross_universe_composition(s.d2, s.d3c, authority())  # type: ignore[misc]


def test_exact_types_are_required_and_subclasses_rejected() -> None:
    s = scenario(HELD, MATCHING)

    class SubD2(PrivateBacktestCandidateEligibilityReplay):
        pass

    class SubD3C(PrivateBacktestRebalanceCurrentState):
        pass

    class SubAuthority(CrossUniverseAuthority):
        pass

    sub_d2 = SubD2(**{f.name: getattr(s.d2, f.name) for f in dataclasses.fields(s.d2)})
    sub_d3c = SubD3C(**{f.name: getattr(s.d3c, f.name) for f in dataclasses.fields(s.d3c)})
    sub_auth = SubAuthority(confirmed_zero_current_value_instrument_ids=(), authorized_exit_instrument_ids=())
    good = authority()
    for bad in (sub_d2, object(), None, s.d3c):
        with pytest.raises(TypeError):
            build_private_backtest_cross_universe_composition(candidate_eligibility=bad, rebalance_current_state=s.d3c, authority=good)  # type: ignore[arg-type]
    for bad in (sub_d3c, object(), None, s.d2):
        with pytest.raises(TypeError):
            build_private_backtest_cross_universe_composition(candidate_eligibility=s.d2, rebalance_current_state=bad, authority=good)  # type: ignore[arg-type]
    for bad in (sub_auth, object(), None, ((), ()), s.d2):
        with pytest.raises(TypeError):
            build_private_backtest_cross_universe_composition(candidate_eligibility=s.d2, rebalance_current_state=s.d3c, authority=bad)  # type: ignore[arg-type]


# --- I-V: identity, anchors, provenance ----------------------------------------------------------------------------------------

def test_d2_and_d3c_are_retained_by_identity_and_the_plan_reuses_their_exact_objects() -> None:
    s = scenario(HELD, MATCHING)
    auth = authority()
    result = compose(s, auth)
    assert result.candidate_eligibility is s.d2 and result.rebalance_current_state is s.d3c
    plan = result.composition_plan
    assert type(plan) is CrossAssetCompositionPlan
    assert plan.current_state is s.d3c.current_state                                      # D3C is the only current state
    assert len(plan.sleeves) == len(s.d2.sleeves) and all(a is b for a, b in zip(plan.sleeves, s.d2.sleeves))     # D2 sleeves, same objects, same order
    assert all(a is b for a, b in zip(plan.sleeves, s.sleeves))
    assert plan.authority is auth                                                          # the explicit authority object is retained through the plan only


def test_same_analysis_context_and_same_projection_binding_succeed_with_distinct_c2e_bundles_and_coverage_wrappers() -> None:
    s = scenario(HELD, MATCHING)
    d2_bundle, d3_bundle = s.d2.input_bundle, s.state.input_bundle
    assert d2_bundle is not d3_bundle and d2_bundle.requirements != d3_bundle.requirements        # different closed manifests, intentionally
    assert d2_bundle.analysis_context is d3_bundle.analysis_context is s.context
    assert d2_bundle.portfolio_history is not d3_bundle.portfolio_history                          # separate verified coverage wrappers ...
    assert d2_bundle.portfolio_history.projection_binding is d3_bundle.portfolio_history.projection_binding is s.binding    # ... over the very same binding
    assert compose(s, authority()).composition_plan.rebalance_target.instrument_ids == (I1, I3)


def test_different_replay_context_is_rejected_even_with_numerically_equal_data() -> None:
    a, b = scenario(HELD, MATCHING), scenario(HELD, MATCHING)
    assert a.context == b.context and a.context is not b.context
    with pytest.raises(ValueError):
        build_private_backtest_cross_universe_composition(candidate_eligibility=a.d2, rebalance_current_state=b.d3c, authority=authority())
    with pytest.raises(ValueError):
        build_private_backtest_cross_universe_composition(candidate_eligibility=b.d2, rebalance_current_state=a.d3c, authority=authority())


def test_equal_valued_cloned_analysis_context_is_rejected() -> None:
    a = scenario(HELD, MATCHING)
    clone = type(a.context)(replay_point=a.context.replay_point, temporal_context=a.context.temporal_context)
    assert clone == a.context and clone is not a.context
    b = scenario(HELD, MATCHING, context=clone)
    with pytest.raises(ValueError):
        build_private_backtest_cross_universe_composition(candidate_eligibility=b.d2, rebalance_current_state=a.d3c, authority=authority())


def test_equal_valued_but_separate_projection_binding_is_rejected_on_the_same_context() -> None:
    context = analysis()
    pf, txs = ledger(HELD)
    binding_a = bind_portfolio(context, pf, tuple(txs))
    binding_b = bind_portfolio(context, pf, tuple(txs))
    assert binding_a == binding_b and binding_a is not binding_b
    d3c, _, _ = make_d3c(context, binding_a, HELD)
    d2, _ = make_d2(context, binding_b, MATCHING)
    assert d2.input_bundle.analysis_context is d3c.investable_cash_selection.marked_holdings_state.input_bundle.analysis_context
    with pytest.raises(ValueError):
        build_private_backtest_cross_universe_composition(candidate_eligibility=d2, rebalance_current_state=d3c, authority=authority())


def test_owner_mismatch_over_the_same_binding_is_rejected() -> None:
    context = analysis()
    pf, txs = ledger(HELD)
    binding = bind_portfolio(context, pf, tuple(txs))
    d3c, _, _ = make_d3c(context, binding, HELD, owner=OWNER)
    d2, _ = make_d2(context, binding, MATCHING, owner=OTHER_OWNER)
    assert d2.input_bundle.portfolio_history.projection_binding is d3c.investable_cash_selection.marked_holdings_state.input_bundle.portfolio_history.projection_binding
    with pytest.raises(ValueError):
        build_private_backtest_cross_universe_composition(candidate_eligibility=d2, rebalance_current_state=d3c, authority=authority())


def test_phase_22_is_the_sole_economic_authority_and_every_call_receives_the_exact_objects(monkeypatch) -> None:
    s = scenario(HELD, TARGET_ONLY)
    auth = authority(zero=[I2])
    calls = []
    real = module_under_test.build_cross_asset_composition_plan

    def spy(*, current_state, sleeves, authority):
        calls.append((current_state, sleeves, authority))
        return real(current_state=current_state, sleeves=sleeves, authority=authority)

    monkeypatch.setattr(module_under_test, "build_cross_asset_composition_plan", spy)
    result = compose(s, auth)
    assert len(calls) >= 1                                                                 # one derivation plus the constructor's canonical re-verification
    assert all(c[0] is s.d3c.current_state and c[1] is s.d2.sleeves and c[2] is auth for c in calls)
    assert result.composition_plan.authority is auth


# --- W-AE: the closed Phase 22 relationships ---------------------------------------------------------------------------------------

def test_matching_held_and_target_universe_needs_the_empty_authority() -> None:
    s = scenario(HELD, MATCHING)
    result = compose(s, authority())
    plan = result.composition_plan
    assert plan.reconciled_instrument_ids == (I1, I3) and plan.target_candidate_instrument_ids == (I1, I3)
    assert plan.target_only_zero_confirmed_instrument_ids == () and plan.current_only_exit_instrument_ids == ()
    assert plan.rebalance_state.current_values == s.d3c.current_state.current_values              # retained holdings keep their D3A values
    for bad in (authority(zero=[I2]), authority(exits=[I3]), authority(zero=[I1])):
        with pytest.raises(ValueError):
            compose(s, bad)


def test_target_only_candidates_need_exact_zero_current_confirmation() -> None:
    s = scenario(HELD, TARGET_ONLY)
    plan = compose(s, authority(zero=[I2])).composition_plan
    assert plan.reconciled_instrument_ids == (I1, I2, I3)
    values = dict(zip(plan.rebalance_state.instrument_ids, plan.rebalance_state.current_values))
    assert values[I2] == D(0) and values[I1] == s.d3c.current_state.current_values[0]
    for bad in (authority(), authority(zero=[I1]), authority(zero=[I2, I3]), authority(zero=[I2], exits=[I3])):
        with pytest.raises(ValueError):
            compose(s, bad)                                                                # missing, wrong, extra or stale: authority is never derived
    with pytest.raises(ValueError):
        compose(s, authority(zero=[I2, UUID(int=0x9999)]))


def test_current_only_holdings_need_exact_exit_authorization_which_is_not_a_trade() -> None:
    s = scenario(HELD, CURRENT_ONLY)
    result = compose(s, authority(exits=[I3]))
    plan = result.composition_plan
    assert plan.current_only_exit_instrument_ids == (I3,) and plan.reconciled_instrument_ids == (I1, I3)
    weights = dict(zip(plan.rebalance_target.instrument_ids, plan.rebalance_target.weights))
    assert weights[I3] == D(0) and weights[I1] == D(1)                                     # the closed composition targets the exit at zero; nothing is sold here
    for bad in (authority(), authority(exits=[I1]), authority(exits=[I1, I3]), authority(exits=[I3, UUID(int=0x9999)]), authority(zero=[I3])):
        with pytest.raises(ValueError):
            compose(s, bad)
    assert not any(hasattr(result, name) for name in ("trades", "instructions", "orders"))


def test_simultaneous_target_only_and_current_only_differences_need_both_explicit_tuples() -> None:
    s = scenario(HELD, BOTH)
    plan = compose(s, authority(zero=[I2], exits=[I3])).composition_plan
    assert plan.reconciled_instrument_ids == (I1, I2, I3)
    for bad in (authority(zero=[I2]), authority(exits=[I3]), authority(), authority(zero=[I3], exits=[I2])):
        with pytest.raises(ValueError):
            compose(s, bad)


# --- AF-AO: cash-only ---------------------------------------------------------------------------------------------------------------

def test_cash_only_with_positive_cash_and_full_zero_confirmation_composes_exact_zero_values() -> None:
    s = scenario((), CASH_ONLY_SPEC, investable="100")
    assert s.d3c.current_state.instrument_ids == () and s.d3c.current_state.current_values == ()
    plan = compose(s, authority(zero=[I1, I2, I3])).composition_plan
    assert plan.rebalance_target.instrument_ids == plan.rebalance_state.instrument_ids == (I1, I2, I3)
    assert plan.rebalance_state.current_values == (D(0), D(0), D(0)) and all(type(v) is D and not v.is_signed() for v in plan.rebalance_state.current_values)
    assert plan.rebalance_state.investable_cash.as_tuple() == s.d3c.current_state.investable_cash.as_tuple() and plan.rebalance_state.investable_cash == D("100")
    assert plan.rebalance_state.currency is s.d3c.current_state.currency is TRY
    assert plan.target_only_zero_confirmed_instrument_ids == (I1, I2, I3) and plan.current_only_exit_instrument_ids == ()


def test_cash_only_needs_every_target_candidate_confirmed_and_no_exit_authority() -> None:
    s = scenario((), CASH_ONLY_SPEC)
    for zero in ([], [I1, I2], [I2, I3], [I1, I2, I3, UUID(int=0x9999)], [I1, I3]):
        with pytest.raises(ValueError):
            compose(s, authority(zero=zero))
    for exits in ([I1], [UUID(int=0x9999)]):
        with pytest.raises(ValueError):
            compose(s, authority(zero=[I1, I2, I3], exits=exits))
    with pytest.raises(ValueError):
        compose(s, authority())                                                            # cash-only status alone never implies zero-current confirmation


def test_cash_only_with_zero_cash_may_compose_and_no_wealth_or_funding_gate_runs_here(monkeypatch) -> None:
    s = scenario((), CASH_ONLY_SPEC, investable="0")
    assert s.d3c.current_state.investable_cash == D(0)
    monkeypatch.setattr("backend.engine.private.allocation_rebalance.build_cash_first_rebalance_plan", lambda **kw: (_ for _ in ()).throw(AssertionError("rebalance")))
    plan = compose(s, authority(zero=[I1, I2, I3])).composition_plan
    assert plan.rebalance_state.investable_cash == D(0)                                    # funding feasibility belongs to the later Phase 21 builder


# --- AP-AV: direct construction -----------------------------------------------------------------------------------------------------

def test_independently_built_canonical_plan_is_accepted() -> None:
    s = scenario(HELD, TARGET_ONLY)
    auth = authority(zero=[I2])
    built = compose(s, auth)
    independent = build_cross_asset_composition_plan(current_state=s.d3c.current_state, sleeves=s.d2.sleeves, authority=auth)
    assert independent is not built.composition_plan
    direct = PrivateBacktestCrossUniverseComposition(candidate_eligibility=s.d2, rebalance_current_state=s.d3c, composition_plan=independent)
    assert direct == built


def test_direct_construction_rejects_forged_foreign_and_non_canonical_plans() -> None:
    s = scenario(HELD, TARGET_ONLY)
    auth = authority(zero=[I2])
    good = compose(s, auth).composition_plan

    def direct(plan):
        return PrivateBacktestCrossUniverseComposition(candidate_eligibility=s.d2, rebalance_current_state=s.d3c, composition_plan=plan)

    for bad in (object(), None, s.d2):
        with pytest.raises(TypeError):
            direct(bad)
    # forged economics are rejected by the closed Phase 22 plan itself
    forged_target = dataclasses.replace(good.rebalance_target, weights=tuple(reversed(good.rebalance_target.weights)))
    for field_changes in ({"rebalance_target": forged_target},
                          {"rebalance_state": dataclasses.replace(good.rebalance_state, current_values=tuple(D(v) + D(1) for v in good.rebalance_state.current_values))},
                          {"rebalance_state": dataclasses.replace(good.rebalance_state, investable_cash=good.rebalance_state.investable_cash + D(1))},
                          {"rebalance_state": dataclasses.replace(good.rebalance_state, currency=USD)}):
        with pytest.raises((TypeError, ValueError)):
            CrossAssetCompositionPlan(**{**{f.name: getattr(good, f.name) for f in dataclasses.fields(good)}, **field_changes})
    # a valid plan built from OTHER inputs is rejected by identity / canonical comparison
    other_sleeves = tuple(mk_sleeve(*spec) for spec in TARGET_ONLY)                                  # equal-valued but different sleeve objects
    cloned_sleeves = build_cross_asset_composition_plan(current_state=s.d3c.current_state, sleeves=other_sleeves, authority=auth)
    with pytest.raises(ValueError):
        direct(cloned_sleeves)
    state_clone = RebalanceCurrentState(instrument_ids=s.d3c.current_state.instrument_ids, current_values=s.d3c.current_state.current_values,
                                        investable_cash=s.d3c.current_state.investable_cash, currency=s.d3c.current_state.currency)
    assert state_clone == s.d3c.current_state and state_clone is not s.d3c.current_state
    with pytest.raises(ValueError):
        direct(build_cross_asset_composition_plan(current_state=state_clone, sleeves=s.d2.sleeves, authority=auth))
    foreign = scenario(HELD, TARGET_ONLY)
    with pytest.raises(ValueError):
        direct(build_cross_asset_composition_plan(current_state=foreign.d3c.current_state, sleeves=foreign.d2.sleeves, authority=auth))
    different = scenario(HELD, CURRENT_ONLY)
    with pytest.raises(ValueError):
        direct(build_cross_asset_composition_plan(current_state=s.d3c.current_state, sleeves=different.d2.sleeves, authority=authority(exits=[I3])))
    # the anchors are re-proven on direct construction as well
    other_replay = scenario(HELD, MATCHING)
    with pytest.raises(ValueError):
        PrivateBacktestCrossUniverseComposition(candidate_eligibility=other_replay.d2, rebalance_current_state=s.d3c, composition_plan=good)
    assert direct(good) == compose(s, auth)


def test_repeated_builds_are_equivalent() -> None:
    s = scenario(HELD, TARGET_ONLY)
    auth = authority(zero=[I2])
    assert compose(s, auth) == compose(s, auth)


# --- AW-BE: scope guards ----------------------------------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_TREE = ast.parse(_PATH.read_text(encoding="utf-8"))
_REL = "backend/engine/private/backtest_cross_universe_composition.py"


def _names() -> set:
    return ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
            | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})


def test_no_discovery_valuation_cash_game_changer_rebalance_or_trade_surface() -> None:
    assert not _names() & {"PointInTimeMarketDataResolver", "PrivateBacktestMarketDataSelectedObservation", "PrivateBacktestMarkedPosition", "CashBucket", "CashPurpose",
                           "PrivateBacktestCashAllocation", "PrivateBacktestGameChangerDecisionReplay", "GameChangerDecisionGate", "RebalanceBandPolicy",
                           "RebalanceFrictionProfile", "RebalanceTradeInstruction", "build_cash_first_rebalance_plan", "build_band_aware_rebalance_plan",
                           "optimize_minimum_cvar_portfolio", "build_bayesian_expected_return_posterior", "scheduler", "dispatcher", "repository", "supabase",
                           "float", "round", "quantize"}
    assert not _names() & {"market_data", "marked_positions", "market_observation", "cash_projection", "positive_balances", "allocations", "position_projection",
                           "resolution_snapshot", "close", "unit_price", "quantity", "bind_candidate_universes_to_sleeves", "confirmed_zero_current_value_instrument_ids",
                           "authorized_exit_instrument_ids", "sorted", "set", "frozenset"}                       # the authority is never derived from set differences here


def test_imports_are_only_the_closed_d2_d3c_and_phase_22_authorities() -> None:
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules == {"backend.engine.private.allocation_universe_composition", "backend.engine.private.backtest_candidate_eligibility_replay",
                       "backend.engine.private.backtest_rebalance_current_state"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.BinOp, ast.AugAssign))]


def test_no_clock_random_hash_io_or_generated_identity() -> None:
    assert not _names() & {"now", "utcnow", "today", "time", "uuid4", "uuid1", "random", "secrets", "urandom", "sha256", "hashlib", "hmac", "hash", "open",
                           "client", "rpc", "table", "environ", "getenv", "sleep", "datetime", "json", "loads", "dumps"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await))]


def test_module_is_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_documentation_and_ci_wiring() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "PRIVATE_BACKTEST_CROSS_UNIVERSE_COMPOSITION.md").read_text(encoding="utf-8")
    for needle in ("counterfactual", "D2", "D3C", "analysis context", "object identity", "projection binding", "coverage wrappers", "separate C2E manifests",
                   "CrossUniverseAuthority", "never derived", "zero-current confirmation", "exit authorization is not a trade", "cash-only", "zero-wealth",
                   "no Game Changer", "no rebalance"):
        assert needle in doc, needle
    current = (root / "docs" / "PRIVATE_BACKTEST_REBALANCE_CURRENT_STATE.md").read_text(encoding="utf-8")
    assert "D4B consumes this current state together with D2 candidate-eligible sleeves and explicit CrossUniverseAuthority" in current
    assert "D4B requires exact confirmation of every target-only candidate" in current
    eligibility = (root / "docs" / "PRIVATE_BACKTEST_CANDIDATE_ELIGIBILITY_REPLAY.md").read_text(encoding="utf-8")
    assert "D4B consumes the exact D2 sleeves only after proving the D2 and D3C sides share the same replay analysis context and exact portfolio projection binding" in eligibility
    assert "D2 remains candidate eligibility only" in eligibility
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    architecture = ci.split("Phase 26 backtest architecture correctness", 1)[1].split("- name:", 1)[0]
    assert "backend/tests/test_backtest_cross_universe_composition.py" in architecture
