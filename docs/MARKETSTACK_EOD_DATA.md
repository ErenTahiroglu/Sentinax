# Marketstack Free Tier European EOD & Corporate Actions Layer

## 1. Executive Summary

Marketstack Free tier serves as Sentinax's dedicated provider for **European Equities and ETFs Rolling 1-Year EOD Price History & Corporate Actions**.

- **Plan / Cost:** Free Tier ($0/month).
- **Free Quota:** 100 API requests / month.
- **History Depth:** 1 Year rolling historical daily data (requested spans <= 366 days).
- **Corporate Actions:** Splits (`split_factor`) and Dividends (`dividend`).
- **Source Classification:** `YELLOW` / `SourceTier.TIER_3_AGGREGATOR` (commercial market data aggregator; not an official exchange authority).
- **Target Role:**
  * `EU_ROLLING_1Y_HISTORY`: Rolling recent 1-year EOD price series for European stocks and ETFs.
  * `EU_RECENT_ADJUSTED_SERIES`: Provider-adjusted OHLCV for recent risk/return models.
  * `EU_CORPORATE_ACTION_MONITOR`: Captures splits/dividends and sets `history_refresh_required = True`.
- **Non-Roles:**
  * Not the daily current valuation authority (Alpha Vantage Free performs `EU_CURRENT_VALUATION`).
  * Not deep history (historical depth > 1 year is rejected with `FREE_HISTORY_WINDOW_EXCEEDED`; `EU_DEEP_HISTORY_BOOTSTRAP >= 5Y` remains **OPEN**).

---

## 2. Quota & Capacity Economics

Marketstack operates on **per-ticker billing**: one ticker symbol requested consumes one API request, even if batch endpoints are used. Therefore, Sentinax queries one canonical instrument per request for strict identity and provenance isolation.

| Asset Universe | Refresh Frequency | Monthly API Quota Used | Quota Margin (of 100/mo) |
| :--- | :--- | :--- | :--- |
| **10 European Stocks/ETFs** | Monthly Rolling History | **10 requests / month** | 90% headroom (90 req buffer) |
| **20 European Stocks/ETFs** | Monthly Rolling History | **20 requests / month** | 80% headroom (80 req buffer) |

---

## 3. API Contract & Parameter Invariants

- **Base Endpoint:** `https://api.marketstack.com/v2/eod`
- **Authentication:** `access_key` query parameter loaded from `MARKETSTACK_ACCESS_KEY` environment variable.
- **Credential Safety:** The `access_key` is used exclusively for outbound network dispatch and is **never** logged, persisted, serialized, or stored in raw snapshot records or metadata.
- **Request Parameters:**
  * `symbols`: Provider alias (e.g. `MBG.XETR`).
  * `date_from` / `date_to`: Canonical `YYYY-MM-DD` date boundaries.
  * `limit`: `1000` (fits full 1-year daily trading observations in a single page).
  * `sort`: `ASC` (chronological ordering).
- **Live Status:** `LIVE_PROVIDER_UNVERIFIED` (pending access key live probe; aliases subject to `PROVIDER_ALIAS_LIVE_VERIFICATION_PENDING`).

---

## 4. Response Validation & Data Integrity

1. **Exact Decimal Boundaries:**
   Parsed directly via `json.loads(raw_text, parse_float=Decimal)`. Python binary floats are strictly rejected.
2. **Exact Exchange / MIC Validation:**
   Each returned observation row's `exchange` is validated with exact string equality against the canonical Instrument Master MIC (`expected_mic`). Fuzzy or substring matches (e.g. `X` or `XETRA` for `XETR`) and missing exchange values flag `INVALID_SOURCE_CONTEXT`.
3. **Symbol Validation:**
   Each row's `symbol` must match the requested alias.
4. **Pagination Hardening:**
   Validates positive integer bounds on `limit`, `offset`, `count`, and `total`. If `count != len(data)`, records `INVALID_PAGINATION`. If `pagination.total > len(data)`, records `TRUNCATED_RESPONSE`. Aggregate status degrades from `COMPLETE` to `PARTIAL`.
5. **Corporate Action Signal:**
   When `split_factor != 1` or `dividend > 0`, `history_refresh_required = True` is set on the snapshot metadata without triggering nested HTTP requests.
6. **Strict Point-in-Time:**
   `trade_date` is the economic market date; `retrieved_at` is network UTC knowledge time; `published_at` is `None` (unfabricated).
