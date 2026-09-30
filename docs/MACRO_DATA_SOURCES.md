# Global & Turkey Macroeconomic Data Layer

**Version:** 3.1 (2026 Euro Area EA21 & Treasury Hardened Release)  
**Effective Date:** 26 August 2026  
**Scope:** Macroeconomic data sources, Point-in-Time (PIT) vintage semantics, authentication, contract verification, and canonical registry for Sentinax Private Engine.

---

## 1. Overview & Data Sources Summary

| Source | Geography | Authority Level | Access Method | Contract Status | Freshness Basis | Secret Requirement | Tax Indexation Eligible? |
|---|---|---|---|---|---|---|---|
| **TCMB EVDS** | TR | `TIER_1_REGULATORY` | REST API (JSON) | **VERIFIED (EVDS3 transport, provider 1.1.0)** | `EFFECTIVE_DATE` | `TCMB_EVDS_API_KEY` (Header `key`) | N/A (FX / Funding Rates) |
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

## 6. Phase 17A — PIT-Safe Exact-Decimal Macro State Input Authority

Phase 17 is the macro + technical roadmap. The continuous macro state itself (GrowthImpulse, PolicyInflationState, FinancialStress) is NOT implemented yet. Phase 17A adds only the safe analytical input boundary, `backend/engine/private/macro/state_inputs.py` (`MacroStateInputFact`, `build_macro_state_input_fact`), which future macro calculations must consume.

- **Exact Decimal only.** `value` is an exact `Decimal` or `None`; float, int, bool, str, Fraction and Decimal subclasses are rejected, and non-finite Decimals are rejected. There is no float-to-Decimal rehabilitation: `Decimal(str(float_value))` would only spell an already rounded binary float, so the legacy `MacroObservationRecord.value: float` provider layer is not accepted as analytical authority. That legacy layer (and its G4 baseline entries) is unchanged.
- **Missing is not zero.** `None` is missing. COMPLETE requires a finite Decimal; UNAVAILABLE requires `None`; PARTIAL, DEGRADED and STALE describe existing data and still require an observed Decimal. A present `Decimal("0")` is an observed zero.
- **Registry authority.** `canonical_key` must resolve through `MacroSeriesRegistry.get` to an active, VERIFIED series, with no aliasing or normalization. Category, unit, frequency, geography and provider are derived from the registry definition, never supplied by the caller. Unverified or inactive series (such as `TR_CPI_TUIK_YOY`) fail closed. At Phase 17A/17B creation time `TR_POLICY_RATE` was also unverified; Phase 17H later verified and activated it, and it is never mapped to `TR_TCMB_AOFM`, which is not the policy rate.
- **Explicit PIT.** `SYSTEM_AS_OF` requires `ingested_at <= as_of` and `published_at` null or `<= as_of`. `SOURCE_AS_OF` requires `coalesce(published_at, observed_at) <= as_of`. In both modes `superseded_at` must be null or strictly after `as_of`, matching migration 006's `get_macro_observation_as_of`. There is no CURRENT_REPORTED mode, and the module performs no resolver or database call, ambient clock read or UUID generation.
- **Not included.** Macro scores or composites, regime labels, real policy stance (at Phase 17A no verified 12-month expectation series existed; Phase 17H later added `TR_EXPECTED_INFLATION_12M_PKA`, and ENAG is still not a monetary-policy expectation input), technical indicators or signals, optimizer inputs and tactical tilts.

## 7. Phase 17B — Exact Persisted PIT Macro State Query Bridge

Phase 17B makes the Phase 17A `MacroStateInputFact` usable from persisted data with `supabase/migrations/022_macro_state_input_exact_pit_rpc.sql` and `backend/engine/private/macro/state_query.py` (`MacroStateInputQueryService.get_input`). It still calculates no macro state or score and adds no technical overlay.

