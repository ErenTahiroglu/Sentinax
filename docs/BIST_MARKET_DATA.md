# 🏛️ Borsa İstanbul (BIST) Equity EOD & ALTIN.S1 Market Data Backbone

## 1. Overview & Architecture

Sentinax implements an official, point-in-time (PIT) compliant End-of-Day (EOD) market data backbone for Borsa İstanbul (BIST) Equities and the Darphane Gold Certificate (`ALTIN.S1`).

The pipeline follows Sentinax core data principles:
1. **Raw Snapshot First**: Every downloaded bulletin is hashed (SHA-256) and stored immutably before normalization.
2. **Exact Decimal Arithmetic**: All prices, volumes, and monetary turnover values are stored and manipulated using pure Python `Decimal`. Zero float conversions. Float inputs to Decimal parsers are strictly rejected (`TypeError`).
3. **No Fake Financial Zero**: Missing or corrupted numeric fields (including `close`) NEVER default to `Decimal("0")`. Missing remains `None` and triggers quarantine / `INVALID_OBSERVATION`.
4. **Point-In-Time (PIT) Integrity**: `trade_date` (economic effective date) is strictly decoupled from `retrieved_at` (network fetch timestamp in UTC).
5. **Two-Header PAY_BULTEN Architecture**: Documented official 2-header-row structure (Row 1: Turkish, Row 2: English, Row 3+: Observations) parsed seamlessly without treating English headers as data.
6. **Instrument Master Authority**: Symbol strings are external provider aliases. Master identity is a canonical UUID `id`. Raw provider symbols (`raw_provider_symbol`, e.g. `KOZAA.E`) are preserved alongside normalized symbols (`KOZAA`).
7. **ALTIN.S1 Economic Realism**: Modeled as an exchange-traded commodity certificate with verified reference facts (0.01g gold, 0.995 purity), priced strictly via exchange market discovery.
8. **Deterministic Conflict Quarantine**: Conflicting duplicate rows for the same symbol/date are deterministically quarantined with order-independence (no first-row authority).

---

## 2. Official Data Source & Access Classification

