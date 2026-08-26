# Global & Turkey Macroeconomic Data Layer

**Version:** 3.1 (2026 Euro Area EA21 & Treasury Hardened Release)  
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
| **Eurostat** | EA (`EA21`) | `TIER_1_REGULATORY` | SDMX 2.1 REST (CSV) | **VERIFIED** | `PUBLISHED_AT` | None (Open Web Service) | N/A (Euro Area Macro) |
| **U.S. Treasury** | US | `TIER_1_REGULATORY` | XML DataServices Feed | **VERIFIED** | `EFFECTIVE_DATE` | None (Open Web Service) | N/A (Sovereign Yields) |

---

## 2. Point-in-Time (PIT) Timestamp Taxonomy

To eliminate lookahead contamination and semantic confusion, Sentinax strictly separates these date/time concepts:

1. **Effective / Observation Date (`effective_date`):**
   - The economic period the measurement applies to (e.g. `2026-01-01` for Q1 2026 GDP, `2026-04-01` for April CPI).
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

### B. Policy Rate Frequency & Freshness Semantics
- **Event-Driven Nature:** Policy rates (Deposit Facility Rate `DFR`, Main Refinancing Operations `MRO`) are date-of-changes series (`MacroFrequency.EVENT_DRIVEN`).
- **Freshness Invariant:** An unchanged policy rate is valid until the next official Governing Council decision. `expected_release_interval_days = None` prevents false staleness penalties.
- **Daily Benchmarks:** €STR (`ESTR`) and EUR/USD reference rates remain daily (`MacroFrequency.BUSINESS_DAILY`) with `expected_release_interval_days = 1`.

### C. Verified Initial Series (Geography: `EA`)
1. `EA_EURUSD_REFERENCE_RATE` (`EXR/D.USD.EUR.SP00.A`): ECB Euro Foreign Exchange Reference Rate: US Dollar / Euro (`1 EUR = X USD`).
2. `EA_ECB_DEPOSIT_FACILITY_RATE` (`FM/D.U2.EUR.4F.KR.DFR.LEV`): Deposit Facility Rate (Key Policy Rate, %, `EVENT_DRIVEN`).
3. `EA_ECB_MAIN_REFINANCING_RATE` (`FM/D.U2.EUR.4F.KR.MRR_FR.LEV`): Main Refinancing Operations Rate (Fixed / Minimum Bid Rate, %, `EVENT_DRIVEN`).
4. `EA_ESTR` (`EST/B.EU000A2X2A25.WT`): Euro Short-Term Rate (€STR, %, `BUSINESS_DAILY`).

---

## 4. Eurostat Dissemination API (2026 Euro Area EA21 & HICP 2025=100)

### A. 2026 Euro Area Composition (`EA21`)
- **Bulgaria Accession:** On 1 January 2026, Bulgaria adopted the Euro. The Euro Area consists of **21 member states** (`EA21`).
- **Canonical Composition:** Current canonical Euro Area series use provider-native geography code `EA21` (`composition_member_count = 21`, `composition_valid_from = 2026-01-01`).

### B. 2026 HICP Reference Period & ECOICOP v2
- **Reference Base:** 2026 HICP index series use the common reference base **2025 = 100** (dimension `I25`).
- **Classification:** ECOICOP version 2 (`CP00` all-items).

### C. Frequency-Aware Period Formatter & Validation
- **Quarterly GDP:** Formats `effective_date` to `YYYY-Qn` (e.g. `2026-Q1`).
- **Monthly Series:** Formats to `YYYY-MM`.
- **Validation Guard:** The returned observation's `TIME_PERIOD` must match the requested formatted period string; otherwise, returns `UNAVAILABLE`.

### D. Verified Initial Series (Geography: `EA`, Provider Native: `EA21`)
1. `EA_HICP_ALL_ITEMS_INDEX` (`prc_hicp_midx/M.I25.CP00.EA21`): Harmonised Index of Consumer Prices (Index 2025=100, Euro Area 21).
2. `EA_HICP_ALL_ITEMS_YOY` (`prc_hicp_manr/M.RCH_A.CP00.EA21`): Harmonised Index of Consumer Prices (Annual Rate of Change, %, Euro Area 21).
3. `EA_UNEMPLOYMENT_RATE` (`une_rt_m/M.SA.TOTAL.PC_ACT.T.EA21`): Civilian Unemployment Rate (% of active population, SA, Euro Area 21).
4. `EA_REAL_GDP` (`namq_10_gdp/Q.CLV10_MNAC.SCA.B1GQ.EA21`): Real Gross Domestic Product (Chain-linked volumes 2010 Million EUR, SA, Euro Area 21).

---

## 5. U.S. Department of the Treasury Daily Yield Curve Feed

### A. Contract & Protocol
- **Official Authority:** U.S. Department of the Treasury.
- **Base Endpoint:** `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml`
- **Protocol:** Atom XML Feed with Microsoft OData DataServices (`data=daily_treasury_yield_curve`).
- **Authentication:** None (Public open feed).

### B. Single Curve Fetch & Fan-Out Architecture
- **Single Curve Row:** A single XML request fetches all tenors (`1M` to `30Y`) for the requested date/month.
- **Raw Snapshot Preservation:** Full raw XML text is preserved in `response.raw["xml_text"]` for audit.
- **Curve Fan-Out Helper:** `USTreasuryYieldCurveProvider.materialize_curve_observations()` produces observations for all 4 canonical tenors (`3M`, `2Y`, `10Y`, `30Y`) sharing the same raw `snapshot_id`.
- **No Silent Default:** Missing provider symbol fails fast as `UNAVAILABLE` without defaulting to 10Y.
- **No Spread Calculation:** Provider delivers pure raw yields; yield spreads (e.g. 10Y-2Y) are never computed by the provider.

### C. Methodology Break Preservation
- **2021-12-06 Transition:** On 6 December 2021, the U.S. Treasury transitioned from quasi-cubic Hermite spline interpolation to monotone convex spline interpolation. Historical values remain official and are preserved with methodology notes.

### D. Verified Initial Tenors (Geography: `US`)
1. `US_TREASURY_PAR_3M` (`BC_3MONTH`): 3-Month Daily Par Yield Rate (%).
2. `US_TREASURY_PAR_2Y` (`BC_2YEAR`): 2-Year Daily Par Yield Rate (%).
3. `US_TREASURY_PAR_10Y` (`BC_10YEAR`): 10-Year Benchmark Daily Par Yield Rate (%).
4. `US_TREASURY_PAR_30Y` (`BC_30YEAR`): 30-Year Daily Par Yield Rate (%).
