# TEFAS Current Fund Metrics Semantics & Unit Contract Gate (Phase 11D.1)

## 1. Executive Summary & Purpose

This audit gate establishes the empirical data contract, unit semantics, accounting consistency, and Point-in-Time (PIT) boundaries for the TEFAS current valuation snapshot endpoint (`POST https://www.tefas.gov.tr/api/funds/fonBilgiGetir`).

Prior to implementing production models or adapters, this gate proves:
1. **The mathematical and accounting identity** between `portBuyukluk` (AUM / Net Asset Value), `sonFiyat` (Unit Price), and `payAdet` (Outstanding Participation Units).
2. **Currency denomination authority** and unit representation.
3. **The strict absence of source economic dates/timestamps** in the endpoint response.
4. **Point-in-Time classification** (`CURRENT_VIEW_ONLY`, `SOURCE_EFFECTIVE_DATE = UNKNOWN`).

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

A diverse sample of 6 funds across distinct asset categories was probed on 2026-08-27. For each fund, the exact accounting relation `sonFiyat * payAdet ≈ portBuyukluk` was evaluated using exact Python `Decimal` arithmetic.

| Fund Code | Category | Unit Price (`sonFiyat`) | Outstanding Units (`payAdet`) | Portfolio Size (`portBuyukluk`) | `sonFiyat * payAdet` | Absolute Error | Relative Error |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **MAC** | Equity (TRY) | `0.761650` | `5,725,524,142` | `4,360,844,111.72` | `4,360,845,462.75` | `+1,351.03` | `0.000031%` |
| **TI1** | Money Market | `1,677.422285` | `135,011,759` | `226,471,732,654.60` | `226,471,733,283.65` | `+629.05` | `0.000000%` |
| **GTA** | Gold / Precious Metal | `1.691149` | `15,329,428,252` | `25,924,350,002.52` | `25,924,347,258.94` | `-2,743.58` | `0.000011%` |
| **DBH** | Eurobond / Debt | `0.385301` | `2,559,052,100` | `986,005,688.66` | `986,005,333.18` | `-355.48` | `0.000036%` |
| **NNF** | Equity (TRY) | `23.497202` | `89,727,966` | `2,108,356,102.07` | `2,108,356,142.15` | `+40.08` | `0.000002%` |
| **CJF** | Serbest (Döviz) | `65.444211` | `25,017,227` | `1,637,232,694.69` | `1,637,232,682.42` | `-12.27` | `0.000001%` |

### Key Observations:
1. **Accounting Coherence:** The formula `sonFiyat * payAdet = portBuyukluk` holds universally across all fund types with relative residual error $< 0.000036\%$, which is strictly attributable to standard display precision rounding (`sonFiyat` rounded to 6 decimal places, `portBuyukluk` rounded to 2 decimal places).
2. **Common Unit Law:** `portBuyukluk` is always expressed in the exact same reporting currency as `sonFiyat`.
3. **Foreign Currency Funds on TEFAS:** Turkish domestic investment funds investing in foreign assets/eurobonds (e.g. `DBH`, `CJF`) report their official TEFAS unit price and AUM in Turkish Lira (TRY) on the public portal. Transaction currency authority remains governed by Sentinax's canonical `InstrumentMaster`.

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
A targeted verification across public API surfaces confirms that **no historical time-series endpoint exists** on TEFAS for `portBuyukluk`, `payAdet`, or `yatirimciSayi` via standard stateless HTTP (`HISTORICAL_CURRENT_METRICS = NOT_FOUND`).
- Sentinax will build its own historical system time-series of AUM and investor counts from ingestion snapshots forward.
- Historical backfilling is strictly prohibited (`CURRENT_VIEW_ONLY`).

---

## 5. Field Semantics & Decision Relevance Matrix

