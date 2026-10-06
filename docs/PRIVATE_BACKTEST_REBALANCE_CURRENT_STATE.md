# Private backtest rebalance current state (Phase 26D3C)

`backend/engine/private/backtest_rebalance_current_state.py` is only a Phase 21 **input-composition boundary**: it packages D3A's historical marked values and D3B's already-classified investable cash into
the existing closed `RebalanceCurrentState`. It chooses no target and builds no rebalance, trade or composition.

## Claim limit: historical, counterfactual, not claimed as persisted

D3A provides historical marked values (PIT-safe prices and ledger quantities). D3B provides investable cash from a conditional/counterfactual fixed replay policy. The resulting state is therefore a
mixed-provenance input: it is **not claimed** to have been historically persisted, that D3B's cash policy was historically used, or that a historical rebalance decision occurred. It says only: given the
historical marked holdings and this explicit replay cash policy, this is the canonical Phase 21 current-state input.

## Contract

`build_private_backtest_rebalance_current_state(*, investable_cash_selection)` returns a frozen `PrivateBacktestRebalanceCurrentState` with exactly two fields: the exact D3B selection (kept by identity)
and the closed `RebalanceCurrentState`. The authoritative D3A state is exactly `selection.marked_holdings_state`; there is no separate state argument, so cash classification and holdings can never come
from different states. Nothing is duplicated from upstream.

Derivation is pure reuse, with no financial arithmetic (no sum, product, difference, rounding, quantization or float):

- `instrument_ids`: the marked positions' ids in D3A's canonical ascending `str(UUID)` order (not re-sorted, dropped or added).
- `current_values`: each position's D3A `market_value` (never recomputed from quantity and price).
- `investable_cash`: `selection.investable_cash` (D3B stays the sole classification and exact-total authority; raw cash balances and allocations are not read).
- `currency`: the D3A valuation currency. no FX, no base currency, and foreign raw cash never appears.

The closed Phase 21 type validates its own fields; they are not duplicated here.

## Exact held-instrument universe only

The universe is strictly the currently marked open holdings. no target (no `RebalanceTargetAllocation`), no candidate additions, no exit inference (a held instrument is neither keep, sell nor target
zero; an exit needs explicit authority at the later Phase 22 composition boundary) and no zero-current target-only candidates (those need D2 target-candidate authority and a `CrossUniverseAuthority`,
which D3C has neither of). One holding is valid; no diversification is required; zero investable cash with holdings is valid (known classified cash, not missing).

## Cash-only portfolios

A portfolio with no marked holding **fails closed** here, whether its investable cash is zero or positive. That is representational, not an investment-policy verdict: the Phase 21 current-state
universe must be non-empty, and D3C refuses to invent an instrument id, a dummy cash instrument or a candidate. A cash-only portfolio is not invalid; the later cross-universe replay must solve it with
explicit target-candidate authority.

## Decimal representation is preserved

Direct construction re-derives the expected state and requires `as_tuple()` equality for every current value and for the investable cash, so a state that changes only the Decimal representation (for
example `1.0` instead of `1.00`) is rejected: this wrapper is a provenance boundary and keeps the exact upstream representation, in the style of the Phase 22 composition integrity. Instrument ids, the
currency member and the exact `RebalanceCurrentState` type (no subclass) are also required. An independently constructed canonical `RebalanceCurrentState` is accepted; wrapper-object identity is not
required, since the provenance lives in the retained selection.

## Not here

no rebalance (no cash-first or band-aware plan, no trade, no weight drift or trigger), no cross-universe composition, no Game Changer or D2 consumption, no market data or price access, no raw cash access.

## Next

The next boundary (Phase 26D4: historical cross-universe target and state composition from the D2 eligible sleeves, this held-universe state and an explicit `CrossUniverseAuthority`) starts only after an
independent Red Team of D3C. Its critical open case is the cash-only portfolio.
