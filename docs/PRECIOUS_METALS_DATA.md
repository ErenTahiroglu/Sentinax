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

## 3. TCMB EVDS Dissemination Series

TCMB EVDS disseminates official BIST-originating precious metals market series under "Döviz Kurları ve Kıymetli Madenler":

| Series Code | Canonical Name | Metal | Currency | Quantity Unit | Price Type | Purity | Originating Source |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `TP.MK.G.ALTIN.USD` | Altın Piyasası (BİST) AOF (USD/ONS) | GOLD | USD | TROY_OZ | WEIGHTED_AVERAGE | 995.0 | BIST |
| `TP.MK.G.ALTIN.TRY` | Altın Piyasası (BİST) AOF (TRY/KG) | GOLD | TRY | KG | WEIGHTED_AVERAGE | 995.0 | BIST |
| `TP.MK.G.GUMUS.USD` | Gümüş Piyasası (BİST) AOF (USD/ONS) | SILVER | USD | TROY_OZ | WEIGHTED_AVERAGE | 99.90 | BIST |

---

## 4. Cross-Source Semantic Comparability

To prevent invalid financial comparisons, `PreciousMetalCrossSourceComparator` enforces strict 8-dimensional equivalence:

```
(metal, price_currency, quantity_unit, price_quantity, price_type, purity, settlement_term, effective_date)
```

1. **`CONSISTENT`**: All 8 dimensions match AND `price_a == price_b`.
2. **`DIVERGENT`**: All 8 dimensions match BUT `price_a != price_b`.
3. **`NOT_COMPARABLE`**: Any dimension differs (e.g. `TRY/KG` vs `USD/OZ`, `995` vs `999.9`, `REFERENCE` vs `WEIGHTED_AVERAGE`, or `T+0` vs `T+1`).

Cross-source comparison never fabricates an average or reconciles differing units silently.
