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

## 10c. Exact Position Quantity Projection (Phase 12C.2)
- **Input Authority:** Position quantity projection (`build_position_quantity_projection`) consumes `LedgerProjectionView.active_transactions` exclusively.
- **Position Scoping:** Position identity is strictly `(portfolio_id, account_id, instrument_id)`. The same instrument held in distinct accounts produces separate position states.
- **Quantity Altering Events:** Only active `BUY` (+quantity) and `SELL` (-quantity) transactions alter security quantities. Non-trade events (cash flows, dividends, interest, fees, taxes, FX) do NOT modify security quantities.
- **Zero-Position Retention & Open Holdings:** Fully closed positions (`quantity == Decimal("0")`) are preserved in `positions` for audit tracking; `open_positions` includes strictly positive holdings.
- **Negative Quantity Fail-Closed:** Final net negative quantity for any `(account_id, instrument_id)` raises `PositionProjectionError`. Short positions or overselling are unsupported.
- **Context-Independent Exact Decimal Aggregation:** Summation is computed via arbitrary-precision integer coefficient alignment, guaranteeing exact arithmetic regardless of ambient Decimal context precision.
- **No Cost/Lot Calculations:** This phase evaluates exact share/unit quantities only. Cost basis, lots, cash balances, and valuations remain deferred to later phases.

---

## 10d. Exact Account Cash Balance Projection (Phase 12C.3)
- **Input Authority:** Account cash balance projection (`build_cash_balance_projection`) consumes `LedgerProjectionView.active_transactions` exclusively.
- **Cash Balance Scoping:** Cash identity is strictly `(portfolio_id, account_id, currency)`. Currencies are never implicitly converted; distinct accounts maintain separate balances.
- **Exact Transaction Cash Effects:**
  - `BUY`: `- (quantity * unit_price)` in `trade_currency`
  - `SELL`: `+ (quantity * unit_price)` in `trade_currency`
  - `CASH_DEPOSIT`, `DIVIDEND`, `INTEREST`: `+ cash_amount` in `cash_currency`
  - `CASH_WITHDRAWAL`, `FEE`, `TAX_WITHHOLDING`: `- cash_amount` in `cash_currency`
  - `FX_CONVERSION`: `- from_amount` in `from_currency`, `+ to_amount` in `to_currency`
- **Context-Independent Exact Decimal Math:** Both trade notional multiplication and delta summation use arbitrary-precision integer arithmetic, immune to ambient Decimal context precision.
- **Zero-Balance Retention & Positive Balances:** Touched cash accounts with net balance `Decimal("0")` remain in `balances` for audit; `positive_balances` includes strictly positive holdings.
- **Negative Cash Fail-Closed:** Any net balance `< Decimal("0")` raises `CashProjectionError`. Overdraft and margin borrowing are unsupported.
- **Deferred Scope:** Cash bucket allocation, base-currency conversion, and investable-cash calculations remain deferred.

---

## 10e. Canonical Portfolio Accounting Snapshot Composition (Phase 12C.4)
- **Unified Composition:** `build_portfolio_accounting_snapshot` composes one `LedgerProjectionView`, one derived `PositionQuantityProjection`, and one derived `CashBalanceProjection` into an immutable `PortfolioAccountingSnapshot`.
- **Shared Authority:** Positions and cash share identical reversal deactivation and point-in-time (PIT) cutoffs inherited strictly from the input ledger view.
- **Fail-Closed Composition:** Lower-level projection errors (`PositionProjectionError`, `CashProjectionError`) propagate directly. Partial or incomplete accounting snapshots are never produced.
- **Exact Object Binding:** Preserves original ledger view, positions, and cash instances by object identity without copying or altering timestamps.
- **Deferred Scope:** Lot matching, cost basis, valuation, weights, and cash buckets remain deferred to Phase 12C.6+.

---

## 10f. Owner-Bound Persisted Accounting Snapshot Query Service (Phase 12C.5)
- **Owner-Bound Query Bridge:** `PortfolioAccountingQueryService` bridges the owner-scoped `PortfolioRepository` and the pure ledger/accounting projection stack.
- **Strict Owner Isolation:** The service inherits owner isolation directly from the injected `PortfolioRepository`. No `owner_id`, `user_id`, or credentials can be passed to query methods.
- **Complete Portfolio Projection:** Projections represent the entire portfolio across all accounts. `list_transactions(portfolio.id)` is called without account filters.
- **Explicit Knowledge Cutoff:** Current snapshot queries capture an explicit UTC system clock cutoff exactly once. Point-in-time (`get_snapshot_as_of`) queries preserve the exact caller-provided timezone-aware representation without normalization.
- **Read-Only & Uncached:** Pure query service with no write/mutation methods and no internal stateful caching.
- **Fail-Closed Error Propagation:** Database/repository failures, non-existent portfolios, and projection errors propagate directly. No partial snapshots or silent empty-history fallbacks.

---

## 11. Multi-Currency Rules
- **No Implicit Conversion:** Portfolio `base_currency` defines user reporting preference, but individual transactions strictly retain their native `trade_currency`, `cash_currency`, and FX conversion currencies.
- **Exact Decimal Precision:** Floating-point representations, strings, integers (for monetary amounts), `NaN`, and `Infinity` are rejected.

---

## 12. Deferred Scope (Phase 12C.6+ & Phase 14E+)
- **Phase 12C.6+:** Lot matching engines (FIFO/LIFO/Average Cost), cash bucket attribution, realized/unrealized P&L, portfolio valuations.
- **Phase 14A:** Observed explicit fee & tax-withholding event projection foundation (completed).
- **Phase 14B:** Exact per-account / per-currency observed fee & tax-withholding aggregation (completed).
- **Phase 14C:** Owner-bound persisted observed fee/tax evidence query service (completed).
- **Phase 14D:** Explicit fee/tax charge-to-transaction attribution evidence foundation (completed).
- **Phase 14E+:** Turkish and international tax rules, withholding calculations, tax liability estimation, and fee optimization.

---

