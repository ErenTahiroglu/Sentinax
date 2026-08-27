# 🌐 Zero-Cost ($0/mo) Global EOD Market Data Mosaic Architecture

## 1. Architectural Principle & Executive Summary

Sentinax implements a **Zero-Cost ($0/month recurring)** market data architecture using a **Differentiated Responsibility Data Mosaic**. Rather than requiring a single expensive commercial provider to deliver global coverage, deep history, real-time current prices, and corporate action adjustments simultaneously, specialized free tiers are mapped to narrowly defined responsibilities.

### 1.1 Feasibility Verdict
- **Verdict**: **`YES_WITH_CONSTRAINTS`**
- **Recurring Cost**: **$0.00 / month** (strictly zero paid subscriptions).
- **Core Constraint**: European scope operates on a curated watchlist/holdings model (~10–20 active European equities/ETFs) due to free API quotas, while US coverage accommodates up to 500 unique assets with 30+ years of adjusted history.

---

## 2. Global Source Mosaic Matrix

| Source / Provider | Plan / Tier | Recurring Cost | Primary Role | Free Quota | History Depth | Geographic Scope | Semantic Output | Source Classification |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Tiingo** | Starter (Free) | **$0 / mo** | **US Primary EOD & Risk History** | 500 symbols/mo<br>50 req/hour<br>1,000 req/day<br>1 GB/mo | **60+ Years Aggregate** (per-ticker listing history; >=5Y target met) | US Equities & US ETFs | Raw OHLCV + Split & Dividend Adjusted (`adjClose`, `divCash`, `splitFactor`) | `TIER_3_AGGREGATOR` |
| **Alpha Vantage** | Free Standard | **$0 / mo** | **EU Current Daily Valuation** | 25 req/day standard limit | Latest 100 days (`compact`) | Global (including XETRA, LSE, Euronext) | Raw as-traded OHLCV | `TIER_3_AGGREGATOR` |
| **Marketstack** | Free Tier | **$0 / mo** | **EU Rolling 1Y History & Corporate Actions** | 100 req/month | Up to 1 Year | Global (70+ exchanges) | Raw + Adjusted OHLCV + Splits/Dividends metadata | `TIER_3_AGGREGATOR` |
| **Open Data / Manual CSV** | User / Open Data Import | **$0** | **EU Deep History Bootstrap (>= 5Y)** | On-demand / File import | Multi-year archive (5Y–20Y) | Selected European Stocks / ETFs | Historical OHLCV (`EU_DEEP_HISTORY_BOOTSTRAP = OPEN`) | `TIER_4_SCRAPED_FALLBACK` (Bootstrap Only) |
| **Twelve Data** (Auxiliary) | Basic (Free) | **$0 / mo** | **Spot Cross-Check & Diagnostics** | 8 credits/min<br>800 credits/day | Limited trial | US + Global trial | Raw spot/EOD cross-check | `TIER_3_AGGREGATOR` (Auxiliary) |

---

## 3. Geographic & Semantic Division of Labor

### 3.1 United States Equities & ETFs (`US_STOCK`, `US_ETF`)
- **Primary Source**: **Tiingo Starter Free**.
- **Valuation**: Tiingo raw `close`.
- **Risk & Returns**: Tiingo adjusted series (`adjClose`, `adjOpen`, `adjHigh`, `adjLow`, `adjVolume`).
- **Corporate Actions**: Tiingo explicitly supplies `divCash` and `splitFactor`.
- **Result**: Completely self-contained; zero bootstrap dataset or fallback needed for US assets.

### 3.2 European Equities & ETFs (`EUROPEAN_STOCK`, `EUROPEAN_ETF`)
- **Current Valuation**: **Alpha Vantage Free** (ingests raw daily EOD close for active holdings).
- **Rolling 1-Year History & Corporate Action Monitor**: **Marketstack Free** (periodic monthly/on-demand refresh to capture 1Y history and split/dividend events).
- **Deep Historical Risk Archive (>= 5 Years)**: **Kaggle / Open Dataset / User CSV Bootstrap**.
- **Corporate Action Gap Policy**: If an EU security lacks verified corporate action adjustments across the transition between bootstrap and rolling APIs, the engine flags `EU_ADJUSTED_RETURN_GAP` and isolates return calculations rather than fabricating synthetic multipliers.

---

## 4. Quota Budget & Capacity Planning

### 4.1 Scenario A: 10 Active European Instruments
- **Alpha Vantage (Daily Valuation)**:
  - 10 symbols × 1 call/day = **10 calls/day** (out of 25 free limit; **40% utilization**, 15 buffer calls for retries/diagnostics).
- **Marketstack (Rolling 1Y History Refresh)**:
  - 10 symbols × 1 refresh/month = **10 calls/month** (out of 100 free limit; **10% utilization**, 90 buffer calls).
- **Tiingo (US Assets)**:
  - 100+ US symbols × 1 call/day = 100 calls/day (out of 1,000 free limit; **10% daily utilization**, 400 symbol buffer).

### 4.2 Scenario B: 20 Active European Instruments
- **Alpha Vantage (Daily Valuation)**:
  - 20 symbols × 1 call/day = **20 calls/day** (out of 25 free limit; **80% utilization**, 5 buffer calls).
- **Marketstack (Rolling 1Y History Refresh)**:
  - 20 symbols × 1 refresh/month = **20 calls/month** (out of 100 free limit; **20% utilization**, 80 buffer calls).
- **Tiingo (US Assets)**:
  - Up to 480 US symbols comfortably accommodated within 500 monthly unique symbol ceiling.

---

## 5. Bootstrap Dataset Acceptance Gate & Invariants

Historical bootstrap datasets (Kaggle/Open Data/Manual CSV) must satisfy the following zero-trust criteria before ingestion:

1. **Licensing**: Explicit permissive or open license (e.g. `CC0`, `CC BY`, `MIT`, `ODbL`) permitting private personal analytical use.
2. **Provenance & Immutability**: Source origin recorded, payload hashed via SHA-256 (`payload_hash`), stored immutably.
3. **Point-in-Time Discipline**: `retrieved_at` is set to the timestamp of local dataset ingestion. Historical publication timestamps (`published_at`) are never fabricated.
4. **Overlap Verification**: Overlapping dates between the bootstrap dataset and live API sources (Alpha Vantage / Marketstack) must be compared for prices, currency, and split adjustments. Unexplained discrepancies reject automatic series stitching.
5. **No Magic Multipliers**: Merging series by fitting arbitrary scaling constants is strictly prohibited.
6. **Survivorship Bias Disclosure**: Datasets containing only current index constituents must be flagged `CURRENT_CONSTITUENTS_ONLY` and restricted to single-instrument risk modeling (never universe backtesting).

---

## 6. Curated Opportunity Engine Scope

Because recurring costs are constrained to $0/month:
- **US Market**: Supports broad scanning across hundreds of stocks and ETFs.
- **European Market**: Operates on a curated candidate watchlist and active portfolio holdings (~10–20 instruments at any one time).
- This is a deliberate, robust scope constraint, ensuring zero operational cost without compromising institutional data integrity.
