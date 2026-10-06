# Private backtest cross-universe composition (Phase 26D4B)

`backend/engine/private/backtest_cross_universe_composition.py` is the historical cross-universe composition boundary. It combines the D2 candidate-eligible explicit sleeves, the D3C Phase 21 current
state and an explicit `CrossUniverseAuthority` through the CLOSED Phase 22 `build_cross_asset_composition_plan`, and returns the closed `CrossAssetCompositionPlan`: a canonical same-universe Phase 21
target/state pair. **No rebalance occurs and no trade exists here.**

## Claim limit: counterfactual

The D2 sleeves are explicit replay policy parameters, D3B's investable cash is an explicit replay policy and the authority supplied here is also an explicit replay authority. D4B does not prove that the
sleeves were historically used, the cash policy was used, the zero-current confirmations were persisted, the exit authorizations were issued, or that any rebalance or trade occurred. It proves only: given
these explicit replay policies and authorities and the historical evidence, this is the canonical Phase 22 composition.

## Contract

`build_private_backtest_cross_universe_composition(*, candidate_eligibility, rebalance_current_state, authority)` returns a frozen `PrivateBacktestCrossUniverseComposition` with exactly three fields: the D2
replay (kept by identity), the D3C state (kept by identity) and the closed plan. The authority is not stored a second time: it is retained by identity inside `composition_plan.authority`. Exact types only.

- **D2 is the only target policy:** `candidate_eligibility.sleeves` (not rebuilt, reweighted or reordered); D2 provenance (sleeve to eligibility binding to candidate-universe evidence) is preserved.
- **D3C is the only current state:** `rebalance_current_state.current_state` (current values, investable cash and currency are not reconstructed); D3A marking and D3B cash provenance stay reachable.
- **Closed Phase 22 delegation:** target-only and current-only set differences, union ordering, zero-value insertion and exit-zero semantics are not duplicated. The plan's `current_state` is the D3C state
  object, its `sleeves` are the exact D2 sleeve objects in D2 order, and its `authority` is the supplied object.

## Shared replay anchors

D2 and D3C use different C2E manifests on purpose (portfolio history plus candidate universes versus portfolio history plus market data), so separate C2E manifests are expected: the bundles are neither
required to be the same object nor compared. They must share:

- the very **analysis context object** (object identity, not value equality), so candidate eligibility from one replay can never be combined with a valuation from another (an equal-valued clone is rejected);
- the very **portfolio projection binding object** with the same `owner_id`; an equal-valued but separately constructed binding is rejected, and owner mismatch is rejected even over the same binding.

Separate coverage wrappers (`PrivateBacktestPortfolioHistoryCoverage` objects with different `observed_at`) over that binding are accepted: coverage wrapper identity is not required.

## CrossUniverseAuthority is explicit and never derived

The caller supplies both tuples; D4B never computes them from a target-current set difference. A target-only candidate with no current holding is not thereby known to have zero current value, and a current-only
holding absent from the target is not thereby authorized to be sold. The closed builder checks the explicit authority against the real difference, so missing, extra, stale or mis-ordered tuples fail.

- **Target-only zero-current confirmation:** `confirmed_zero_current_value_instrument_ids` only authorizes the representation "current value = 0" for explicit D2 target candidates; it discovers nothing.
- **Current-only exit authorization:** `authorized_exit_instrument_ids` is cross-universe target-composition authority (the later target may name that holding at zero). An exit authorization is not a trade:
  it places no sell and creates no `RebalanceTradeInstruction`; D4B produces no trade.
- All closed relationships are supported: equal universes (empty authority), target-only, current-only, both simultaneously, and a retained holding (no exit authorization).

## cash-only portfolios

The D3C current state may be empty. Then every target candidate is target-only, so the authority must confirm exactly all target candidates (in canonical UUID-string order) with no exit authorization; the closed
Phase 22 builder enforces it and D4B does not special-case the zero insertion. The result has a non-empty target and a state whose current values are exact zeros, with the D3C investable cash and currency
preserved. Being cash-only never by itself implies a zero-current confirmation.

## zero-wealth

Composition is not funding feasibility. A cash-only portfolio with zero investable cash still composes; the later Phase 21 rebalance builder is responsible for rejecting non-positive total wealth.

## Not here

no candidate discovery, ranking, replacement or weight change; no market data, price, quantity or valuation work; no raw cash, allocation or CashBucket access; no Game Changer composition (D1 stays independent);
no rebalance (no cash-first or band-aware plan, band, friction or trade).

## Next

D5A consumes the canonical D4B target/state pair through closed Phase 21B with explicit fixed replay band/friction parameters (`docs/PRIVATE_BACKTEST_REBALANCE_PLAN.md`); those parameters are explicit counterfactual inputs, not historical evidence.
After an independent Red Team of D4B the next policy boundary had to be decided: whether and how D1 gates constrain deployment, whether band and friction parameters are fixed counterfactual parameters, and whether
the next decision slice is a frictionless Phase 21A or a Phase 21B replay.
