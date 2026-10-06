"""
backend/engine/private/backtest_rebalance_plan.py
=================================================
Historical band-aware rebalance plan replay (Phase 26D5A). The canonical D4B same-universe target/state pair (`composition_plan.rebalance_target` / `composition_plan.rebalance_state`, never D3C's
pre-composition state, because Phase 22 may have inserted confirmed zero-current candidates, kept holdings and given authorized exits a zero target) is replayed through the CLOSED Phase 21B
`build_band_aware_rebalance_plan` under an explicit `RebalanceBandPolicy` and `RebalanceFrictionProfile`. That builder is the sole rebalance authority: trigger detection, destination bands, the
no-trade region, the minimum sale, the friction tie resolution, staging and the wealth arithmetic are not duplicated, and Phase 21A is not called (with zero bands 21B reproduces the 21A exact-target
reference, which the tests prove).

Policy provenance limit. Nothing in the repository proves which band policy or friction profile was active at a past replay point, so both are EXPLICIT FIXED REPLAY (counterfactual) parameters: the result
says only "had these parameters been applied to this historical target/current state, this is the canonical Phase 21B plan". It does not claim they were configured, approved or used, that the friction
rates are real fees, or that anything was acted upon. There is no parameter discovery, default, calibration or optimization, no fee, tax or spread inference, and no recorded_at, id or revision requirement.
Friction stays the closed Phase 21B planning input (tie-breaking and diagnostics) and is never deducted from cash, wealth, notionals or current values.

The policy and friction universes must equal the reconciled universe exactly; this module repairs nothing (no sorting, insertion or omission) and the closed builder rejects a mismatch. The plan retains the
exact target, state, policy and friction objects, so none is stored again here; D4B is retained by identity, keeping D2, D3 and the explicit cross-universe authority reachable. An authorized exit
(target zero) is not forced liquidation: a SELL appears only if the band policy produces it. Inside the trigger band there is no trade and investable cash is not mandatory deployment; a cash-only
reconciled state is an ordinary Phase 21B state (zero total wealth still fails the existing wealth gate and no funding is invented). No Game Changer gate is consumed (admission over a plan is a separate
checkpoint), no target weight or sleeve is changed, and nothing is executed, ordered, settled or persisted. Non-pure by dependency composition; no I/O, clock, randomness or hashing.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.allocation_rebalance_policy import (
    BandAwareRebalancePlan,
    RebalanceBandPolicy,
    RebalanceFrictionProfile,
    build_band_aware_rebalance_plan,
)
from backend.engine.private.backtest_cross_universe_composition import PrivateBacktestCrossUniverseComposition

_ERR_COMPOSITION = "cross_universe_composition must be an exact PrivateBacktestCrossUniverseComposition instance"
_ERR_POLICY = "policy must be an exact RebalanceBandPolicy instance"
_ERR_FRICTION = "friction must be an exact RebalanceFrictionProfile instance"
_ERR_PLAN = "rebalance_plan must be an exact BandAwareRebalancePlan instance"
_ERR_TARGET = "rebalance_plan.target must be the very D4B reconciled rebalance target object"
_ERR_STATE = "rebalance_plan.state must be the very D4B reconciled rebalance state object"
_ERR_CANONICAL = "rebalance_plan must equal the canonical closed Phase 21B plan over its own target, state, policy and friction"


@dataclass(frozen=True)
class PrivateBacktestBandAwareRebalanceReplay:
    """The D4B composition (kept by identity) and the closed Phase 21B plan over its target/state pair; the plan itself retains the explicit policy and friction."""
    cross_universe_composition: PrivateBacktestCrossUniverseComposition
    rebalance_plan: BandAwareRebalancePlan

    def __post_init__(self) -> None:
        if type(self.cross_universe_composition) is not PrivateBacktestCrossUniverseComposition:
            raise TypeError(_ERR_COMPOSITION)
        plan = self.rebalance_plan
        if type(plan) is not BandAwareRebalancePlan:
            raise TypeError(_ERR_PLAN)
        if type(plan.policy) is not RebalanceBandPolicy:
            raise TypeError(_ERR_POLICY)
        if type(plan.friction) is not RebalanceFrictionProfile:
            raise TypeError(_ERR_FRICTION)
        composition_plan = self.cross_universe_composition.composition_plan
        if plan.target is not composition_plan.rebalance_target:
            raise ValueError(_ERR_TARGET)
        if plan.state is not composition_plan.rebalance_state:
            raise ValueError(_ERR_STATE)
        if plan != build_band_aware_rebalance_plan(target=plan.target, state=plan.state, policy=plan.policy, friction=plan.friction):
            raise ValueError(_ERR_CANONICAL)


def replay_private_backtest_band_aware_rebalance(
    *,
    cross_universe_composition: PrivateBacktestCrossUniverseComposition,
    policy: RebalanceBandPolicy,
    friction: RebalanceFrictionProfile,
) -> PrivateBacktestBandAwareRebalanceReplay:
    """Replay the closed Phase 21B plan over the D4B target/state pair under explicit fixed counterfactual band and friction parameters."""
    if type(cross_universe_composition) is not PrivateBacktestCrossUniverseComposition:
        raise TypeError(_ERR_COMPOSITION)
    if type(policy) is not RebalanceBandPolicy:
        raise TypeError(_ERR_POLICY)
    if type(friction) is not RebalanceFrictionProfile:
        raise TypeError(_ERR_FRICTION)
    composition_plan = cross_universe_composition.composition_plan
    plan = build_band_aware_rebalance_plan(target=composition_plan.rebalance_target, state=composition_plan.rebalance_state, policy=policy, friction=friction)
    return PrivateBacktestBandAwareRebalanceReplay(cross_universe_composition=cross_universe_composition, rebalance_plan=plan)
