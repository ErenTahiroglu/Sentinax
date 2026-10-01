# Allocation Universe Composition

Phase 22 builds the cross-asset layer of the Private Investment Decision Engine in checkpoints.

| Checkpoint | Scope | Status |
|---|---|---|
| 22A | Explicit strategic-sleeve composition + cross-universe reconciliation (Phase 21 bridge) | implemented (this document) |
| 22B | PIT candidate eligibility / universe provenance | deferred |

Sentinax is a decision-support system. Nothing here is an order, a ledger event or a recommendation.

## The Phase 21 same-universe limitation

Phase 21A/21B require the target, the current state, the band policy and the friction profile to share one canonical instrument
universe (`rebalance target and current state must use the same canonical instrument universe`). That is correct for a rebalance
engine, but a wider pipeline needs candidates that are not yet held, holdings intentionally absent from the new target, and several
asset-class sleeves. Phase 22A is the deterministic bridge. It does not change Phase 21.

## Phase 22A: explicit bridge

Module: `backend/engine/private/allocation_universe_composition.py` (pure, in the static-guard `PURE_MANIFEST`; it imports only the
standard library, `domain.AssetClass` and the Phase 21A types and exact helpers). Entry point:

```python
build_cross_asset_composition_plan(*, current_state, sleeves: tuple[CrossAssetSleeve, ...], authority: CrossUniverseAuthority) -> CrossAssetCompositionPlan
```

### Strategic AssetClass sleeves and within-sleeve weights

`CrossAssetSleeve(asset_class, target_weight, instrument_ids, instrument_weights)`:

- `target_weight` is the explicit strategic portfolio weight of the sleeve: exact `Decimal`, finite, unsigned, strictly `> 0`.
- `instrument_weights` are explicit conditional weights inside the sleeve: each finite, unsigned, strictly `> 0`, `<= 1`, summing to
  **exactly** 1. A zero within-sleeve weight is rejected, so "candidate with zero weight" is never confused with "holding explicitly
  authorized for exit".
- Final target of instrument `i`: `final_weight_i = sleeve.target_weight * instrument_weight_i`, exact, no normalization, no repair.
- Sleeves: unique `AssetClass`, in strictly ascending `AssetClass.value` order (`commodity, equity, etf, fixed_income, fund, fx`),
  validated and never reordered; an instrument may appear in exactly one sleeve; `sum(target_weight) == 1` exactly.
- The `AssetClass` is explicit caller authority. Phase 22A does **not** check that an instrument really belongs to it.

### Explicit authority

`CrossUniverseAuthority(confirmed_zero_current_value_instrument_ids, authorized_exit_instrument_ids)` (tuples of exact UUIDs,
unique, canonical ascending UUID-string order, empty allowed, disjoint, no defaults):

```text
target_only  = target candidates - current instruments   must EQUAL confirmed_zero_current_value_instrument_ids
current_only = current instruments - target candidates   must EQUAL authorized_exit_instrument_ids
```

- A **zero-current confirmation** only authorizes this composition to represent the current marked value as exact `Decimal(0)`.
  Missing from the state is never inferred to be zero; no position history is inferred. Missing or stale confirmations fail closed.
- An **exit authorization** only grants permission for target weight `0`. It is not a sell instruction, not a tax-optimal sale, not a
  lot choice and not a broker order; Phase 21 decides the notional behavior later. Missing or stale authorizations fail closed.
- **No automatic liquidation / no keep by default**: a holding that should stay invested must appear in a sleeve with a strictly
  positive weight; then it needs no exit authorization.

### Canonical union and derived pair

```text
reconciled_ids = canonical ascending UUID-string union(target_ids, current_ids)
target weights  = sleeve.target_weight * instrument_weight   (candidates)   |   Decimal(0)   (authorized exits)
current values  = copied exactly from the source state       |   Decimal(0)   (confirmed target-only)
investable_cash, currency = preserved exactly from the source state
```

The pair is built with the Phase 21A `RebalanceTargetAllocation` / `RebalanceCurrentState` constructors (an additional fail-closed
boundary). **UUID ordering has no investment meaning: it is representation only**, never a preference, ranking or attractiveness.

Hand fixture: `EQUITY 0.60 {A 0.70, B 0.30}`, `FIXED_INCOME 0.40 {C 0.25, D 0.75}`, current `{A, C, E}`, confirmed zero `(B, D)`, exit
`(E)` gives `A = 0.60 x 0.70 = 0.42`, `B = 0.60 x 0.30 = 0.18`, `C = 0.40 x 0.25 = 0.10`, `D = 0.40 x 0.75 = 0.30`, `E = 0` over the union
`A B C D E`.

### Plan authority

`CrossAssetCompositionPlan(current_state, sleeves, authority, rebalance_target, rebalance_state)` retains the source state and the
authority by identity and the sleeves as the validated tuple. On construction it recomputes the single canonical derived pair and
rejects any forged weight, value, universe, cash, currency or alternative representation. Non-stored diagnostics:
`target_candidate_instrument_ids`, `current_only_exit_instrument_ids`, `target_only_zero_confirmed_instrument_ids`,
`reconciled_instrument_ids`, `asset_class_target_weights`. There is no score, rank, recommendation or expected return.

### Phase 21 integration

`plan.rebalance_target` and `plan.rebalance_state` have the same universe and are directly consumable by
`build_cash_first_rebalance_plan` and, with caller-supplied same-universe `RebalanceBandPolicy` / `RebalanceFrictionProfile`, by
`build_band_aware_rebalance_plan`. Phase 22A constructs neither of those and chooses no band or friction rate.

### Exact arithmetic

All sums and products are context-free exact coefficient arithmetic (Phase 21A helpers); no ambient Decimal context, no division, no
quantization, no tolerance, no binary64. The Phase 21A representation ceiling applies unchanged.

## Non-goals

```text
no universe discovery, no eligibility screen, no provider / TEFAS category mapping
no hidden taxonomy: no InstrumentRecord / InstrumentType / symbol / name inference; no UUID-to-type lookup (no crypto route)
no automatic liquidation or retention
no tax, capital gain, lot, cost basis; no FX / valuation / price / quantity inference
no optimizer, expected-return model, score or ranking; no execution or orders
```

## Methodology claim limit

Given explicit strategic sleeve weights, explicit within-sleeve candidate weights, explicit zero-current-value confirmations and
explicit exit authorizations, Phase 22A deterministically constructs a same-universe Phase 21 target / current-state pair. It does
not claim an optimal portfolio, the best assets, a complete investable universe, tax optimality, global diversification or a
recommended allocation.
