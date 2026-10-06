# Private backtest investable cash (Phase 26D3B)

`backend/engine/private/backtest_investable_cash.py` closes the missing cash-classification authority with **explicit fixed replay allocations**. It answers only: under an explicitly supplied
fixed replay policy, how much of each positive raw historical cash balance is declared investable? It does not build `RebalanceCurrentState` and does not compose or rebalance.

## Why it is needed, and why a fixed policy

D3A retains raw account/currency cash, and raw cash is not investable cash: Phase 21 `investable_cash` means cash an upstream authority has already classified as investable, while the repository
distinguishes INVESTABLE, EMERGENCY_RESERVE, NEAR_TERM and RESTRICTED_OTHER. `public.cash_buckets` stores `purpose`, `included_in_investable_assets` and `archived_at`, but the persistence schema
lets those lifecycle fields be UPDATED in place (only id, portfolio, owner, account and currency are immutable). There is no revision table, no recorded-time version history and no availability
timeline, so a current `cash_buckets` row cannot tell what a historical replay point would have classified. `created_at` is not revision history. Hence no database lookup of current rows.

## Claim limit: conditional / counterfactual

The allocations are **fixed replay policy** parameters (not market data, not database evidence, not C2E evidence; there is no new C2E input kind). D3B proves: given the historical raw cash state
and this policy, these amounts are designated investable. It does not prove the allocations or any CashBucket classification existed historically, that the user applied this policy, or that current
CashBucket rows describe the past.

## Contract

`build_private_backtest_investable_cash_selection(*, marked_holdings_state, allocations)` returns a frozen `PrivateBacktestInvestableCashSelection` with exactly two fields, the D3A state (kept by
identity, untouched) and the allocations (a tuple of `PrivateBacktestCashAllocation(account_id, currency, investable_amount)`). `investable_cash` is a derived read-only property, never stored.
Exact types everywhere; list, set, generator, subclasses and string or numeric stand-ins are rejected.

- **Coverage: every positive raw balance needs one explicit decision**, identified by (account_id, currency) from `cash_projection.positive_balances`. Missing, duplicate and extra keys are rejected: no first-wins,
  no implicit zero. The classification is not aggregated first (two TRY accounts need two decisions, preserving account-level audit).
- **A zero amount is an explicit decision, not missing.** A raw balance of zero stays in the projection for audit but takes no allocation; an allocation for it is extra evidence.
- **The amount cannot exceed the raw balance** of its (account, currency): exact comparison, no tolerance, no rounding. Amounts are finite and unsigned.
- **Valuation currency rule:** only the explicit valuation currency of the D3A state may carry a positive amount. Any non-valuation currency must be exactly zero: foreign raw cash stays visible,
  unconverted and not investable in this no-FX replay. no FX: no rate lookup, no transaction rate reuse, no base-currency conversion.
- **Canonical order** is (str(account_id), currency.value); the builder canonicalizes caller order (no financial meaning) and keeps each supplied allocation object by identity; direct construction
  requires the canonical order and re-runs the whole validation.
- **investable_cash** is the exact sum of the valuation-currency allocations, computed with integer arithmetic independent of the ambient Decimal context (exact arithmetic: no float, rounding or
  quantization). Zero is valid ("the policy designates none"); with no positive raw cash the allocations must be exactly empty and the total is zero.

## What is deliberately not used

no CashBucket inference: no CashBucket or CashPurpose type, no bucket name, purpose or `included_in_investable_assets`, no account or broker rule. no transaction bucket reconstruction: transaction
`cash_bucket_id` is not read (the cash projection is account/currency authority, not a bucket ledger, and no bucket-transfer accounting is invented). no risk-evidence substitution: CASH_BALANCE risk
evidence carries portfolio, account, currency and balance but no purpose or investability. No current database, repository or PostgREST lookup. No holding, price or valuation change.

D3B does not build RebalanceCurrentState and does not compose or rebalance.

## Next

D3C consumes D3B's classified investable cash together with D3A marked holdings to construct the held-universe Phase 21 RebalanceCurrentState (`docs/PRIVATE_BACKTEST_REBALANCE_CURRENT_STATE.md`), built after
an independent Red Team of D3B. D3C can now carry cash-only as an empty-universe current state. Target candidates are still deferred to Phase 22/D4B explicit cross-universe composition.
