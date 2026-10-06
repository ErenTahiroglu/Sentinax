"""
backend/tests/test_backtest_rebalance_plan.py
=============================================
Phase 26D5A: historical band-aware rebalance plan replay. The canonical D4B same-universe target/state pair goes through the CLOSED Phase 21B `build_band_aware_rebalance_plan` under EXPLICIT
fixed counterfactual `RebalanceBandPolicy` / `RebalanceFrictionProfile` parameters (never discovered, defaulted, calibrated or inferred from fees). No second rebalance algorithm, no Game Changer
consumption, no target/sleeve mutation, no friction settlement, no execution.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import backtest_rebalance_plan as module_under_test
from backend.engine.private.allocation_rebalance import (
    RebalanceCurrentState,
    RebalanceTargetAllocation,
    RebalanceTradeStage,
    build_cash_first_rebalance_plan,
)
from backend.engine.private.allocation_rebalance_policy import (
    BandAwareRebalancePlan,
    RebalanceBandPolicy,
    RebalanceFrictionProfile,
    build_band_aware_rebalance_plan,
)
from backend.engine.private.backtest_cross_universe_composition import PrivateBacktestCrossUniverseComposition
from backend.engine.private.backtest_rebalance_plan import (
    PrivateBacktestBandAwareRebalanceReplay,
    replay_private_backtest_band_aware_rebalance,
)
from backend.tests.invariants import static_guards as sg
from backend.tests.test_backtest_cross_universe_composition import (
    BOTH,
    CASH_ONLY_SPEC,
    CURRENT_ONLY,
    HELD,
    I1,
    I2,
    I3,
    MATCHING,
    TARGET_ONLY,
    authority,
    compose,
    scenario,
)

D = Decimal
S = RebalanceTradeStage


# --- fixtures ----------------------------------------------------------------------------------------------------------------------

def policy_for(ids, trigger="0", destination="0") -> RebalanceBandPolicy:
    return RebalanceBandPolicy(instrument_ids=tuple(ids), trigger_drifts=(D(trigger),) * len(ids), destination_drifts=(D(destination),) * len(ids))


def friction_for(ids, buy="0.001", sell="0.002") -> RebalanceFrictionProfile:
    return RebalanceFrictionProfile(instrument_ids=tuple(ids), buy_friction_rates=(D(buy),) * len(ids), sell_friction_rates=(D(sell),) * len(ids))


def d4b(held=HELD, spec=MATCHING, auth=None, investable="100") -> PrivateBacktestCrossUniverseComposition:
    s = scenario(held, spec, investable=investable)
    return compose(s, auth if auth is not None else authority())


def replay(composition, trigger="0", destination="0", policy=None, friction=None):
    ids = composition.composition_plan.reconciled_instrument_ids
    return replay_private_backtest_band_aware_rebalance(
        cross_universe_composition=composition, policy=policy or policy_for(ids, trigger, destination), friction=friction or friction_for(ids))


def summary(trades):
    return [(t.instrument_id, t.stage, t.notional.as_tuple()) for t in trades]


# --- A-M: contract, exact types and identities ------------------------------------------------------------------------------------

def test_result_is_exactly_two_frozen_fields_without_defaults() -> None:
    fs = dataclasses.fields(PrivateBacktestBandAwareRebalanceReplay)
    assert [f.name for f in fs] == ["cross_universe_composition", "rebalance_plan"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestBandAwareRebalanceReplay.__dataclass_params__.frozen is True
    assert not {"target", "state", "policy", "friction", "trades", "analysis_context", "portfolio", "sleeves", "authority"} & {f.name for f in fs}


def test_builder_is_keyword_only_with_three_parameters_and_no_defaults() -> None:
    params = inspect.signature(replay_private_backtest_band_aware_rebalance).parameters
    assert list(params) == ["cross_universe_composition", "policy", "friction"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in params.values())
    composition = d4b()
    ids = composition.composition_plan.reconciled_instrument_ids
    with pytest.raises(TypeError):
        replay_private_backtest_band_aware_rebalance(composition, policy_for(ids), friction_for(ids))  # type: ignore[misc]


def test_exact_types_are_required_and_subclasses_rejected() -> None:
    composition = d4b()
    ids = composition.composition_plan.reconciled_instrument_ids
    policy, friction = policy_for(ids), friction_for(ids)

    class SubComposition(PrivateBacktestCrossUniverseComposition):
        pass

    class SubPolicy(RebalanceBandPolicy):
        pass

    class SubFriction(RebalanceFrictionProfile):
        pass

    sub_comp = SubComposition(**{f.name: getattr(composition, f.name) for f in dataclasses.fields(composition)})
    sub_policy = SubPolicy(**{f.name: getattr(policy, f.name) for f in dataclasses.fields(policy)})
    sub_friction = SubFriction(**{f.name: getattr(friction, f.name) for f in dataclasses.fields(friction)})
    for bad in (sub_comp, object(), None, composition.composition_plan):
        with pytest.raises(TypeError):
            replay_private_backtest_band_aware_rebalance(cross_universe_composition=bad, policy=policy, friction=friction)  # type: ignore[arg-type]
    for bad in (sub_policy, object(), None, friction):
        with pytest.raises(TypeError):
            replay_private_backtest_band_aware_rebalance(cross_universe_composition=composition, policy=bad, friction=friction)  # type: ignore[arg-type]
    for bad in (sub_friction, object(), None, policy):
        with pytest.raises(TypeError):
            replay_private_backtest_band_aware_rebalance(cross_universe_composition=composition, policy=policy, friction=bad)  # type: ignore[arg-type]


def test_identities_d4b_target_state_policy_and_friction_are_preserved(monkeypatch) -> None:
    composition = d4b()
    ids = composition.composition_plan.reconciled_instrument_ids
    policy, friction = policy_for(ids), friction_for(ids)
    calls = []
    real = module_under_test.build_band_aware_rebalance_plan

    def spy(*, target, state, policy, friction):
        calls.append((target, state, policy, friction))
        return real(target=target, state=state, policy=policy, friction=friction)

    monkeypatch.setattr(module_under_test, "build_band_aware_rebalance_plan", spy)
    result = replay_private_backtest_band_aware_rebalance(cross_universe_composition=composition, policy=policy, friction=friction)
    plan = result.rebalance_plan
    assert result.cross_universe_composition is composition
    assert type(plan) is BandAwareRebalancePlan
    assert plan.target is composition.composition_plan.rebalance_target and plan.state is composition.composition_plan.rebalance_state
    assert plan.policy is policy and plan.friction is friction
    assert len(calls) >= 1 and all(c[0] is plan.target and c[1] is plan.state and c[2] is policy and c[3] is friction for c in calls)


def test_the_reconciled_phase_22_state_is_used_not_the_pre_composition_d3c_state() -> None:
    composition = d4b(spec=TARGET_ONLY, auth=authority(zero=[I2]))
    pre = composition.rebalance_current_state.current_state
    plan = replay(composition).rebalance_plan
    assert pre.instrument_ids == (I1, I3) and plan.state.instrument_ids == (I1, I2, I3)
    assert plan.state is composition.composition_plan.rebalance_state


# --- N-T: universe matching ---------------------------------------------------------------------------------------------------------

def test_matching_policy_and_friction_universe_succeeds_and_any_mismatch_fails_without_repair() -> None:
    composition = d4b(spec=TARGET_ONLY, auth=authority(zero=[I2]))
    ids = composition.composition_plan.reconciled_instrument_ids
    assert ids == (I1, I2, I3)
    assert replay(composition).rebalance_plan.policy.instrument_ids == ids
    for bad_ids in (ids[1:], ids[:-1], ids + (UUID(int=0x9999),), (I1, I3)):
        with pytest.raises(ValueError):
            replay_private_backtest_band_aware_rebalance(cross_universe_composition=composition, policy=policy_for(bad_ids), friction=friction_for(ids))
        with pytest.raises(ValueError):
            replay_private_backtest_band_aware_rebalance(cross_universe_composition=composition, policy=policy_for(ids), friction=friction_for(bad_ids))
    for reordered in ((I3, I2, I1), (I2, I1, I3)):                               # the closed ids require canonical order: never sorted for the caller
        with pytest.raises(ValueError):
            policy_for(reordered)
        with pytest.raises(ValueError):
            friction_for(reordered)


# --- U-W: no-trade region and triggered plans ------------------------------------------------------------------------------------------

def test_no_trigger_gives_no_trades_and_investable_cash_alone_does_not_bypass_the_band() -> None:
    composition = d4b(investable="100")
    state = composition.composition_plan.rebalance_state
    assert state.investable_cash == D(100)
    result = replay(composition, trigger="1", destination="0")
    plan = result.rebalance_plan
    assert plan.is_triggered is False and plan.trades == ()
    assert plan.state.investable_cash == D(100)                                 # the cash stays unspent: it is not mandatory deployment


def test_a_breached_trigger_band_is_replayed_entirely_by_phase_21b() -> None:
    composition = d4b()
    result = replay(composition, trigger="0", destination="0")
    plan = result.rebalance_plan
    assert plan.is_triggered is True and plan.trades != ()
    ids = composition.composition_plan.reconciled_instrument_ids
    assert plan == build_band_aware_rebalance_plan(target=plan.target, state=plan.state, policy=plan.policy, friction=plan.friction)
    assert {t.instrument_id for t in plan.trades} <= set(ids)


# --- X-AC: Phase 22 provenance semantics ---------------------------------------------------------------------------------------------------

def test_a_confirmed_target_only_candidate_is_treated_normally_by_phase_21b() -> None:
    composition = d4b(spec=TARGET_ONLY, auth=authority(zero=[I2]))
    state, target = composition.composition_plan.rebalance_state, composition.composition_plan.rebalance_target
    index = target.instrument_ids.index(I2)
    assert state.current_values[index] == D(0) and target.weights[index] > 0
    triggered = replay(composition, trigger="0", destination="0").rebalance_plan
    assert any(t.instrument_id == I2 and t.stage is not S.SELL for t in triggered.trades)
    wide = replay(composition, trigger="1", destination="0").rebalance_plan
    assert wide.trades == ()                                                    # nothing is auto-bought outside the explicit policy


def test_an_authorized_exit_is_target_zero_and_is_never_forced_liquidation() -> None:
    composition = d4b(spec=CURRENT_ONLY, auth=authority(exits=[I3]))
    state, target = composition.composition_plan.rebalance_state, composition.composition_plan.rebalance_target
    index = target.instrument_ids.index(I3)
    assert target.weights[index] == D(0) and state.current_values[index] > 0
    quiet = replay(composition, trigger="1", destination="0").rebalance_plan
    assert quiet.trades == () and quiet.is_triggered is False                    # exit authority alone sells nothing
    active = replay(composition, trigger="0", destination="0").rebalance_plan
    assert any(t.instrument_id == I3 and t.stage is S.SELL for t in active.trades)    # a SELL only under the normal band policy


def test_simultaneous_target_only_and_current_only_provenance_is_replayed_normally() -> None:
    composition = d4b(spec=BOTH, auth=authority(zero=[I2], exits=[I3]))
    plan = replay(composition, trigger="0", destination="0").rebalance_plan
    assert plan.state.instrument_ids == (I1, I2, I3) and plan.is_triggered


# --- AA-AC: cash-only ------------------------------------------------------------------------------------------------------------------------

def test_cash_only_positive_cash_replays_and_the_outcome_depends_only_on_the_explicit_bands() -> None:
    composition = d4b(held=(), spec=CASH_ONLY_SPEC, auth=authority(zero=[I1, I2, I3]), investable="100")
    assert composition.rebalance_current_state.current_state.instrument_ids == ()
    state = composition.composition_plan.rebalance_state
    assert state.current_values == (D(0), D(0), D(0)) and state.investable_cash == D(100)
    active = replay(composition, trigger="0", destination="0").rebalance_plan
    assert active.trades != () and all(t.stage is S.CASH_FUNDED_BUY for t in active.trades)
    quiet = replay(composition, trigger="1", destination="0").rebalance_plan
    assert quiet.trades == () and quiet.is_triggered is False                    # both are valid policy outcomes


def test_cash_only_with_zero_total_wealth_fails_through_the_existing_wealth_gate_and_invents_no_funding() -> None:
    composition = d4b(held=(), spec=CASH_ONLY_SPEC, auth=authority(zero=[I1, I2, I3]), investable="0")
    assert composition.composition_plan.rebalance_state.investable_cash == D(0)
    ids = composition.composition_plan.reconciled_instrument_ids
    for trigger in ("0", "1"):
        with pytest.raises(ValueError, match="rebalance total wealth must be strictly positive"):
            replay(composition, trigger=trigger, destination="0")
    with pytest.raises(ValueError, match="rebalance total wealth must be strictly positive"):
        build_band_aware_rebalance_plan(target=composition.composition_plan.rebalance_target, state=composition.composition_plan.rebalance_state,
                                        policy=policy_for(ids), friction=friction_for(ids))


# --- AD-AH: zero-band Phase 21A equivalence and friction semantics --------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["matching", "target_only", "current_only", "both", "cash_only"])
def test_zero_trigger_and_zero_destination_reproduce_the_phase_21a_reference_exactly(case) -> None:
    composition = {
        "matching": lambda: d4b(),
        "target_only": lambda: d4b(spec=TARGET_ONLY, auth=authority(zero=[I2])),
        "current_only": lambda: d4b(spec=CURRENT_ONLY, auth=authority(exits=[I3])),
        "both": lambda: d4b(spec=BOTH, auth=authority(zero=[I2], exits=[I3])),
        "cash_only": lambda: d4b(held=(), spec=CASH_ONLY_SPEC, auth=authority(zero=[I1, I2, I3])),
    }[case]()
    plan = replay(composition, trigger="0", destination="0").rebalance_plan
    reference = build_cash_first_rebalance_plan(target=composition.composition_plan.rebalance_target, state=composition.composition_plan.rebalance_state)
    assert summary(plan.trades) == summary(reference.trades)                      # ids, stages and exact Decimal representations


def test_friction_rates_do_not_alter_the_exact_target_result_when_the_destination_is_zero() -> None:
    composition = d4b(spec=TARGET_ONLY, auth=authority(zero=[I2]))
    ids = composition.composition_plan.reconciled_instrument_ids
    cheap = replay(composition, policy=policy_for(ids), friction=friction_for(ids, "0", "0")).rebalance_plan
    costly = replay(composition, policy=policy_for(ids), friction=friction_for(ids, "0.5", "0.9")).rebalance_plan
    assert summary(cheap.trades) == summary(costly.trades)


def test_a_nonzero_destination_may_differ_from_the_exact_target_reference_by_policy_design() -> None:
    composition = d4b()
    ids = composition.composition_plan.reconciled_instrument_ids
    banded = replay(composition, policy=policy_for(ids, trigger="0.2", destination="0.1")).rebalance_plan
    exact = build_cash_first_rebalance_plan(target=composition.composition_plan.rebalance_target, state=composition.composition_plan.rebalance_state)
    assert banded.is_triggered and summary(banded.trades) != summary(exact.trades)


def test_friction_is_diagnostic_and_never_settled_against_cash_wealth_or_notionals() -> None:
    composition = d4b(spec=TARGET_ONLY, auth=authority(zero=[I2]))
    state = composition.composition_plan.rebalance_state
    ids = state.instrument_ids
    result = replay(composition, policy=policy_for(ids), friction=friction_for(ids, "0.3", "0.4"))
    plan = result.rebalance_plan
    assert plan.state is state and state.investable_cash == composition.rebalance_current_state.current_state.investable_cash
    wealth = D(0)
    for value in state.current_values:
        wealth += value
    assert plan.total_wealth == wealth + state.investable_cash                   # no friction deducted from wealth, cash or current values


# --- AI-AN: direct construction -------------------------------------------------------------------------------------------------------------

def test_independently_built_canonical_phase_21b_plan_is_accepted() -> None:
    composition = d4b()
    ids = composition.composition_plan.reconciled_instrument_ids
    built = replay(composition)
    own_policy, own_friction = policy_for(ids), friction_for(ids)               # its own explicit exact objects
    independent = build_band_aware_rebalance_plan(target=composition.composition_plan.rebalance_target, state=composition.composition_plan.rebalance_state,
                                                  policy=own_policy, friction=own_friction)
    assert independent is not built.rebalance_plan
    direct = PrivateBacktestBandAwareRebalanceReplay(cross_universe_composition=composition, rebalance_plan=independent)
    assert direct.rebalance_plan.policy is own_policy and direct == built


def test_direct_construction_rejects_forged_foreign_and_mistyped_plans() -> None:
    composition = d4b()
    target, state = composition.composition_plan.rebalance_target, composition.composition_plan.rebalance_state
    ids = target.instrument_ids
    good = replay(composition).rebalance_plan

    def direct(plan, comp=composition):
        return PrivateBacktestBandAwareRebalanceReplay(cross_universe_composition=comp, rebalance_plan=plan)

    for bad in (object(), None, composition, target):
        with pytest.raises(TypeError):
            direct(bad)
    with pytest.raises(TypeError):
        PrivateBacktestBandAwareRebalanceReplay(cross_universe_composition=object(), rebalance_plan=good)  # type: ignore[arg-type]
    # forged trades are rejected by the closed Phase 21B plan itself
    with pytest.raises(ValueError):
        BandAwareRebalancePlan(target=target, state=state, policy=good.policy, friction=good.friction, trades=good.trades[:-1])
    with pytest.raises(ValueError):
        BandAwareRebalancePlan(target=target, state=state, policy=good.policy, friction=good.friction, trades=tuple(reversed(good.trades)) if len(good.trades) > 1 else ())
    # equal-valued but non-identical target / state fail the D4B provenance
    target_clone = RebalanceTargetAllocation(instrument_ids=target.instrument_ids, weights=target.weights)
    state_clone = RebalanceCurrentState(instrument_ids=state.instrument_ids, current_values=state.current_values, investable_cash=state.investable_cash, currency=state.currency)
    assert target_clone == target and state_clone == state and target_clone is not target and state_clone is not state
    with pytest.raises(ValueError):
        direct(build_band_aware_rebalance_plan(target=target_clone, state=state, policy=policy_for(ids), friction=friction_for(ids)))
    with pytest.raises(ValueError):
        direct(build_band_aware_rebalance_plan(target=target, state=state_clone, policy=policy_for(ids), friction=friction_for(ids)))
    # a plan over the pre-composition D3C state or another replay's pair is foreign
    foreign = d4b()
    with pytest.raises(ValueError):
        direct(replay(foreign).rebalance_plan)
    with pytest.raises(ValueError):
        direct(good, comp=foreign)


def test_repeated_builds_are_equivalent() -> None:
    composition = d4b(spec=TARGET_ONLY, auth=authority(zero=[I2]))
    assert replay(composition) == replay(composition)


# --- AO-AW: scope guards ------------------------------------------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_TREE = ast.parse(_PATH.read_text(encoding="utf-8"))
_REL = "backend/engine/private/backtest_rebalance_plan.py"


def _names() -> set:
    return ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
            | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})


def test_only_the_closed_phase_21b_builder_decides_and_nothing_else_is_composed() -> None:
    assert not _names() & {"build_cash_first_rebalance_plan", "CashFirstRebalancePlan", "PrivateBacktestGameChangerDecisionReplay", "GameChangerDecisionGate",
                           "GameChangerInstrumentNewCapitalGate", "PointInTimeMarketDataResolver", "PrivateBacktestMarkedPosition", "PrivateBacktestMarketDataSelectedObservation",
                           "CashBucket", "CashPurpose", "PrivateBacktestCashAllocation", "PortfolioTransaction", "CrossAssetSleeve", "bind_candidate_universes_to_sleeves",
                           "build_cross_asset_composition_plan", "optimize_minimum_cvar_portfolio", "build_bayesian_expected_return_posterior", "scheduler", "dispatcher",
                           "repository", "supabase", "float", "round", "quantize"}
    assert not _names() & {"market_data", "marked_positions", "market_observation", "cash_projection", "allocations", "weights", "trigger_drifts", "destination_drifts",
                           "buy_friction_rates", "sell_friction_rates", "current_values", "investable_cash", "sleeves", "authority", "fee", "commission", "tax", "spread"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.BinOp, ast.AugAssign))]


def test_imports_are_only_the_d4b_composition_and_the_closed_phase_21b_module() -> None:
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules == {"backend.engine.private.allocation_rebalance_policy", "backend.engine.private.backtest_cross_universe_composition"}
    for forbidden in ("game_changer", "market_data", "backtest_market", "portfolio", "allocation_rebalance ", "supabase", "repository", "scheduler"):
        assert not any(forbidden in m for m in modules), forbidden


def test_no_clock_random_hash_io_or_generated_identity() -> None:
    assert not _names() & {"now", "utcnow", "today", "time", "uuid4", "uuid1", "random", "secrets", "urandom", "sha256", "hashlib", "hmac", "hash", "open",
                           "client", "rpc", "table", "environ", "getenv", "sleep", "datetime", "json", "loads", "dumps", "order", "execute", "persist"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await))]


def test_module_is_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_documentation_and_ci_wiring() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "PRIVATE_BACKTEST_REBALANCE_PLAN.md").read_text(encoding="utf-8")
    for needle in ("Phase 21B", "Phase 21A", "fixed replay", "counterfactual", "no parameter discovery", "no historical fee inference", "no-trade region", "zero-band",
                   "reconciled", "not forced liquidation", "cash-only", "zero-wealth", "not settled", "no Game Changer", "no execution"):
        assert needle in doc, needle
    composition = (root / "docs" / "PRIVATE_BACKTEST_CROSS_UNIVERSE_COMPOSITION.md").read_text(encoding="utf-8")
    assert "D5A consumes the canonical D4B target/state pair through closed Phase 21B with explicit fixed replay band/friction parameters" in composition
    assert "not historical evidence" in composition
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    architecture = ci.split("Phase 26 backtest architecture correctness", 1)[1].split("- name:", 1)[0]
    assert "backend/tests/test_backtest_rebalance_plan.py" in architecture