- **One PIT authority.** Migration 006's `get_pit_macro_observation` remains the winner-selection authority. Migration 022 only wraps it (`get_pit_macro_state_input`): it does not copy the SYSTEM_AS_OF / SOURCE_AS_OF algorithm, and it additionally requires the series to be the requested key, active and `verified`. It is `STABLE`, `SECURITY INVOKER`, has an explicit `search_path`, writes no data, revokes execute from PUBLIC and anon, and grants it to `authenticated` and `service_role`.
- **Exact numeric transport.** PostgREST returns NUMERIC as a JSON number that a client may decode into a float, so it is not trusted for exact analytics. The database casts the value to text (`o.value::text AS value_text`) and Python parses that text directly into `Decimal` (finite, no quantization). No provider or legacy float is rehabilitated, and the Python service never queries `macro_observations` directly.
- **AsOfMode transport token.** The Python `AsOfMode` enum values are lower-case domain serialization (`system_as_of` / `source_as_of`), while migration 006's historical SQL contract accepts upper-case RPC tokens (`SYSTEM_AS_OF` / `SOURCE_AS_OF`). `MacroStateInputQueryService` therefore performs an explicit transport mapping, and migration 022 forwards that SQL token unchanged. The returned fact still carries the caller's domain enum.
- **Explicit queries.** Every query supplies `canonical_key`, `effective_date`, `mode` and an aware `as_of`; there is no implicit current query and no clock. The series is checked against the active VERIFIED registry before any RPC, so genuinely unverified series (e.g. `TR_CPI_TUIK_YOY`) fail closed. `TR_POLICY_RATE` failed closed at Phase 17B creation time; Phase 17H later verified it, and it is never aliased to `TR_TCMB_AOFM`.
- **Strict response handling.** The RPC returns `[]` or exactly one row with exactly the expected columns; anything else (more than one row, a non-list, a non-mapping row, missing or extra keys) fails closed. Returned `canonical_key` and `effective_date` must equal the request. Enums are decoded exactly, with no normalization or fallback. Timestamps and UUIDs are parsed strictly.
- **Phase 17A stays the final defense.** The row is passed to `build_macro_state_input_fact`, so PIT eligibility, supersession and value/status rules propagate unchanged.
- **Missing versus unavailable.** No eligible row returns `None`. A returned explicit UNAVAILABLE row (`value_text` NULL) returns a fact whose value is `None`. These two cases stay distinct, and neither means numerical zero.
- **Still deferred.** Legacy float provider layer, (at Phase 17B time) a verified Turkish policy-rate source and a verified 12-month expectations source (both later closed by Phase 17H), macro normalization, the continuous macro state formulas and the technical overlay.

## 8. Phase 17C — PIT-Safe Exact Macro History Window

Phase 17C adds a bounded history query over `MacroStateInputFact` with `supabase/migrations/023_macro_state_input_history_rpc.sql` (`get_pit_macro_state_input_history`) and `MacroStateInputQueryService.get_history`. It calculates nothing: no transform, normalization, score or regime.

- **Authority chain 023 -> 022 -> 006.** Migration 006 remains the PIT winner authority and migration 022 the single-point exact-TEXT transport authority. Migration 023 only enumerates candidate `effective_date` values (`SELECT DISTINCT`, inclusive range, requested series, active and `verified`) and calls the 022 wrapper per date through `CROSS JOIN LATERAL`. It copies no PIT predicate and does not call 006 directly. The candidate query reads no value, status or timestamp column, so a date that exists only from a row ingested after `as_of` produces no output row.
- **One shared cutoff.** The whole window uses one `mode` and one aware `as_of`; the SQL mode token is forwarded to 022 unchanged. Every returned fact carries the caller's `mode` and `as_of`, and each row remains subject to the Phase 17A SYSTEM_AS_OF / SOURCE_AS_OF / supersession / value-status checks. One malformed row fails the whole history; nothing is skipped.
- **Exact Decimal only.** Values arrive as database-generated text (from 022) and are parsed directly into `Decimal`. No float, int, quantization or recast.
- **Ordered immutable tuple.** SQL orders by `effective_date ASC`; Python verifies rows are within the inclusive range, match the requested key and are strictly increasing (no duplicates), and never sorts or repairs. The result is a `tuple[MacroStateInputFact, ...]`.
- **No synthetic periods.** Frequency is owned by the registry. There is no resampling, forward fill, interpolation or gap row.
- **Three distinct states.** `()` means no eligible history. A missing period is simply an absent effective date. An explicit persisted UNAVAILABLE observation is a real fact with `value is None`. None of these is zero.
- **Validation.** `canonical_key` must be an exact registered active VERIFIED key (unverified series such as `TR_CPI_TUIK_YOY` fail closed; `TR_POLICY_RATE` did at Phase 17C time until Phase 17H), both dates exact `date`, `start <= end` (never swapped), exact `AsOfMode` and aware `as_of`, all before the single RPC call.
- **Still deferred.** Historical transforms, robust normalization, GrowthImpulse / PolicyInflationState / FinancialStress, verified TR policy-rate and 12-month expectation series (closed later by Phase 17H), broad USD and market-stress inputs, technical overlay and Phase 17 CI consolidation.