## 13. Broker/File Import Provenance & Raw-Record Identity Foundation (Phase 13A)
- **Target-Bound & Source-Bound Identity:** Raw import file identity is the canonical tuple `(portfolio_id, account_id, source_key, content_sha256)`. Same content imported into different accounts/portfolios or under different source parsers produces distinct file identities.
- **Bytes-Only Content Digest:** `content_sha256` is the exact lowercase SHA-256 hex digest of raw file bytes without salts, timestamps, filenames, or target IDs.
- **Filename as Metadata:** Filenames are display metadata only and do not participate in content identity.
- **Raw Record Provenance:** Record identity is the canonical tuple `(portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256)`.
- **Identity Separation:** Import provenance exists strictly for staging, traceability, and diagnostics. It MUST NOT be mapped to `PortfolioTransaction.external_source` or `PortfolioTransaction.external_reference`.

---

## 13b. Immutable Import Batch Manifest & Record-Set Integrity (Phase 13B)
- **Manifest Binding Authority:** One `ImportBatchManifest` binds one `ImportFileProvenance` to an immutable, ordered tuple of `ImportRecordProvenance` instances.
- **Unique & Contiguous Ordinals:** For non-empty manifests, records must be uniquely and contiguously numbered `1, 2, ..., N` without gaps or duplicates.
- **Deterministic Manifest Preimage:** `manifest_sha256` is computed from compact JSON `[portfolio_id, account_id, source_key, file_content_sha256, [[ord, rec_sha], ...]]`. Display filenames, timestamps, and byte lengths are excluded.
- **Staging Identity Separation:** `manifest_identity` is `(portfolio_id, account_id, source_key, file_content_sha256, manifest_sha256)`. It remains staging provenance and is NOT mapped to ledger idempotency keys.

---

## 13c. Parser-Neutral Record Extraction Contract & Raw-Byte Binding (Phase 13C)
- **Cryptographic Raw-Byte Binding:** Builders enforce `sha256(raw_record) == record_provenance.record_sha256` before accepting extracted fields. Raw bytes are not stored.
- **Explicit Parser Revision:** `parser_revision` is a strict positive integer (`>= 1`). Revisions alter `parsed_sha256` and `parsed_identity`.
- **String-Only Field Model:** `ImportParsedField` retains exact textual representations without semantic coercion, stripping, or Unicode normalization. Blank fields (`""`) remain distinct from absent fields.
- **Canonical Field Ordering:** Fields are stored sorted by `field_key` ascending; duplicate keys fail closed.
- **Parsed Identity Tuple:** `(portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256, parser_revision, parsed_sha256)`.

---

## 13d. Immutable Parsed-Batch Manifest & Full Record-Coverage Integrity (Phase 13D)
- **Parsed Batch Authority:** One `ParsedImportBatchManifest` binds one raw `ImportBatchManifest` to complete, verified `ParsedImportRecord` instances under a single explicit `parser_revision`.
- **Full Coverage & Exact Provenance:** For a raw manifest with N records, exactly N parsed records must correspond 1:1 to `raw_manifest.records` with identical `ImportRecordProvenance`. No omissions, extras, or duplicates.
- **Deterministic Preimage:** `parsed_manifest_sha256` is computed from compact JSON `[portfolio_id, account_id, source_key, file_content_sha256, raw_manifest_sha256, parser_revision, [[ord, rec_sha, parsed_sha], ...]]`. Display filenames and timestamps are excluded.
- **Staging Identity Separation:** `parsed_manifest_identity` is `(portfolio_id, account_id, source_key, file_content_sha256, raw_manifest_sha256, parser_revision, parsed_manifest_sha256)`.
- **Zero-Field Records Count as Coverage:** A parsed record with `fields == ()` satisfies full coverage.

---

## 13e. Verified Source-Parser Execution Harness & Canonical Staging Pipeline (Phase 13E)
- **Parser Metadata Authority:** One `PortfolioImportSourceParser` adapter supplies `source_key` and `parser_revision`. The pipeline captures parser metadata in a single snapshot read; callers cannot supply or override source key or revision.
- **Single Invocation with Exact Original Payload:** `parser.extract_records(content)` is invoked exactly once with the exact original bytes object without copying, re-encoding, or newline conversion.
- **Logical Ordinal Assignment:** The sequence order returned by `extract_records` directly defines 1-indexed `record_ordinal` `1..N`. Duplicate raw records are preserved without deduplication.
- **Closed Layer Composition:** Pure staging pipeline composes all artifacts using closed Phase 13A-13D builders (`build_import_file_provenance`, `build_import_record_provenance`, `build_import_batch_manifest`, `build_parsed_import_record`, `build_parsed_import_batch_manifest`).
- **Exact Object Binding:** `ImportStagingResult` binds verified `file_provenance`, `raw_manifest`, and `parsed_manifest` with exact object identity.
- **Raw-Byte Non-Retention:** `ExtractedImportRecord` is an ephemeral DTO; `ImportStagingResult` retains no raw bytes, no raw record strings, and no parser references.
- **Fail-Closed Error Propagation:** Lower-layer integrity errors (`PortfolioImportProvenanceError`, `PortfolioImportBatchError`, `PortfolioImportParsingError`, `PortfolioParsedImportBatchError`) and parser runtime exceptions propagate unchanged without partial returns.

---

## 13f. Sentinax Canonical CSV v1 — Reference Source Parser Adapter (Phase 13F)
- **First Real Parser:** `SentinaxCanonicalCsvParserV1` is the authoritative reference source parser adapter implementing `PortfolioImportSourceParser`.
- **Fixed Metadata:** Fixed `source_key = "sentinax_csv"` and fixed `parser_revision = 1`.
- **Strict Line-Oriented Subset:** Strict UTF-8 without BOM; NUL bytes forbidden; each logical record is exactly one physical line. Multiline quoted fields containing physical newlines are rejected.
- **Delimiter & Quotes:** Comma (`,`) delimiter, double-quote (`"`) quoting with `""` escape semantics.
- **Newline Policy:** Uniform LF or CRLF supported; mixed newline styles in a single file and bare CR (`\r`) fail closed. Blank physical lines fail closed.
- **Header Contract:** First physical row is the header; header keys must strictly match Phase 13C field-key grammar (`^[a-z][a-z0-9_]{0,63}$`) and be unique.
- **Exact Raw Byte Slice Authority:** `ExtractedImportRecord.raw_record` is the exact byte slice of original content excluding only line terminators. Never reconstructed via text encoding.
- **Textual Fidelity:** Values remain exact strings without stripping, trimming, null-coercion, or date/numeric interpretation.

