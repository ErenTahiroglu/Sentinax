"""
backend/engine/private/backtest_game_changer_replay.py
======================================================
Deterministic historical Game Changer decision replay (Phase 26D1), the first Phase 26D decision slice. It consumes exactly one COMPLETE Phase 26C2E input bundle whose
declared contract is one PORTFOLIO_HISTORY slot plus one or more GAME_CHANGER slots, and replays the CLOSED Phase 23C2 gate once per historical revision family, in the
bundle's canonical order, retaining each family resolution by object identity. The only economic builder called is `build_game_changer_decision_gate`; there is no second
policy, no severity ordering, no latest-revision search and no aggregation (multiple gates stay multiple explicit gates).

Admission: the bundle must be COMPLETE with no missing requirement (replay admission, not a second completeness calculation), must declare nothing besides portfolio
history and Game Changer slots (a candidate, macro, risk, market-data or user-view slot is rejected because D1 would ignore its evidence and blur the decision contract)
and must hold at least one Game Changer family: a replay with none would present an empty tuple as if no event existed, which the manifest never claims. Portfolio history
is the replay anchor and is retained only through the bundle; D1 reads no transaction, derives no holdings and never filters events by what is held (the gate also governs
new capital).

An INCOMPLETE_COVERAGE family is a real historical state and is passed unchanged to the closed gate (review required; instrument scope paused pending evidence). A
SYSTEMIC event stays NOT_APPLICABLE_SYSTEMIC and is never widened to the portfolio. QUARANTINED keeps its Phase 23 meaning: pause NEW capital to the exact affected
instruments plus review. There is no sale, exit, target weight, rebalance, order, execution or portfolio mutation, and no market data is consumed.

One private validation path is shared by the builder and `__post_init__`; a valid independently constructed canonical gate is accepted when its resolution is the
bundle binding's resolution (identity, position by position). Non-pure by composition; no I/O, clock, randomness, hashing or persistence.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.backtest_input_completeness import (
    PrivateBacktestInputBundle,
    PrivateBacktestInputCompletenessStatus,
    PrivateBacktestInputKind,
)
from backend.engine.private.game_changer_gate import GameChangerDecisionGate, build_game_changer_decision_gate

_ERR_BUNDLE = "input_bundle must be an exact PrivateBacktestInputBundle instance"
_ERR_INCOMPLETE = "input_bundle must be COMPLETE with no missing requirement"
_ERR_SURFACE = "D1 manifest must declare only PORTFOLIO_HISTORY and GAME_CHANGER requirements"
_ERR_NO_FAMILY = "D1 requires at least one Game Changer revision family"
_ERR_GATES_TYPE = "game_changer_gates must be an exact tuple of exact GameChangerDecisionGate instances"
_ERR_GATES_MATCH = "game_changer_gates must hold exactly one gate per bundle Game Changer binding, in order, over that binding's very resolution"

_D1_KINDS = frozenset({PrivateBacktestInputKind.PORTFOLIO_HISTORY, PrivateBacktestInputKind.GAME_CHANGER})


def _check_bundle(input_bundle: object) -> None:
    if type(input_bundle) is not PrivateBacktestInputBundle:
        raise TypeError(_ERR_BUNDLE)
    if input_bundle.status is not PrivateBacktestInputCompletenessStatus.COMPLETE or input_bundle.missing_requirements != ():
        raise ValueError(_ERR_INCOMPLETE)
    if not {requirement.kind for requirement in input_bundle.requirements} <= _D1_KINDS:
        raise ValueError(_ERR_SURFACE)
    if len(input_bundle.game_changers) < 1:
        raise ValueError(_ERR_NO_FAMILY)


def _validate(input_bundle: object, game_changer_gates: object) -> None:
    """The single D1 validation path: admission of the bundle, then the exact gate composition over it."""
    _check_bundle(input_bundle)
    if type(game_changer_gates) is not tuple or not all(type(gate) is GameChangerDecisionGate for gate in game_changer_gates):
        raise TypeError(_ERR_GATES_TYPE)
    bindings = input_bundle.game_changers
    if len(game_changer_gates) != len(bindings) or not all(gate.resolution is binding.resolution for gate, binding in zip(game_changer_gates, bindings)):
        raise ValueError(_ERR_GATES_MATCH)


@dataclass(frozen=True)
class PrivateBacktestGameChangerDecisionReplay:
    """The COMPLETE input bundle and the closed Phase 23C2 gate of each of its Game Changer families, in the bundle's canonical order."""
    input_bundle: PrivateBacktestInputBundle
    game_changer_gates: tuple[GameChangerDecisionGate, ...]

    def __post_init__(self) -> None:
        _validate(self.input_bundle, self.game_changer_gates)


def replay_private_backtest_game_changer_decision(*, input_bundle: PrivateBacktestInputBundle) -> PrivateBacktestGameChangerDecisionReplay:
    """Replay the closed Game Changer gate over every historical family of one COMPLETE D1-shaped bundle."""
    _check_bundle(input_bundle)
    gates = tuple(build_game_changer_decision_gate(resolution=binding.resolution) for binding in input_bundle.game_changers)
    return PrivateBacktestGameChangerDecisionReplay(input_bundle=input_bundle, game_changer_gates=gates)
