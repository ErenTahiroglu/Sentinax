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
2. **Authoritative Metal-Specific Purity Scale (No Magnitude Heuristics)**:
   - **BIST Gold Convention**: Saflık is declared per mille ($x/1000$, standard minimum threshold $995.0$). `purity_scale = "PER_MILLE"`, `fineness_per_mille = raw_purity_val` (valid range: $0 < \text{val} \le 1000$).
   - **BIST Silver Convention**: Saflık is declared in percent ($x/100$, standard minimum threshold $99.9\%$). `purity_scale = "PERCENT"`, `fineness_per_mille = raw_purity_val \times 10` (valid range: $0 < \text{val} \le 100$).
   - **Unsupported/Unknown Metals**: Preserves raw value/text, sets `purity_scale = "UNKNOWN"`, and canonical `fineness_per_mille = None` (forbidden to infer scale from numeric magnitude).
   - **Summary Benchmarks (`Fiyatlar` sheet)**: Purity is `None` (summary benchmarks do not declare purity).
3. **Raw Settlement Provenance & No-Fabrication Date Policy**:
   - `Takas Tarihi` cell text is preserved verbatim in `raw_value_date_text` (e.g. `"2608"`).
   - Arbitrary rollover or current-year attachment is strictly forbidden (`value_date = None`, `settlement_term = None`).
   - Literal `"T+0"` tokens explicitly set `settlement_term = "T+0"` and `value_date = effective_date`.
4. **Finite Decimal & Discrete Integral Counts**:
   - `parse_kmtp_decimal` and `TCMBEVDSProvider._parse_exact_decimal` reject `NaN`, `sNaN`, `+Infinity`, `-Infinity`.
   - `parse_kmtp_int` strictly rejects floats, non-integral values (e.g. `"1.5"` $\to$ `None`), and negative counts.
   - `Decimal("0")` remains valid where zero is semantically permitted.
5. **Snapshot Lineage**:
   - `to_normalized_observation_record()` preserves `snapshot_id` from raw snapshot record. No synthetic UUIDs are generated during conversion.

---

## 5. Cross-Source Semantic Comparability

To prevent invalid financial comparisons, `PreciousMetalCrossSourceComparator` enforces strict multi-dimensional equivalence:

```
(metal, price_currency, quantity_unit, price_quantity, price_type, purity_semantics, settlement_semantics, effective_date)
```

1. **`CONSISTENT`**: All dimensions match AND `price_a == price_b`.
2. **`DIVERGENT`**: All dimensions match BUT `price_a != price_b`.
3. **`NOT_COMPARABLE`**: Any dimension differs:
   - Currency mismatch (`TRY` vs `USD`).
   - Unit mismatch (`KG` vs `TROY_OZ`).
   - Price type mismatch (`REFERENCE` vs `WEIGHTED_AVERAGE`).
   - Purity unverified/missing: Both observations MUST have authoritative canonical `fineness_per_mille` that are equal. Observations with `fineness_per_mille = None` (including summary benchmarks) fail closed to `NOT_COMPARABLE`.
   - Settlement mismatch (`None` vs `"T+0"`, or differing `raw_value_date_text`).

Cross-source comparison never fabricates an average or reconciles differing units silently.