---

## 13g. Immutable Import Interpretation Assessment & Batch Review Foundation (Phase 13G)
- **Explicit Review Classification:** Every Phase 13C `ParsedImportRecord` is classified into exactly one `ImportAssessmentStatus`: `READY`, `UNRESOLVED`, or `REJECTED`.
- **READY Status Rule:** `READY` indicates eligibility to proceed to future canonical transaction-draft construction and MUST contain zero diagnostics. It does not imply ledger authorization or valid economics.
- **Diagnostic Requirement for Exceptions:** `UNRESOLVED` and `REJECTED` statuses require at least one `ImportAssessmentDiagnostic`.
- **Diagnostic Integrity:** Diagnostic code follows strict grammar `^[a-z][a-z0-9_]{0,63}$`; message is non-empty, non-whitespace-only, max 2048 chars; optional `field_key` must exist in `parsed_record.fields`. Diagnostics within an assessment are uniquely keyed by `(code, field_key)` and canonically sorted.
- **Complete Batch Coverage:** An `ImportAssessmentBatch` requires exact 1:1 correspondence for all records in the underlying `ParsedImportBatchManifest` in ascending ordinal order.
- **Cryptographic Preimage & Manifest Digest:** `assessment_manifest_sha256` deterministically digests `[portfolio_id, account_id, source_key, file_content_sha256, raw_manifest_sha256, parser_revision, parsed_manifest_sha256, [[ord, parsed_sha, status, [[code, field_key, msg], ...]], ...]]`.

---

## 13h. Immutable Source-Neutral Economic Transaction Draft Contract (Phase 13H)
- **Authoritative READY Gate:** Only records with explicit `ImportAssessmentStatus.READY` in a Phase 13G `ImportAssessmentBatch` may be drafted into typed economics.
- **Pre-Ledger Domain Boundary:** `ImportTransactionDraft` holds typed financial values but remains strictly pre-ledger. Zero internal transaction UUIDs, zero `recorded_at` timestamps, zero canonical `instrument_id` UUIDs, zero `cash_bucket_id` attributions, zero `external_source` / `external_reference` derivations, and zero ledger mutations.
- **Strict Typing & Numeric Discipline:** `transaction_type` must be an actual `TransactionType` member (`REVERSAL` forbidden); `effective_date` requires exact `date` type (rejecting `datetime`); `executed_at` requires timezone-aware datetime; financial amounts require strictly positive, finite `Decimal` instances (> 0); currencies require `Currency` enum members.
- **Field Family Exclusivity:** BUY/SELL requires `instrument_reference`, `quantity`, `unit_price`, `trade_currency`; CASH movements require `cash_amount`, `cash_currency`; INCOME/FEES require `cash_amount`, `cash_currency` with optional `instrument_reference`; FX conversions require `from_currency != to_currency`, `from_amount`, `to_amount`. Contradictory cross-family fields fail closed.
- **Instrument Reference Authority:** Unresolved instruments are preserved verbatim as `instrument_reference: Optional[str]` (1..256 chars, non-blank) without canonical lookup, stripping, or normalization.
- **Deterministic Draft Preimage & Digest:** `draft_sha256` binds the draft to `assessment_manifest_sha256`, `record_ordinal`, `parsed_sha256`, and canonicalized economics (canonical Decimal and UTC instant strings).
- **Deferred Scope:** Draft batch composition, canonical instrument resolution, ledger materialization, and persistence remain deferred to Phase 13I+.

---

## 13i. Immutable Economic Draft Batch Manifest & Complete READY-Coverage Integrity (Phase 13I)
- **One-Draft-Per-READY Contract:** Exactly one `ImportTransactionDraft` is required for every READY record in the authoritative `ImportAssessmentBatch`. UNRESOLVED records must have zero drafts. REJECTED records must have zero drafts. No READY record may be omitted; no READY record may have two drafts.
- **Exact Assessment Batch Binding:** Every draft's `assessment_batch` must be the same object as the manifest's `assessment_batch` (not merely the same SHA). Drafts from foreign batches fail closed immediately.
- **READY Ordinal Derivation:** Canonical READY ordinals are derived solely from `assessment_batch.assessments` where `status == READY`. Caller-supplied ordinal lists are never accepted.
- **One Logical Record → One Economic Draft:** Multi-event rows must be represented as separate logical records before this boundary. Phase 13I enforces a strict one-logical-record / one-economic-draft boundary.
- **Deterministic Canonical Ordering:** Drafts are canonically sorted by `record_ordinal` ascending. Builder input order is irrelevant; the output tuple is always deterministic.
- **Canonical Draft Batch Preimage & Digest:** `draft_manifest_sha256` is computed from compact JSON: `[str(portfolio_id), str(account_id), source_key, file_content_sha256, raw_manifest_sha256, parser_revision, parsed_manifest_sha256, assessment_manifest_sha256, [[record_ordinal, parsed_sha256, draft_sha256], ...]]`. Entries sorted by `record_ordinal` ascending.
- **Manifest Identity:** `draft_manifest_identity` extends `assessment_manifest_identity` with `draft_manifest_sha256`. Not a ledger external identity. No UUID generated at manifest level.
- **Zero-READY Batch:** A batch containing only UNRESOLVED/REJECTED records (or empty) is valid with `drafts == ()` and a deterministic manifest SHA.
- **Still Pre-Ledger:** Zero `PortfolioTransaction`. Zero canonical `instrument_id` UUIDs. Zero `external_source` / `external_reference` derivations. Zero `cash_bucket_id` attribution. Zero persistence. Zero ledger mutations.
- **Deferred Scope:** Canonical instrument resolution, ledger materialization, external identity assignment, cash-bucket attribution, and persistence remain deferred to Phase 13J+.

