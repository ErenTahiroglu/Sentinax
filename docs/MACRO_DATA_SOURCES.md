# Global & Turkey Macroeconomic Data Layer

**Version:** 2.1 (PIT Semantic & Provenance Hardened)  
**Effective Date:** 26 August 2026  
**Scope:** Macroeconomic data sources, Point-in-Time (PIT) vintage semantics, authentication, contract verification, and canonical registry for Sentinax Private Engine.

---

## 1. Overview & Data Sources

| Source | Geography | Authority Level | Access Method | Contract Status | Freshness Basis | Secret Requirement | Tax Indexation Eligible? |
|---|---|---|---|---|---|---|---|
| **TCMB EVDS** | TR | `TIER_1_REGULATORY` | REST API (JSON) | **VERIFIED (EVDS2)** | `EFFECTIVE_DATE` | `TCMB_EVDS_API_KEY` (Header `key`) | N/A (FX / Funding Rates) |
| **TÜİK SDMX** | TR | `TIER_1_REGULATORY` | SDMX 2.1 REST API | **UNVERIFIED (YELLOW)** | `PUBLISHED_AT` | None (Open Web Service) | **YES** (Yİ-ÜFE Only, once verified) |
| **ENAG Manual** | TR | `TIER_3_AGGREGATOR` | Manual Ingestion | **VERIFIED (MANUAL)** | `PUBLISHED_AT` | None (Audit Trail) | **NO** (Strictly Prohibited) |
| **FRED / ALFRED** | US | `TIER_1_REGULATORY` | REST API v1 (JSON) | **VERIFIED** | `PUBLISHED_AT` / `EFFECTIVE_DATE` | `FRED_API_KEY` (Query `api_key`) | N/A (Global Macro) |

---

## 2. Point-in-Time (PIT) Timestamp & Date Taxonomy

To eliminate lookahead contamination and semantic confusion, Sentinax strictly separates these date/time concepts:

1. **Effective / Observation Date (`effective_date`):**
   - The economic period the measurement applies to (e.g. `2023-01-01` for Q1 2023 GDP, `2024-04-01` for April CPI).
2. **Requested Vintage Snapshot Date (`vintage_date`):**
   - The as-of date requested from ALFRED (`vintage_dates=YYYY-MM-DD`). Represents "what was known on this calendar date".
3. **FRED Real-Time Period (`realtime_start` / `realtime_end`):**
   - The observation's validity window in the FRED database for the given query. In live current queries, even 1990 data carries `realtime_start = Today`.
   - *CRITICAL INVARIANT:* `realtime_start` is **NOT** the date when data first became public knowledge.
4. **Actual Source Availability Date (`source_available_date`):**
   - The proven date when the observation/revision became public knowledge. If unproven, remains `None` (missing != fabricated).
5. **Release Calendar Date (`release_name` / calendar context):**
   - The statistical agency's planned announcement date. Does not guarantee exact release time or same-day ALFRED availability.
6. **Retrieval Time (`retrieved_at`):**
   - Wall-clock UTC timestamp when Sentinax executed the HTTP request.
7. **Ingestion Time (`ingested_at` / `observed_at`):**
   - Wall-clock UTC timestamp when Sentinax recorded the observation in local PIT storage (`SYSTEM_AS_OF` boundary).

---

## 3. FRED / ALFRED Unified Adapter (United States)

### A. Architectural Concept
- **Unified Engine:** St. Louis Fed FRED (Current) and ALFRED (Vintage Point-in-Time) operate on the **same underlying API (Version 1)**. `FREDALFREDProvider` provides a single unified interface supporting both `CURRENT` observations and `SOURCE_VINTAGE` historical revisions.
- **Base Endpoint:** `https://api.stlouisfed.org/fred/`
- **Security:** `api_key` is passed as a query parameter as required by official FRED API v1 specifications.
  - *Invariant:* The API key is stripped from cache keys, logs, raw snapshots, diagnostics, and exceptions.

