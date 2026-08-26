# Global & Turkey Macroeconomic Data Layer

**Version:** 2.0 (Global Hardened)  
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

## 2. FRED / ALFRED Unified Adapter (United States)

### A. Architectural Concept
- **Unified Engine:** St. Louis Fed FRED (Current) and ALFRED (Vintage Point-in-Time) operate on the **same underlying API (Version 1)**. Rather than creating duplicate adapters, `FREDALFREDProvider` provides a single unified interface supporting both `CURRENT` observations and `SOURCE_VINTAGE` historical revisions.
- **Base Endpoint:** `https://api.stlouisfed.org/fred/`
- **Security:** `api_key` is passed as a query parameter as required by official FRED API v1 specifications.
  - *Invariant:* The API key is stripped from cache keys, logs, raw snapshots, diagnostics, and exceptions.

### B. Execution Modes
1. **Current Mode (FRED):**
   - Fetches latest available observations from `series/observations` with `units=lin` (enforces raw linear levels; server-side aggregations/transformations are strictly avoided).
2. **Vintage Mode (ALFRED):**
   - Point-in-Time requests with `as_of_mode == "SOURCE_AS_OF"` pass `vintage_dates=YYYY-MM-DD(as_of_time)`.
   - Returns the exact revision that was public knowledge on the requested date. Future revisions are strictly prevented from leaking into historical queries.
3. **SYSTEM_AS_OF Historical Guard:**
   - Historical queries with `as_of_mode == "SYSTEM_AS_OF"` are rejected at the external provider boundary with a clear diagnostic (`"Historical SYSTEM_AS_OF requires local PIT storage"`).

### C. Origin-Source Distinction & Provenance
FRED is the delivery aggregator; the originating statistical authorities are distinct:
- `CPIAUCSL`, `CPILFESL`, `UNRATE` -> **U.S. Bureau of Labor Statistics (BLS)**
- `GDPC1` -> **U.S. Bureau of Economic Analysis (BEA)**
- `INDPRO`, `DFF` -> **Board of Governors of the Federal Reserve System**

### D. Availability & Lookahead Semantics
- `realtime_start` provides **DATE-level precision** (e.g. `2024-05-15`).
- *Conservative Rule:* Date-only availability cannot guarantee intraday timing. An observation is safe for backtesting if `source_available_date < as_of_date`. If exact `published_at` timestamp is known, `published_at <= as_of_time` is used.
- *Missing Marker:* FRED missing marker `"."` is strictly normalized to `None`. Zero values (`0.0`, `0`) are preserved.

### E. Verified Initial US Registry
1. `US_CPI_HEADLINE_INDEX` (`CPIAUCSL`): Headline Consumer Price Index (Index 1982-1984=100, SA).
2. `US_CPI_CORE_INDEX` (`CPILFESL`): Core Consumer Price Index Less Food & Energy (Index 1982-1984=100, SA).
3. `US_UNEMPLOYMENT_RATE` (`UNRATE`): Civilian Unemployment Rate (Percent, SA).
4. `US_REAL_GDP` (`GDPC1`): Real Gross Domestic Product (Billions of Chained 2017 Dollars, SAAR).
5. `US_INDUSTRIAL_PRODUCTION` (`INDPRO`): Industrial Production Index (Index 2017=100, SA).
6. `US_EFFECTIVE_FED_FUNDS_RATE` (`DFF`): Effective Federal Funds Rate (Percent, NSA).

*(Note: U.S. Treasury yield curve series such as DGS10/DGS2 are intentionally excluded from this phase and will be added in a dedicated official Treasury layer).*

---

## 3. Turkey Official Macroeconomic Sources

### A. TCMB EVDS
- **EVDS3 Transition:** TCMB opened EVDS3 Beta on **26 January 2026**. EVDS2 remains fully active and supported.
- **Verified Series:** `TR_FX_USDTRY`, `TR_FX_EURTRY`, `TR_TCMB_AOFM` (`TP.APIFON4` - Weighted Average Funding Cost).
- **Policy Rate:** Statutory 1-week repo policy rate code in EVDS is `UNVERIFIED` and disabled until officially confirmed.

### B. TÜİK SDMX
- **Status:** `ProviderAccessStatus.YELLOW`. Guessed dataflows are disabled pending official catalog discovery.

### C. Manual ENAG
- **Status:** Manual verified ingestion (`PENDING` -> `VERIFIED`). Overwrites are strictly prohibited; revisions require `supersedes_record_id`.
- **Constraint:** Strictly prohibited from tax indexation (tax indexation strictly requires TÜİK Yİ-ÜFE).