## 9. Phase 17D — Exact U.S. Treasury Yield-Curve Slope Evidence

Phase 17D adds the first macro calculation, `backend/engine/private/macro/us_treasury_curve.py` (`USTreasuryCurveSlopePoint`, `build_us_treasury_curve_slope_history`). It is a pure layer: it performs no query, provider call, clock read or registry search, and consumes only Phase 17C `MacroStateInputFact` histories of the VERIFIED official series `US_TREASURY_PAR_10Y`, `US_TREASURY_PAR_2Y` and `US_TREASURY_PAR_3M`.

- **Slopes only.** `slope_10y_2y = 10Y - 2Y` and `slope_10y_3m = 10Y - 3M`, as signed percentage-point spreads (inputs are percent levels; no basis points, annualization, rounding or quantization). Neither is chosen as "the" signal and they are not combined.
- **Exact subtraction.** Done on integer coefficients at a common exponent and rebuilt from (sign, digits, exponent), so the result does not depend on the ambient `decimal` context and no global context is touched. No float.
- **Strict inputs.** Each history must be an exact `tuple` of exact `MacroStateInputFact` bound to its own canonical key (no aliases, no 30Y or other series), strictly increasing by `effective_date` (validated, never sorted), and every fact must carry the caller's `mode` and an `as_of` equal to the caller's. The point stores the caller's own `mode` and `as_of` objects.
- **Exact-date union alignment.** Output dates are the union of actual effective dates. A tenor with no row on a date has slot `None`: no synthetic fact, forward fill, interpolation or nearest-date match. Empty histories give `()`.
- **Missing row versus explicit UNAVAILABLE.** A missing row is a `None` slot; an explicit UNAVAILABLE fact is retained unchanged (value `None`) and yields no slope. Neither is zero.
- **Independent availability.** A slope needs only its two numeric legs; a missing 3M does not block 10Y-2Y. PARTIAL, DEGRADED and STALE facts with numeric values are used as-is, with no aggregate status or confidence invented.
- **Signed evidence.** A negative spread is a legitimate number and zero is a real value. There is no inversion flag, label, regime, score, probability, recession view or portfolio action. Component facts are retained by identity for provenance.
- **Still deferred.** Normalization, structural breaks, yield-curve interpretation, GrowthImpulse / PolicyInflationState / FinancialStress, and any tactical use.

## 10. Phase 17E — Verified Broad U.S. Dollar Input

Phase 17E activates the broad-USD evidence input as a registry contract only.

- **Series.** Canonical key `US_BROAD_DOLLAR_INDEX`, provider `FRED_ALFRED`, FRED series `DTWEXBGS` (Nominal Broad U.S. Dollar Index). Origin: Board of Governors of the Federal Reserve System, release `H.10 Foreign Exchange Rates`. Category FX, unit `INDEX_POINTS`, native unit `Index Jan 2006=100`, Not Seasonally Adjusted, geography `US`, VERIFIED and active.
- **Cadence.** Observations are daily (`MacroFrequency.DAILY`), but they reach FRED through the weekly H.10 release cycle, so `expected_release_interval_days = 7`. Observation frequency and release interval are distinct. `freshness_basis` is `EFFECTIVE_DATE`, as for the other daily FRED series (`DFF`); no new freshness mechanism and no publication timestamp is inferred (`published_at` stays `None` in the provider response).
- **No provider change.** The existing FRED provider already resolves a verified registry key to its provider series code, so `US_BROAD_DOLLAR_INDEX` is requested as `series_id=DTWEXBGS` and its response metadata carries the registry `origin_source` and `release_name`. A missing value (`.`) stays UNAVAILABLE, never zero; a real `0` stays an observation.
- **No DXY.** The discontinued `DTWEXB` / `TWEXB` and the ICE DXY are not used, aliased or used as fallback.
- **Not included.** No strength, momentum, change, z-score or trend transform, no regime interpretation and no portfolio effect. The legacy FRED provider float parsing is unchanged; Phase 17 exact analytics must still consume persisted PIT-safe Decimal facts (`MacroStateInputFact`), never provider floats.

## 11. Phase 17F — Verified U.S. Real-Rate & Financial-Stress Inputs

