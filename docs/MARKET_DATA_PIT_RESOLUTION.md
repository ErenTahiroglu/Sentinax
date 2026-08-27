# ⏱️ Point-in-Time Market Data Observation Resolver (BIST Equity + ALTIN.S1 + Precious Metals)

## 1. Overview & Architecture

The **Point-in-Time (PIT) Market Data Observation Resolver** (`backend/engine/private/market_data/resolver.py`) is the authoritative point-in-time pricing and reference observation gateway for Sentinax downstream analytical systems (Portfolio Valuation, Risk Engine, Technical Overlay, Optimizer).

Raw provider snapshots and normalized observation records alone cannot determine authoritative prices in historical backtesting or live execution because:
1. Bulletins are periodically corrected and re-downloaded at distinct timestamps.
2. Historical backtests require strict knowledge isolation (`as_of`) to prevent look-ahead bias.
3. Obsolete snapshots or corrupted entries must not be arbitrarily resurrected.
4. Transport failures (HTTP 500) must not erase previously retrieved valid market state.

The resolver operates as a **pure functional, deterministic evaluator** with zero network calls, zero `datetime.now()` clock dependencies, and zero synthetic arithmetic.

---

## 2. Resolution Perspectives (`MarketDataResolutionMode`)

| Resolution Mode | Semantics | Availability / Constraints |
| :--- | :--- | :--- |
| **`CURRENT_REPORTED`** | Selects from the latest locally retrieved successful (HTTP 200) official full snapshot for the target trade date. | Always available if at least one successful snapshot exists. `as_of` is `None`. |
| **`SYSTEM_AS_OF`** | Selects from the latest eligible successful (HTTP 200) official full snapshot retrieved at or before the exact timezone-aware `as_of` timestamp. | Enforces strict lookahead protection: snapshots with `retrieved_at > as_of` are filtered out **before** conflict checks and observation lineage evaluation. Requires aware `as_of`. |
| **`SOURCE_AS_OF`** | Historical source knowledge perspective based on external public dissemination timestamps. | **`UNAVAILABLE_SOURCE_AS_OF`**: Public BIST and KMTP exchange bulletin feeds do not provide authoritative historical first-publication timestamps. |

---

## 3. Core Invariants & Chronology Rules

### 3.1 SYSTEM_AS_OF Chronological Isolation (No Future Leakage)
- In `SYSTEM_AS_OF` mode, temporal filtering (`snapshot.retrieved_at <= as_of`) executes **prior** to conflict detection and observation evaluation.
- Future snapshot conflicts (e.g. two conflicting bulletin payloads downloaded at 11:00 UTC) do **not** poison or invalidate a historical `SYSTEM_AS_OF` query at 10:30 UTC.
- Future observation corruptions (e.g. malformed rows or lineage mismatches at 11:00 UTC) do **not** contaminate a historical `SYSTEM_AS_OF` query at 10:30 UTC.

### 3.2 Successful Full Snapshot Authority (HTTP 200 Rule)
- An HTTP transport error (e.g. HTTP 500, 502, 503) or an empty payload does **not** constitute a valid official exchange bulletin.
- Failed snapshot fetch attempts do **not** supersede or erase existing valid full market snapshots for that trade date.
- In `CURRENT_REPORTED` and `SYSTEM_AS_OF`, only snapshots with `http_status == 200` and non-empty `payload_hash` are eligible for authoritative selection.

### 3.3 Full Snapshot Supersession (No Old-Snapshot Resurrection)
- Both BIST daily Pay Piyasası bulletins (`PAY_BULTEN_YYYYMMDD.csv` / `THB`) and KMTP daily bulletins (`KMP_Bulten_BISTECH.xlsx`) represent **full daily snapshot payloads**.
- A later valid snapshot for the same `trade_date` **completely supersedes** prior snapshots.
- If an instrument or precious-metal row was present and valid in Snapshot A (e.g. at 10:00 UTC), but is absent or corrupted in the corrected authoritative Snapshot B (e.g. at 11:00 UTC), the resolver **fails closed (`NO_ELIGIBLE_OBSERVATION`)** in `CURRENT_REPORTED` or `SYSTEM_AS_OF >= 11:00`. It is **strictly forbidden to backfill or resurrect** the obsolete value from Snapshot A.

### 3.4 Logical Snapshot Deduplication vs Snapshot Conflict
- **Logical Duplicates**: Multiple snapshots sharing identical `(trade_date, retrieved_at, payload_hash)` with different database UUIDs are deduplicated deterministically by selecting the minimum UUID string.
- **Snapshot Conflict (`SNAPSHOT_CONFLICT`)**: Multiple snapshots sharing identical `(trade_date, retrieved_at)` with **differing payload hashes** within the eligible snapshot set fail closed.

### 3.5 Logical Observation Fingerprinting & Deduplication
- Multiple valid observations matching the target query within an authoritative snapshot are evaluated via **deterministic logical fingerprints**:
  - Excludes random record UUIDs.
  - BIST fingerprint: `(instrument_id, trade_date, symbol, raw_provider_symbol, open, high, low, close, previous_close, weighted_average, volume, turnover, trade_count, currency, market_segment, instrument_type, status, snapshot_hash)`.
  - Precious metals fingerprint: `(metal, effective_date, price, price_currency, quantity_unit, price_type, price_quantity, fineness_per_mille, settlement_term, value_date, raw_value_date_text, market, provider, originating_source, status, payload_hash)`.
- If fingerprints are identical: observations are exact logical duplicates and deduplicate deterministically.
- If fingerprints differ (e.g. identical `close` price but conflicting `high`/`volume`): the resolver fails closed with `OBSERVATION_CONFLICT`.

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

1. **UUID-Independent Resolution Key**: Every resolution generates a deterministic SHA-256 `resolution_key` derived strictly from economic inputs, authoritative snapshot payload hash, and the selected observation's logical fingerprint. Re-parsing identical data with new UUIDs yields identical keys.
2. **Audit Lineage & Evaluation IDs**: `evaluation_snapshot_ids` contains only the sorted IDs of snapshots that were temporally eligible and evaluated for that specific resolution request. Future snapshot IDs are never exposed in `SYSTEM_AS_OF`.
3. **No FX / Unit Conversion**: USD gold is never converted to TRY gold; KG is never converted to Troy Ounce or Grams. Pure observed facts only.
4. **No Trading Day Fallback**: The resolver strictly evaluates the requested `effective_date`. It never falls back to the previous trading session (calendar roll belongs in higher orchestration layers).
5. **No Non-Finite Decimals**: Observations with `NaN`, `sNaN`, `Infinity`, or `-Infinity` are rejected.
6. **Confidence Propagation & Stale Discovery**:
   - Resolution confidence never exceeds observation confidence.
   - Snapshots resolved with `is_stale_discovery = True` append diagnostic `DEGRADED_DISCOVERY` and degrade `HIGH` confidence to `MEDIUM`.
