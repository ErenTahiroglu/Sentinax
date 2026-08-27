# Point-in-Time Resolution Architecture: TEFAS Current Fund Metrics (Phase 11D.3)

## 1. Executive Summary & Core Temporal Principle

This document specifies the Point-in-Time (PIT) resolution architecture and fail-closed invariants for **TEFAS Current Fund Valuation and Metrics** (`TefasFundCurrentMetricsObservation`, `TefasFundMetricsSnapshot`).

Unlike dated time-series endpoints (such as `fonFiyatBilgiGetir` with historical `trade_date`), the TEFAS current valuation snapshot endpoint (`fonBilgiGetir`) **contains no economic publication date, valuation date, or source timestamp**.

Consequently:
- **Authority Axis:** Sentinax knowledge time (`retrieved_at` in UTC).
- **`effective_date` & `published_at`:** Strictly `None` (zero timestamp fabrication).
- **System History:** Sentinax records an immutable time-series of its own ingestion snapshots forward; this is strictly **system knowledge history**, NOT source historical AUM.

---

## 2. Query Key & Resolution Modes

### A) Query Key: `TefasFundCurrentMetricsQueryKey`
- `instrument_id: UUID` (Canonical authority)
- `provider_symbol: Optional[str]` (Diagnostic only)
- Provider is fixed to `"TEFAS"`.
- Contains **no `trade_date` or `effective_date`** query parameters.

### B) Supported Resolution Modes

| Mode | Behavior | Semantics |
| :--- | :--- | :--- |
| **`CURRENT_REPORTED`** | Selects latest authoritative HTTP-200 snapshot by `retrieved_at`. | "What is the latest current view Sentinax has ingested from TEFAS?" |
| **`SYSTEM_AS_OF`** | Filters snapshots with `retrieved_at <= as_of` before authority selection. | "What was the latest TEFAS current view known to Sentinax at `as_of`?" |
| **`SOURCE_AS_OF`** | **Always returns `UNAVAILABLE_SOURCE_AS_OF`**. | Source does not provide an economic publication date. |

---

## 3. Strict Fail-Closed & Anti-Leakage Invariants

### 1. HTTP-200 Invalid View Blocks Resurrection (Fail-Closed)
If the latest HTTP-200 snapshot in the evaluated scope has no observation, malformed AUM, status $\neq$ `VALID`, or schema mismatch:
- The resolver returns **`MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION`**.
- It **NEVER resurrects** an older valid snapshot.
- *Rationale:* A new source view was captured, but Sentinax cannot safely interpret it. Returning stale older data would silently masquerade obsolete metrics as current.

### 2. HTTP Transport Failures Do Not Supersede
Snapshots/attempts resulting in HTTP `403`, `429`, `500`, or network timeouts:
- Are excluded from authority candidate evaluation (`http_status == 200` required).
- Do **NOT** supersede older valid HTTP-200 snapshots.

### 3. Fresh Partial Metrics Beat Old Complete Metrics
If the latest authoritative snapshot has valid `portfolio_size` (AUM) but missing `investor_count` or `outstanding_units`:
- The latest `PARTIAL` observation is selected.
- The resolver **does NOT resurrect** missing fields from an older `COMPLETE` snapshot.
- *Rationale:* Fresh partial metrics represent newer truth than stale complete metrics.

### 4. Zero Staleness Threshold in PIT Resolver
The PIT resolver answers strictly: **"What did Sentinax know at knowledge time?"**
- It does NOT discard or reject snapshots for being 24h, 48h, or 30 days old.
- Staleness policy is evaluated downstream by decision and risk engines, preserving clean separation of concerns.

### 5. Deterministic, UUID-Independent Resolution Key
- The resolution result produces a deterministic SHA-256 `resolution_key` computed from:
  - Observation type (`TEFAS_FUND_CURRENT_METRICS`)
  - Resolution mode and `as_of` timestamp
  - Canonical `instrument_id`
  - Authoritative snapshot scope: `(provider, endpoint, provider_symbol, retrieved_at, payload_hash)`
  - Economic observation fingerprint: `(instrument_id, provider, provider_symbol, portfolio_size, currency, outstanding_units, investor_count, reported_current_unit_price, instrument_type, status)`
- Re-running queries across reversed input orders or regenerated UUIDs yields identical resolution keys.

### 6. TRY-Only Currency Enforcement
- Only observations with canonical `portfolio_size_currency == Currency.TRY` are eligible.
- Non-TRY canonical instruments fail closed.
