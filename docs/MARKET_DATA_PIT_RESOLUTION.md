# ⏱️ Point-in-Time Market Data Observation Resolver (BIST Equity + ALTIN.S1 + Precious Metals)

## 1. Overview & Architecture

The **Point-in-Time (PIT) Market Data Observation Resolver** (`backend/engine/private/market_data/resolver.py`) is the authoritative point-in-time pricing and reference observation gateway for Sentinax downstream analytical systems (Portfolio Valuation, Risk Engine, Technical Overlay, Optimizer).

Raw provider snapshots and normalized observation records alone cannot determine authoritative prices in historical backtesting or live execution because:
1. Bulletins are periodically corrected and re-downloaded at distinct timestamps.
2. Historical backtests require strict knowledge isolation (`as_of`) to prevent look-ahead bias.
3. Obsolete snapshots or corrupted entries must not be arbitrarily resurrected.

The resolver operates as a **pure functional, deterministic evaluator** with zero network calls, zero `datetime.now()` clock dependencies, and zero synthetic arithmetic.

---

## 2. Resolution Perspectives (`MarketDataResolutionMode`)

| Resolution Mode | Semantics | Availability / Constraints |
| :--- | :--- | :--- |
| **`CURRENT_REPORTED`** | Selects from the latest locally retrieved complete official snapshot for the target trade date. | Always available if at least one snapshot exists. `as_of` is `None`. |
| **`SYSTEM_AS_OF`** | Selects from the latest eligible complete official snapshot retrieved at or before the exact timezone-aware `as_of` timestamp. | Enforces strict lookahead protection: snapshots with `retrieved_at > as_of` are strictly ignored. Requires aware `as_of`. |
| **`SOURCE_AS_OF`** | Historical source knowledge perspective based on external public dissemination timestamps. | **`UNAVAILABLE_SOURCE_AS_OF`**: Public BIST and KMTP exchange bulletin feeds do not provide authoritative historical first-publication timestamps. |

---

## 3. Core Invariants & Chronology Rules

### 3.1 Full Snapshot Supersession (No Old-Snapshot Resurrection)
- Both BIST daily Pay Piyasası bulletins (`PAY_BULTEN_YYYYMMDD.csv` / `THB`) and KMTP daily bulletins (`KMP_Bulten_BISTECH.xlsx`) represent **full daily snapshot payloads**.
- A later valid snapshot for the same `trade_date` **completely supersedes** prior snapshots.
- If an instrument or precious-metal row was present and valid in Snapshot A (e.g. at 10:00 UTC), but is absent or corrupted in the corrected authoritative Snapshot B (e.g. at 11:00 UTC), the resolver **fails closed (`NO_ELIGIBLE_OBSERVATION`)** in `CURRENT_REPORTED` or `SYSTEM_AS_OF >= 11:00`. It is **strictly forbidden to backfill or resurrect** the obsolete value from Snapshot A.

### 3.2 Logical Snapshot Deduplication vs Snapshot Conflict
- **Logical Duplicates**: Multiple snapshots sharing identical `(provider, trade_date, retrieved_at, payload_hash)` with different database UUIDs are deduplicated deterministically.
- **Snapshot Conflict (`SNAPSHOT_CONFLICT`)**: Multiple snapshots sharing identical `(provider, trade_date, retrieved_at)` with **differing payload hashes** indicate non-deterministic data capture at the exact same timestamp. The resolver fails closed.

### 3.3 Strict Temporal Lineage
- Every candidate observation inside an authoritative snapshot must have matching `snapshot_id`, `payload_hash`, and `trade_date`. Any divergence fails closed with `INVALID_TEMPORAL_LINEAGE`.

---

## 4. Query Keys & Identity Authority

### 4.1 BIST Equity & Commodity Certificates (`BISTInstrumentQueryKey`)
- **Authority**: Canonical `instrument_id: UUID` and `trade_date: date`.
- Symbol strings (`THYAO`, `ALTIN.S1`) are diagnostic helpers only.
- Unresolved identity (`instrument_id is None` or `status = UNRESOLVED_IDENTITY`) cannot be selected as a canonical price.
- **`ALTIN.S1`**: Evaluated as `instrument_type = COMMODITY_CERTIFICATE`. Underlying physical conversion is never performed in this layer.

### 4.2 Precious Metals (`PreciousMetalSemanticKey`)
- Evaluated against explicit multi-dimensional coordinates:
  ```
  (metal, effective_date, price_currency, quantity_unit, price_type, price_quantity, fineness_per_mille, settlement_term, value_date, raw_value_date_text, provider, originating_source)
  ```
- Summary benchmarks (`Fiyatlar` sheet) with `fineness_per_mille = None` and `settlement_term = None` can be resolved individually via exact query without collision.
- Distinct settlement tokens (e.g. `"2608"` vs `"2708"`) or terms (`"T+0"` vs `None`) remain strictly isolated.

---

## 5. Defense In Depth

1. **No FX / Unit Conversion**: USD gold is never converted to TRY gold; KG is never converted to Troy Ounce or Grams. Pure observed facts only.
2. **No Trading Day Fallback**: The resolver strictly evaluates the requested `effective_date`. It never falls back to the previous trading session (calendar roll is handled in higher orchestration layers).
3. **No Non-Finite Decimals**: Observations with `NaN`, `sNaN`, `Infinity`, or `-Infinity` are rejected.
4. **Confidence Propagation & Stale Discovery**:
   - Resolution confidence never exceeds observation confidence.
   - Snapshots resolved with `is_stale_discovery = True` append diagnostic `DEGRADED_DISCOVERY` and degrade `HIGH` confidence to `MEDIUM`.
5. **Deterministic Resolution Key**: Every resolution generates a SHA-256 `resolution_key` capturing the exact input parameters and selected observation identity.
