# SEC EDGAR Filing & Raw XBRL CompanyFacts Data Layer

**Version:** 1.1 (Phase 8A Hardened — Point-in-Time, Lineage & Integrity Specification)  
**Effective Date:** 26 August 2026  
**Scope:** Official SEC EDGAR Submissions, CompanyFacts APIs, CIK symbology, Point-in-Time acceptance vs public availability boundaries, append-only fact-filing linkage, and raw fact storage.

---

## 1. Overview & Data Sources Summary

| Source | Authority Level | Access Method | Contract Status | Rate Limit (Official / Safety) | Authentication | PIT Knowledge Boundary |
|---|---|---|---|---|---|---|
| **SEC EDGAR Submissions** | `TIER_1_REGULATORY` | REST API (JSON) | **VERIFIED** | 10 req/s / **<= 8 req/s** | Declared `User-Agent` | `acceptance_datetime` (Acceptance Event) |
| **SEC EDGAR CompanyFacts** | `TIER_1_REGULATORY` | REST API (JSON) | **VERIFIED** | 10 req/s / **<= 8 req/s** | Declared `User-Agent` | Linked `acceptance_datetime` via Link Table |
| **SEC Ticker Mapping** | `TIER_1_REGULATORY` | Static JSON Feed | **CANDIDATE ONLY** | 10 req/s / **<= 8 req/s** | Declared `User-Agent` | Non-authoritative candidate suggestion |

---

## 2. Official SEC Endpoints

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

## 3. CIK & Symbology Semantics

- **CIK Definition:** Central Index Key is an issuer-level numerical identifier assigned by the SEC.
- **Canonical Storage:** 10-digit zero-padded string (`zfill(10)` e.g. `"0000320193"`). Stored with DB check constraint `CHECK (cik ~ '^[0-9]{10}$')`.
- **Security vs Issuer Distinction:**
  - `instruments.id` (UUID) is Sentinax's **single canonical security-level identity**.
  - `instruments.cik` (VARCHAR(10)) represents the **issuer**.
  - Multiple share classes (e.g. GOOG Class C vs GOOGL Class A) or dual-listed securities share the same issuer CIK. Therefore, CIK is indexed but **not unique** across `instruments`.

---

## 4. Accession Number Semantics & Third-Party Filers

- **Accession Format:** 20-character hyphenated identifier: `0000320193-24-000106` (regex: `^[0-9]{10}-[0-9]{2}-[0-9]{6}$`).
- **Submitting Entity CIK Prefix:** The first 10 digits of an accession number indicate the **submitting entity CIK**. This can be a third-party filing agent, legal advisor, or parent company (e.g. `0001140361-24-024352` for Apple Inc.).
- **Invariant:** Sentinax does **NOT** require `accession[:10] == issuer_cik`. Archive directory URLs are constructed using the subject issuer's CIK.

---

## 5. Acceptance Timestamp vs Public Availability (Critical PIT Distinction)

- **`acceptance_datetime`:** The exact timestamp when EDGAR accepted and timestamped the transmission.
  - Parsed with strict timezone semantics (`EXPLICIT_UTC`, `EXPLICIT_OFFSET`, or `EDGAR_LOCAL_UNSPECIFIED`).
  - Timezone-less strings are **NEVER** silently coerced to UTC.
  - Date-only strings are **NEVER** fabricated into midnight UTC.
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
- **Amendments:** Filings with `/A` suffix produce new accession-scoped facts. Previous facts for the same economic period are preserved without overwriting.

---

## 7. Append-Only Fact-Filing Linkage (`sec_fact_filing_links`)

- When CompanyFacts contains facts whose filing accession has not yet been ingested into `sec_filings`, the fact is stored with `filing_id = None` (unresolved status) and **NEVER dropped**.
- When the corresponding historical filing is ingested later, a new linkage row is inserted into `sec_fact_filing_links` (`fact_id`, `filing_id`, `accession_number`, `cik`).
- Because `sec_raw_facts` is strictly immutable, the raw fact row is **never modified or updated**.

---

## 8. Fair Access & Leaky Pacing Rate Limiter

- **Declared User-Agent:** Mandated by SEC fair access policy (`SEC_USER_AGENT` environment variable). Missing agent raises `ProviderConfigurationError`.
- **Serialized Leaky Pacing:** `SECRateLimiter` enforces strict serialized spacing: `min_interval = 1.0 / rate` (e.g. `0.125s` for 8 req/s; burst capacity = 1) to eliminate initial multi-request bursts.
- **Process-Wide Shared Limiter:** All `SECEdgarClient` instances, Submissions, CompanyFacts, and Ticker Discovery calls share a single singleton limiter.

---

## 9. Boundary to Phase 8B

Phase 8A provides the **hardened, Point-in-Time, append-only raw fact backbone**.  
Financial concept standardization (e.g. mapping `Revenues` vs `SalesRevenueNet`, computing TTM, FCF, ROIC, EPS, Margins, or Restatement Reconciliation) is strictly deferred to **Phase 8B**.
