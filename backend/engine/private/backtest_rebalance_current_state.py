"""
backend/engine/private/backtest_rebalance_current_state.py
==========================================================
Historical Phase 21 current-state composition (Phase 26D3C). One narrow packaging step: the D3A marked holdings and the D3B already-classified investable cash become the existing closed
`RebalanceCurrentState`. The only input is a `PrivateBacktestInvestableCashSelection`; the authoritative D3A state is exactly `selection.marked_holdings_state`, so cash classification from one D3A
state can never be paired with holdings from another.

Derivations, all by direct reuse of upstream values (this module performs no arithmetic at all: no sum, product, difference, rounding or quantization):
    instrument_ids   the marked positions' ids, in D3A's canonical ascending str(UUID) order (not re-sorted, dropped or added)
    current_values   each position's D3A `market_value` (never recomputed from quantity and price; the very Decimal objects are used)
    investable_cash  `selection.investable_cash`, the D3B-derived exact total (raw cash balances and allocations are not read here; D3B stays the sole classification authority)
    currency         the D3A valuation currency (no FX, no base currency, no foreign raw cash)
The closed Phase 21 type validates its own fields; nothing is duplicated.

Claim limit: the result is a historical/counterfactual Phase 21 input. The marked values are historical PIT evidence (D3A); the investable cash is D3B's fixed replay policy. It is not claimed to
have been historically persisted, nor that the cash policy was used, nor that a rebalance decision ever happened. The universe is strictly the currently marked OPEN holdings: no target, no candidate
or zero-current target-only addition, no exit inference (a held instrument is neither keep nor sell here), no cross-universe authority or composition, no Game Changer input and no rebalance.
A cash-only portfolio (no marked holding) yields the same closed type with an EMPTY universe (instrument_ids and current_values both empty, no dummy or sentinel instrument): representation only. It does not mean
no investable candidate exists, that no target should exist, that any candidate has a confirmed zero current value, or that cash should stay cash; Phase 22 still needs an explicit zero-current confirmation.

Direct construction re-derives the expected state and requires exact Decimal REPRESENTATION equality (`as_tuple`) for every value, so a numerically equal but differently represented amount is
rejected; an independently constructed canonical `RebalanceCurrentState` is accepted. Non-pure by dependency composition; no I/O, clock, randomness or hashing.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.allocation_rebalance import RebalanceCurrentState
from backend.engine.private.backtest_investable_cash import PrivateBacktestInvestableCashSelection

_ERR_SELECTION = "investable_cash_selection must be an exact PrivateBacktestInvestableCashSelection instance"
_ERR_STATE_TYPE = "current_state must be an exact RebalanceCurrentState instance"
_ERR_IDS = "current_state.instrument_ids must be exactly the marked held instruments in D3A order"
_ERR_VALUES = "current_state.current_values must be exactly the D3A market values, with identical Decimal representation, by position"
_ERR_CASH = "current_state.investable_cash must be exactly the D3B investable cash, with identical Decimal representation"
_ERR_CURRENCY = "current_state.currency must be the D3A valuation currency"


def _expected(investable_cash_selection: object) -> tuple:
    """(instrument_ids, current_values, investable_cash, currency): pure reuse of D3A / D3B values, in D3A order."""
    if type(investable_cash_selection) is not PrivateBacktestInvestableCashSelection:
        raise TypeError(_ERR_SELECTION)
    marked_holdings_state = investable_cash_selection.marked_holdings_state
    marked_positions = marked_holdings_state.marked_positions
    return (
        tuple(position.instrument_id for position in marked_positions),
        tuple(position.market_value for position in marked_positions),
        investable_cash_selection.investable_cash,
        marked_holdings_state.valuation_currency,
    )


@dataclass(frozen=True)
class PrivateBacktestRebalanceCurrentState:
    """The D3B selection (retained by identity, with its D3A state) and the closed Phase 21 current state composed from it."""
    investable_cash_selection: PrivateBacktestInvestableCashSelection
    current_state: RebalanceCurrentState

    def __post_init__(self) -> None:
        instrument_ids, current_values, investable_cash, currency = _expected(self.investable_cash_selection)
        if type(self.current_state) is not RebalanceCurrentState:
            raise TypeError(_ERR_STATE_TYPE)
        if self.current_state.instrument_ids != instrument_ids:
            raise ValueError(_ERR_IDS)
        if len(self.current_state.current_values) != len(current_values) or not all(
            supplied.as_tuple() == expected.as_tuple() for supplied, expected in zip(self.current_state.current_values, current_values)
        ):
            raise ValueError(_ERR_VALUES)
        if self.current_state.investable_cash.as_tuple() != investable_cash.as_tuple():
            raise ValueError(_ERR_CASH)
        if self.current_state.currency is not currency:
            raise ValueError(_ERR_CURRENCY)


def build_private_backtest_rebalance_current_state(
    *,
    investable_cash_selection: PrivateBacktestInvestableCashSelection,
) -> PrivateBacktestRebalanceCurrentState:
    """Compose the held-universe Phase 21 `RebalanceCurrentState` from D3A marked values and D3B investable cash; no target, no rebalance."""
    instrument_ids, current_values, investable_cash, currency = _expected(investable_cash_selection)
    current_state = RebalanceCurrentState(instrument_ids=instrument_ids, current_values=current_values, investable_cash=investable_cash, currency=currency)
    return PrivateBacktestRebalanceCurrentState(investable_cash_selection=investable_cash_selection, current_state=current_state)
