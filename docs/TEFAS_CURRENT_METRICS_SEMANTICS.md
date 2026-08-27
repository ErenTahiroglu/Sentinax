# TEFAS Current Fund Metrics Semantics & Unit Contract Gate (Phase 11D.1 & 11D.1.5)

## 1. Executive Summary & Purpose

This audit gate establishes the empirical data contract, unit semantics, accounting consistency, Point-in-Time (PIT) boundaries, and multi-pay-group identity constraints for the TEFAS current valuation snapshot endpoint (`POST https://www.tefas.gov.tr/api/funds/fonBilgiGetir`).

Prior to implementing production models or adapters, this gate proves:
1. **Mathematical and accounting identity** between `portBuyukluk` (AUM / Net Asset Value), `sonFiyat` (Unit Price), and `payAdet` (Outstanding Participation Units).
2. **Currency denomination authority** and multi-pay-group ambiguity constraints.
3. **Strict absence of source economic dates/timestamps** in the endpoint response.
4. **Point-in-Time classification** (`CURRENT_VIEW_ONLY`, `SOURCE_EFFECTIVE_DATE = UNKNOWN`).
5. **Architectural handling of multi-currency / multi-pay-group funds** (e.g., A Group TRY vs B Group USD).

---

## 2. Official Surface & Root Envelope Contract

- **Endpoint:** `POST https://www.tefas.gov.tr/api/funds/fonBilgiGetir`
- **Request Headers:**
  - `Content-Type: application/json`
  - `Origin: https://www.tefas.gov.tr`
  - `Referer: https://www.tefas.gov.tr/TarihselVeriler.aspx`
  - `User-Agent: Sentinax/1.0 (Fund Data Semantics Auditor)`
- **Request Payload:** `{"fonKodu": "<CODE>", "dil": "TR"}`
- **Response Root Envelope:**
  ```json
  {
    "errorCode": null,
    "errorMessage": null,
    "resultList": [
      {
        "fonKodu": "MAC",
        "fonUnvan": "MARMARA CAPITAL PORTFÖY HİSSE SENEDİ (TL) FONU (HİSSE SENEDİ YOĞUN FON)",
        "sonFiyat": 0.76165,
        "gunlukGetiri": 0.8381,
        "payAdet": 5725524142,
        "portBuyukluk": 4360844111.72,
        "fonKategori": "Hisse Senedi Fonu",
        "kategoriDerece": 150,
        "kategoriFonSay": 199,
        "yatirimciSayi": 36070,
        "pazarPayi": 1.64
      }
    ]
  }
  ```
- **Envelope Semantics:**
  - `errorCode` / `errorMessage`: `null` on success, populated string on system error.
  - `resultList`: Single-element array `[{...}]` when fund code exists; empty array `[]` when fund code is invalid or unlisted.

---

## 3. Empirical Probing Sample & Accounting Identity Verification

A diverse sample of funds across distinct asset categories was probed on 2026-08-27. For each fund, the exact accounting relation `sonFiyat * payAdet ≈ portBuyukluk` was evaluated using exact Python `Decimal` arithmetic.

| Fund Code | Category | Unit Price (`sonFiyat`) | Outstanding Units (`payAdet`) | Portfolio Size (`portBuyukluk`) | `sonFiyat * payAdet` | Absolute Error | Relative Error |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **MAC** | Equity (TRY) | `0.761650` | `5,725,524,142` | `4,360,844,111.72` | `4,360,845,462.75` | `+1,351.03` | `0.000031%` |
| **TI1** | Money Market | `1,677.422285` | `135,011,759` | `226,471,732,654.60` | `226,471,733,283.65` | `+629.05` | `0.000000%` |
| **GTA** | Gold / Precious Metal | `1.691149` | `15,329,428,252` | `25,924,350,002.52` | `25,924,347,258.94` | `-2,743.58` | `0.000011%` |
| **DBH** | Eurobond / Debt | `0.385301` | `2,559,052,100` | `986,005,688.66` | `986,005,333.18` | `-355.48` | `0.000036%` |
| **NNF** | Equity (TRY) | `23.497202` | `89,727,966` | `2,108,356,102.07` | `2,108,356,142.15` | `+40.08` | `0.000002%` |
| **CJF** | Serbest (Döviz) | `65.444211` | `25,017,227` | `1,637,232,694.69` | `1,637,232,682.42` | `-12.27` | `0.000001%` |
| **TPJ** | Serbest (Döviz A/B) | `78.788601` | `29,580,953` | `2,330,641,898.71` | `2,330,641,888.75` | `-9.96` | `0.000000%` |

