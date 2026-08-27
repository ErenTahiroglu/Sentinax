# SEC EDGAR Filing & Raw XBRL CompanyFacts Data Layer

**Version:** 1.2 (Phase 8A.6 Hardened — Entity-Level Persistence & Storage Consistency Specification)  
**Effective Date:** 26 August 2026  
**Scope:** Official SEC EDGAR Submissions, CompanyFacts APIs, CIK entity vs security-level symbology, Point-in-Time acceptance (TIMESTAMPTZ vs local TIMESTAMP) vs public availability boundaries, append-only fact-filing linkage trigger, and raw fact storage.

---

## 1. Overview & Data Sources Summary

| Source | Authority Level | Access Method | Contract Status | Rate Limit (Official / Safety) | Authentication | PIT Knowledge Boundary |
|---|---|---|---|---|---|---|
| **SEC EDGAR Submissions** | `TIER_1_REGULATORY` | REST API (JSON) | **VERIFIED** | 10 req/s / **<= 8 req/s** | Declared `User-Agent` | `acceptance_datetime` (Aware) / `acceptance_local_datetime` (Local) |
| **SEC EDGAR CompanyFacts** | `TIER_1_REGULATORY` | REST API (JSON) | **VERIFIED** | 10 req/s / **<= 8 req/s** | Declared `User-Agent` | Linked `acceptance_datetime` via Link Table |
| **SEC Ticker Mapping** | `TIER_1_REGULATORY` | Static JSON Feed | **CANDIDATE ONLY** | 10 req/s / **<= 8 req/s** | Declared `User-Agent` | Non-authoritative candidate suggestion |

---

## 2. Entity Level (CIK) vs Security Level (`instruments.id`)

- **Issuer Entity Level:** SEC filings (`sec_filings`) and XBRL facts (`sec_raw_facts`) represent the corporate reporting entity (identified by 10-digit zero-padded `CIK`).
- **Security Level Identity:** Sentinax `InstrumentRecord.id` (UUID) represents an investable security (e.g. Common Stock Class A, Common Stock Class B, Preferred Stock, ADR).
- **Association Semantics:**
  - **Security -> SEC:** `instrument.cik` points to the reporting issuer's CIK.
  - **SEC -> Securities:** `resolve_instruments_for_sec_cik(cik)` dynamically resolves all active securities sharing that issuer CIK at query time.
  - **Invariant:** Canonical SEC raw storage tables do **NOT** store a 1-to-1 canonical `instrument_id`. No parallel `sec_company_uuid` is introduced.

---

## 3. Official SEC Endpoints

Base URL: `https://data.sec.gov`

1. **Submissions API:**
   - URL: `https://data.sec.gov/submissions/CIK##########.json`
   - Purpose: Master filer metadata and columnar array of recent filings (~1,000 filings / 1+ years).
   - Secondary Files: Additional historical files referenced in `payload["filings"]["files"]` (e.g. `CIK0000320193-submissions-001.json`). Each file produces an independent raw provider snapshot.

2. **CompanyFacts API:**
   - URL: `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`
   - Purpose: Aggregation of all standard public taxonomy XBRL disclosures for an issuer (`us-gaap`, `dei`, `ifrs-full`, `srt`).

3. **Ticker-to-CIK Exchange Mapping:**
   - URL: `https://www.sec.gov/files/company_tickers_exchange.json`
   - Purpose: Discovery of candidate CIKs for US ticker symbols. Note: The SEC explicitly disclaims guaranteed accuracy or completeness of this mapping file.

---

## 4. Accession Number Semantics & Third-Party Filers

- **Accession Format:** 20-character hyphenated identifier: `0000320193-24-000106` (regex: `^[0-9]{10}-[0-9]{2}-[0-9]{6}$`).
- **Submitting Entity CIK Prefix:** The first 10 digits of an accession number indicate the **submitting entity CIK**. This can be a third-party filing agent, legal advisor, or parent company (e.g. `0001140361-24-024352` for Apple Inc.).
- **Invariant:** Sentinax does **NOT** require `accession[:10] == issuer_cik`. Archive directory URLs are constructed using the subject issuer's CIK.

---

## 5. Acceptance Timestamp vs Local Time vs Public Availability

