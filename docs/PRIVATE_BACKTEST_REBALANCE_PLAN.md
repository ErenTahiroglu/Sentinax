# Private backtest band-aware rebalance plan replay (Phase 26D5A)

`backend/engine/private/backtest_rebalance_plan.py` replays the CLOSED Phase 21B `build_band_aware_rebalance_plan` over the canonical D4B same-universe target/state pair, under explicit
caller-supplied `RebalanceBandPolicy` and `RebalanceFrictionProfile`. It is the sole rebalance authority; D5A invents no rebalance algorithm. The result is decision support: notional reference trades, not orders.

## Contract

`replay_private_backtest_band_aware_rebalance(*, cross_universe_composition, policy, friction)` returns a frozen `PrivateBacktestBandAwareRebalanceReplay` with exactly two fields, the D4B composition (kept
by identity) and the closed `BandAwareRebalancePlan`. Target, state, policy and friction are not stored again: the plan retains them by identity. Exact types only.

- **D4B target/state authority:** the target is `composition_plan.rebalance_target` and the state is the reconciled `composition_plan.rebalance_state`, never D3C's pre-composition state, because Phase 22 may
  have inserted confirmed zero-current target-only candidates, retained holdings and given authorized exits a zero target weight. Phase 21B must see the reconciled same-universe state.
- **Phase 21B is the policy replay layer; Phase 21A remains the exact-target reference.** There is no 21A-versus-21B selector and production does not call 21A.
- **zero-band equivalence:** with zero trigger and zero destination drifts for every instrument, the 21B plan reproduces the 21A exact-target trade sequence (ids, stages and exact Decimal representations);
  the tests prove it for matching, target-only, current-only, simultaneous and cash-only cases, using 21A in tests only. Friction rates do not change that result, and a nonzero destination may differ by policy design.

## Fixed, counterfactual parameters (no historical provenance claim)

Nothing in the repository proves which band policy or friction profile was active at a past replay point, so both are explicit fixed replay parameters. D5A says only: had these parameters been applied to this
historical target and current state, this is the canonical Phase 21B plan. It does not claim they were configured, approved or used, that the friction rates are real fees, or that the plan was acted upon.

- no parameter discovery: no default, optimization, calibration, historical-return tuning or fee-table lookup; the caller supplies the drifts and rates.
- no historical fee inference: friction is not derived from commissions, tax evidence, broker records, spreads, slippage, transaction prices or ledger fees. No recorded_at, id or revision is required of a policy.
- Friction is a closed Phase 21B planning input (tie-breaking and diagnostics) and is not settled: nothing is deducted from cash, wealth, notionals or current values.
- **Universe:** the policy and friction instrument ids must equal the reconciled universe exactly; D5A repairs nothing (no sorting, insertion or omission) and the closed builder rejects a mismatch.

## Behavior

- **no-trade region:** if no asset breaches its trigger band the plan is not triggered and has no trades; existing investable cash alone is not mandatory deployment and stays unspent.
- **Triggered plans** are delegated entirely to Phase 21B (D5A interprets no mandatory sale or buy, minimum sale or friction ranking).
- **Provenance semantics stay Phase 22:** a confirmed target-only candidate has current value zero and a positive target and is treated normally by the bands; an authorized exit has target zero but is
  not forced liquidation: a SELL appears only if the band policy produces it, since the authority permitted target-zero composition and issued no order.
- **cash-only:** a reconciled cash-only state (all current values zero plus investable cash) is an ordinary Phase 21B state; depending on the explicit bands the plan may trigger or stay in the no-trade region.
  zero-wealth: with zero investable cash it fails through the existing "total wealth must be strictly positive" authority; no funding is invented and no empty successful plan is produced.

## Validation

Direct construction requires the exact D4B type, an exact plan, the very D4B target and state objects (an equal-valued clone fails) and recomputes the canonical Phase 21B plan from the plan's own policy and
friction. A separately built canonical plan over the same D4B target/state with its own policy and friction objects is accepted; a forged or foreign plan is rejected. The public builder returns a plan whose
`policy` and `friction` are the caller's objects.

## Not here

no Game Changer consumption inside D5A itself (admission constraints on new capital must not silently change weights, bands, friction or the Phase 21B arithmetic).

D5B evaluates the completed canonical plan without mutating it (`docs/PRIVATE_BACKTEST_GAME_CHANGER_PLAN_ADMISSION.md`).
Specifically, both CASH_FUNDED_BUY and SALE_FUNDED_BUY are treated as new-capital deployment for instrument-scoped Phase 23 admission, and a SELL is not.

D5A has no Game Changer input, no target-weight or sleeve change, no candidate discovery, no market data or valuation work, no raw cash access, and no execution: no order, broker call, settlement, ledger transaction,
notional-to-quantity translation or persistence.