### 2.1 Official Source Pages & Discovery
- **Official Portal**: [Borsa İstanbul Bülten Verileri](https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri)
- **Equity Market Portal**: [Pay Piyasası Verileri](https://www.borsaistanbul.com/tr/sayfa/25/pay-piyasasi-verileri)
- **Historical DataStore Portal**: [BIST DataStore](https://datastore.borsaistanbul.com)
- **Verified Resource Locator**: Handled by `BISTBulletinLocator`, which determines verified official file paths and preserves discovery landing metadata.

### 2.2 Access Classification: YELLOW Provider
- `source_quality`: `SourceTier.TIER_2_EXCHANGE` (Official exchange data).
- `access_status`: `ProviderAccessStatus.YELLOW` (Public web bulletin download surface; not an SLA-guaranteed developer API).
- `official_source`: `True`
- `developer_api`: `False`
- `sla_guaranteed`: `False`

### 2.3 Provider Capabilities
- `CURRENT_DAILY_PUBLIC`: Latest daily bulletin publicly accessible.
- `HISTORICAL_PUBLIC_IF_AVAILABLE`: Historical dates within the public bulletin window.
- `HISTORICAL_DATASTORE_RESTRICTED`: Older historical dates moved to DataStore return explicit restricted status (`RESOURCE_NOT_FOUND` / DataStore note), never fabricated or assumed empty.

---

## 3. Official PAY_BULTEN Schema & Schema Drift

### 3.1 Documented File Specification
- **Filename**: `PAY_BULTEN_YYYYAAGG.csv` (e.g. `PAY_BULTEN_20241001.csv`).
- **Format**: CSV (or ZIP containing the canonical CSV).
- **Delimiter**: Semicolon (`;`).
- **Decimal Symbol**: Dot (`.`) in official technical specifications.
- **Frequency**: End-of-Day (EOD).
- **Header Rows**: Exactly 2 rows:
  - Row 1: Turkish Column Names
  - Row 2: English Column Names
  - Row 3+: Market Observation Records

### 3.2 Header Mapping Table
The parser normalizes Turkish (Row 1) and English (Row 2) columns to canonical field names:

| Column # | Official Turkish Header (Row 1) | Official English Header (Row 2) | Canonical Field |
| :---: | :--- | :--- | :--- |
| 1 | `PAZAR KODU` | `MARKET SEGMENT` | `market_segment` |
| 2 | `PAY KODU` | `INSTRUMENT CODE` | `symbol` / `raw_provider_symbol` |
| 3 | `PAY ADI` | `INSTRUMENT NAME` | `instrument_name` |
| 4 | `ONCEKI KAPANIS FIYATI` | `PREVIOUS CLOSING PRICE` | `previous_close` |
| 5 | `ACILIS FIYATI` | `OPENING PRICE` | `open` |
| 6 | `EN DUSUK FIYAT` | `LOWEST PRICE` | `low` |
| 7 | `EN YUKSEK FIYAT` | `HIGHEST PRICE` | `high` |
| 8 | `KAPANIS FIYATI` | `CLOSING PRICE` | `close` |
| 9 | `DEGISIM(%)` | `CHANGE(%)` | `change_pct` |
| 10 | `GUNLUK AGIRLIKLI ORTALAMA FIYAT` | `WAP` / `DAILY WEIGHTED AVERAGE PRICE` | `weighted_average` |
| 11 | `TOPLAM ISLEM HACMI` | `TOTAL TRADE VALUE` | `turnover` (monetary TRY) |
| 12 | `TOPLAM ISLEM ADEDI` | `TOTAL TRADE QUANTITY` | `volume` (traded shares) |
| 13 | `TOPLAM SOZLESME SAYISI` | `TOTAL NUMBER OF TRADES` | `trade_count` (trade count) |

### 3.3 Trade Date Semantics & Filename Date
- PAY_BULTEN data rows do NOT contain a trade date column.
- `trade_date` is determined by the verified bulletin context or official filename (`PAY_BULTEN_YYYYMMDD.csv`).
- If the requested trade date and filename date disagree $\to$ fails closed (`BISTSchemaDriftError`).
- **Required Columns**: `symbol`, `close` (no row-level trade date column requirement).

---

## 4. Numeric Integrity & No-Fabrication Policy

### 4.1 Strict Decimal & Zero-Float Policy
- Zero float conversion: `parse_bist_decimal` explicitly rejects `float` instances (`TypeError`).
- Parses strings directly into `Decimal` using `.` decimal point.

### 4.2 Missing / Malformed Numeric Handling
- Malformed or missing close prices remain `None` and flag the observation as `INVALID_OBSERVATION`.
- NEVER converts missing / bad values to `Decimal("0")`.

### 4.3 OHLC Integrity Invariants
- If `open`, `high`, `low`, `close` are present:
  - $High \ge \max(Open, Close)$
  - $Low \le \min(Open, Close)$
  - $High \ge Low$
- Violations are marked as `INVALID_OBSERVATION` with audit diagnostics.
- Negative prices or volumes are strictly invalid.

---

## 5. Instrument Identity & `ALTIN.S1` Modeling

### 5.1 Symbol Normalization & Raw Symbol Preservation
- BISTECH equity share suffix `.E` (e.g. `KOZAA.E`, `THYAO.E`) is stripped to canonical ticker `KOZAA`, `THYAO` for normalized resolution.
- `raw_provider_symbol` preserves the exact source string (`KOZAA.E`).
- **CRITICAL**: The `.S1` suffix on `ALTIN.S1` is NEVER stripped. `ALTIN.S1` and `ALTIN` are distinct financial identities.

### 5.2 `ALTIN.S1` Official Definition
- **Asset Class**: `AssetClass.COMMODITY`
- **Instrument Type**: `InstrumentType.COMMODITY_CERTIFICATE`
- **Canonical Name**: `Darphane Altın Sertifikası`
- **Issuer**: `T.C. Hazine ve Maliye Bakanlığı Darphane ve Damga Matbaası`
- **Underlying**: `gold`
- **Certificate Representation**: $1\text{ certificate} = 0.01\text{ gram gold}$
- **Purity**: $0.995$ ($995/1000$ fine gold)
- **Currency**: `TRY`
- **MIC**: `XIST`

### 5.3 Valuation & Price Invariant
- `ALTIN.S1` price comes strictly from BIST market transactions.
- Zero synthetic fair-value calculation (e.g. $gram\_gold \times 0.01$ prohibited).
- Zero premium/discount calculation in Phase 9A.

### 5.4 Unresolved Symbols
- Unknown symbols in the bulletin are quarantined with `instrument_id = None`, `status = BISTObservationStatus.UNRESOLVED_IDENTITY`, `confidence_level = DataConfidenceLevel.MEDIUM`.

---

## 6. Point-in-Time (PIT) Storage & Revision Semantics

- **Raw Snapshot**: Persisted in `raw_provider_snapshots` table with `payload_hash` (SHA-256).
- **Normalized Observations**: Persisted in `normalized_observations` table linking back to `snapshot_id`.
- **Idempotency**: Re-fetching identical payload produces identical hash $\to$ idempotent.
- **Revisions**: If BIST publishes a revised bulletin, a new raw snapshot is created with `supersedes_record_id`, ensuring full historical auditability.

---

## 7. Operational & Licensing Constraints

- **Internal Use Only**: Bulletin data is consumed internally by Sentinax for personal investment decision support. No public data redistribution API.
- **Non-Trading Days**: Weekends return `status = DataStatus.UNAVAILABLE` with diagnostic `"NON_TRADING_DAY"`. Empty weekday responses return `"EMPTY_SOURCE_PAYLOAD"` (not automatically assumed to be a holiday without calendar proof).
- **Exclusions**: Phase 9A does NOT compute returns, volatility, technical indicators, or adjusted prices.