- **`acceptance_datetime` (`TIMESTAMPTZ`):** Only populated when the SEC payload provides an explicit timezone or offset (e.g. ISO 8601 with `Z` or `+/-offset`).
- **`acceptance_local_datetime` (`TIMESTAMP WITHOUT TIME ZONE`):** Populated when the SEC payload provides a compact 14-digit (`YYYYMMDDHHMMSS`) or naive local datetime. SEC official FAQ documents EDGAR acceptance time as Eastern Standard/Daylight Time (EST/EDT). Naive datetimes are **never** silently coerced into UTC `TIMESTAMPTZ`.
- **`public_available_at`:** The verified timestamp when filing documents actually became accessible on sec.gov.
  - SEC guidance notes that documents typically appear on sec.gov **1–3 minutes** after acceptance.
  - SEC does **NOT** publish a first-public-availability timestamp.
  - During peak filing periods, dissemination queues may experience longer delays.
  - **INVARIANT:** `acceptance_datetime` is **NOT** equal to `public_available_at`. Sentinax never fabricates deterministic availability delays (`acceptance + 1 min`). `public_available_at` remains `None` unless explicitly verified.

---

## 6. CompanyFacts Raw Fact Taxonomy & Precision

- **Taxonomy Scope:** Non-custom public taxonomies: `us-gaap`, `dei`, `ifrs-full`, `srt`.
- **Custom Taxonomy Limitation:** Company extension taxonomies (custom company-specific tags) are omitted from CompanyFacts. Missing facts in CompanyFacts do not imply a disclosure failure.
- **Period Types:**
  - `PeriodType.DURATION`: Facts measured over a period with both `start` and `end` dates (e.g. Revenues, Cash Flows).
  - `PeriodType.INSTANT`: Facts measured at a point in time with `end` date only (e.g. Balance Sheet Assets).
  - Facts lacking `end` dates are rejected as schema-invalid.
- **Precision & Zero Invariant:**
  - Values are parsed and stored as exact **`Decimal`** instances (mapped to `NUMERIC` in SQL).
  - Genuine zero is preserved as `Decimal("0")`. Missing values remain `None`.
  - Serialized via `to_dict()` as exact decimal strings; float casting during serialization is prohibited.
- **Nullable Accession:** Raw facts without an accession number in the source payload are preserved with `accession_number = NULL` (no fake `"UNKNOWN_ACCN"`).

---

## 7. Append-Only Fact-Filing Linkage (`sec_fact_filing_links`)

- When CompanyFacts contains facts whose filing accession has not yet been ingested into `sec_filings`, the fact is stored with `filing_id = None` (unresolved status) and **NEVER dropped**.
- When the corresponding historical filing is ingested later, a new linkage row is inserted into `sec_fact_filing_links` (`fact_id`, `filing_id`, `accession_number`, `cik`).
- **Database Trigger:** PostgreSQL trigger `trg_validate_sec_fact_filing_link_integrity` strictly enforces CIK and accession number equality between the linked fact and filing before insertion.
- Because `sec_raw_facts` is strictly immutable, the raw fact row is **never modified or updated**.

---

## 8. Fair Access & Leaky Pacing Rate Limiter

- **Declared User-Agent:** Mandated by SEC fair access policy (`SEC_USER_AGENT` environment variable). Missing agent raises `ProviderConfigurationError`.
- **Serialized Leaky Pacing:** `SECRateLimiter` enforces strict serialized spacing: `min_interval = 1.0 / rate` (e.g. `0.125s` for 8 req/s; burst capacity = 1) to eliminate initial multi-request bursts.
- **Process-Wide Shared Limiter:** All `SECEdgarClient` instances, Submissions, CompanyFacts, and Ticker Discovery calls share a single singleton limiter (`rate_limit_scope = "PROCESS_LOCAL"`).
- **Architecture Note (Distributed Clusters):** In a multi-worker / distributed environment, global rate limiting across instances will be enforced via Redis token buckets.

---

## 9. Phase 8B.1 / 8B.1.6 — Canonical Concept Families & Semantic Hardening

Phase 8B.1 / 8B.1.6 defines the deterministic registry that maps raw taxonomy tags to canonical economic concept families without metric calculations or winner selection.

### 9.1 Authoritative Taxonomy Support Reality

- **US-GAAP**: SEC currently supports the **2026 release** (and historical 2020–2025 releases).
- **IFRS**: In the official SEC Standard Taxonomies list, the current SEC-supported IFRS taxonomy is **IFRS 2025** (and earlier 2020–2024 releases).  
  *Note*: Publication of a taxonomy by the IASB does not constitute acceptance by the SEC until officially adopted on `sec.gov`. Sentinax strictly avoids fabricating "SEC-supported IFRS 2026".