---

## 13j. Immutable PIT-Safe Instrument Resolution Outcome & Complete Draft-Coverage Manifest (Phase 13J)
- **Authoritative Four-State Outcome:** Every Phase 13I economic draft explicitly terminates in exactly one resolution state: `NOT_REQUIRED`, `RESOLVED`, `UNRESOLVED`, or `AMBIGUOUS`. Missing or ambiguous instruments never silently default or pick an arbitrary candidate.
- **Strict Eligibility Matrix:**
  - `NOT_REQUIRED`: Valid only when `draft.instrument_reference is None` (e.g. CASH_DEPOSIT, CASH_WITHDRAWAL, FX_CONVERSION, or unreferenced DIVIDEND/INTEREST/FEE/TAX_WITHHOLDING). BUY/SELL are strictly forbidden from NOT_REQUIRED.
  - `RESOLVED`: Requires non-None `instrument_reference`, resolver metadata (`resolver_key`, `resolver_revision >= 1`), exactly one canonical `instrument_id: UUID`, zero candidates, and zero diagnostics.
  - `UNRESOLVED`: Requires non-None `instrument_reference`, resolver metadata, `instrument_id is None`, zero candidates, and at least one diagnostic (`code`, `message`).
  - `AMBIGUOUS`: Requires non-None `instrument_reference`, resolver metadata, `instrument_id is None`, at least TWO distinct `candidate_instrument_ids` (sorted canonically by `str(uuid)` ascending), and at least one diagnostic.
- **Point-in-Time (PIT) Date Invariant:** `resolution_as_of_date` is strictly an exact `datetime.date` equal to `draft.effective_date`. Zero runtime date lookups (`date.today()`, `datetime.now()`, `utcnow()` forbidden).
- **Immutable Diagnostic Grammar:** `code` must match `^[a-z][a-z0-9_]{0,63}$` without normalization; `message` is 1..2048 non-whitespace characters.
- **Complete Batch Coverage:** An `ImportInstrumentResolutionBatch` requires 1:1 correspondence for all drafts in the underlying `ImportDraftBatchManifest` in ascending `record_ordinal` order. Semantic draft equality is required (reconstructed immutable drafts accepted).
- **Derived Readiness & Counts:** Derived properties `resolution_count`, `not_required_count`, `resolved_count`, `unresolved_count`, and `ambiguous_count` sum to total drafts. `is_fully_resolved` is True iff `unresolved_count == 0 and ambiguous_count == 0`.
- **Pre-Ledger Boundary:** Pure Python domain outcome model. Zero `PortfolioTransaction` construction, zero `InstrumentResolverService` execution, zero external identity derivation, zero persistence, and zero ledger mutation.
- **Deferred Scope:** Resolver service execution adapter, ledger materialization, external identity assignment, cash-bucket attribution, and persistence remain deferred to Phase 13K+.

---

## 13k. PIT-Safe Instrument Resolver Execution Port & Complete Batch Harness (Phase 13K)
- **Source-Neutral Resolver Execution:** The instrument resolver execution harness takes a Phase 13I `ImportDraftBatchManifest` and an adapter implementing `PortfolioImportInstrumentResolver`. The resolver receives strictly `(instrument_reference, effective_date)` with zero broker, filename, currency, or environmental metadata.
- **Snapshot & TOCTOU Hardening:** `resolver_key`, `resolver_revision`, and `resolve_candidates` callable are snapshotted exactly ONCE at the start of execution. Property descriptors are resolved once and reused across all drafts, preventing dynamic TOCTOU drift.
- **Strict Execution Exceptions:** Adapter-level exceptions raised by `resolve_candidates` propagate unchanged (not wrapped into domain errors), while adapter contract violations raise `PortfolioImportInstrumentResolverError`.
- **Zero-Cache Invariant:** Every instrument-bearing draft invokes the resolver separately, ensuring explicit execution tracing and accounting.
- **Candidate Cardinality Mapping:**
  - 0 candidates -> `UNRESOLVED` with diagnostic code `instrument_not_found`
  - 1 candidate -> `RESOLVED` with exact `instrument_id: UUID`
  - >= 2 candidates -> `AMBIGUOUS` with canonical ascending candidate UUID tuple and diagnostic code `ambiguous_reference`
  - Drafts without instrument reference (cash, FX, unreferenced dividend/fee) become `NOT_REQUIRED` without invoking the resolver.
- **Pre-Ledger Domain Boundary:** Pure domain execution harness. Zero `InstrumentResolverService` legacy execution, zero `PortfolioTransaction` construction, zero external identity derivation, zero persistence, and zero ledger mutation.
- **Deferred Scope:** Canonical CSV semantic interpretation, ledger transaction materialization, idempotency derivation, cash bucket assignment, and ledger persistence remain deferred to Phase 13L+.

---

## 13l. Sentinax Canonical CSV v1 Semantic Interpreter to Assessment & Economic Draft Batch (Phase 13L)
- **Exact 13-Field Canonical Semantic Schema:** Canonical CSV semantic revision 1 requires every data row to decode to exactly the 13 canonical fields: `transaction_type`, `effective_date`, `executed_at`, `instrument_reference`, `quantity`, `unit_price`, `trade_currency`, `cash_amount`, `cash_currency`, `from_currency`, `from_amount`, `to_currency`, `to_amount`. Missing or extraneous fields abort the batch with `SentinaxCanonicalCsvSemanticError`.
- **Empty String to None Mapping:** At the semantic layer, empty string values (`""`) in optional fields explicitly map to `None`. Non-empty strings are preserved verbatim without whitespace stripping.
- **Strict Lexical Parsing:**
  - `transaction_type`: exact lowercase supported enum name (`buy`, `sell`, `cash_deposit`, `cash_withdrawal`, `dividend`, `interest`, `fee`, `tax_withholding`, `fx_conversion`; `reversal` rejected row-level).
  - `effective_date`: exact `YYYY-MM-DD` calendar-valid date.
  - `executed_at`: empty or timezone-aware ISO-8601 with explicit `±HH:MM` offset.
  - `Decimal`: `(?:0|[1-9][0-9]*)(?:\.[0-9]+)?` without signs, commas, exponents, or NaN/Inf.
  - `Currency`: exact canonical member name (`TRY`, `USD`, `EUR`, `GBP`, `XAU`, `XAG`).
