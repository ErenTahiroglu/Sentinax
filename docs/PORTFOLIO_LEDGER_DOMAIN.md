# Portfolio Ledger Domain Contract (Phase 12A, Phase 12A.5 & Phase 12A.6)

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

## 5b. External Idempotency Identity & Deduplication Contract (Phase 12A.6, Phase 12B.2C.1)
- **All-or-None Pair:** `external_source` and `external_reference` must either BOTH be `None` (manual/internal transaction) or BOTH be non-empty strings.
- **Fail-Closed on Malformed Input:** Partial presence, empty strings, space-only strings, and non-string types are strictly rejected with `ValueError` or `TypeError`. They are NEVER silently downgraded to manual transactions.
- **Canonical Cross-Language Normalization Contract:**
  1. `external_source`:
     - Strips ASCII `U+0020` spaces from boundaries ONLY (`strip(" ")` / `btrim(s, ' ')`).
     - Case-normalizes ASCII lowercase `a-z` -> `A-Z` via explicit translation table (locale-independent).
     - Preserves every other character (including tabs, newlines, and non-ASCII Unicode characters).
  2. `external_reference`:
     - Strips ASCII `U+0020` spaces from boundaries ONLY (`strip(" ")` / `btrim(s, ' ')`).
     - Case-sensitive (preserves exact case).
- **Manual Event Invariant:** Manual transactions (where `external_source` and `external_reference` are `None`) are **NOT economically auto-deduplicated**. Two identical manual economic events with different UUIDs are both appended.
- **Idempotency Key:** For external events, `(portfolio_id, account_id, normalize_external_source(external_source), normalize_external_reference(external_reference))` uniquely identifies the ingestion event:
  - Replay of identical economics returns `IDEMPOTENT_DUPLICATE` (pointing to the original transaction ID).
  - Same key with conflicting economics returns `CONFLICT`.

---

## 6. Reversal and Correction Contract
- **No In-Place Edits:** Historical records are never updated or deleted.
- **Reference-Only Semantics:** A `REVERSAL` has no separate economic amount; its economics derive entirely from the referenced original event.
- **Invariants:**
  - Self-reversal is forbidden.
  - Cross-portfolio and cross-account reversals are rejected.
  - Reversal of a reversal is rejected.
  - Double reversal is rejected (a transaction may be reversed at most once).
- **Idempotency & Reversal Interaction:** Replay of the same external reversal event resolves as `IDEMPOTENT_DUPLICATE`, whereas a distinct second reversal of an already-reversed transaction is rejected as `INVALID`.

---

## 7. Cash Buckets & Liquidity Isolation
- **`CashBucket`:** Segregates personal cash into distinct liquidity purposes (`INVESTABLE`, `EMERGENCY_RESERVE`, `NEAR_TERM`, `RESTRICTED_OTHER`).
- **Strict Bool Typing:** `included_in_investable_assets` must be an explicit `bool` (or `None` for purpose-driven default; non-bool values like `1`, `0`, `"true"` are rejected).
- **Default Inclusion Rule:**
  - `INVESTABLE`: Default `included_in_investable_assets = True`.
  - `EMERGENCY_RESERVE`, `NEAR_TERM`, `RESTRICTED_OTHER`: Default `included_in_investable_assets = False`.
- **Asset Protection:** Protects emergency funds and near-term expenditure from being allocated into long-term risk investments.
- **Explicit Reference Consistency (Phase 12A.6):**
  - `cash_bucket_id` is an explicit reference. If specified, the exact `CashBucket` object must be supplied to the validator, matching ID, `portfolio_id`, and `account_id` (or portfolio-wide with `account_id=None`).
  - No implicit or default bucket is selected by transaction validation.
  - Supplying an unrelated `CashBucket` when `cash_bucket_id` is `None` fails closed.
- **Currency Consistency:**
  - For cash-flow events (`CASH_DEPOSIT`, `CASH_WITHDRAWAL`, `DIVIDEND`, `INTEREST`, `FEE`, `TAX_WITHHOLDING`), `cash_bucket.currency` must match `transaction.cash_currency`.
  - For `BUY` and `SELL` funding buckets, `cash_bucket.currency` must match `transaction.trade_currency`.

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

## 10b. Point-in-Time Ledger View Foundation (Phase 12C.1)
- **System-Knowledge Point-in-Time Cutoff:**
  - System PIT cutoff uses `recorded_at` physical UTC instants only (`as_of_recorded_at`).
  - `effective_date` and `executed_at` are economic-time fields and do NOT gate system knowledge.
  - Events recorded strictly after `as_of_recorded_at` are completely excluded from the system knowledge snapshot.
- **PIT Reversal Semantics:**
  - A reversal recorded in the future does NOT retroactively affect an earlier system snapshot.
  - Prior to a reversal's `recorded_at`, the targeted base transaction remains fully active.
  - On or after a reversal's `recorded_at`, the targeted base transaction transitions to `is_reversed = True` with `reversal_transaction_id` populated, and is excluded from `active_transactions`.
- **REVERSAL Audit Retention:**
  - `REVERSAL` events remain visible in `known_transactions` audit history (ordered by `(recorded_at, id)`).
  - `REVERSAL` events carry no independent active economics and NEVER appear in `active_transactions`.
- **Derived Authority:**
  - Projections (`LedgerProjectionView`, `ProjectedTransactionState`) are pure derived in-memory views and are never persistence authority.

---

## 11. Multi-Currency Rules
- **No Implicit Conversion:** Portfolio `base_currency` defines user reporting preference, but individual transactions strictly retain their native `trade_currency`, `cash_currency`, and FX conversion currencies.
- **Exact Decimal Precision:** Floating-point representations, strings, integers (for monetary amounts), `NaN`, and `Infinity` are rejected.

---

## 12. Deferred Scope (Phase 12C.2+ & Phase 14)
- **Phase 12C.2+:** Lot matching engines (FIFO/LIFO/Average Cost), cash balance and position calculations.
- **Phase 14:** Turkish and international tax rules, withholding calculations, and fee optimization.