Phase 17F activates two more raw evidence inputs as registry contracts only, both delivered by the existing FRED/ALFRED provider (no provider change).

### Real yield
- `US_TREASURY_REAL_10Y_YIELD` -> FRED `DFII10`: Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity, Quoted on an Investment Basis, Inflation-Indexed. Origin: Board of Governors of the Federal Reserve System, release `H.15 Selected Interest Rates`. Daily, Percent, Not Seasonally Adjusted, category `INTEREST_RATE`, `freshness_basis=EFFECTIVE_DATE`, `expected_release_interval_days=1`.
- It is raw market real-yield evidence. It is not the policy rate, an inflation expectation, breakeven inflation or the nominal 10Y yield, and it is not combined with `DFF`, CPI or the curve slopes (no real policy stance, no change or regime).

### Financial stress
- `US_FINANCIAL_STRESS_INDEX` -> FRED `STLFSI4`: St. Louis Fed Financial Stress Index. Origin: Federal Reserve Bank of St. Louis, release `St. Louis Fed Financial Stress Index`. Weekly (ending Friday), Index, Not Seasonally Adjusted, new category `MacroCategory.FINANCIAL_STRESS`, `freshness_basis=PUBLISHED_AT`, `expected_release_interval_days=7`, `TIER_1_REGULATORY` (an official Federal Reserve Bank product; `TIER_4_DERIVED` is reserved for Sentinax-derived computation).
- STLFSI4 is itself the official composite. Its internals are not reproduced, and the older STLFSI / STLFSI2 / STLFSI3 versions are not used. Negative values are valid observations. No threshold, band, label or signal is attached.
- The raw series is not Sentinax's future `FinancialStress_t` state axis.
- No database migration is needed for the new category: migration 006 stores `category VARCHAR(32) NOT NULL` with no category allow-list `CHECK` (a test guards this across all migrations).

### Common
- A missing value (`.`) stays UNAVAILABLE, zero stays an observation. `published_at` is never fabricated; freshness uses the existing fallback chain.
- These close raw evidence gaps only: no normalization, macro-state construction, interpretation or portfolio effect.
- Provider floats remain non-authoritative for Phase 17 analytics; exact analytics still consume persisted PIT-safe `MacroStateInputFact` values.

## 12. Phase 17G — PIT-Safe Monthly U.S. Macro Evidence Transforms

Phase 17G adds `backend/engine/private/macro/us_macro_evidence.py`: a pure calculation layer (`build_us_macro_evidence_snapshot`) over Phase 17C `MacroStateInputFact` histories and Phase 17D `USTreasuryCurveSlopePoint` histories. It produces evidence only: no MacroState, score, regime, weight or portfolio action.

### PIT semantics
> A Phase17G snapshot is valid only for its single explicit `mode/as_of`. Historical decision paths must rebuild the snapshot separately at each historical cutoff. Earlier months inside a current-as-of input history are normalization evidence, not historical decision-state authority.

The builder returns ONE `USMacroEvidenceSnapshot` (there is no `build_*_history` for evidence states). Histories are exact tuples bound to their canonical keys, strictly increasing by `effective_date` (validated, never sorted), carrying the caller's `mode` and `as_of`. Any `effective_date` after `as_of.date()` is rejected, never filtered.

### Monthly sampling
- A month is `date(year, month, 1)`; `reference_month` is that key, not a publication or observation date. Source objects keep their real effective date and are retained by identity.
- Daily / weekly series use the last actual object of each calendar month (no average, interpolation, nearest date or synthetic month-end). An explicit UNAVAILABLE that is latest in the month (or overall) wins; there is no backward search and no fallback to an earlier month.
- Components may have different `reference_month`s (release lags differ); nothing is forward-filled to a common month. An empty history gives a `None` component; an explicit UNAVAILABLE gives a component with `raw = None`.

### Growth
- Source `US_INDUSTRIAL_PRODUCTION` / INDPRO (already seasonally adjusted). Transform 3m/3m SAAR: `100 x [(recent 3m sum / previous 3m sum)^4 - 1]` in percentage points over six consecutive calendar-month slots (`t-5 ... t`, `None` for an absent month). Any absent or UNAVAILABLE slot gives `raw = None` (no bridging, no zero). A numeric index `<= 0` raises; two facts in one calendar month raise.

### Real rate
- Source `DFII10` (`US_TREASURY_REAL_10Y_YIELD`); raw latest level in percent. Negative values are valid. No change, momentum, CPI or Fed Funds combination.

