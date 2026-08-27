# 🥇 Precious Metals (Gold / Silver) Market Backbone

## 1. Overview & Architecture

Sentinax implements an institutional, point-in-time (PIT) compliant market data backbone for Precious Metals (**Gold** and **Silver**), unifying:
1. **Borsa İstanbul (BIST) KMTP**: Official originating exchange market (Kıymetli Madenler ve Kıymetli Taşlar Piyasası).
2. **TCMB EVDS**: Official monetary authority dissemination system (BIST Gold / Silver Market series).

### Core Invariants:
1. **Reference != Investable Instrument**: Market reference observations are modeled explicitly as `PRECIOUS_METAL_MARKET_REFERENCE`. They are not physical retail gram gold, bank accounts, or `ALTIN.S1`.
2. **Never Universal "Gold Price"**: Every price is dimensioned with `(metal, currency, quantity_unit, purity, price_type, settlement_term, effective_date)`.
3. **Exact Decimal Arithmetic**: Zero float usage. Float inputs to Decimal parsers are strictly rejected (`TypeError`). Missing values remain `None`, never `Decimal("0")`.
4. **No Retail Gold**: Kapalıçarşı, local jewellers, and retail bank spread quotes are strictly excluded from institutional reference data.
5. **No Synthetic FX / Unit Conversion**: USD gold is not converted to TRY gold, and Kilogram/Troy Ounce are not converted to Gram in this ingestion layer. Pure observed exchange facts are preserved.
6. **No ALTIN.S1 Premium/Discount**: `ALTIN.S1` remains an exchange-traded certificate priced via equity market discovery; BIST KMTP physical gold references remain decoupled.

---

## 2. Live Discovery Authority & BIST KMTP Structure

### 2.1 Manifest Discovery Evidence
- **Discovery Endpoint**: `https://www.borsaistanbul.com/files/DataFilePaths.zip`
- **Retrieval Date**: `2026-08-27`
- **Observed Manifest Key**:
  - Turkish: `Kıymetli Madenler Piyasası Günlük Bülten`
  - Directory: `/data/kmpbltn/YYYY/AA/`
  - Filename: `KMPYYYYAAGG.zip` (English: `Precious Metals Market Bulletins Daily Bulletin` $\to$ `/data/kmtpbltn/YYYY/AA/PMDYYYYAAGG.zip`)
- **Example Verified Target**: `https://www.borsaistanbul.com/data/kmpbltn/2026/08/KMP20260826.zip`

### 2.2 Official Archive Content & Workbook Schema
The daily archive `KMPYYYYAAGG.zip` contains:
- `KMP_Bulten.pdf` (Official exchange publication)
- `KMP_Bulten_BISTECH.xlsx` (Machine-readable BISTECH workbook)

The workbook `KMP_Bulten_BISTECH.xlsx` contains the following sheets:
1. **Sheet 5 (`Fiyatlar`)**:
   - `Referans Fiyat (TRY/KG)`: Official daily standard gold and silver reference prices.
   - `Metal Fiyatı (TRY/KG, USD/ONS, EUR/ONS)`: Benchmark prices per currency/unit.
   - `AOF (USD/ONS, TRY/KG)`: Daily volume-weighted average prices.
2. **Sheet 2 (`Seri İstatistikleri`)**:
   - Granular transaction records categorized by series, metal (`Altın`, `Gümüş`), bar type (`Külçe`, `Granül Torbası`), price unit (`TRY/KG`, `USD/OZ`, `EUR/ONS`), purity (`Ayar`: `995.0`, `99.90`, `99.99`), settlement date (`Takas Tarihi`), volume (`Adet`), turnover, trade count, AOF, high, low, close.
3. **Sheet 3 (`T+0 Fiyat İstatistikleri`)**:
   - T+0 settlement price statistics for standard and non-standard metals.

---

## 3. TCMB EVDS Dissemination Series (Unverified Status)

> [!IMPORTANT]
> **EVDS Verification Status**: EVDS precious-metal series (`TP.MK.G.ALTIN.USD`, `TP.MK.G.ALTIN.TRY`, `TP.MK.G.GUMUS.USD`) are marked **`UNVERIFIED`** and **`is_active = False`**.
> They are disabled in production runtime pending authenticated official metadata response verification from TCMB EVDS API. `TCMBEVDSProvider` refuses requests for unverified precious metal series and returns `DataStatus.UNAVAILABLE` without making unverified network requests.

Candidate definitions (subject to authenticated metadata proof):

| Series Code | Canonical Name | Metal | Currency | Quantity Unit | Price Type | Verification Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `TP.MK.G.ALTIN.USD` | Altın Piyasası (BİST) AOF (USD/ONS) | GOLD | USD | TROY_OZ | WEIGHTED_AVERAGE | `UNVERIFIED` (Disabled) |
| `TP.MK.G.ALTIN.TRY` | Altın Piyasası (BİST) AOF (TRY/KG) | GOLD | TRY | KG | WEIGHTED_AVERAGE | `UNVERIFIED` (Disabled) |
| `TP.MK.G.GUMUS.USD` | Gümüş Piyasası (BİST) AOF (USD/ONS) | SILVER | USD | TROY_OZ | WEIGHTED_AVERAGE | `UNVERIFIED` (Disabled) |

---

## 4. Purity, Settlement & PIT Semantics Hardening

1. **Economic Date Resolution (Zero `date.today()` Fallback)**:
   - Economic date MUST originate from either explicit `trade_date` or verified outer filename (`KMPYYYYAAGG.zip`).
   - If both are supplied and mismatch, parser fails closed with `BISTKMTPSchemaDriftError`.
   - If neither exists, parsing fails closed with `MISSING_EFFECTIVE_DATE`. System clock is never used.
2. **Purity Representation & Scale**:
   - Summary benchmarks (`Fiyatlar` sheet): Purity is `None` (summary benchmarks do not declare purity).
   - Granular transaction series (`Seri İstatistikleri`): Raw purity text and value are preserved. Purity scale is classified as `"PER_MILLE"` (e.g. `995.0`) or `"PERCENT"` (e.g. `99.9%`), with `fineness_per_mille` calculated strictly where verified.
3. **Settlement Term Semantics (No Default `T+0`)**:
   - `settlement_term` is `None` (unknown) unless explicitly established by source data.
   - `None` (unknown settlement) and `"T+0"` (explicit T+0 settlement) are strictly non-equivalent.
4. **Snapshot Lineage**:
   - `to_normalized_observation_record()` preserves `snapshot_id` from raw snapshot record. No synthetic UUIDs are generated during conversion.

---

## 5. Cross-Source Semantic Comparability

To prevent invalid financial comparisons, `PreciousMetalCrossSourceComparator` enforces strict 8-dimensional equivalence:

```
(metal, price_currency, quantity_unit, price_quantity, price_type, purity_semantics, settlement_term, effective_date)
```

1. **`CONSISTENT`**: All 8 dimensions match AND `price_a == price_b`.
2. **`DIVERGENT`**: All 8 dimensions match BUT `price_a != price_b`.
3. **`NOT_COMPARABLE`**: Any dimension differs (e.g. `TRY/KG` vs `USD/OZ`, `995‰` vs `None`, `REFERENCE` vs `WEIGHTED_AVERAGE`, or `None` vs `T+0`).

Cross-source comparison never fabricates an average or reconciles differing units silently.