- **Two-Pass Assessment & Economic Authority:**
  - Pass 1: Collect all lexical field diagnostics; rows with lexical errors become provisional `REJECTED`, valid rows become provisional `READY`.
  - Pass 2: Provisional `READY` rows are validated against Phase 13H economic field-family rules via `build_import_transaction_draft`. Contradictory rows become `REJECTED` with diagnostic code `invalid_economic_contract`.
  - Final authoritative `ImportAssessmentBatch` is constructed, and final typed economic drafts are materialized and bound to it in `ImportDraftBatchManifest`.
- **Pre-Ledger Boundary:** Pure Python domain converter. Zero instrument resolution, zero external identity derivation, zero cash bucket assignment, zero `PortfolioTransaction` construction, and zero ledger mutation.
- **Deferred Scope:** Ledger transaction materialization, idempotency derivation, cash bucket assignment, and ledger persistence remain deferred to Phase 13M+.

---

## 13m. Sentinax Canonical CSV v1 End-to-End Pre-Ledger Import Orchestration (Phase 13M)
- **Thin End-to-End Pre-Ledger Composition:** Single authoritative entry point `run_sentinax_canonical_csv_import_v1` composes raw immutable CSV bytes through Phase 13E/F staging and parsing (`SentinaxCanonicalCsvParserV1`), Phase 13L semantic interpretation (`SentinaxCanonicalCsvSemanticInterpreterV1`), and Phase 13K PIT instrument resolver execution (`resolve_import_draft_batch_instruments`).
- **Direct Authoritative Return:** Returns the immutable Phase 13J `ImportInstrumentResolutionBatch` directly without redundant result wrappers. The complete nested provenance chain (`resolution_batch` -> `draft_manifest` -> `assessment_batch` -> `parsed_manifest` -> `raw_manifest` -> `file_provenance`) is fully inspectable.
- **Exception & Rejection Semantics:** Lower-layer domain exceptions and resolver adapter errors propagate unchanged without wrapping. Semantically `REJECTED` rows remain inspectable in the assessment batch but receive no draft or resolver invocation and do not abort the valid batch.
- **Zero New Hashes or UUIDs:** Introduces zero new cryptographic hashes or identifier schemes; relies exclusively on the closed immutable hash manifest hierarchy established in Phases 13A–13L.
- **Provenance Authority:** `imported_at` is explicit caller observation metadata and is not mapped to ledger `recorded_at`.
- **Pre-Ledger Domain Boundary:** Pure pre-ledger pipeline orchestration. Zero `PortfolioTransaction` construction, zero external identity/idempotency derivation, zero cash bucket assignment, zero repository or database persistence, and zero mutation of the financial ledger.
- **Deferred Scope:** Ledger transaction materialization, idempotency key derivation, cash bucket attribution, and ledger persistence remain deferred to Phase 13N.

---

## 13n. Immutable Ledger-Materialization Plan Contract & Full Resolution-Batch Eligibility (Phase 13N)
- **Immutable Bridge Contract:** Pure domain bridge (`import_materialization.py`) connecting an immutable `ImportInstrumentResolutionBatch` (Phase 13J/K) to an immutable `ImportLedgerMaterializationBatch`.
- **Eligible Resolution States:** A resolution outcome is materializable if and only if its status is `RESOLVED` or `NOT_REQUIRED`.
- **Full-Batch Fail-Closed Gate:** Batch materialization requires `is_fully_resolved is True`. If any resolution is `UNRESOLVED` or `AMBIGUOUS`, the entire batch fails closed and raises `PortfolioImportMaterializationError`. No partial materialization batch is ever returned.
- **Exact Field Copying & Target Binding:** The plan copies exact target identifiers (`portfolio_id`, `account_id`) from immutable file provenance, and freezes exact economic fields (`transaction_type`, `effective_date`, `executed_at`, `quantity`, `unit_price`, `trade_currency`, `cash_amount`, `cash_currency`, `from_currency`, `from_amount`, `to_currency`, `to_amount`) from draft authority.
- **Instrument ID Assignment:** Resolved canonical instrument UUID becomes `plan.instrument_id`. For `NOT_REQUIRED` resolutions, `plan.instrument_id` is strictly `None`.
- **Semantic Rejections:** Rows `REJECTED` in Phase 13L remain visible in the nested `assessment_batch` and receive zero plans.
- **Staging-Only Identity:** `plan_identity` and `materialization_manifest_identity` extend upstream staging identities. They MUST NOT be mapped to `PortfolioTransaction.external_source` or `external_reference`.
- **Deferred Scope:** Transaction UUID generation, `recorded_at` assignment, ledger external identity/idempotency mapping, cash-bucket attribution, and ledger append remain deferred to Phase 13O.

---

