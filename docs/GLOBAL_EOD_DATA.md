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

1. **Exact Decimal Representation**:
   - `open`, `high`, `low`, `close`, and `volume` are parsed as pure Python `Decimal`.
   - Floating-point numbers (`float`) are strictly prohibited in parser and models.
2. **Missing != Zero**:
   - Absent fields remain `None`. Missing volume or open price is never defaulted to `0` or `0.0`.
3. **Non-Finite Value Defense**:
   - Any occurrences of `NaN`, `sNaN`, `Infinity`, or `-Infinity` are rejected, flagging the observation as `INVALID_OBSERVATION`.
4. **OHLC Envelope Validation**:
   - `close >= 0` and `volume >= 0`.
   - `high >= low`.
   - `high >= open` and `high >= close` (when open/close are present).
   - `low <= open` and `low <= close` (when open/close are present).
   - Any envelope violation flags the observation as `INVALID_OBSERVATION` without modifying source data.

---

## 4. Point-in-Time (PIT) Semantics

- **`trade_date`**: The economic trading session calendar date (`YYYY-MM-DD`).
- **`retrieved_at`**: The UTC timestamp when Sentinax executed the HTTP request and captured the snapshot.
- **`published_at`**: Set to `None`. Alpha Vantage daily endpoint does not provide authoritative historical first-publication timestamps per row.
- **Per-Instrument Snapshot Scope**: Each API response is isolated to the requested symbol. Failure or missing data for one symbol never invalidates or supersedes other instruments in the database.

---

## 5. API Error & Rate Limit Handling

Alpha Vantage often returns status messages and errors inside HTTP 200 JSON payloads. The adapter handles:

1. **Rate Limit Exhaustion**:
   - JSON responses containing `"Information"` or `"Note"` regarding the 25 requests/day limit flag `is_rate_limited = True`, returning `DataStatus.UNAVAILABLE` with diagnostic `RATE_LIMIT_EXHAUSTED`.
2. **Invalid Symbol / Bad Parameter**:
   - JSON responses with `"Error Message"` flag `DataStatus.UNAVAILABLE` with diagnostic `PROVIDER_ERROR`.
3. **HTTP 429 & HTTP 5xx**:
   - HTTP 429 yields `RATE_LIMITED`.
   - HTTP 5xx yields `SERVER_ERROR`.
4. **Network Timeouts**:
   - Handled via `ProviderTimeoutError` returning `DataStatus.UNAVAILABLE` without hanging.