| Field Key | Source Type | Semantic Meaning | Economic Date? | Currency / Unit | PIT Classification | Decision Relevance | Future Implementation Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `portBuyukluk` | Numeric | Net Asset Value / Fund Size (AUM) | No (Absent) | Fund unit currency (TRY) | `CURRENT_VIEW_ONLY` | **High** (Scale & liquidity filter) | **Normalize** (`Decimal`) |
| `payAdet` | Integer | Outstanding Participation Units | No (Absent) | Unit count | `CURRENT_VIEW_ONLY` | **Medium** (Accounting validation) | **Normalize** (`Decimal`/`int`) |
| `yatirimciSayi` | Integer | Registered Unit-Holder Count | No (Absent) | Investor count | `CURRENT_VIEW_ONLY` | **High** (Retail breadth/concentration) | **Normalize** (`int`) |
| `sonFiyat` | Numeric | Latest Reported Unit NAV Price | No (Absent) | Fund unit currency (TRY) | `CURRENT_VIEW_ONLY` | **Low/Diagnostic** (Cross-check only) | **Diagnostic Only** (`Optional[Decimal]`) |
| `fonKategori` | String | TEFAS High-Level Category String | No (Absent) | Categorical Text | `CURRENT_METADATA_ONLY` | **Low** (Metadata label) | **Raw Context Only** (`Optional[str]`) |
| `gunlukGetiri` | Numeric | Provider-Calculated Daily Return % | No (Absent) | Percentage | `CURRENT_VIEW_ONLY` | **None** (Sentinax calculates returns) | **Ignore** (Redundant) |
| `pazarPayi` | Numeric | Category Market Share % | No (Absent) | Percentage | `CURRENT_VIEW_ONLY` | **Low** (Informational only) | **Raw Context Only** (`Optional[Decimal]`) |
| `kategoriDerece`| Integer | Category Performance Rank | No (Absent) | Rank Integer | `CURRENT_VIEW_ONLY` | **None** (External proprietary rank) | **Ignore** |
| `kategoriFonSay`| Integer | Total Funds in Category | No (Absent) | Count Integer | `CURRENT_VIEW_ONLY` | **None** (External metadata) | **Ignore** |

---

## 6. Implementation Gate Decision

### Decision: **`GO_CURRENT_METRICS`**

**Justification:**
1. The mathematical relationship and unit semantics of `portBuyukluk`, `payAdet`, and `sonFiyat` are thoroughly verified ($<0.000036\%$ error).
2. Investor count (`yatirimciSayi`) provides a high-value breadth signal for risk management and liquidity screening.
3. Standard stateless HTTP POST requests work without anti-bot evasion dependencies.
4. Current-only time boundaries can be represented with strict PIT fidelity (`retrieved_at` known, `effective_date = UNKNOWN`, `SOURCE_AS_OF = UNAVAILABLE`).

---

## 7. Recommended Phase 11D.2 Production Scope

### In Scope for Phase 11D.2:
- **Module:** `backend/engine/private/providers/tefas_metrics.py` (or extending `tefas_eod.py` with `TefasFundMetricsProvider`).
- **Normalized Model:** `TefasFundCurrentMetrics` with:
  - `portfolio_size`: `Decimal` (from `portBuyukluk`)
  - `portfolio_size_currency`: `Currency` (resolved from `InstrumentRecord`)
  - `outstanding_units`: `Decimal` (from `payAdet`)
  - `investor_count`: `int` (from `yatirimciSayi`)
  - `reported_current_unit_price`: `Optional[Decimal]` (diagnostic from `sonFiyat`)
  - `category_name`: `Optional[str]` (raw metadata from `fonKategori`)
- **Snapshot Model:** `TefasFundMetricsSnapshot` with immutable payload hash and UTC `retrieved_at`.

### Excluded from Phase 11D.2:
- Universe ingestion / `fonUnvanAra` loops.
- Portfolio allocation breakdown (`dagilimSiraliGetirT`).
- Management fees / KAP scraping.
- Historical backfilling of AUM.
- Return / volatility calculations from `gunlukGetiri`.
- Frontend dashboard changes.
