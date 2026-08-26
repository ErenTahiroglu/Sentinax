# Global & Turkey Macroeconomic Data Layer

**Version:** 3.0 (Global Multi-Provider Release)  
**Effective Date:** 26 August 2026  
**Scope:** Macroeconomic data sources, Point-in-Time (PIT) vintage semantics, authentication, contract verification, and canonical registry for Sentinax Private Engine.

---

## 1. Overview & Data Sources Summary

| Source | Geography | Authority Level | Access Method | Contract Status | Freshness Basis | Secret Requirement | Tax Indexation Eligible? |
|---|---|---|---|---|---|---|---|
| **TCMB EVDS** | TR | `TIER_1_REGULATORY` | REST API (JSON) | **VERIFIED (EVDS2)** | `EFFECTIVE_DATE` | `TCMB_EVDS_API_KEY` (Header `key`) | N/A (FX / Funding Rates) |
| **TÜİK SDMX** | TR | `TIER_1_REGULATORY` | SDMX 2.1 REST API | **UNVERIFIED (YELLOW)** | `PUBLISHED_AT` | None (Open Web Service) | **YES** (Yİ-ÜFE Only, once verified) |
| **ENAG Manual** | TR | `TIER_3_AGGREGATOR` | Manual Ingestion | **VERIFIED (MANUAL)** | `PUBLISHED_AT` | None (Audit Trail) | **NO** (Strictly Prohibited) |
| **FRED / ALFRED** | US | `TIER_1_REGULATORY` | REST API v1 (JSON) | **VERIFIED** | `PUBLISHED_AT` / `EFFECTIVE_DATE` | `FRED_API_KEY` (Query `api_key`) | N/A (Global Macro) |
| **ECB Data Portal** | EA | `TIER_1_REGULATORY` | SDMX 2.1 REST (CSV) | **VERIFIED** | `EFFECTIVE_DATE` | None (Open Web Service) | N/A (Euro Area Macro) |
| **Eurostat** | EA | `TIER_1_REGULATORY` | SDMX 2.1 REST (CSV) | **VERIFIED** | `PUBLISHED_AT` | None (Open Web Service) | N/A (Euro Area Macro) |
| **U.S. Treasury** | US | `TIER_1_REGULATORY` | XML DataServices Feed | **VERIFIED** | `EFFECTIVE_DATE` | None (Open Web Service) | N/A (Sovereign Yields) |

---

## 2. Point-in-Time (PIT) Timestamp Taxonomy

To eliminate lookahead contamination and semantic confusion, Sentinax strictly separates these date/time concepts:

1. **Effective / Observation Date (`effective_date`):**
   - The economic period the measurement applies to (e.g. `2023-01-01` for Q1 2023 GDP, `2024-04-01` for April CPI).
2. **Requested Vintage Snapshot Date (`vintage_date`):**
   - The as-of date requested from ALFRED (`vintage_dates=YYYY-MM-DD`). Represents "what was known on this calendar date".
3. **FRED / SDMX Real-Time Period (`realtime_start` / `realtime_end`):**
   - The observation's validity window in the provider database for the given query. In live current queries, even 1990 data carries `realtime_start = Today`.
   - *CRITICAL INVARIANT:* `realtime_start` is **NOT** the date when data first became public knowledge.
4. **Actual Source Availability Date (`source_available_date`):**
   - The proven date when the observation/revision became public knowledge. If unproven, remains `None` (missing != fabricated).
5. **Release Calendar Date (`release_name` / calendar context):**
   - The statistical agency's planned announcement date. Does not guarantee exact release time.
6. **Retrieval Time (`retrieved_at`):**
   - Wall-clock UTC timestamp when Sentinax executed the HTTP request.
7. **Ingestion Time (`ingested_at` / `observed_at`):**
   - Wall-clock UTC timestamp when Sentinax recorded the observation in local PIT storage (`SYSTEM_AS_OF` boundary).

---

## 3. European Central Bank (ECB) Data Portal

### A. Contract & Protocol
- **Official Authority:** European Central Bank (ECB).
- **Base Endpoint:** `https://data-api.ecb.europa.eu/service/`
- **Protocol:** SDMX 2.1 RESTful Web Service.
- **Format:** SDMX-CSV (`format=csvdata`).
- **Authentication:** None (Public open API).

### B. Execution Modes & Query Bounding
- **Current Mode:** Bounded single latest observation via `lastNObservations=1`.
- **Historical Query:** Bounded by `startPeriod` and `endPeriod`.
- **PIT Limitations:** External `SOURCE_AS_OF` and `SYSTEM_AS_OF` return `UNAVAILABLE` (`"ECB historical SOURCE_AS_OF requires local PIT storage"`).