### 9.2 Critical Semantic Safeguards

1. **IFRS 18 Operating Profit vs IAS 1 Operating Subtotal**:
   - `ifrs-full:ProfitLossFromOperatingActivities` was an optional, entity-specific subtotal under IAS 1 and maps to `OPERATING_INCOME_LEGACY_IAS1`.
   - `ifrs-full:OperatingProfitLossOperating` is the final verified standardized operating category under IFRS 18 and maps to `OPERATING_PROFIT_IFRS18`. Proposal-stage `OperatingProfitLoss` is rejected.
   - These two concepts are semantically distinct and are **never treated as blind interchangeable aliases**.

2. **Net Income Scope Split: Parent vs Including NCI**:
   - `NET_INCOME_ATTRIBUTABLE_TO_PARENT`: Measures bottom-line earnings attributable exclusively to parent entity stockholders (`us-gaap:NetIncomeLoss`, `ifrs-full:ProfitLossAttributableToOwnersOfParent`).
   - `NET_INCOME_INCLUDING_NCI`: Measures total consolidated profit or loss before noncontrolling interest allocation (`us-gaap:ProfitLoss`, `ifrs-full:ProfitLoss`). *Note*: `ProfitLoss` is distinct from Other Comprehensive Income (OCI) and does not represent Comprehensive Income.
   - Common-stockholder-available income (`NetIncomeLossAvailableToCommonStockholdersBasic`) is kept strictly distinct and is not collapsed into parent or consolidated income.

3. **CapEx PP&E vs Productive Assets**:
   - `CAPEX_PP&E`: Measures strictly cash paid for physical property, plant, and equipment (`us-gaap:PaymentsToAcquirePropertyPlantAndEquipment`, `ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities`).
   - `CAPEX_PRODUCTIVE_ASSETS`: Measures broader capital additions including software, licenses, and intangible assets (`us-gaap:PaymentsToAcquireProductiveAssets`).
   - *Double-Count Guard*: Downstream analytical engines and future FCF calculators must treat these as distinct items and **never sum them together**.

4. **Strict Fail-Closed Unit Validation**:
   - `MONETARY`: Restricted to explicit ISO 4217 standard currency codes; unknown codes (e.g. `ABC`) fail closed as `INVALID_UNIT`.
   - `MONETARY_PER_SHARE`: Requires explicit currency numerator and shares denominator (e.g. `USD/shares`, `EUR/shares`); plain `USD`, `pure`, or `ratio` are rejected.
   - `SHARES`: Strictly limited to `shares` / `share`.

### 9.3 Canonical Concept Families Table

