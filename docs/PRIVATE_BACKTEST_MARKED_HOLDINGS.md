# Private backtest marked holdings and raw cash projection (Phase 26D3A)

`backend/engine/private/backtest_marked_holdings.py` is the first historical portfolio-marking checkpoint: closed historical ledger quantities plus PIT-safe typed C2C2 prices give exact
marked holding values, and the closed **raw** cash projection of the same ledger view is retained. It builds no `RebalanceCurrentState` and classifies no cash.

## Chain

C2B historical ledger view (`input_bundle.portfolio_history.projection_binding.projection`) goes through the closed `build_position_quantity_projection` (reversal-aware open quantities) and the
closed `build_cash_balance_projection`; each open instrument is priced by a C2C2 typed selected observation. No quantity is computed from transactions directly.

## Contract

`build_private_backtest_marked_holdings_state(*, input_bundle, valuation_currency)` returns a frozen `PrivateBacktestMarkedHoldingsState` with exactly five fields: `input_bundle`,
`valuation_currency`, `position_projection`, `cash_projection` and `marked_positions` (a tuple of `PrivateBacktestMarkedPosition`: `instrument_id`, `quantity`, `market_observation` (the C2C2
wrapper) and `market_value`; no account id and nothing duplicated from the observation). Exact types everywhere; one private derivation serves the builder and `__post_init__`.

- **Requires a COMPLETE C2E bundle** with only PORTFOLIO_HISTORY and MARKET_DATA requirements (no candidate, macro, Game Changer, risk or user-view requirement). COMPLETE only means the declared
  surface is represented: D3A independently proves that the market-data set covers exactly the open holdings.
- **Explicit valuation_currency:** a fixed replay parameter, never inferred from the portfolio, an account, a transaction, a price or cash. It is **not claimed** to have been historically
  persisted or used; D3A is a conditional claim ("mark these holdings in this currency"). `Portfolio.base_currency` is never read.
- **Valuation field policy (first layer to choose it):** BIST close, Global close, TEFAS unit price. Global `adj_close`, `previous_close`, `weighted_average`, transaction prices and cost basis are not
  used. TEFAS current metrics are unsupported: their reported price remains diagnostic. A precious-metal reference is unsupported for portfolio marking: it has no portfolio instrument identity.
- **A positive price policy applies:** the price must be an exact finite Decimal strictly greater than zero. This is a D3A valuation rule stricter than the closed resolver (which accepts any finite BIST/Global
  close); C2C2 is not changed.
- **Currency:** the price currency must be exactly the valuation currency (missing or different fails). There is no FX, no conversion and no implicit normalization; cash in another currency stays raw.
- **Exact evaluation date:** a price must have query date equal to the replay evaluation date. no previous-day fallback, no nearest or last-known price, no weekend or holiday roll, no CURRENT_REPORTED.
- **Cross-account aggregation:** open quantities are summed across accounts per instrument with exact, ambient-context-independent integer arithmetic; ordering is ascending `str(instrument_id)`.
  Zero-quantity (closed) positions need no price and a price for one is rejected.
- **One price per open instrument:** exactly one supported snapshot each; missing, duplicates (across any surface), unrelated or closed-instrument prices, current metrics and precious metals are rejected.
  A cash-only portfolio has no open instruments, so `market_data` must be empty.
- **Uses exact arithmetic:** market value is exactly quantity times the selected price: no rounding, no quantization, no decimal-place assumption, independent of the ambient Decimal precision.
- **Identity:** each marked position keeps the very C2E snapshot object; a wrapper over an equal-valued clone is rejected.
- **Frontiers stay separate:** portfolio knowledge is governed by the portfolio recorded cutoff, market knowledge by C2C1/C2C2; knowledge cutoff, recorded cutoff and evaluation date are not collapsed.

## Raw cash is NOT investable cash

The retained `CashBalanceProjection` is raw cash by account and currency. Phase 21 `investable_cash` means cash an upstream authority has already classified as investable; the repository distinguishes
INVESTABLE, EMERGENCY_RESERVE, NEAR_TERM and RESTRICTED_OTHER, and the historical bundle holds no PIT-safe CashBucket classification. So D3A does not sum cash, drop currencies, apply CashBucket
semantics or infer investability from an account or the portfolio currency, exposes no `investable_cash`, and constructs no RebalanceCurrentState (no RebalanceCurrentState; that belongs to D3B or later).

## Not here

no FX, no cost basis (no average cost, FIFO, realized or unrealized gain), no rebalance, no composition, no target allocation, and no Game Changer, candidate, macro or user-view input.

## Next

D3B uses explicit fixed replay cash allocations because the current mutable CashBucket lifecycle does not provide a PIT-safe classification history (`docs/PRIVATE_BACKTEST_INVESTABLE_CASH.md`).
D3B remains counterfactual and does not claim the policy was historically used. D3B (after an independent Red Team) was the prerequisite before Phase 21 can consume this state; D3A itself still builds no RebalanceCurrentState.