### C. Verified Initial Series (Geography: `EA`)
1. `EA_EURUSD_REFERENCE_RATE` (`EXR/D.USD.EUR.SP00.A`): ECB Euro Foreign Exchange Reference Rate: US Dollar / Euro (`1 EUR = X USD`).
2. `EA_ECB_DEPOSIT_FACILITY_RATE` (`FM/D.U2.EUR.4F.KR.DFR.LEV`): Deposit Facility Rate (Key Policy Rate, %).
3. `EA_ECB_MAIN_REFINANCING_RATE` (`FM/D.U2.EUR.4F.KR.MRR_FR.LEV`): Main Refinancing Operations Rate (Fixed / Minimum Bid Rate, %).
4. `EA_ESTR` (`EST/B.EU000A2X2A25.WT`): Euro Short-Term Rate (€STR, %).

---

## 4. Eurostat Dissemination API

### A. Contract & Protocol
- **Official Authority:** Eurostat (Statistical Office of the European Union).
- **Base Endpoint:** `https://ec.europa.eu/eurostat/api/dissemination/`
- **Protocol:** SDMX 2.1 REST Dissemination Service (`/sdmx/2.1/data/{flowRef}/{key}?format=SDMX-CSV`).
- **Authentication:** None (Public open API).

### B. Execution Modes & PIT Reality
- **Current Mode:** Bounded by `lastNObservations=1`.
- **Historical Query:** Bounded by `startPeriod` and `endPeriod` (e.g. `YYYY-MM`).
- **PIT Limitations:** Eurostat dissemination API does not support past vintage reconstruction. `SOURCE_AS_OF` and `SYSTEM_AS_OF` fail closed as `UNAVAILABLE` requiring local PIT storage.

### C. Verified Initial Series (Geography: `EA`, Provider Native: `EA20`)
1. `EA_HICP_ALL_ITEMS_INDEX` (`prc_hicp_midx/M.I15.CP00.EA20`): Harmonised Index of Consumer Prices (Index 2015=100).
2. `EA_HICP_ALL_ITEMS_YOY` (`prc_hicp_manr/M.RCH_A.CP00.EA20`): Harmonised Index of Consumer Prices (Annual Rate of Change, %).
3. `EA_UNEMPLOYMENT_RATE` (`une_rt_m/M.SA.TOTAL.PC_ACT.T.EA20`): Civilian Unemployment Rate (% of active population, SA).
4. `EA_REAL_GDP` (`namq_10_gdp/Q.CLV10_MNAC.SCA.B1GQ.EA20`): Real Gross Domestic Product (Chain-linked volumes 2010 Million EUR, SA).

---

## 5. U.S. Department of the Treasury Daily Yield Curve Feed

### A. Contract & Protocol
- **Official Authority:** U.S. Department of the Treasury.
- **Base Endpoint:** `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml`
- **Protocol:** Atom XML Feed with Microsoft OData DataServices (`data=daily_treasury_yield_curve`).
- **Authentication:** None (Public open feed).

### B. Single Curve Fetch Model & Bounded Queries
- **Current / Month Query:** Fetches monthly feed `field_tdr_date_value_month=YYYYMM` (~20 rows) and selects the latest date row.
- **Exact Date Query:** Selects the exact date entry in the month feed. If not found, returns `UNAVAILABLE` (no automatic forward-filling).
- **All Tenors in Single Fetch:** A single XML response contains all tenors (`1M`, `2M`, `3M`, `4M`, `6M`, `1Y`, `2Y`, `3Y`, `5Y`, `7Y`, `10Y`, `20Y`, `30Y`).
- **No Spread Calculation:** Provider delivers pure raw yields; yield spreads (e.g. 10Y-2Y) are never computed by the provider.

### C. Methodology Break Preservation
- **2021-12-06 Transition:** On 6 December 2021, the U.S. Treasury transitioned from quasi-cubic Hermite spline interpolation to monotone convex spline interpolation. Historical values remain official and are preserved with methodology notes.

### D. Verified Initial Tenors (Geography: `US`)
1. `US_TREASURY_PAR_3M` (`BC_3MONTH`): 3-Month Daily Par Yield Rate (%).
2. `US_TREASURY_PAR_2Y` (`BC_2YEAR`): 2-Year Daily Par Yield Rate (%).
3. `US_TREASURY_PAR_10Y` (`BC_10YEAR`): 10-Year Benchmark Daily Par Yield Rate (%).
4. `US_TREASURY_PAR_30Y` (`BC_30YEAR`): 30-Year Daily Par Yield Rate (%).

---

## 6. St. Louis Fed FRED / ALFRED (United States)
- See Section 3 of previous version for detailed FRED/ALFRED architecture and ALFRED vintage mode.

---

## 7. Turkey Official Sources (TCMB EVDS, TÜİK SDMX, ENAG)
- See Section 4 of previous version for detailed EVDS2 and manual ENAG architecture.
