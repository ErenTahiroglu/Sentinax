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

## 9. Boundary to Phase 8B

Phase 8A provides the **hardened, Point-in-Time, entity-level raw fact backbone**.  
Financial concept standardization (e.g. mapping `Revenues` vs `SalesRevenueNet`, computing TTM, FCF, ROIC, EPS, Margins, or Restatement Reconciliation) is strictly deferred to **Phase 8B**.
