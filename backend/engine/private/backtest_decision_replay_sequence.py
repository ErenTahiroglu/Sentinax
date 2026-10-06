"""
backend/engine/private/backtest_decision_replay_sequence.py
===========================================================
Ordered decision replay sequence (Phase 26D6): sequence/provenance closure for the Phase 26 deterministic decision chain. It binds a Phase 26B `PrivateBacktestReplayPlan` to exactly ONE completed D5B
`PrivateBacktestGameChangerPlanAdmission` per replay point, in the plan's order. No economic policy is added and nothing is recomputed: no D1 to D5B builder is called, no gate, trade, state or value is
derived, and no replay point or date is generated (the plan stays the sole point-list authority).

Binding. `admissions` must be an exact tuple with exactly one exact admission per point; the admission at index i must carry, through its D1 analysis context, the very replay-point OBJECT at
`replay_plan.points[i]` (object identity: an equal-valued clone, another plan's point or the same date at another cutoff does not match) and the explicit `horizon` of the sequence (Horizon is never
inferred, so a mixed-horizon run is rejected and another horizon needs another sequence). Caller order is authoritative: nothing is sorted, matched by date or cutoff, searched or deduplicated. The
plan, the admissions tuple and every admission are retained by identity. The as-of mode is not repeated: the plan already enforces one mode and each admission sits on its exact plan point.

BLOCKED_NEW_CAPITAL and NOT_BLOCKED admissions are both valid members and are preserved exactly: this is an audit sequence, not execution control, so a blocked point never stops, drops or skips a later
point, NOT_BLOCKED is never read as approval or success, and there is no aggregate verdict, rate, worst state or pass/fail score. There is no simulated execution: the trades of one admission are never
applied to the next point, no ledger, quantity, cash or friction is mutated or carried forward, and each point's own verified historical ledger projection stays its portfolio authority.

One portfolio. All points must share the owner id, the portfolio id and the PortfolioMode (MY_PORTFOLIO and SANDBOX are strict bounded contexts and a sequence never crosses them), read through each
admission's retained D1-side portfolio history. The projection binding objects, coverage wrappers, observed_at times and ledger contents are NOT compared across points: they evolve with historical time. The mode
equality is consistency of the supplied pointwise states, not proof of historical portfolio-metadata revision provenance, and no valuation-currency or policy equality is implied.

Claim limit. The sequence is POINTWISE COUNTERFACTUAL decision replay. The D2 sleeves, D3B cash allocations, D4B authority and D5A band and friction parameters of different points are never compared,
required equal or rejected for varying, because no historical strategy-configuration authority exists; nothing claims one fixed strategy ran through time (a true walk-forward would need explicit,
versioned policy provenance). There is no performance, profit, loss, return, risk or turnover measure. The closed chain does not consume macro, risk-evidence or user-view inputs and none is added here.
Known limit: Phase 26 has no source-wide Game Changer discovery coverage, so a zero-event replay point cannot be proven; an absent D1/D5B result is never read as "no events" and no sentinel or synthetic
gate exists, so such a point simply cannot be part of a completed sequence. Non-pure by dependency composition; no I/O, clock, randomness or hashing.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.backtest_game_changer_plan_admission import PrivateBacktestGameChangerPlanAdmission
from backend.engine.private.backtest_replay_plan import PrivateBacktestReplayPlan
from backend.engine.private.domain import Horizon

_ERR_PLAN = "replay_plan must be an exact PrivateBacktestReplayPlan instance"
_ERR_HORIZON = "horizon must be an exact Horizon member"
_ERR_TUPLE = "admissions must be an exact tuple"
_ERR_MEMBER = "every admission must be an exact PrivateBacktestGameChangerPlanAdmission instance"
_ERR_COUNT = "admissions must contain exactly one admission per replay-plan point"
_ERR_POINT = "the admission at each position must be built on the very replay-plan point object at that position"
_ERR_HORIZON_MATCH = "every admission must use the sequence's explicit horizon"
_ERR_HISTORY = "every admission must carry verified portfolio history"
_ERR_OWNER = "every admission must belong to the same portfolio owner"
_ERR_PORTFOLIO = "every admission must belong to the same portfolio id"
_ERR_MODE = "every admission must carry the same PortfolioMode"


def _validate(replay_plan: object, horizon: object, admissions: object) -> None:
    """The single validation path for the builder and `__post_init__`; nothing is repaired, matched, sorted or recomputed."""
    if type(replay_plan) is not PrivateBacktestReplayPlan:
        raise TypeError(_ERR_PLAN)
    if type(horizon) is not Horizon:
        raise TypeError(_ERR_HORIZON)
    if type(admissions) is not tuple:
        raise TypeError(_ERR_TUPLE)
    if not all(type(admission) is PrivateBacktestGameChangerPlanAdmission for admission in admissions):
        raise TypeError(_ERR_MEMBER)
    if len(admissions) != len(replay_plan.points):
        raise ValueError(_ERR_COUNT)
    reference = admissions[0].game_changer_replay.input_bundle.portfolio_history
    if reference is None:
        raise ValueError(_ERR_HISTORY)
    for replay_point, admission in zip(replay_plan.points, admissions):
        context = admission.game_changer_replay.input_bundle.analysis_context
        if context.replay_point is not replay_point:
            raise ValueError(_ERR_POINT)
        if context.horizon is not horizon:
            raise ValueError(_ERR_HORIZON_MATCH)
        # One sequence is one portfolio replay. The D1-side history is the point-level anchor (D5B already proved it shares the exact binding and owner with the D5A/D4B side); across points only
        # owner, portfolio id and bounded-context mode must stay equal: bindings, coverage wrappers, observed_at and ledger contents are expected to differ through historical time.
        history = admission.game_changer_replay.input_bundle.portfolio_history
        if history is None:
            raise ValueError(_ERR_HISTORY)
        if history.owner_id != reference.owner_id:
            raise ValueError(_ERR_OWNER)
        if history.portfolio_id != reference.portfolio_id:
            raise ValueError(_ERR_PORTFOLIO)
        if history.projection_binding.projection.mode is not reference.projection_binding.projection.mode:
            raise ValueError(_ERR_MODE)


@dataclass(frozen=True)
class PrivateBacktestDecisionReplaySequence:
    """The replay plan, one explicit horizon and exactly one completed D5B admission per plan point in plan order; all three retained by identity."""
    replay_plan: PrivateBacktestReplayPlan
    horizon: Horizon
    admissions: tuple[PrivateBacktestGameChangerPlanAdmission, ...]

    def __post_init__(self) -> None:
        _validate(self.replay_plan, self.horizon, self.admissions)


def build_private_backtest_decision_replay_sequence(
    *,
    replay_plan: PrivateBacktestReplayPlan,
    horizon: Horizon,
    admissions: tuple[PrivateBacktestGameChangerPlanAdmission, ...],
) -> PrivateBacktestDecisionReplaySequence:
    """Bind one completed D5B admission to every replay-plan point, by positional replay-point object identity and one explicit horizon."""
    _validate(replay_plan, horizon, admissions)
    return PrivateBacktestDecisionReplaySequence(replay_plan=replay_plan, horizon=horizon, admissions=admissions)