### Yield curve
- Primary `10Y - 3M`; diagnostic `10Y - 2Y`; both taken as supplied by Phase 17D (no recomputation, no averaging). Only the primary is normalized. A latest point without `10Y - 3M` gives `primary = None` with no fallback.

### Broad dollar
- Source `DTWEXBGS` (`US_BROAD_DOLLAR_INDEX`); raw level only (no YoY / MoM / momentum / DXY proxy).

### Financial stress
- Source `STLFSI4` (`US_FINANCIAL_STRESS_INDEX`); raw level only. It is already an official standardized index: no second normalization, winsorization or rescaling, and no interpretation of its sign or size.

### Robust normalization
- Applied to growth, real yield, `10Y - 3M` and broad dollar. Window: the 120 immediately preceding calendar months (`t-120 ... t-1`), current month excluded, all required; otherwise `normalization = None` (no 119/60-month, expanding or "last 120 observations" fallback). A fully normalized growth value therefore needs about 126 consecutive industrial-production months.
- `median`; `MAD = median(|x - median|)`; `scaled_MAD = 1.4826 x MAD`; `z = (current - median) / scaled_MAD`. `MAD == 0` keeps median / MAD / scaled MAD and gives `z = None` (no standard-deviation fallback, no epsilon). No winsorization or clipping; economic direction is never inverted.
- Decimal: a fresh context (prec 50, `ROUND_HALF_EVEN`, `MIN_EMIN` / `MAX_EMAX`), independent of the ambient context, never mutated; no float; no output quantization. Overflow raises the static `ValueError("US macro evidence exceeds supported Decimal analytics range")`.

### Explicitly deferred
`GrowthImpulse_t`, `PolicyInflationState_t`, `FinancialStress_t`, final MacroState, macro regime labels, component weights, structural-break detector, winsorization, recession prediction, broad-dollar momentum, real-yield momentum, curve inversion interpretation, Turkey activity composite, Turkey verified policy rate, Turkey 12m inflation expectations, technical overlay, portfolio tactical tilt, optimizer integration.

## 13. Phase 17H-P0 — TCMB EVDS3 Transport

`TCMBEVDSProvider` (version `1.1.0`) now uses the current EVDS3 REST data service. This is a transport change only: no registry contract, macro category, series activation or methodology changed, and at that checkpoint `TR_POLICY_RATE` was still UNVERIFIED / inactive (activated afterwards in Phase 17H, below).

As of the 2026-09-30 authenticated Sentinax compatibility check, the legacy provider URL returned the EVDS3 HTML application shell rather than the expected JSON data contract, while the EVDS3 `igmevdsms-dis` data service returned JSON. No broader claim is made about the EVDS2 service's lifecycle, and there is no EVDS2 fallback (a silent old/new fallback would hide contract drift).

- **Endpoint.** `https://evds3.tcmb.gov.tr/igmevdsms-dis/`, path-style request `series=<code>&startDate=<DD-MM-YYYY>&endDate=<DD-MM-YYYY>&type=json`. The conventional `?series=` query form returned HTTP 404 on this host. Request values are percent-encoded so a symbol cannot inject parameters.
- **Authentication.** HTTP header `key` only (plus `Accept: application/json`); never in the URL, path, query, warnings or provenance.
- **Response.** `{"totalCount": n, "items": [{"Tarih": ..., "<SERIES_WITH_UNDERSCORES>": "...", "UNIXTIME": {"$numberLong": "..."}}]}`. Series fields parse exactly as before (missing is `None`, zero stays zero, multi-series gives a deterministic `values` mapping). `UNIXTIME` is kept as raw provider metadata only: it is never a series value, `effective_date`, `published_at` or availability timestamp.
- **`Tarih`.** Daily series keep `DD-MM-YYYY` (also `YYYY-MM-DD`, `DD.MM.YYYY`). Monthly series use `YYYY-M` (e.g. `2026-9`), parsed to the first day of that month. That date is a period label (September 2026), not a publication date: `published_at` stays `None` and no availability date is inferred from it.
- **Fail closed.** An HTML or otherwise non-JSON body raises the schema error and is never scraped; JSON without observation items is UNAVAILABLE.
- **Unchanged.** Legacy macro values remain `float` (the exact-Decimal path for precious metals is untouched); provider calls still resolve canonical registry keys to provider codes (`TR_FX_USDTRY` -> `TP.DK.USD.A.YTL`, `TR_TCMB_AOFM` -> `TP.APIFON4`).
- **Manual smoke.** `scripts/smoke_evds.py` (never run in CI) prints no part of the API key and also transport-probes the raw monthly codes `TP.ENFBEK.PKA12ENF` and `TP.BISPOLFAIZ.TUR`. A successful probe is not registry verification and not a statement that either is the final methodology authority.

