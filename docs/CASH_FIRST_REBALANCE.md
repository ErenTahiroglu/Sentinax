# Cash-First Rebalance

Phase 21 builds the rebalance layer of the Private Investment Decision Engine in two checkpoints.

| Checkpoint | Scope | Status |
|---|---|---|
| 21A | Exact target / current-state reconciliation + frictionless cash-first reference plan | this document |
| 21B | Rebalance bands, no-trade region, partial rebalance, minimal-sales policy, explicit transaction-friction evidence | deferred |

Sentinax is a decision-support system. The plan is a notional reference plan: it is not an order, not a ledger event and it
is never executed.

## Phase 21A

Module: `backend/engine/private/allocation_rebalance.py` (pure Private Engine module, in the static-guard `PURE_MANIFEST`; it
depends only on `domain.Currency` and the reviewed Phase 18B exact-sum helper). Entry point:

```python
build_cash_first_rebalance_plan(*, target: RebalanceTargetAllocation, state: RebalanceCurrentState) -> CashFirstRebalancePlan
```

There are no policy parameters: no band, threshold, fee, tax or friction argument exists.

### Explicit inputs only

- **Explicit target** (`RebalanceTargetAllocation`): instrument ids (unique, canonical ascending UUID string order, validated and
  never silently sorted) and exact `Decimal` weights, `0 <= w <= 1`, unsigned zero, a zero target weight allowed, summing to
  **exactly** `1` (context-free exact coefficient arithmetic; no tolerance, normalization or repair). The target is decided
  upstream; Phase 21A never calls Equal Weight, Inverse Volatility, ERC, HRP, CVaR or the Bayesian posterior.
- **Explicit marked current values** (`RebalanceCurrentState.current_values`): the caller-supplied current market value of each
  instrument, `>= 0`, unsigned zero. The repository has exact quantities and cash balances but no authoritative market-value
  projection, cost-basis, lot-matching or tax-liability engine, so nothing is inferred: no quantity x price, no price
  resolver, no FX conversion.
- **Explicit investable cash** (`investable_cash`): cash that an upstream authority (existing `CashBucket` semantics) has
  **already** classified as investable. The planner never assumes all account cash is investable and never consumes emergency,
  near-term or restricted cash; cash-bucket attribution is not implemented here.
- **Single-currency authority**: one `Currency` for the values and the cash; no multi-currency conversion.
- **Same-universe limitation**: the target and the state must have exactly the same canonical instrument ids. No union, no
  dropping, no inferred zero position, no automatic sell-to-zero of unknown assets and no automatic addition of missing target
  assets (`rebalance target and current state must use the same canonical instrument universe`). Broader universe composition
  is a later phase.

### Exact amount-space arithmetic

```text
W        = sum(current_values) + investable_cash           must be > 0
T_i      = weight_i * W                                     exact; sum(T_i) == W with no rounding residual
delta_i  = T_i - current_value_i                            > 0 buy need ; < 0 sell need ; = 0 no trade
gross_buy_notional  = sum(max(delta_i, 0))
gross_sell_notional = sum(max(-delta_i, 0))
funding identity:  gross_buy_notional == investable_cash + gross_sell_notional      (fail closed if broken)
```

Sums, subtractions and multiplications are context-free exact coefficient arithmetic (`Decimal.as_tuple()` integers aligned to a
common exponent); the ambient Decimal context is never used, nothing is rounded and **no currency quantization (e.g. to 2
decimals) is applied**. Exact alignment is bounded by `_EXACT_MAX_DECIMAL_PLACES = 1000`, a representation / memory resource
ceiling checked before any big-integer expansion; it is not money rounding, financial materiality, a trade minimum or a drift
tolerance, and exceeding it is the static error `rebalance analytics exceeds supported Decimal range`. Public economic zeros are
unsigned `Decimal("0")`.

### Three-stage cash-first sequencing

```text
1. CASH_FUNDED_BUY   for each positive delta in canonical instrument order: piece = min(buy_need, remaining_cash);
                     emit if piece > 0; remaining_cash -= piece        (initial cash is consumed BEFORE any sale proceeds)
2. SELL              one instruction per delta < 0, notional = -delta, canonical order
3. SALE_FUNDED_BUY   residual_buy = buy_need - cash_funded_amount, emitted if > 0, canonical order;
                     sum(residual buys) == gross_sell_notional exactly
```

The canonical UUID order only fixes the bookkeeping order in which the initial cash is staged. It is **not** a ranking: it does not
change any final trade delta, target holding, gross buy or gross sell, and it does not imply that earlier ids are more
attractive investments. No stage emits a zero instruction.

### Minimum necessary sales under the exact-target / frictionless model

Only assets with `delta < 0` are sold, each by exactly `-delta`; no underweight asset is sold and no overweight asset is
oversold, so the total sale equals the minimum sale that reaches the exact target. Special cases: if cash alone reconciles the
target there are no `SELL` and no `SALE_FUNDED_BUY`; with zero cash there is no `CASH_FUNDED_BUY` and sales come first; an
already aligned state with no cash yields no trades (no artificial round trip); a zero target weight sells exactly the current
value; a zero current value with a positive target weight creates a buy need.

Hand fixtures: target `A = B = 0.5`, current `A = 80, B = 10`, cash `10` gives `CASH_FUNDED_BUY B 10`, `SELL A 30`,
`SALE_FUNDED_BUY B 30`; current `A = B = 40`, cash `20` gives only `CASH_FUNDED_BUY A 10` and `CASH_FUNDED_BUY B 10`.

### Post-trade authority

Applying the notional instructions exactly gives `post_trade_values == target_values` and `post_trade_cash == 0` (initial cash plus
sale proceeds minus every buy). The plan retains its `target` and `state` by identity and recomputes the canonical trades on
construction, rejecting forged, missing, extra, mis-staged, mis-ordered, wrong-notional or zero instructions. Derived
(non-stored) diagnostics: `total_wealth`, `target_values`, `trade_deltas`, `gross_buy_notional`, `gross_sell_notional`,
`cash_funded_buy_notional`, `sale_funded_buy_notional`, `post_trade_values`, `post_trade_cash`, and the **diagnostic-only** views
`current_weights` and `weight_drifts` (fresh 50-digit context). Weights and drifts never drive trade notionals and no trigger band
is attached to them.

### Explicit non-goals of 21A

```text
no tax, tax-law inference, capital gain, tax lot, cost basis, FIFO / LIFO / HIFO or Turkey / US tax rate
no commission, spread, slippage or fee model (historical Phase 14 fee/tax evidence is not a future cost model)
no rebalance band, threshold, minimum drift or hidden trigger: the caller explicitly requested exact-target reconciliation
no quantities, prices, share counts, lot-size rounding, limit / market orders or broker execution
no PortfolioTransaction / ledger event, no persistence, no API or frontend
no allocation optimizer call and no decision about the target
```

## Phase 21B (DEFERRED, not implemented)

Phase 21B: rebalance bands / no-trade region / partial rebalance / minimal-sales policy / explicit transaction-friction evidence.

Future transaction and tax friction must be supplied by an authoritative upstream model; Phase 21 will not infer tax law from
historical fee/tax observations.
