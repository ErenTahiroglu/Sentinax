# SEC EDGAR Filing & Raw XBRL CompanyFacts Data Layer

**Version:** 1.0 (Phase 8A — Ingestion, Identity, Provenance & Raw Fact Backbone)  
**Effective Date:** 26 August 2026  
**Scope:** Official SEC EDGAR Submissions, CompanyFacts APIs, CIK symbology, Point-in-Time acceptance boundaries, and raw fact storage.

---

## 1. Overview & Data Sources Summary

| Source | Authority Level | Access Method | Contract Status | Rate Limit (Official / Safety) | Authentication | PIT Knowledge Boundary |
|---|---|---|---|---|---|---|
| **SEC EDGAR Submissions** | `TIER_1_REGULATORY` | REST API (JSON) | **VERIFIED** | 10 req/s / **<= 8 req/s** | Declared `User-Agent` | `acceptance_datetime` |
| **SEC EDGAR CompanyFacts** | `TIER_1_REGULATORY` | REST API (JSON) | **VERIFIED** | 10 req/s / **<= 8 req/s** | Declared `User-Agent` | Linked `acceptance_datetime` |
| **SEC Ticker Mapping** | `TIER_1_REGULATORY` | Static JSON Feed | **CANDIDATE ONLY** | 10 req/s / **<= 8 req/s** | Declared `User-Agent` | Non-authoritative suggestion |

---

## 2. Official SEC Endpoints

Base URL: `https://data.sec.gov`

1. **Submissions API:**
   - URL: `https://data.sec.gov/submissions/CIK##########.json`
   - Purpose: Master filer metadata and columnar array of recent filings (~1,000 filings / 1+ years).
   - Secondary Files: Additional historical files referenced in `payload["filings"]["files"]` (e.g. `CIK0000320193-submissions-001.json`).

2. **CompanyFacts API:**
   - URL: `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`
   - Purpose: Aggregation of all non-custom XBRL disclosures for an issuer (`us-gaap`, `dei`, `ifrs-full`, `srt`).

3. **Ticker-to-CIK Exchange Mapping:**
   - URL: `https://www.sec.gov/files/company_tickers_exchange.json`
   - Purpose: Discovery of candidate CIKs for US ticker symbols. Note: The SEC explicitly disclaims guaranteed accuracy or completeness of this mapping file.

---

## 3. CIK Semantics & Instrument Master Integration

- **CIK Definition:** Central Index Key is an issuer-level numerical identifier assigned by the SEC.
- **Canonical Storage:** 10-digit zero-padded string (`zfill(10)` e.g. `"0000320193"`). Integer conversion is prohibited to prevent loss of leading zeros.
- **Issuer vs Security Distinction:**
  - `instruments.id` (UUID) is Sentinax's **single canonical security-level identity**.
  - `instruments.cik` (VARCHAR(10)) represents the **issuer**.
  - Multiple share classes (e.g. GOOG Class C vs GOOGL Class A) or dual-listed securities share the same issuer CIK. Therefore, CIK is indexed but **not unique** across `instruments`.

---

## 4. Fair Access & User-Agent Policy

- **Declared User-Agent:** SEC automated access policy strictly mandates a declared `User-Agent` in the format `Sample Company Name AdminContact@<sample company domain>.com`.
- **Environment Configuration:** Configured via `SEC_USER_AGENT` environment variable. If missing, the SEC provider fails gracefully (`UNAVAILABLE` / configuration diagnostic) without crashing application boot.
- **Fair Access Limits:**
  - SEC Official Limit: Total <= 10 requests / second.
  - Sentinax Safety Limit: Token-bucket limiter enforced at <= 8 requests / second (`DEFAULT_SENTINAX_SAFETY_RPS = 8.0`).
- **HTTP Headers:** Every request includes `User-Agent`, `Accept-Encoding: gzip, deflate`, and `Accept: application/json`.

---

## 5. Submissions Contract & Acceptance Point-In-Time (PIT) Boundary

- **Filing Identity:** `accession_number` in official hyphenated format (e.g. `0000320193-24-000123`) is the unique filing identifier (`sec_filings.accession_number UNIQUE`).
- **Timestamp Taxonomy:**
  - `report_date`: Economic accounting period end date (e.g. `2023-12-31`).
  - `filing_date`: Official calendar filing date (e.g. `2024-02-01`).
  - `acceptance_datetime`: **Official Point-In-Time knowledge boundary** when EDGAR accepted and timestamped the transmission (e.g. `2024-02-01 16:05:34 UTC`).
- **Amendments:** Filings with `/A` suffix (e.g. `10-K/A`) are marked `is_amendment = true`. They are stored as separate filing records and never overwrite original filings.
- **Columnar Array Validation:** All parallel arrays in `payload["filings"]["recent"]` must have identical lengths. Any length mismatch raises `ProviderSchemaError`.

---

## 6. CompanyFacts Contract & Raw Fact Taxonomy

- **Taxonomy Scope:** CompanyFacts aggregates standard public taxonomies:
  - `us-gaap` (U.S. GAAP disclosures)
  - `dei` (Document and Entity Information)
  - `ifrs-full` (IFRS disclosures for foreign private issuers)
  - `srt` (SEC Reporting Taxonomy)
- **Custom Taxonomy Limitation:** Company extension taxonomies (custom issuer tags) are not included in CompanyFacts. Missing facts in CompanyFacts do not mean the company disclosed nothing.
- **Period Types:**
  - `PeriodType.DURATION`: Facts spanning a period with `start` and `end` dates (e.g. Income Statement, Cash Flow).
  - `PeriodType.INSTANT`: Facts measured at a single point in time with `end` date only (e.g. Balance Sheet).
- **Precision & Units:**
  - `value`: Numerical precision is preserved (NUMERIC in database). Genuine `0.0` is preserved; missing values are `None`.
  - `unit`: Preserved verbatim (`USD`, `shares`, `pure`, `USD/shares`).
- **Lineage:** Facts carry `accn` which links directly to `sec_filings.accession_number` -> `sec_filings.id` and the corresponding `acceptance_datetime`.

---

## 7. Point-In-Time (PIT) & Aggregation Invariants

- **Current Aggregate Nature:** The CompanyFacts API returns SEC's current aggregate view today.
- **Fail-Closed PIT Invariant:** External historical `SOURCE_AS_OF` and `SYSTEM_AS_OF` queries are rejected (`UNAVAILABLE`). True historical point-in-time reconstruction requires local immutable raw snapshots.
- **Post-Acceptance Correction Protection:** Raw API responses are stored immutably in `raw_provider_snapshots` with SHA-256 payload hashes.

---

## 8. Boundary to Phase 8B

Phase 8A is strictly limited to **Ingestion, Identity, Lineage, and Raw Fact Storage**.  
Semantic metric mapping (e.g. standardizing `Revenues` vs `SalesRevenueNet`, computing TTM, FCF, ROIC, EPS, Margins, or Restatement Reconciliation) is deferred entirely to **Phase 8B**.
