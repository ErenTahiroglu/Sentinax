# 🏛️ Borsa İstanbul (BIST) Equity EOD & ALTIN.S1 Market Data Backbone

## 1. Overview & Architecture

Sentinax implements an official, point-in-time (PIT) compliant End-of-Day (EOD) market data backbone for Borsa İstanbul (BIST) Equities and the Darphane Gold Certificate (`ALTIN.S1`).

The pipeline follows Sentinax core data principles:
1. **Raw Snapshot First**: Every downloaded bulletin is hashed (SHA-256) and stored immutably before normalization.
2. **Exact Decimal Arithmetic**: All prices, volumes, and monetary turnover values are stored and manipulated using pure Python `Decimal`. Zero floating-point conversions.
3. **Point-In-Time (PIT) Integrity**: `trade_date` (economic effective date) is strictly decoupled from `retrieved_at` (network fetch timestamp in UTC).
4. **Instrument Master Authority**: Symbol strings are external provider aliases. Master identity is a canonical UUID `id`.
5. **ALTIN.S1 Economic Realism**: Modeled as an exchange-traded commodity certificate with verified reference facts (0.01g gold, 0.995 purity), priced strictly via exchange market discovery.

---

## 2. Official Data Source & Access Classification

### 2.1 Official Source Pages & Endpoints
- **Official Portal**: [Borsa İstanbul Bülten Verileri](https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri)
- **Equity Market Portal**: [Pay Piyasası Verileri](https://www.borsaistanbul.com/tr/sayfa/25/pay-piyasasi-verileri)
- **Direct Daily Bulletin Download URL**: `https://www.borsaistanbul.com/data/bulten/` (e.g. `bulten_YYYYMMDD.zip` or `gunluk_bulten_YYYYMMDD.csv`)
- **Historical DataStore Portal**: [BIST DataStore](https://datastore.borsaistanbul.com)

### 2.2 Access Classification: YELLOW Provider
- `source_quality`: `SourceTier.TIER_2_EXCHANGE` (Official exchange data).
- `access_status`: `ProviderAccessStatus.YELLOW` (Public web bulletin download surface; not an SLA-guaranteed developer API).
- `official_source`: `True`
- `developer_api`: `False`
- `sla_guaranteed`: `False`

### 2.3 Provider Capabilities
- `CURRENT_DAILY_PUBLIC`: Latest daily bulletin publicly accessible.
- `HISTORICAL_PUBLIC_IF_AVAILABLE`: Historical dates within the public bulletin window.
- `HISTORICAL_DATASTORE_RESTRICTED`: Older historical dates moved to DataStore return explicit restricted status (HTTP 404 / `HISTORICAL_DATASTORE_RESTRICTED`), never fabricated or assumed empty.

---

## 3. Bulletin File Format & Schema Drift

### 3.1 Format & Delimiters
- Format: CSV, TXT, or ZIP archive containing daily BISTECH bulletin data.
- Delimiters: Semicolon (`;`), comma (`,`), or tab (`\t`).
- Encoding: UTF-8 with BOM (`utf-8-sig`) or UTF-8.

### 3.2 Header Mapping Table
The parser normalizes Turkish and English header variants to canonical column names:

| Canonical Field | Recognized Header Variants |
| :--- | :--- |
| `trade_date` | `BULTEN_TARIHI`, `BÜLTEN TARİHİ`, `TARIH`, `TARİH`, `DATE`, `TRADE_DATE` |
| `symbol` | `HISSE_KODU`, `HİSSE KODU`, `MENKUL_KIYMET_KODU`, `INSTRUMENT_CODE`, `SYMBOL`, `KOD` |
| `market_segment` | `PAZAR`, `PAZAR_KODU`, `SEKTOR`, `MARKET`, `MARKET_SEGMENT`, `GRUP_KODU` |
| `close` | `KAPANIS_FIYATI`, `KAPANIŞ FİYATI`, `KAPANIS`, `CLOSE_PRICE`, `CLOSE`, `SON_FIYAT` |
| `open` | `ACILIS_FIYATI`, `AÇILIŞ FİYATI`, `ACILIS`, `OPEN_PRICE`, `OPEN`, `ILK_FIYAT` |
| `high` | `EN_YUKSEK_FIYAT`, `EN YÜKSEK FİYAT`, `EN_YUKSEK`, `HIGH_PRICE`, `HIGH`, `MAX_PRICE` |
| `low` | `EN_DUSUK_FIYAT`, `EN DÜŞÜK FİYAT`, `EN_DUSUK`, `LOW_PRICE`, `LOW`, `MIN_PRICE` |
| `previous_close` | `ONCEKI_KAPANIS_FIYATI`, `ÖNCEKİ KAPANIŞ FİYATI`, `ONCEKI_KAPANIS`, `PREVIOUS_CLOSE` |
| `weighted_average`| `AGIRLIKLI_ORTALAMA_FIYAT`, `AOF`, `WEIGHTED_AVERAGE_PRICE`, `WAP` |
| `volume` | `ISLEM_MIKTARI`, `İŞLEM MİKTARI`, `TOPLAM_ISLEM_MIKTARI`, `VOLUME`, `LOT` |
| `turnover` | `ISLEM_HACMI`, `İŞLEM HACMİ`, `ISLEM_HACMI_TL`, `TURNOVER`, `TOTAL_TURNOVER` |
| `trade_count` | `SOZLESME_SAYISI`, `SÖZLEŞME SAYISI`, `ISLEM_ADEDI`, `TRADE_COUNT`, `NUM_TRADES` |

### 3.3 Schema Drift Policy
- **Required Columns**: `trade_date`, `symbol`, `close`.
- If any required column is missing $\to$ `BISTSchemaDriftError` / `SCHEMA_DRIFT` fail closed.
- Unknown optional columns are safely ignored for normalization while preserved in the raw snapshot payload.

---

## 4. Numeric & Locale Parsing

### 4.1 Locale-Safe Number Parsing
- Supports Turkish formatting (`1.234,56` $\to$ `1234.56`) and standard decimal formatting (`1234.56`).
- Converts directly into `Decimal` without intermediate `float` conversion.
- Rejects non-finite values (`NaN`, `Infinity`).

### 4.2 OHLC Integrity Invariants
- If `open`, `high`, `low`, `close` are all present:
  - $High \ge \max(Open, Close)$
  - $Low \le \min(Open, Close)$
  - $High \ge Low$
- Violations are marked as `INVALID_OBSERVATION` with diagnostic audit notes. No artificial "fixing" of bad source data.
- Negative prices or volumes are strictly invalid.

---

## 5. Instrument Identity & `ALTIN.S1` Modeling

### 5.1 Symbol Normalization
- BISTECH equity share suffix `.E` (e.g. `THYAO.E`, `GARAN.E`) is stripped to canonical ticker `THYAO`, `GARAN`.
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
- Zero synthetic fair-value calculation (e.g. $gram\_gold \times 0.01$ is prohibited in this phase).
- Zero premium/discount calculation in Phase 9A.

### 5.4 Unresolved Symbols
- Unknown symbols in the bulletin are not silently created as fake master instruments.
- Quarantined with `instrument_id = None`, `status = BISTObservationStatus.UNRESOLVED_IDENTITY`, `confidence_level = DataConfidenceLevel.MEDIUM`.

---

## 6. Point-in-Time (PIT) Storage & Revision Semantics

- **Raw Snapshot**: Persisted in `raw_provider_snapshots` table with `payload_hash` (SHA-256).
- **Normalized Observations**: Persisted in `normalized_observations` table linking back to `snapshot_id`.
- **Idempotency**: Re-fetching identical payload produces identical hash $\to$ idempotent.
- **Revisions**: If BIST publishes a revised bulletin, a new raw snapshot is created with `supersedes_record_id`, ensuring full historical auditability.

---

## 7. Operational & Licensing Constraints

- **Internal Use Only**: Bulletin data is consumed internally by Sentinax for personal investment decision support. No public data redistribution API.
- **Non-Trading Days**: Weekends and official exchange holidays return `status = DataStatus.UNAVAILABLE` with diagnostic `"NON_TRADING_DAY"`, clearly distinguished from network errors.
- **Exclusions**: Phase 9A does NOT compute returns, volatility, technical indicators, or adjusted prices.
