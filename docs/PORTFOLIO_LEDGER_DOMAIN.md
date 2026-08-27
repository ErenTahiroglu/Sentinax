# Portfolio Ledger Domain Contract (Phase 12A & Phase 12A.5)

## 1. Authoritative Ledger & Derived Projections
The Private Personal Investment Decision Engine defines an **event-sourced, append-only immutable ledger** as the sole source of truth for portfolio holdings, cost basis, and cash balances.
- **Authoritative Source:** `PortfolioTransaction` events (the only frozen, immutable ledger events).
- **Lifecycle & Reference Entities:** `Portfolio`, `PortfolioAccount`, `CashBucket`, `InvestmentGoal`, and `PlannedContribution` are domain lifecycle entities (not append-only ledger events).
- **Derived Projections:** Current holdings, average costs, accounting/tax lots (`PositionLot`), cash balances, and portfolio valuations are deterministic projections computed from immutable transaction history.
- **No Direct Mutation:** Holding quantities and cash balances are never directly edited or overwritten.

---

## 2. Economic Time vs. System Knowledge Time
Every transaction event explicitly decouples the economic timeline from the ingestion timeline:
- **Economic Event Time (`effective_date` / `executed_at`):** The date and optional timezone-aware timestamp when the trade or cash movement actually occurred in the financial market.
- **System Knowledge Time (`recorded_at`):** The timezone-aware timestamp when Sentinax learned and ingested the event.
- **Late Imports:** A trade executed on August 1st and imported on August 20th retains `effective_date = 2026-08-01` and `recorded_at = 2026-08-20T...`. Point-in-time backtests and audit views rely on this strict temporal separation.

---

## 3. Portfolio Aggregate Binding & Account Boundaries
- **`Portfolio`:** The root aggregate boundary (`PortfolioMode.MY_PORTFOLIO` or `PortfolioMode.SANDBOX`).
- **Ledger Binding:** `PortfolioLedger(portfolio: Portfolio)` binds strictly to the root `Portfolio` instance; `mode` and `portfolio_id` are derived directly from the aggregate, eliminating mode split-brain.
- **`PortfolioAccount`:** Custody, brokerage, or manual ledger account (e.g. Midas, Garanti, Interactive Brokers).
- **Multi-Account Coexistence:** The same canonical instrument (`InstrumentRecord.id`) can exist across multiple accounts within a single portfolio with separate lot histories, custody fees, and cash balances.
- **Transaction Scope:** Every transaction must specify both `portfolio_id` and `account_id`.

---

## 4. Sandbox Mode Isolation & Provenance
- **Strict Bounded Context:** `MY_PORTFOLIO` (real user assets) and `SANDBOX` (hypothetical scenarios) are strictly isolated.
- **No Cross-Contamination:** Appending a real-owned transaction into a sandbox ledger or a sandbox-owned transaction into a real ledger is strictly rejected (`INVALID`).
- **Cloning Provenance:** A sandbox portfolio may reference origin provenance (`source_portfolio_id`, `source_snapshot_time`). If `source_snapshot_time` is specified, `source_portfolio_id` is required. Self-cloning (`source_portfolio_id == self.id`) is rejected.

---

## 5. Canonical Transaction Types & Mutually Exclusive Field Families
Every `PortfolioTransaction` carries exactly ONE unambiguous economic meaning. Mutually exclusive field families enforce fail-closed validation:
- **`BUY` / `SELL`:** Requires security fields (`instrument_id`, `quantity > 0`, `unit_price > 0`, `trade_currency`). Rejects `cash_amount`, `cash_currency`, FX conversion legs, and `reverses_transaction_id`. `cash_bucket_id` allowed as funding context.
- **`CASH_DEPOSIT` / `CASH_WITHDRAWAL`:** Requires `cash_amount > 0`, `cash_currency`. Rejects `instrument_id`, security trade fields, FX conversion legs, and `reverses_transaction_id`. `cash_bucket_id` allowed.
- **`DIVIDEND` / `INTEREST` / `FEE` / `TAX_WITHHOLDING`:** Requires `cash_amount > 0`, `cash_currency`. Optional `instrument_id` allowed. Rejects `quantity`, `unit_price`, `trade_currency`, FX conversion legs, and `reverses_transaction_id`. `cash_bucket_id` allowed.
- **`FX_CONVERSION`:** Requires `from_currency`, `from_amount > 0`, `to_currency`, `to_amount > 0` (`from_currency != to_currency`). Rejects `instrument_id`, security trade fields, `cash_amount`, `cash_currency`, `cash_bucket_id` (Phase 12A MVP), and `reverses_transaction_id`.
- **`REVERSAL`:** Strictly reference-only. Requires `reverses_transaction_id` (UUID != self.id). All independent economic fields (`instrument_id`, `quantity`, `unit_price`, `trade_currency`, `cash_amount`, `cash_currency`, `cash_bucket_id`, `from_currency`, `from_amount`, `to_currency`, `to_amount`) MUST be `None`.

