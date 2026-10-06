"""
backend/engine/private/backtest_game_changer_plan_admission.py
==============================================================
Historical Game Changer new-capital admission over a COMPLETED canonical rebalance plan (Phase 26D5B). Question: does any instrument-scoped Phase 23 gate prohibit the new capital that is actually
present in this plan? The plan is never changed: no weight, band, friction, trade or funding is touched, no replacement plan exists, and nothing is recomputed (neither a Game Changer gate nor a
Phase 21 plan).

New capital at a completed Phase 21B plan means the two explicit BUY stages, CASH_FUNDED_BUY and SALE_FUNDED_BUY: both increase an instrument's exposure, and treating only external cash as new
capital would let a quarantined instrument be funded by selling another one. SELL is not new capital to the sold instrument, so the new-capital rule never blocks it (that does not make a sale
recommended: it exists only because the closed Phase 21B policy produced it). A gate blocks its exact affected instruments iff its instrument gate is PAUSED_PENDING_EVIDENCE or QUARANTINED. OPEN is
not approval and imposes no pause; SYSTEMIC (NOT_APPLICABLE_SYSTEMIC) is never widened to anything; review state alone never blocks (the closed Phase 23 contract separates review from the capital
gate), and materiality, urgency, thesis and reasons are never read. Any positive BUY to an affected instrument is blocked regardless of notional; any blocking family is sufficient with no precedence
or severity aggregation, and a trade hit by several gates is listed once.

One blocked BUY blocks the WHOLE plan (BLOCKED_NEW_CAPITAL): the Phase 21B plan is one coupled solution whose funding identity, destination feasibility, minimum sale and friction tie resolution break if
single instructions are removed, so there is no allowed, filtered or executable subset and no partial execution. A quarantined target with no actual BUY, a no-trade plan, a SELL-only instrument and an
affected instrument absent from the plan do not block. NOT_BLOCKED means only that no BUY in this exact plan is prohibited by the supplied historical gates: it is not approval, safety, suitability,
a recommendation, execution authorization or a statement that no human review is needed (the D1 gates stay reachable for review).

Anchors: D1 and D5A must share the very analysis context object and the very portfolio projection binding object, with the same owner (D4B's own proof is not reopened; separate coverage wrappers and
separate C2E manifests are expected). Outputs (`admission_state`, `blocked_buy_trades`, `blocked_instrument_ids`, `blocking_gates`) are derived read-only views of the retained D1 and D5A objects, so nothing
stored can be forged. Counterfactual like its inputs; no execution, ordering, settlement or persistence. Non-pure by dependency composition; no I/O, clock, randomness or hashing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from backend.engine.private.allocation_rebalance import RebalanceTradeInstruction, RebalanceTradeStage
from backend.engine.private.backtest_game_changer_replay import PrivateBacktestGameChangerDecisionReplay
from backend.engine.private.backtest_rebalance_plan import PrivateBacktestBandAwareRebalanceReplay
from backend.engine.private.game_changer_gate import GameChangerDecisionGate, GameChangerInstrumentNewCapitalGate

_ERR_D1 = "game_changer_replay must be an exact PrivateBacktestGameChangerDecisionReplay instance"
_ERR_D5A = "rebalance_replay must be an exact PrivateBacktestBandAwareRebalanceReplay instance"
_ERR_CONTEXT = "D1 and D5A must share the very analysis context object"
_ERR_HISTORY = "D1 and D5A must both carry verified portfolio history"
_ERR_BINDING = "D1 and D5A must share the very portfolio projection binding object"
_ERR_OWNER = "D1 and D5A portfolio history coverage must have the same owner"

# The two explicit new-capital stages; deliberately not "anything that is not a SELL".
_BUY_STAGES = frozenset({RebalanceTradeStage.CASH_FUNDED_BUY, RebalanceTradeStage.SALE_FUNDED_BUY})
_BLOCKING_GATES = frozenset({GameChangerInstrumentNewCapitalGate.PAUSED_PENDING_EVIDENCE, GameChangerInstrumentNewCapitalGate.QUARANTINED})


class PrivateBacktestNewCapitalAdmissionState(Enum):
    """NOT_BLOCKED is not approval: it only says no BUY of this exact plan is prohibited by the supplied gates."""
    NOT_BLOCKED = "not_blocked"
    BLOCKED_NEW_CAPITAL = "blocked_new_capital"


def _check(game_changer_replay: object, rebalance_replay: object) -> None:
    if type(game_changer_replay) is not PrivateBacktestGameChangerDecisionReplay:
        raise TypeError(_ERR_D1)
    if type(rebalance_replay) is not PrivateBacktestBandAwareRebalanceReplay:
        raise TypeError(_ERR_D5A)
    d1_bundle = game_changer_replay.input_bundle
    d5_bundle = rebalance_replay.cross_universe_composition.candidate_eligibility.input_bundle
    if d1_bundle.analysis_context is not d5_bundle.analysis_context:
        raise ValueError(_ERR_CONTEXT)
    d1_history, d5_history = d1_bundle.portfolio_history, d5_bundle.portfolio_history
    if d1_history is None or d5_history is None:
        raise ValueError(_ERR_HISTORY)
    if d1_history.projection_binding is not d5_history.projection_binding:
        raise ValueError(_ERR_BINDING)
    if d1_history.owner_id != d5_history.owner_id:
        raise ValueError(_ERR_OWNER)


def _hits(gate: GameChangerDecisionGate, trade: RebalanceTradeInstruction) -> bool:
    return gate.instrument_new_capital_gate in _BLOCKING_GATES and trade.stage in _BUY_STAGES and trade.instrument_id in gate.affected_instrument_ids


@dataclass(frozen=True)
class PrivateBacktestGameChangerPlanAdmission:
    """The D1 gates and the completed D5A plan (both retained by identity); every admission output is a derived read-only view."""
    game_changer_replay: PrivateBacktestGameChangerDecisionReplay
    rebalance_replay: PrivateBacktestBandAwareRebalanceReplay

    def __post_init__(self) -> None:
        _check(self.game_changer_replay, self.rebalance_replay)

    @property
    def blocked_buy_trades(self) -> tuple[RebalanceTradeInstruction, ...]:
        """The exact plan BUY trade objects, in plan order, that a blocking gate prohibits (each once)."""
        gates = self.game_changer_replay.game_changer_gates
        return tuple(trade for trade in self.rebalance_replay.rebalance_plan.trades if any(_hits(gate, trade) for gate in gates))

    @property
    def blocked_instrument_ids(self) -> tuple[UUID, ...]:
        """The instruments of the blocked BUYs in first-occurrence plan order, each once (no independent sorting)."""
        ids: list = []
        for trade in self.blocked_buy_trades:
            if trade.instrument_id not in ids:
                ids.append(trade.instrument_id)
        return tuple(ids)

    @property
    def blocking_gates(self) -> tuple[GameChangerDecisionGate, ...]:
        """The exact D1 gates, in D1 order, that block at least one BUY of this plan; no severity aggregation."""
        trades = self.rebalance_replay.rebalance_plan.trades
        return tuple(gate for gate in self.game_changer_replay.game_changer_gates if any(_hits(gate, trade) for trade in trades))

    @property
    def admission_state(self) -> PrivateBacktestNewCapitalAdmissionState:
        if self.blocked_buy_trades:
            return PrivateBacktestNewCapitalAdmissionState.BLOCKED_NEW_CAPITAL
        return PrivateBacktestNewCapitalAdmissionState.NOT_BLOCKED


def build_private_backtest_game_changer_plan_admission(
    *,
    game_changer_replay: PrivateBacktestGameChangerDecisionReplay,
    rebalance_replay: PrivateBacktestBandAwareRebalanceReplay,
) -> PrivateBacktestGameChangerPlanAdmission:
    """Wrap the D1 gates and the completed D5A plan after proving their shared replay anchors; nothing is recomputed."""
    _check(game_changer_replay, rebalance_replay)
    return PrivateBacktestGameChangerPlanAdmission(game_changer_replay=game_changer_replay, rebalance_replay=rebalance_replay)