## 14. Phase 17H — Verified Turkey Policy Rate & 12M Inflation Expectation Inputs

Phase 17H activates two raw Turkey evidence contracts through the existing EVDS3 provider transport (Phase 17H-P0; no provider change). It calculates nothing: no `PolicyRate - ExpectedInflation12m`, no real policy stance, `PolicyInflationState`, normalization, weight, score or regime.

### Policy rate — `TR_POLICY_RATE`
- EVDS `TP.BISPOLFAIZ.TUR` (datagroup `bie_bispolfaiz`), verified against authenticated EVDS3 series metadata on 2026-09-30: "Türkiye (TUR) Merkez Bankası Politika Faiz Oranı" / "Türkiye (TUR) Central Bank Policy Interest Rate", monthly (`AYLIK`, aggregation `last`).
- **Upstream identity.** The metadata names the Bank for International Settlements as the data source; EVDS is only the distributor, so `origin_source` is BIS, not TCMB. EVDS supplies no unit field, so no provider-native unit is invented; the unit is `PERCENT`. Category `INTEREST_RATE`, `freshness_basis=EFFECTIVE_DATE`, `expected_release_interval_days=31`, `TIER_1_REGULATORY`, VERIFIED and active.
- **Semantic authority.** The current TCMB policy instrument is the one-week repo auction rate. The official TCMB 1-week-repo / PPK publications remain the semantic cross-check authority (PPK of 2026-09-10: 37%); the verified EVDS monthly series is the automated monthly evidence input. The monthly observation date is a period label and is never a PPK meeting date.
- **Freshness limitation (deliberate evidence).** As of 2026-09-30 the EVDS range ends at July 2026 (value 37.0). The series identity is verified; its current freshness is NOT assumed. A consumer must not treat an old monthly observation as a current snapshot, and August / September 2026 values known from PPK publications are never inserted into this BIS-distributed series (that would fabricate provider observations; source lineages stay separate).
- **Not AOFM.** `TR_TCMB_AOFM` (`TP.APIFON4`, business-daily weighted average funding cost) is unchanged and remains a separate series; there is no alias, fallback or substitution in either direction.

### 12-month inflation expectation — `TR_EXPECTED_INFLATION_12M_PKA`
- EVDS `TP.ENFBEK.PKA12ENF` (datagroup `bie_enfbek`), metadata verified 2026-09-30: "Annual inflation expectations of market participants (12-month ahead, %)", monthly (aggregation `average`), source TCMB / CBRT, range 2015-01 to 2026-09 (2026-09 = 23.70).
- New category `MacroCategory.INFLATION_EXPECTATION` (appended last). Expectation is not realized CPI. No migration is needed: migration 006 stores `category VARCHAR(32)` with no allow-list CHECK (guarded by a test across all migrations).
- Unit `PERCENT`, `freshness_basis=PUBLISHED_AT`, `expected_release_interval_days=31`, `TIER_1_REGULATORY`, VERIFIED and active. It is the market participants' 12-month-ahead annual inflation expectation; it is NOT a year-end, 24-month, real-sector or household expectation, realized CPI or ENAG.

### PIT semantics
- EVDS effective period does not prove an exact publication timestamp. No `published_at` timestamp is fabricated, and nothing is inferred from `effective_date == publication`. `SOURCE_AS_OF` keeps its conservative fallback to `observed_at`.
- Phase 17A/17B/17C now accept both keys as active VERIFIED contracts with registry-derived taxonomy and exact `Decimal` values; unverified series such as `TR_CPI_TUIK_YOY` still fail closed before any RPC.

### Activity remains deferred
The Turkey activity input remains deferred pending exact current official seasonally-adjusted series-code verification.

### Also deferred
RealPolicyStance calculation, `PolicyInflationState`, Turkey `GrowthImpulse`, `FinancialStress_t`, final MacroState, macro weights, structural-break detector, technical overlay, tactical allocation and Phase 17 CI consolidation.