---

## 6. Reversal and Correction Contract
- **No In-Place Edits:** Historical records are never updated or deleted.
- **Reference-Only Semantics:** A `REVERSAL` has no separate economic amount; its economics derive entirely from the referenced original event.
- **Invariants:**
  - Self-reversal is forbidden.
  - Cross-portfolio and cross-account reversals are rejected.
  - Reversal of a reversal is rejected.
  - Double reversal is rejected (a transaction may be reversed at most once).

---

## 7. Cash Buckets & Liquidity Isolation
- **`CashBucket`:** Segregates personal cash into distinct liquidity purposes (`INVESTABLE`, `EMERGENCY_RESERVE`, `NEAR_TERM`, `RESTRICTED_OTHER`).
- **Strict Bool Typing:** `included_in_investable_assets` must be an explicit `bool` (or `None` for purpose-driven default; non-bool values like `1`, `0`, `"true"` are rejected).
- **Default Inclusion Rule:**
  - `INVESTABLE`: Default `included_in_investable_assets = True`.
  - `EMERGENCY_RESERVE`, `NEAR_TERM`, `RESTRICTED_OTHER`: Default `included_in_investable_assets = False`.
- **Asset Protection:** Protects emergency funds and near-term expenditure from being allocated into long-term risk investments.

---

## 8. Investment Goals
- **`InvestmentGoal`:** Represents financial targets with `target_amount` (strict Decimal > 0), `target_currency`, and arbitrary `target_date` (not restricted to fixed horizon buckets).
- **Priorities & Status:** `GoalPriority` (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`) and `GoalStatus` (`ACTIVE`, `PAUSED`, `COMPLETED`, `CANCELLED`).

---

## 9. Planned Contributions
- **`PlannedContribution`:** Forward-looking cash inflows tied to goals or cash buckets.
- **No Balance Authority:** A planned contribution (even with status `RECEIVED`) is **NOT portfolio cash**. Actual portfolio cash changes exclusively via `CASH_DEPOSIT` ledger events.

---

## 10. Position & Lot Projection Boundary
- **`PositionLot`:** A pure projection model representing open/partially closed acquisition tax lots.
- **Finite Decimal Invariants:** `original_quantity` (> 0), `quantity_open` (>= 0 and <= original_quantity), and `native_unit_cost` (>= 0) must be finite exact `Decimal` instances. `NaN`, `Infinity`, `float`, `int`, `str`, and `bool` are rejected.
- **Derivation Only:** Lots are computed dynamically from transaction events; they are never persisted as primary authority.

---

## 11. Multi-Currency Rules
- **No Implicit Conversion:** Portfolio `base_currency` defines user reporting preference, but individual transactions strictly retain their native `trade_currency`, `cash_currency`, and FX conversion currencies.
- **Exact Decimal Precision:** Floating-point representations, strings, integers (for monetary amounts), `NaN`, and `Infinity` are rejected.

---

## 12. Deferred Scope (Phase 12B, 12C & Phase 14)
- **Phase 12B:** Supabase SQL persistence, repository adapters, PIT ledger hydration.
- **Phase 12C:** Portfolio state projections, lot matching engines (FIFO/LIFO/Average Cost), cash balance calculations.
- **Phase 14:** Turkish and international tax rules, withholding calculations, and fee optimization.
