# 🌐 Global (US + Europe) EOD Market Data Architecture

## 1. Overview & Provider Classification

Sentinax utilizes **Alpha Vantage** (`TIME_SERIES_DAILY`) as the baseline low-volume adapter for daily End-of-Day (EOD) pricing across US and European equities and ETFs.

| Attribute | Classification / Value | Rationale |
| :--- | :--- | :--- |
| **Provider Name** | `ALPHA_VANTAGE` | Commercial Market Data Vendor (`https://www.alphavantage.co/query`) |
| **Access Status** | **`YELLOW`** | Commercial API with free-tier rate limits and capacity constraints; not an official exchange authority. |
| **Source Tier** | **`TIER_3_AGGREGATOR`** | Commercial aggregator tier (distinct from TIER_2_EXCHANGE such as Borsa İstanbul or NYSE direct). |
| **Standard Free Capacity** | **`25 requests / day`** | Free tier is constrained to 25 daily requests. Ingestion is strictly low-volume and on-demand per symbol. |
| **History Output Size** | **`FREE_COMPACT_HISTORY` (~100 days)** | Standard free endpoint returns latest ~100 trading days (`compact`). Full multi-year history requires premium entitlement (`full`). |
| **Pricing Policy** | **Raw / As-Traded Only** | Operates on raw as-traded daily prices (`open`, `high`, `low`, `close`, `volume`). No adjusted prices, splits, or dividend adjustments in this layer. |
| **Fallback Policy** | **No Stooq / No yfinance Fallback** | Stooq and yfinance are strictly forbidden. Missing data reports `UNAVAILABLE` without fabricated fallbacks. |
| **Live Verification State** | **`LIVE_PROVIDER_UNVERIFIED`** | No live Alpha Vantage API key was configured; implementation adheres strictly to official API documentation. |

---

## 2. Supported Instrument Universes & Symbology

Provider symbols are external aliases and must resolve to canonical `InstrumentRecord` instances (`id: UUID`) via `InstrumentResolverService`.

### 2.1 US Equities & ETFs
- **US Stock Example**: `AAPL` (Apple Inc., NASDAQ / `XNAS`, Currency: `USD`, `InstrumentType.US_STOCK`)
- **US ETF Example**: `SPY` (SPDR S&P 500 ETF Trust, NYSE Arca / `ARCX`, Currency: `USD`, `InstrumentType.US_ETF`)

### 2.2 European Equities & ETFs
- **XETRA Stock Example**: `MBG.DEX` (Mercedes-Benz Group AG, Deutsche Börse XETRA / `XETR`, Currency: `EUR`, `InstrumentType.EUROPEAN_STOCK`)
- **London Stock Example**: `TSCO.LON` (Tesco PLC, London Stock Exchange / `XLON`, Currency: `GBP`, `InstrumentType.EUROPEAN_STOCK`)

---

## 3. Data Integrity & Decimal Guarantees

1. **Exact Decimal Representation & Float Rejection**:
   - `open`, `high`, `low`, `close`, and `volume` are parsed as pure Python `Decimal`.
   - Floating-point inputs (`float`) are explicitly rejected by the exact decimal parser to prevent loss-of-precision contamination.
2. **Missing != Zero**:
   - Absent fields remain `None`. Missing volume or open price is never defaulted to `0` or `0.0`.
3. **Non-Negative OHLC**:
   - All prices (`open`, `high`, `low`, `close`) and `volume` must be `>= 0`. Negative values are flagged as `INVALID_OBSERVATION`.
4. **Non-Finite Value Defense**:
   - Any occurrences of `NaN`, `sNaN`, `Infinity`, or `-Infinity` are rejected, flagging the observation as `INVALID_OBSERVATION`.
5. **OHLC Envelope Validation**:
   - `high >= low`.
   - `high >= open` and `high >= close` (when open/close are present).
   - `low <= open` and `low <= close` (when open/close are present).
   - Any envelope violation flags the observation as `INVALID_OBSERVATION` without modifying source data.

---

## 4. Point-in-Time (PIT) & Identity Semantics

- **`trade_date`**: The economic trading session calendar date (`YYYY-MM-DD`).
- **`retrieved_at`**: The UTC timestamp when Sentinax executed the HTTP request and captured the snapshot.
- **`published_at`**: Set to `None`. Alpha Vantage daily endpoint does not provide authoritative historical first-publication timestamps per row.
- **Request Identity Binding**:
  - If a `FetchContext` specifies both `canonical_instrument_id` and `provider_symbol`, the provider verifies that the alias maps to the identical canonical UUID before making any network calls. Mismatches fail closed with `DataStatus.UNAVAILABLE` and `IDENTITY_MISMATCH` warning, saving API quota.
- **Response Metadata Symbol Validation**:
  - The `"2. Symbol"` field in `"Meta Data"` is validated against the requested symbol. If the response contains mismatched symbol metadata (e.g. requested `AAPL` but received `MSFT`), the parser fails closed with `RESPONSE_SYMBOL_MISMATCH` and produces zero valid observations.
- **Per-Instrument Snapshot Scope**: Each API response is isolated to the requested symbol. Failure or missing data for one symbol never invalidates or supersedes other instruments in the database.

---

## 5. Aggregate Status Calculation

`ProviderResponse.status` is computed strictly from observation validation results:

| Condition | `ProviderResponse.status` | Description |
| :--- | :--- | :--- |
| Rate limited / Quota exceeded | **`UNAVAILABLE`** | Rate limit notice in JSON or HTTP 429. |
| Observation count == 0 | **`UNAVAILABLE`** | Empty time series or provider error. |
| Valid count == 0 | **`UNAVAILABLE`** | All rows are `UNRESOLVED_IDENTITY`, `INVALID_OBSERVATION`, or `DUPLICATE_CONFLICT`. |
| Valid count == Observation count | **`COMPLETE`** | All returned observations parsed cleanly and resolved to master instruments. |
| 0 < Valid count < Observation count | **`PARTIAL`** | Mixed series with both valid and invalid/unresolved/conflict rows. |

Detailed breakdown counts (`valid_count`, `invalid_count`, `unresolved_count`, `conflict_count`) are surfaced in `source_metadata`.
