# 🏛️ Borsa İstanbul (BIST) Equity EOD & ALTIN.S1 Market Data Backbone

## 1. Overview & Architecture

Sentinax implements an official, point-in-time (PIT) compliant End-of-Day (EOD) market data backbone for Borsa İstanbul (BIST) Equities and the Darphane Gold Certificate (`ALTIN.S1`).

The pipeline follows Sentinax core data principles:
1. **Raw Snapshot First**: Every downloaded bulletin is hashed (SHA-256) and stored immutably before normalization.
2. **Exact Decimal Arithmetic**: All prices, volumes, and monetary turnover values are stored and manipulated using pure Python `Decimal`. Zero float conversions. Float inputs to Decimal parsers are strictly rejected (`TypeError`).
3. **No Fake Financial Zero**: Missing or corrupted numeric fields (including `close`) NEVER default to `Decimal("0")`. Missing remains `None` and triggers quarantine / `INVALID_OBSERVATION`.
4. **Point-In-Time (PIT) Integrity**: `trade_date` (economic effective date) is strictly decoupled from `retrieved_at` (network fetch timestamp in UTC).
5. **Verified Manifest-Driven Discovery**: File/directory paths are derived dynamically from the official Borsa İstanbul `DataFilePaths.zip` directory authority. No hard-coded guessed URLs.
6. **Two-Header Schema Architecture**: Documented official 2-header-row structure (Row 1: Turkish, Row 2: English, Row 3+: Observations) parsed seamlessly without treating English headers as data.
7. **Instrument Master Authority**: Symbol strings are external provider aliases. Master identity is a canonical UUID `id`. Raw provider symbols (`raw_provider_symbol`, e.g. `KOZAA.E`) are preserved alongside normalized symbols (`KOZAA`).
8. **ALTIN.S1 Economic Realism**: Modeled as an exchange-traded commodity certificate with verified reference facts (0.01g gold, 0.995 purity), priced strictly via exchange market discovery.
9. **Deterministic Conflict Quarantine**: Conflicting duplicate rows for the same symbol/date are deterministically quarantined with order-independence (no first-row authority).

---

## 2. Official Discovery Authority: `DataFilePaths.zip`

### 2.1 Official Discovery Evidence
- **Discovery Endpoint**: `https://www.borsaistanbul.com/files/DataFilePaths.zip`
- **Retrieval Date**: `2026-08-27`
- **Observed Payload SHA-256**: `3fe49eaaf3a2787e7b19040f5340856931d4bbdf63aaf01b035f2d86855029ed`
- **Internal Member**: `VerilerDosyaIsimleri.xlsx` (Excel Spreadsheet)

### 2.2 Observed Manifest Structure & Mappings
The official manifest file `VerilerDosyaIsimleri.xlsx` defines the directory hierarchy across two sheets:
- Sheet 1 (`TR - www.borsaistanbul.com`):
  - **Row 9**:
    - `Açıklama`: `Bülten Verileri`
    - `Web Sitesi Dizin Adresi`: `/data/thb/YYYY/AA/`
    - `Dosya Adı`: `thbYYYYAAGGS.zip` (where `YYYY` is year, `AA` is month, `GG` is day, and `S=1` denotes single session)
- Sheet 2 (`EN - www.borsaistanbul.com`):
  - **Row 6**:
    - `Name`: `Bulletin Data`
    - `Directory Structure`: `/data/ehb/YYYY/AA/`
    - `File Name`: `ehbYYYYAAGGS.zip`

### 2.3 Verified Resource Resolution Pattern
Given trade date `2024-10-01`:
1. `BISTDirectoryManifestParser` extracts `Bülten Verileri` $\to$ `/data/thb/YYYY/AA/` and `thbYYYYAAGGS.zip`.
2. `BISTBulletinLocator` formats date tokens:
   - Directory: `/data/thb/2024/10/`
   - Filename: `thb202410011.zip`
   - Download URL: `https://www.borsaistanbul.com/data/thb/2024/10/thb202410011.zip`
3. Inside `thb202410011.zip`, the canonical CSV is `thb202410011.csv`.

---

## 3. Security, Domain Whitelisting & Cache Policy

### 3.1 Domain Whitelist (SSRF Defense)
- All resolved URLs must use scheme `https://`.
- Hostname must be strictly within `BIST_OFFICIAL_HOSTS`:
  - `www.borsaistanbul.com`
  - `borsaistanbul.com`
- Path traversal elements (`..`) and javascript URIs are strictly rejected with `UNSAFE_RESOLVED_URL`.

### 3.2 Cache & Staleness Policy
- Manifest is cached in-memory with a `ttl_seconds = 86400.0` (24 hours).
- If network refresh fails temporarily, a cached manifest is permitted up to `max_stale_seconds = 604800.0` (7 days) with explicit diagnostic `DEGRADED_DISCOVERY`.
- If cache is empty or older than 7 days, provider fails closed with `DISCOVERY_UNAVAILABLE`.