## 13o. Immutable Import-Commit Claim & Ledger-Binding Intent Contract (Phase 13O)
- **Immutable Claim Intent Bridge:** Pure domain contract (`import_commit.py`) bridging an immutable `ImportLedgerMaterializationBatch` (Phase 13N) to an immutable `ImportLedgerBindingBatch`.
- **Authoritative Claim Identity:** Import commit claims are anchored exclusively to immutable Phase 13A raw record provenance: `(portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256)`. Filename and observation time (`imported_at`) are excluded.
- **Strict Ledger Identity Separation:** Import claim identities identify source records within imported files and are NEVER mapped to `PortfolioTransaction.external_source` or `external_reference`.
- **Conflict Detection Data:** `expected_plan_sha256` records the exact plan interpretation at commit creation. It is NOT the claim identity itself. Future persistence layers use this separation to distinguish safe idempotent replays (same claim, same plan) from semantic conflicts (same claim, changed plan).
- **Batch Coverage & Canonical Ordering:** Exactly one binding intent per materialization plan, ordered strictly by `record_ordinal` ascending. Duplicate claim identities within a single batch fail closed.
- **Semantic Rejections:** Rows rejected in Phase 13L remain visible in the nested assessment batch and receive zero binding intents.
- **Deferred Scope:** Transaction UUID generation, `recorded_at` assignment, cash-bucket attribution, and ledger append remain deferred to Phase 13P.

---

## 13p. Import Claim-Binding Persistence Schema & Strict Pure Codec (Phase 13P)
- **Persistent Table:** Supabase migration 014 creates `public.portfolio_import_claim_bindings`.
- **Composite Primary Key Uniqueness:** Exact raw claim identity `(portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256)` forms the authoritative database primary key.
- **Interpretation Separation:** `expected_plan_sha256` is stored separately as the interpretation snapshot and excluded from primary key uniqueness. Same claim + changed plan surfaces as an explicit uniqueness conflict.
- **Target Transaction Foreign Key:** `transaction_id` references `portfolio_transactions(id, portfolio_id, account_id)` with `ON DELETE RESTRICT`. Non-unique index allows multiple overlapping claims to bind to a single transaction.
- **Relational Integrity:** Foreign keys enforce target portfolio/owner and account/portfolio consistency with `ON DELETE RESTRICT`.
- **Domain Grammar & Constraints:** Strict ASCII regex check constraints for `source_key` (`^[a-z0-9][a-z0-9._-]{0,63}$`), 64-char lowercase hex for all SHA fields, and `record_ordinal >= 1`.
- **Anti-Tamper Immutability:** Dedicated trigger `trg_prevent_import_claim_binding_tamper` blocks `UPDATE` and `DELETE` on binding rows.
- **Row Level Security (RLS):** Under migration 014 (Phase 13P), authenticated users had `SELECT` and `INSERT` access scoped strictly to `owner_id = auth.uid()`. (Note: The authenticated `INSERT` surface was subsequently revoked in migration 016 under Phase 13Q.3 to enforce backend service-role write exclusivity).
- **Pure Python Codec:** `import_commit_persistence.py` provides `PersistedImportLedgerBinding`, `serialize_import_ledger_binding`, and `hydrate_import_ledger_binding`. Zero DB/network I/O, zero UUID generation, zero clock calls.
- **Owner Context Defense-in-Depth:** Serializer and hydrator enforce explicit trusted `expected_owner_id`.
- **Persistence Boundary:** Phase 13P establishes schema and codec only; runtime atomic ledger commit and conflict resolution are deferred to Phase 13Q.

---

## 13q. Atomic Import Claim + Ledger Transaction Commit RPC & Owner-Bound Repository Execution (Phase 13Q)
- **First Real Import-to-Ledger Write Boundary:** Bridges an immutable `ImportLedgerBindingIntent` (Phase 13O) to persistent ledger storage via owner-scoped `PortfolioRepository.commit_import_binding_intent`.
- **Single-Intent Atomic Execution:** Exactly one binding intent is committed at a time. The database function `public.commit_portfolio_import_claim` atomically inserts both the candidate `portfolio_transactions` row and the `portfolio_import_claim_bindings` row within a single PL/pgSQL transaction.
- **Raw Claim Idempotency Authority:** The raw claim identity `(portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256)` is the sole authority for import deduplication.
- **Idempotent Replay vs. Conflict Behavior:**
  - *Same Claim, Same Plan & Economics:* Returns `AppendStatus.IDEMPOTENT_DUPLICATE` with the existing bound transaction UUID. Zero new rows are inserted.
  - *Same Claim, Changed Plan or Economics:* Returns `AppendStatus.CONFLICT` with the existing bound transaction UUID and diagnostic. Zero new rows are inserted.
- **Race-Safe Subtransaction Handling (SQLSTATE 23505):** Concurrent execution races on the same claim identity roll back tentative transaction insertions in losing subtransactions and re-read the authoritative persisted claim.
- **External Identity Separation:** Import transactions strictly have `external_source = NULL` and `external_reference = NULL`. Import raw claim identity is not mapped to ledger external identity.
- **Cash Bucket Independence:** `cash_bucket_id = NULL` for all Phase 13Q imported transactions. Cash bucket attribution is deferred.
- **System Clock Authority:** `recorded_at` is assigned by the owner-bound repository's system clock (`self._get_system_time()`). Source `imported_at` and `bound_at` are NOT used as `recorded_at`.
- **Database `bound_at` Authority:** `bound_at` on claim binding rows is generated exclusively by PostgreSQL `DEFAULT now()`.
- **Write-Surface Exclusivity & Trust Boundary:**
  - `commit_portfolio_import_claim` is a backend persistence primitive executable strictly by `service_role`.
  - Direct RPC execution is revoked from `authenticated` and `PUBLIC` to prevent callers from bypassing Python domain serialization and supplying arbitrary economic fingerprints.
  - Direct `INSERT` on `portfolio_import_claim_bindings` is revoked from `authenticated` (and the authenticated INSERT policy is dropped via migration 016) to prevent claim-squatting attacks.
  - Authenticated users retain owner-scoped `SELECT` visibility (`owner_id = auth.uid()`) on their own claim bindings.
  - Canonical Python `PortfolioTransaction` and serializers remain the sole financial domain and fingerprint authority.
- **No Fuzzy/Cross-Claim Deduplication:** Distinct raw source records describing identical economics are not merged; each receives its own canonical transaction.

---