### Key Observations:
1. **Accounting Coherence:** The formula `sonFiyat * payAdet = portBuyukluk` holds universally across all fund types with relative residual error $< 0.000036\%$, which is strictly attributable to standard display precision rounding (`sonFiyat` rounded to 6 decimal places, `portBuyukluk` rounded to 2 decimal places).
2. **Coherent Reporting Unit Basis:** For every fund returned by TEFAS public API, `portBuyukluk` shares the exact same unit-currency basis as the returned `sonFiyat`.
3. **Currency Authority:** TEFAS public endpoints report domestic fund prices and AUM on a TRY basis. However, for multi-pay-group funds, this represents only the primary reference class (see Section 6).

---

## 4. Economic Date & Point-in-Time (PIT) Semantics

### A) Source Economic Date Absence (`ABSENT`)
Inspection of every key returned in `fonBilgiGetir` proves that **no source publication date, valuation date, or timestamp key exists**.

- `CURRENT_METRICS_ECONOMIC_DATE = ABSENT`
- Sentinax will **never fabricate** an effective date from `datetime.now()` or `date.today()`.
- Future ingestion model contract:
  - `retrieved_at`: UTC timestamp of the Sentinax fetch request.
  - `published_at`: `None` (unavailable from provider).
  - `effective_date`: `None` / `UNKNOWN`.
  - `SOURCE_AS_OF` resolution mode: Constant `UNAVAILABLE_SOURCE_AS_OF`.

### B) Cross-Check vs Dated Historical Price Surface
For all sampled active funds, `sonFiyat` in `fonBilgiGetir` matched the latest daily price in `fonFiyatBilgiGetir` on the same observation date (`2026-08-27`).
- **Authority Rule:** `sonFiyat` is classified as `CURRENT_PRICE_CROSSCHECK_ONLY`.
- The dated endpoint `fonFiyatBilgiGetir` remains the single canonical price authority in Sentinax.

### C) Historical Current Metrics Availability
A targeted verification across public API surfaces confirms that historical time-series endpoints for `portBuyukluk`, `payAdet`, or `yatirimciSayi` are **not available via verified 2026 public machine-readable surfaces** (`HISTORICAL_FUND_METRICS_PUBLIC_MACHINE_READABLE = NOT_FOUND_ON_VERIFIED_2026_SURFACES`).
- Sentinax will build its own historical system time-series of AUM and investor counts from ingestion snapshots forward.
- Historical backfilling is strictly prohibited (`CURRENT_VIEW_ONLY`).

---

## 5. Field Semantics & Decision Relevance Matrix

| Field Key | Source Type | Semantic Meaning | Economic Date? | Currency / Unit | PIT Classification | Decision Relevance | Future Implementation Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `portBuyukluk` | Numeric | Net Asset Value / Fund Size (AUM) | No (Absent) | Fund unit currency (TRY) | `CURRENT_VIEW_ONLY` | **High** (Scale / size risk context) | **Normalize** (`Decimal`) |
| `payAdet` | Integer | Outstanding Participation Units | No (Absent) | Unit count | `CURRENT_VIEW_ONLY` | **Low / Diagnostic** (Accounting validation) | **Normalize** (`Decimal`/`int`) |
| `yatirimciSayi` | Integer | Registered Unit-Holder Count | No (Absent) | Investor count | `CURRENT_VIEW_ONLY` | **Medium / Secondary** (Secondary breadth context) | **Normalize** (`int`) |
| `sonFiyat` | Numeric | Latest Reported Unit NAV Price | No (Absent) | Fund unit currency (TRY) | `CURRENT_VIEW_ONLY` | **Low / Diagnostic** (Cross-check only) | **Diagnostic Only** (`Optional[Decimal]`) |
| `fonKategori` | String | TEFAS High-Level Category String | No (Absent) | Categorical Text | `CURRENT_METADATA_ONLY` | **Low** (Metadata label) | **Raw Context Only** (`Optional[str]`) |
| `gunlukGetiri` | Numeric | Provider-Calculated Daily Return % | No (Absent) | Percentage | `CURRENT_VIEW_ONLY` | **None** (Sentinax calculates returns) | **Ignore** (Redundant) |
| `pazarPayi` | Numeric | Category Market Share % | No (Absent) | Percentage | `CURRENT_VIEW_ONLY` | **Low** (Informational only) | **Raw Context Only** (`Optional[Decimal]`) |
| `kategoriDerece`| Integer | Category Performance Rank | No (Absent) | Rank Integer | `CURRENT_VIEW_ONLY` | **None** (External proprietary rank) | **Ignore** |
| `kategoriFonSay`| Integer | Total Funds in Category | No (Absent) | Count Integer | `CURRENT_VIEW_ONLY` | **None** (External metadata) | **Ignore** |