| Canonical Concept | Statement Family | Expected Period | Expected Unit Class | Primary Verified Tags |
|---|---|---|---|---|
| `REVENUE` | `INCOME_STATEMENT` | `DURATION` | `MONETARY` | `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`, `us-gaap:Revenues`, `ifrs-full:Revenue` |
| `OPERATING_INCOME` | `INCOME_STATEMENT` | `DURATION` | `MONETARY` | `us-gaap:OperatingIncomeLoss` |
| `OPERATING_INCOME_LEGACY_IAS1` | `INCOME_STATEMENT` | `DURATION` | `MONETARY` | `ifrs-full:ProfitLossFromOperatingActivities` (Legacy IAS 1) |
| `OPERATING_PROFIT_IFRS18` | `INCOME_STATEMENT` | `DURATION` | `MONETARY` | `ifrs-full:OperatingProfitLossOperating` (Standardized IFRS 18) |
| `NET_INCOME_ATTRIBUTABLE_TO_PARENT` | `INCOME_STATEMENT` | `DURATION` | `MONETARY` | `us-gaap:NetIncomeLoss`, `ifrs-full:ProfitLossAttributableToOwnersOfParent` |
| `NET_INCOME_INCLUDING_NCI` | `INCOME_STATEMENT` | `DURATION` | `MONETARY` | `us-gaap:ProfitLoss`, `ifrs-full:ProfitLoss` |
| `TOTAL_ASSETS` | `BALANCE_SHEET` | `INSTANT` | `MONETARY` | `us-gaap:Assets`, `ifrs-full:Assets` |
| `TOTAL_LIABILITIES` | `BALANCE_SHEET` | `INSTANT` | `MONETARY` | `us-gaap:Liabilities`, `ifrs-full:Liabilities` |
| `EQUITY_ATTRIBUTABLE_TO_PARENT` | `BALANCE_SHEET` | `INSTANT` | `MONETARY` | `us-gaap:StockholdersEquity`, `ifrs-full:EquityAttributableToOwnersOfParent` |
| `EQUITY_INCLUDING_NCI` | `BALANCE_SHEET` | `INSTANT` | `MONETARY` | `us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`, `ifrs-full:Equity` |
| `CURRENT_ASSETS` | `BALANCE_SHEET` | `INSTANT` | `MONETARY` | `us-gaap:AssetsCurrent`, `ifrs-full:CurrentAssets` |
| `CURRENT_LIABILITIES` | `BALANCE_SHEET` | `INSTANT` | `MONETARY` | `us-gaap:LiabilitiesCurrent`, `ifrs-full:CurrentLiabilities` |
| `CASH_AND_CASH_EQUIVALENTS` | `BALANCE_SHEET` | `INSTANT` | `MONETARY` | `us-gaap:CashAndCashEquivalentsAtCarryingValue`, `ifrs-full:CashAndCashEquivalents` |
| `OPERATING_CASH_FLOW` | `CASH_FLOW_STATEMENT` | `DURATION` | `MONETARY` | `us-gaap:NetCashProvidedByUsedInOperatingActivities`, `ifrs-full:CashFlowsFromUsedInOperatingActivities` |
| `CAPEX_PP&E` | `CASH_FLOW_STATEMENT` | `DURATION` | `MONETARY` | `us-gaap:PaymentsToAcquirePropertyPlantAndEquipment`, `ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities` |
| `CAPEX_PRODUCTIVE_ASSETS` | `CASH_FLOW_STATEMENT` | `DURATION` | `MONETARY` | `us-gaap:PaymentsToAcquireProductiveAssets` |
| `DILUTED_EPS` | `INCOME_STATEMENT` | `DURATION` | `MONETARY_PER_SHARE` | `us-gaap:EarningsPerShareDiluted`, `ifrs-full:DilutedEarningsLossPerShare` |
| `DILUTED_WEIGHTED_AVERAGE_SHARES` | `SHARE_DATA` | `DURATION` | `SHARES` | `us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding`, `ifrs-full:AdjustedWeightedAverageShares` |
| `SHARES_OUTSTANDING` | `SHARE_DATA` | `INSTANT` | `SHARES` | `dei:EntityCommonStockSharesOutstanding`, `us-gaap:CommonStockSharesOutstanding` |

### 9.4 Candidate Invariant (Candidate != Winner)

- `SECCanonicalFactCandidate` represents a validly mapped economic disclosure entry.
- All candidate entries from original filings, amendments (`10-K/A`), and comparative restatement columns are preserved simultaneously.
- No candidate is discarded or chosen as a single "winner" in Phase 8B.1 / 8B.1.6.

## 10. Phase 8B.2A / 8B.2A.6 — Economic Period Context Classification & Filing Association Proof

Phase 8B.2A / 8B.2A.6 deterministically classifies the economic period context of canonical candidates without picking filing winners.

### 10.1 Economic Period Kinds (`SECEconomicPeriodKind`)

- **`INSTANT`**: Balance sheet point-in-time financial observation.
- **`COVER_DATE_INSTANT`**: Cover page disclosure dated after the balance sheet report date (strictly `dei:EntityCommonStockSharesOutstanding`).
- **`ANNUAL_DURATION`**: Full fiscal year period (`330 <= duration_days <= 385` inclusive; supports 52/53-week fiscal years).
- **`QUARTER_DURATION`**: Standalone ~3-month fiscal quarter (`70 <= duration_days <= 115` inclusive).
- **`YTD_DURATION`**: Year-to-date interim period (`150 <= duration_days <= 210` for 6M, `240 <= duration_days <= 300` for 9M).
- **`IRREGULAR_DURATION`**: Non-standard transition, stub, or irregular duration periods.
- **`UNKNOWN`**: Insufficient or contradictory date evidence.

### 10.2 Period Alignment Status (`SECPeriodAlignmentStatus`)