## 13r. File-Level Atomic Binding-Batch Commit & All-or-Nothing Import Execution (Phase 13R)
- **File-Level Commit Unit:** `ImportLedgerBindingBatch` represents the transactional commit unit for an entire imported file.
- **Zero-Intent NOOP:** Zero-intent binding batches return `ImportBatchCommitStatus.NOOP` without database calls, clock access, or UUID allocation.
- **Atomic Batch RPC:** Non-empty batches are committed in a single PostgreSQL call via `public.commit_portfolio_import_claim_batch(p_items JSONB)` with `service_role` exclusivity.
- **Single-Intent Delegation:** The batch RPC delegates each item to the closed `public.commit_portfolio_import_claim`, preserving one canonical SQL validation and constraint boundary.
- **All-or-Nothing Conflict Rollback (SQLSTATE P13R1):** Any item conflict triggers a dedicated internal exception (`P13R1`), completely rolling back all newly inserted transactions and claims created earlier in the batch.
- **Generic Database Error Safety:** Any structural or constraint error from PostgreSQL aborts the entire batch statement, preventing partial imports.
- **Shared Ingestion Clock:** All candidate transactions in a single batch share a single `recorded_at` timestamp obtained once from `self._get_system_time()`.
- **Coexistence of New & Replayed Items:** Mixed batches (containing both new records and exact replays) commit successfully with `batch_status = appended`, returning all final transaction UUIDs in original input order.
- **Full Replay Idempotency:** Batches consisting entirely of exact replays return `batch_status = idempotent_duplicate` with existing transaction UUIDs and zero writes.
- **Rejection & Scope Boundaries:** Semantic `REJECTED` rows remain outside the binding batch; no cross-file fuzzy deduplication, external identity derivation, or cash bucket assignment.

---

## 13s. End-to-End Canonical CSV Import Execution Orchestrator & Execution Result (Phase 13S)
- **Single Canonical Production Execution Entrypoint:** `execute_sentinax_canonical_csv_import_v1` composes the full Canonical CSV v1 pipeline (`run_sentinax_canonical_csv_import_v1` -> `build_import_ledger_materialization_batch` -> `build_import_ledger_binding_batch` -> `repository.commit_import_binding_batch`) into a single deterministic call.
- **Zero Duplication of Lower-Layer Logic:** Orchestration contains zero financial parsing, zero CSV parsing, zero transaction construction, zero UUID generation, zero clock access, zero hashlib computations, zero direct SQL/RPC/table calls, and zero retry loops.
- **Resolution Gate & Fail-Closed Blocking:** If any draft outcome is `UNRESOLVED` or `AMBIGUOUS`, `execute_sentinax_canonical_csv_import_v1` immediately returns `SentinaxCanonicalCsvImportExecutionStatus.RESOLUTION_BLOCKED` and halts before materialization, binding, or database execution (0 ledger writes).
- **Semantic Rejection Visibility:** Rows assessed as `REJECTED` in Phase 13L remain fully audit-visible in the nested `assessment_batch` (and via `.rejected_record_ordinals`), but receive zero ledger materialization plans and zero binding intents.
- **All-Rejected NOOP Outcome:** A source file where all rows are semantically rejected yields `0` binding intents and terminates cleanly as `SentinaxCanonicalCsvImportExecutionStatus.NOOP` without ledger writes or fabricated error states.
- **File-Level Atomic Batch Commit:** Fully resolved batches commit atomically via Phase 13R `PortfolioRepository.commit_import_binding_batch`.
- **Exact Top-Level Status Mapping:**
  - `ImportBatchCommitStatus.NOOP -> SentinaxCanonicalCsvImportExecutionStatus.NOOP`
  - `ImportBatchCommitStatus.APPENDED -> SentinaxCanonicalCsvImportExecutionStatus.APPENDED`
  - `ImportBatchCommitStatus.IDEMPOTENT_DUPLICATE -> SentinaxCanonicalCsvImportExecutionStatus.IDEMPOTENT_DUPLICATE`
  - `ImportBatchCommitStatus.CONFLICT -> SentinaxCanonicalCsvImportExecutionStatus.CONFLICT`
  - `ImportBatchCommitStatus.INVALID -> SentinaxCanonicalCsvImportExecutionStatus.INVALID`
- **Immutable Execution Result Envelope:** `SentinaxCanonicalCsvImportExecutionResult` encapsulates the authoritative stage outputs (`resolution_batch`, `materialization_batch`, `binding_batch`, `commit_result`) and enforces direct constructor tamper rejection.
- **Time Authority Isolation:** `imported_at` is preserved strictly as provenance observation time; ledger `recorded_at` is assigned by the repository system clock; `bound_at` is generated by PostgreSQL `DEFAULT now()`.
- **Identity & Cash Boundaries:** Staging claims are strictly separated from ledger external identity (`external_source = NULL`, `external_reference = NULL`); zero cash bucket assignment (`cash_bucket_id = NULL`).
- **Unmodified Exception Propagation:** Parser, semantic format, resolver, and database/repository exceptions propagate directly without exception wrapping or conversion to `INVALID`.

---

## 14. Observed Explicit Fee & Tax-Withholding Projection (Phase 14A)
- **Input Authority:** Sole input authority is `LedgerProjectionView.active_transactions` exclusively (Phase 12C.1).
- **Observed-Only Semantics:** Captures ONLY actual explicit `FEE` and `TAX_WITHHOLDING` ledger events.
  - `observed ledger charge ≠ estimated tax liability ≠ expected future tax ≠ synthetic fee estimate`.
- **Reversal & PIT Aware:** Automatically inherits system PIT cutoff and reversal deactivation:
  - At PIT before a reversal's `recorded_at`: charge remains visible.
  - At PIT on/after a reversal's `recorded_at`: charge is absent from active events.
- **Exact Object & Precision Preservation:** Preserves original authoritative `PortfolioTransaction` instances by exact object identity (`actual is expected`) and exact `Decimal` precision without numeric normalization or floating-point conversions.
- **Canonical Event Ordering:** Strictly preserves the upstream relative ordering in `ledger_view.active_transactions`. No independent sorting by date, amount, or type.
- **Optional Instrument Linkage vs. Account-Level Charges:**
  - `instrument_linked_events`: filters events with `instrument_id is not None`.
  - `account_level_events`: filters events with `instrument_id is None`.
  - Instrument linkage is strictly the explicit ledger field already recorded; it does NOT imply heuristic trade attribution.