### B. Execution Modes & Query Bounding
1. **Current Mode (FRED):**
   - Fetches latest single observation with `sort_order=desc`, `limit=1`, `output_type=1`, `units=lin`.
   - Prevents fetching 100,000 unbounded historical rows on live refresh.
2. **Vintage Mode (ALFRED):**
   - Point-in-Time requests with `as_of_mode == AsOfMode.SOURCE_AS_OF`.
   - Passes `vintage_dates=YYYY-MM-DD(snapshot_date)` with `sort_order=desc`, `limit=1`.
3. **SYSTEM_AS_OF Historical Guard:**
   - Historical queries with `as_of_mode == AsOfMode.SYSTEM_AS_OF` are rejected at the external provider boundary with a clear diagnostic (`"Historical SYSTEM_AS_OF requires local PIT storage"`).
4. **Fail-Closed Historical Handling:**
   - Any unknown or unhandled `AsOfMode` fails closed immediately.

### C. Same-Day Lookahead Policy
- Because FRED/ALFRED vintage precision is **DATE-level**, an intraday query at 09:30 AM could leak an afternoon revision into backtests.
- *Conservative Default:* If `as_of_time` is intraday, Sentinax queries `vintage_dates = as_of_date - 1 calendar day` (prior-day knowledge snapshot) unless exact same-day vintage is explicitly requested.

### D. Origin-Source Distinction & Provenance
FRED is the delivery aggregator; originating statistical authorities are preserved in `ProviderProvenance.metadata`:
- `CPIAUCSL`, `CPILFESL`, `UNRATE` -> **U.S. Bureau of Labor Statistics (BLS)**
- `GDPC1` -> **U.S. Bureau of Economic Analysis (BEA)**
- `INDPRO`, `DFF` -> **Board of Governors of the Federal Reserve System**

### E. Verified Initial US Registry
1. `US_CPI_HEADLINE_INDEX` (`CPIAUCSL`): Headline Consumer Price Index (Index 1982-1984=100, SA).
2. `US_CPI_CORE_INDEX` (`CPILFESL`): Core Consumer Price Index Less Food & Energy (Index 1982-1984=100, SA).
3. `US_UNEMPLOYMENT_RATE` (`UNRATE`): Civilian Unemployment Rate (Percent, SA).
4. `US_REAL_GDP` (`GDPC1`): Real Gross Domestic Product (Billions of Chained 2017 Dollars, SAAR).
5. `US_INDUSTRIAL_PRODUCTION` (`INDPRO`): Industrial Production Index (Index 2017=100, SA).
6. `US_EFFECTIVE_FED_FUNDS_RATE` (`DFF`): Effective Federal Funds Rate (Percent, NSA).

*(Note: U.S. Treasury yield curve series such as DGS10/DGS2 are intentionally excluded from this phase).*

---

## 4. Turkey Official Macroeconomic Sources

### A. TCMB EVDS
- **EVDS3 Transition:** TCMB opened EVDS3 Beta on **26 January 2026**. EVDS2 remains fully active and supported.
- **Verified Series:** `TR_FX_USDTRY`, `TR_FX_EURTRY`, `TR_TCMB_AOFM` (`TP.APIFON4` - Weighted Average Funding Cost).
- **Policy Rate:** Statutory 1-week repo policy rate code in EVDS is `UNVERIFIED` and disabled until officially confirmed.

### B. TÜİK SDMX
- **Status:** `ProviderAccessStatus.YELLOW`. Guessed dataflows are disabled pending official catalog discovery.

### C. Manual ENAG
- **Status:** Manual verified ingestion (`PENDING` -> `VERIFIED`). Overwrites are strictly prohibited; revisions require `supersedes_record_id`.
- **Constraint:** Strictly prohibited from tax indexation (tax indexation strictly requires TÜİK Yİ-ÜFE).