- **`PRIMARY_REPORT_PERIOD`**: Current period ending on the containing filing's `report_date` (supported primary forms: `10-K`, `10-Q`, `20-F`, `40-F` and their amendments).
- **`COMPARATIVE_PRIOR_PERIOD`**: Prior comparative period disclosed alongside current results (`end_date < filing.report_date`).
- **`COVER_DATE_CONTEXT`**: Associated with filing cover date / subsequent disclosure.
- **`NON_PRIMARY_CONTEXT`**: Interim or event filings (`6-K`, `8-K`, `OTHER`, `10-KT`, `10-QT`) or non-primary context.
- **`UNRESOLVED_FILING`**: Classification based on candidate dates when containing filing report date is unlinked or lacks association proof.
- **`INSUFFICIENT_EVIDENCE`**: Missing start_date or end_date.
- **`INVALID_CONTEXT`**: Malformed dates (`start_date > end_date`) or filing vs candidate metadata mismatch (CIK, Accession, Filing ID, Form).

### 10.3 Fail-Closed Invariants & Filing Association Proof

1. **CIK ≠ Specific Filing Identity**: CIK proves entity identity; it does not prove specific filing identity. A supplied `SECFilingRecord` is trusted ONLY if explicit filing identity proof is established:
   - `candidate.accession_number == filing.accession_number` OR
   - `candidate.filing_id == filing.id`
   - If both `accession_number` and `filing_id` are absent on candidate, `filing.report_date` is ignored, and status defaults to `UNRESOLVED_FILING` (not `INVALID_CONTEXT`).
2. **No Fabricated Dates**: Missing `economic_end_date` is strictly `None` (no `1970-01-01` epoch sentinels).
3. **Ungroupable Isolation (`SECPeriodGroupingResult`)**: Candidates with `UNKNOWN`, `IRREGULAR_DURATION`, `INVALID_CONTEXT`, or `INSUFFICIENT_EVIDENCE` are preserved in the `ungroupable` list and never merged into a fake `UNKNOWN` group.
4. **Strict DEI Cover-Date Shares**: Only `dei:EntityCommonStockSharesOutstanding` with proven filing association receives `COVER_DATE_INSTANT` and `COVER_DATE_CONTEXT`. `us-gaap:CommonStockSharesOutstanding` does not.
5. **Filing vs Candidate Consistency**: Any proven discrepancy between candidate and supplied filing (CIK, accession, filing_id, or form) fails closed as `INVALID_CONTEXT`.

### 10.4 Economic Grouping Key

```
Economic Group Key = (cik, canonical_concept, unit, economic_period_kind, economic_start_date, economic_end_date)
```
- Grouping preserves all candidates (original filings, amendments `10-K/A`, and comparative disclosures) without picking a winner.

---

## 11. Phase 8B.2B — Snapshot-Scoped Winner Resolution & Restatement Reconciliation

Phase 8B.2B implements deterministic, auditable, and fail-closed winner resolution across multiple candidates in an economic group.

### 11.1 Resolution Modes (`SECWinnerResolutionMode`)

- **`CURRENT_REPORTED`**: Represents the current state of financial facts in Sentinax's latest locally observed valid full CompanyFacts snapshot for the target CIK.
- **`SYSTEM_AS_OF`**: Point-in-Time view answering *"What did Sentinax locally know at or before `as_of`?"* (evaluates the latest valid snapshot with `retrieved_at <= as_of`).
- **`SOURCE_AS_OF`**: Fails closed with `UNAVAILABLE_SOURCE_AS_OF` because SEC CompanyFacts does not provide exact historical external API state reconstruction and SEC acceptance timestamp is not exact first public availability.

### 11.2 Critical Invariants

1. **Strict Snapshot Membership & Isolation**: Candidates from different CompanyFacts snapshots are NEVER mixed in a single winner pool. Older snapshot facts are never resurrected if removed in the latest snapshot.
2. **Disclosure Chronology Hierarchy**: Timezone-aware UTC `acceptance_datetime` > SEC local naive `acceptance_local_datetime` > `filing_date` fallback. Accession lexicographical order, UUID, or database `created_at` are NEVER used as economic authority.
3. **Acceptance Time != First Public Availability**: Acceptance timestamps reflect internal SEC ingestion events, not exact SEC.gov public availability.
4. **Current View != Original Reported View**: `CURRENT_REPORTED` presents the most authoritative current restated figure (e.g. later comparative disclosure or amendment), not the original historical filing view.
5. **Fail-Closed Semantic Quality Conflicts**: A later disclosure with lower semantic quality (e.g. `COMPATIBLE` alias) cannot overwrite an earlier `EXACT` fact with a different value (`SEMANTIC_SCOPE_CONFLICT`).
6. **No Metric Calculation**: Phase 8B.2B strictly selects raw canonical facts. No YTD subtraction, no Q4 derivation, no TTM calculation, and no ratio arithmetic.