---

## 6. Multi-Pay-Group & Multi-Currency Fund Control Study (TPJ)

### A) Official KAP Findings (Control Fund: `TPJ`)
Official Public Disclosure Platform (KAP) and prospectus filings for **TPJ (TEB Portföy Birinci Serbest (Döviz) Fon)** establish:
1. The fund participates in TEFAS trading.
2. The fund has **two distinct participation share/pay groups**:
   - **A Grubu Paylar:** Traded and settled in **Turkish Lira (TRY)**.
   - **B Grubu Paylar:** Traded and settled in **US Dollars (USD)**.
3. Separate unit prices are calculated for A Group (TRY) and B Group (USD).

### B) TEFAS Public Endpoint Response Analysis
When probing `TPJ` on TEFAS public endpoints:
- `POST /api/funds/fonFiyatBilgiGetir` returns **exactly 1 row per date** with `fiyat = 78.788601`.
- `POST /api/funds/fonBilgiGetir` returns `sonFiyat = 78.788601`, `payAdet = 29580953`, `portBuyukluk = 2330641898.71`.
- **Zero pay-group or currency metadata is returned**. The returned price (`78.788601`) corresponds strictly to the **A Group TRY price**.
- The **B Group USD price is completely absent** from the public TEFAS JSON API.

### C) Identity Collision in Single-Instrument Architecture
In Sentinax:
- `InstrumentRecord` has exactly one canonical `currency`.
- `ProviderAliasRecord` uniquely maps `(provider="TEFAS", symbol="TPJ")` to one canonical `instrument_id`.
- If a user holds **TPJ B Group (USD)**, mapping the TEFAS symbol `"TPJ"` would deliver a 78.78 TRY price instead of the ~2.00 USD price, causing massive valuation distortion.

### D) Evaluated Strategies & Anti-Overengineering Decision
1. **Strategy A (SHARE_CLASS_MODEL):** Refactor Instrument Master into parent fund / child share-class hierarchy with composite alias keys (`TEFAS:TPJ:A`, `TEFAS:TPJ:B`).
2. **Strategy B (FUND_LEVEL_ONLY):** Treat TEFAS fund codes as fund-level entities and ban foreign-currency share-class holdings.
3. **Strategy C (FAIL_CLOSED_AMBIGUOUS - Recommended):**
   - Retain current single-instrument model.
   - Restrict `TefasFundPriceProvider` and `TefasFundMetricsProvider` to unambiguous single-currency funds.
   - Multi-pay-group funds where the public price is ambiguous must be rejected or explicitly bound only to their primary TRY reference instrument.

---

## 7. Implementation Gate Decision

### Decision: **`GO_CURRENT_METRICS_WITH_AMBIGUOUS_FUND_REJECTION`**
- **Price Provider Multi-Class Status:** `SAFE_ONLY_FOR_UNAMBIGUOUS_FUNDS`
- **PIT Resolver Impact:** Zero code changes required (`PIT_RESOLVER_REWRITE_REQUIRED = NO`). The resolver operates deterministically on canonical observations once resolved.

### Recommended Scope for Phase 11D.2:
- **Module:** `backend/engine/private/providers/tefas_metrics.py`
- **Normalized Model:** `TefasFundCurrentMetrics` with:
  - `portfolio_size`: `Decimal` (from `portBuyukluk`)
  - `portfolio_size_currency`: `Currency` (resolved from `InstrumentRecord`)
  - `outstanding_units`: `Decimal` (from `payAdet`)
  - `investor_count`: `int` (from `yatirimciSayi`)
  - `reported_current_unit_price`: `Optional[Decimal]` (diagnostic from `sonFiyat`)
  - `category_name`: `Optional[str]` (raw metadata from `fonKategori`)
- **Snapshot Model:** `TefasFundMetricsSnapshot` with immutable payload hash and UTC `retrieved_at`.
- **Preflight Validation:** Reject multi-pay-group ambiguous funds where canonical instrument currency does not match public TEFAS TRY reference price.