- **No Monetary Aggregation Across Currencies:** Phase 14A does NOT compute total fees, total taxes, total costs, or effective tax rates. Multi-currency values remain distinct.
- **No Tax Law / Inference / Attribution Heuristics:**
  - Zero hard-coded tax rates or jurisdiction rules (Turkey, US, BIST, TEFAS, Eurobond).
  - Zero fee inference from trade notionals or broker commission schedules.
  - Zero tax withholding inference from dividend, interest, or sale events.
  - Zero causal trade-attribution heuristics (matching by date, nearest timestamp, or account).
- **Observed Charges as Evidence Layer:** Observed charges establish a trustworthy factual evidence foundation before later tax/fee calculation engines (Phase 14B+).

---

## 15. Exact Observed Fee & Tax-Withholding Aggregation (Phase 14B)
- **Input Authority:** Sole input authority is `ObservedFeeTaxProjection` (Phase 14A) exclusively. Does NOT independently read raw ledger transactions.
- **Aggregation Key:** Aggregation identity is strictly `(account_id, cash_currency)`.
- **Category Separation:** `fee_amount` and `tax_withholding_amount` are aggregated separately along with respective counts (`fee_event_count`, `tax_withholding_event_count`).
- **Context-Independent Exact Decimal Summation:** Monetary totals are computed via arbitrary-precision integer coefficient and exponent alignment, completely immune to ambient `decimal.Context` precision.
- **Exact Decimal Representation Preservation:** Aggregation preserves exact scale representation without normalization or rounding (e.g. `1.20 + 2.300 = 3.500`).
- **First-Seen Canonical State Ordering:** Aggregate states are canonically ordered by first appearance of each `(account_id, currency)` key in `observed_projection.events`.
- **No Cross-Account or Cross-Currency Aggregation:** Currencies (USD, TRY, EUR, etc.) and accounts remain strictly distinct.
- **Instrument-Linked and Account-Level Coexistence:** Events with or without `instrument_id` belonging to the same account and currency combine into the same aggregate state without splitting or attribution heuristics.
- **Total Observed Charge Property:** `total_observed_charge` provides the exact within-state sum of fees and taxes as historical factual cost evidence.
- **Zero Tax Law / Liability / Inference:**
  - No tax rate or withholding expectation calculations.
  - No tax liability, refund, or credit estimates.
  - No broker commission schedule or fee inference.
  - No FX conversion or base-currency rollup.
  - No cost-basis or tax-lot modification.

---

## 16. Owner-Bound Persisted Observed Fee/Tax Evidence Query Service (Phase 14C)
- **Persistence Authority:** Injected owner-scoped `PortfolioRepository` (or structural `PortfolioFeeTaxRepositoryPort`) acts as sole data authority.
- **Complete Portfolio History:** `list_transactions(portfolio_id)` loads complete portfolio transaction history without account-level filtering, allowing full multi-account aggregation.
- **Explicit Knowledge Cutoff:**
  - `get_current_aggregation`: Captures system clock exactly once per query, validates timezone awareness, normalizes to UTC, and uses that exact cutoff.
  - `get_aggregation_as_of`: Validates caller's timezone-aware datetime and strictly preserves caller's exact representation (e.g. `+03:00`).
- **Downstream Pipeline Execution:**
  - `LedgerProjectionView` owns reversal/PIT semantics.
  - `ObservedFeeTaxProjection` (Phase 14A) owns observed explicit charge filtering.
  - `ObservedFeeTaxAggregation` (Phase 14B) owns exact per-account/currency Decimal aggregation.
- **Read-Only & Stateless:** Zero write methods, zero caching/memoization, zero state mutation.
- **Purity & Isolation:**
  - No internal fee/tax arithmetic or filter duplication.
  - No FX conversion or base-currency rollup.
  - No tax law, liability estimates, or heuristic trade attribution.
  - Fail-closed error propagation for lower-layer projection and operational repository exceptions.

---

## 17. Explicit Fee/Tax Charge Attribution Evidence (Phase 14D)
- **Explicit Caller-Supplied Evidence Only:** Charge-to-transaction linkage is never inferred from shared instruments, matching dates, nearby timestamps, matching amounts, or shared accounts.
- **Input Authority:** Validates explicit intents against `ObservedFeeTaxProjection` (Phase 14A) active charge events and `ObservedFeeTaxProjection.ledger_view.active_transactions` (Phase 12C.1) active economic targets.
- **Multi-Target Allocation:** A single charge event may be explicitly split across multiple active economic targets.
- **Partial Allocation Allowed:** Allocations do not need to cover 100% of the charge amount; unallocated remainders are explicitly tracked.
- **Over-Allocation Rejected:** Exact context-independent Decimal sum of allocations across targets cannot exceed the charge's `cash_amount`.
- **Target Restrictions:** Valid targets are active non-reversal economic events (`BUY`, `SELL`, `DIVIDEND`, `INTEREST`, `CASH_DEPOSIT`, `CASH_WITHDRAWAL`, `FX_CONVERSION`). Charges (`FEE`, `TAX_WITHHOLDING`) and `REVERSAL` events are strictly prohibited as attribution targets.
- **Account & Portfolio Isolation:** Both charge and target must share the exact same `portfolio_id` and `account_id`. Cross-account attribution is rejected.
- **Missing Attribution ≠ Zero Economic Relationship:** If no attribution intents are supplied for a charge, it remains 100% unallocated in evidence; this reflects lack of explicit evidence rather than proof of no relationship.
- **Zero Ledger Modification & Zero Persistence:** Attribution evidence is an independent factual layer; no fields are added to `PortfolioTransaction`, and no database tables/RPCs are introduced in Phase 14D.
- **Zero Tax Law / Cost Basis Effect:** Does not modify `PositionLot`, unit cost, realized/unrealized P&L, or tax liabilities.



