"""
backend/engine/private/backtest_cross_universe_composition.py
=============================================================
Historical cross-universe target / state composition (Phase 26D4B). The D2 candidate-eligible explicit sleeves, the D3C Phase 21 current state (held universe, or empty for a cash-only
portfolio) and an EXPLICIT `CrossUniverseAuthority` are composed by the CLOSED Phase 22 `build_cross_asset_composition_plan`, the sole economic composition authority (target-only and current-only
set differences, union ordering, zero-value insertion and exit-zero semantics are not duplicated here). The result is the closed `CrossAssetCompositionPlan`; nothing is rebalanced or traded.

Anchors. D2 and D3C use different C2E manifests on purpose (portfolio history + candidate universes versus portfolio history + market data), so the bundles are neither required to be the same
object nor compared. What they must share is: the very analysis context OBJECT (value equality is not enough, so candidate eligibility from one replay can never meet a valuation from another),
and the very portfolio projection binding OBJECT with the same owner. Separate coverage wrappers over that binding (different `observed_at`) are fine.

Authority is explicit and never derived. A target-only candidate with no current holding is not thereby known to have zero current value, and a current-only holding absent from the target is not
thereby authorized to be sold: the caller states both tuples and the closed builder checks them against the real difference (so for a cash-only portfolio every target candidate needs an exact
zero-current confirmation and no exit authorization may exist). `authorized_exit_instrument_ids` is composition authority only (the later target may name that holding at zero); it is not a sell,
an instruction or an execution. Composition is not funding feasibility: with zero investable cash a plan still composes and the later Phase 21 builder rejects non-positive wealth.

Claim limit: conditional / counterfactual. The sleeves, the cash policy and this authority are explicit replay parameters; nothing proves they were historically used or persisted, nor that any
rebalance happened. D2, D3C and the plan are retained by identity (the authority only through the plan, never stored twice). One private derivation serves the builder and `__post_init__`; an
independently built canonical plan over the very same current state, sleeve objects and authority is accepted. No candidate discovery, market data, cash classification, Game Changer input,
rebalance, band, friction or trade. Non-pure by dependency composition; no I/O, clock, randomness or hashing.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.allocation_universe_composition import (
    CrossAssetCompositionPlan,
    CrossUniverseAuthority,
    build_cross_asset_composition_plan,
)
from backend.engine.private.backtest_candidate_eligibility_replay import PrivateBacktestCandidateEligibilityReplay
from backend.engine.private.backtest_rebalance_current_state import PrivateBacktestRebalanceCurrentState

_ERR_D2 = "candidate_eligibility must be an exact PrivateBacktestCandidateEligibilityReplay instance"
_ERR_D3C = "rebalance_current_state must be an exact PrivateBacktestRebalanceCurrentState instance"
_ERR_AUTHORITY = "authority must be an exact CrossUniverseAuthority instance"
_ERR_PLAN = "composition_plan must be an exact CrossAssetCompositionPlan instance"
_ERR_CONTEXT = "D2 and D3C must share the very analysis context object"
_ERR_HISTORY = "D2 and D3C must both carry verified portfolio history"
_ERR_BINDING = "D2 and D3C must share the very portfolio projection binding object"
_ERR_OWNER = "D2 and D3C portfolio history coverage must have the same owner"
_ERR_STATE = "composition_plan.current_state must be the D3C current state object"
_ERR_SLEEVES = "composition_plan.sleeves must be the exact D2 sleeve objects in the D2 order"
_ERR_CANONICAL = "composition_plan must equal the canonical closed Phase 22 composition of the D3C current state, the D2 sleeves and its authority"


def _check_types(candidate_eligibility: object, rebalance_current_state: object) -> None:
    if type(candidate_eligibility) is not PrivateBacktestCandidateEligibilityReplay:
        raise TypeError(_ERR_D2)
    if type(rebalance_current_state) is not PrivateBacktestRebalanceCurrentState:
        raise TypeError(_ERR_D3C)


def _check_anchors(candidate_eligibility: PrivateBacktestCandidateEligibilityReplay, rebalance_current_state: PrivateBacktestRebalanceCurrentState) -> None:
    """The two sides must share one replay context object and one portfolio projection binding object (not merely equal values)."""
    d2_bundle = candidate_eligibility.input_bundle
    d3_bundle = rebalance_current_state.investable_cash_selection.marked_holdings_state.input_bundle
    if d2_bundle.analysis_context is not d3_bundle.analysis_context:
        raise ValueError(_ERR_CONTEXT)
    d2_history, d3_history = d2_bundle.portfolio_history, d3_bundle.portfolio_history
    if d2_history is None or d3_history is None:
        raise ValueError(_ERR_HISTORY)
    if d2_history.projection_binding is not d3_history.projection_binding:
        raise ValueError(_ERR_BINDING)
    if d2_history.owner_id != d3_history.owner_id:
        raise ValueError(_ERR_OWNER)


def _canonical_plan(candidate_eligibility: PrivateBacktestCandidateEligibilityReplay, rebalance_current_state: PrivateBacktestRebalanceCurrentState,
                    authority: CrossUniverseAuthority) -> CrossAssetCompositionPlan:
    """The only composition call: the closed Phase 22 builder over the D3C current state and the exact D2 sleeves."""
    return build_cross_asset_composition_plan(current_state=rebalance_current_state.current_state, sleeves=candidate_eligibility.sleeves, authority=authority)


@dataclass(frozen=True)
class PrivateBacktestCrossUniverseComposition:
    """D2 candidate eligibility, the D3C current state and the closed Phase 22 composition plan over them; the authority lives only inside the plan."""
    candidate_eligibility: PrivateBacktestCandidateEligibilityReplay
    rebalance_current_state: PrivateBacktestRebalanceCurrentState
    composition_plan: CrossAssetCompositionPlan

    def __post_init__(self) -> None:
        _check_types(self.candidate_eligibility, self.rebalance_current_state)
        _check_anchors(self.candidate_eligibility, self.rebalance_current_state)
        plan = self.composition_plan
        if type(plan) is not CrossAssetCompositionPlan:
            raise TypeError(_ERR_PLAN)
        if type(plan.authority) is not CrossUniverseAuthority:
            raise TypeError(_ERR_AUTHORITY)
        if plan.current_state is not self.rebalance_current_state.current_state:
            raise ValueError(_ERR_STATE)
        sleeves = self.candidate_eligibility.sleeves
        if len(plan.sleeves) != len(sleeves) or not all(supplied is expected for supplied, expected in zip(plan.sleeves, sleeves)):
            raise ValueError(_ERR_SLEEVES)
        if plan != _canonical_plan(self.candidate_eligibility, self.rebalance_current_state, plan.authority):
            raise ValueError(_ERR_CANONICAL)


def build_private_backtest_cross_universe_composition(
    *,
    candidate_eligibility: PrivateBacktestCandidateEligibilityReplay,
    rebalance_current_state: PrivateBacktestRebalanceCurrentState,
    authority: CrossUniverseAuthority,
) -> PrivateBacktestCrossUniverseComposition:
    """Compose the D2 sleeves and the D3C current state through the closed Phase 22 builder under an explicit authority; no rebalance."""
    _check_types(candidate_eligibility, rebalance_current_state)
    if type(authority) is not CrossUniverseAuthority:
        raise TypeError(_ERR_AUTHORITY)
    _check_anchors(candidate_eligibility, rebalance_current_state)
    plan = _canonical_plan(candidate_eligibility, rebalance_current_state, authority)
    return PrivateBacktestCrossUniverseComposition(candidate_eligibility=candidate_eligibility, rebalance_current_state=rebalance_current_state, composition_plan=plan)