---

## 4. Access Classification: YELLOW Provider

- `source_quality`: `SourceTier.TIER_2_EXCHANGE` (Official exchange data).
- `access_status`: `ProviderAccessStatus.YELLOW` (Public web bulletin download surface; not an SLA-guaranteed developer API).
- `official_source`: `True`
- `developer_api`: `False`
- `sla_guaranteed`: `False`

### Capabilities
- `CURRENT_DAILY_PUBLIC`: Latest daily bulletin publicly accessible.
- `HISTORICAL_PUBLIC_IF_AVAILABLE`: Historical dates within the public bulletin window.
- `HISTORICAL_DATASTORE_RESTRICTED`: Older historical dates moved to DataStore return explicit restricted status (`RESOURCE_NOT_FOUND` / DataStore note), never fabricated or assumed empty.

---

## 5. Official PAY_BULTEN & THB CSV Schema

### 5.1 Documented Header Structure
- **Format**: CSV with `;` delimiter, `.` decimal point.
- **Header Structure**: 2 header rows:
  - Row 1: Turkish Column Names
  - Row 2: English Column Names
  - Row 3+: Market Observation Records

### 5.2 Header Mapping Table
The parser normalizes Turkish (Row 1) and English (Row 2) columns to canonical field names:

| Official Turkish Header (Row 1) | Official English Header (Row 2) | Canonical Field |
| :--- | :--- | :--- |
| `PAZAR KODU` / `PAZAR` | `MARKET SEGMENT` | `market_segment` |
| `ISLEM  KODU` / `PAY KODU` | `INSTRUMENT SERIES CODE` / `INSTRUMENT CODE` | `symbol` / `raw_provider_symbol` |
| `BULTEN ADI` / `PAY ADI` | `INSTRUMENT NAME` | `instrument_name` |
| `ONCEKI KAPANIS FIYATI` | `PREVIOUS LAST PRICE` / `PREVIOUS CLOSING PRICE` | `previous_close` |
| `ACILIS FIYATI` | `OPENING PRICE` | `open` |
| `EN DUSUK FIYAT` | `LOWEST PRICE` | `low` |
| `EN YUKSEK FIYAT` | `HIGHEST PRICE` | `high` |
| `KAPANIS FIYATI` | `CLOSING PRICE` | `close` |
| `DEGISIM (%)` / `DEGISIM(%)` | `CHANGE TO PREVIOUS CLOSING (%)` / `CHANGE(%)` | `change_pct` |
| `A.O.F` / `GUNLUK AGIRLIKLI ORTALAMA FIYAT` | `VWAP` / `WAP` | `weighted_average` |
| `TOPLAM ISLEM HACMI` | `TOTAL TRADED VALUE` / `TOTAL TRADE VALUE` | `turnover` (monetary TRY) |
| `TOPLAM ISLEM ADEDI` | `TOTAL TRADED VOLUME` / `TOTAL TRADE QUANTITY` | `volume` (traded shares) |
| `TOPLAM SOZLESME SAYISI` | `TOTAL NUMBER OF CONTRACTS` / `TOTAL NUMBER OF TRADES`| `trade_count` (trade count) |

---

## 6. Instrument Identity & `ALTIN.S1` Modeling

### 6.1 Symbol Normalization & Raw Symbol Preservation
- BISTECH equity share suffix `.E` (e.g. `KOZAA.E`, `THYAO.E`) is stripped to canonical ticker `KOZAA`, `THYAO` for master resolution.
- `raw_provider_symbol` preserves the exact source string (`KOZAA.E`).
- **CRITICAL**: The `.S1` suffix on `ALTIN.S1` is NEVER stripped. `ALTIN.S1` and `ALTIN` are distinct financial identities.

### 6.2 `ALTIN.S1` Official Definition
- **Asset Class**: `AssetClass.COMMODITY`
- **Instrument Type**: `InstrumentType.COMMODITY_CERTIFICATE`
- **Canonical Name**: `Darphane Altın Sertifikası`
- **Issuer**: `T.C. Hazine ve Maliye Bakanlığı Darphane ve Damga Matbaası`
- **Underlying**: `gold`
- **Certificate Representation**: $1\text{ certificate} = 0.01\text{ gram gold}$
- **Purity**: $0.995$ ($995/1000$ fine gold)
- **Currency**: `TRY`
- **MIC**: `XIST`

### 6.3 Valuation & Price Invariant
- `ALTIN.S1` price comes strictly from BIST market transactions.
- Zero synthetic fair-value calculation (e.g. $gram\_gold \times 0.01$ prohibited).
- Zero premium/discount calculation in Phase 9A.
