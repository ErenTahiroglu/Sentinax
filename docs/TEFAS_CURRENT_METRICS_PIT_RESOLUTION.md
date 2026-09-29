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
  - Authoritative snapshot scope: `(provider, endpoint, instrument_id, provider_symbol, retrieved_at, payload_hash, parser_version)`
  - Economic observation fingerprint: `(instrument_id, provider, provider_symbol, portfolio_size, currency, outstanding_units, investor_count, reported_current_unit_price, instrument_type, status)`
- Re-running queries across reversed input orders or regenerated UUIDs yields identical resolution keys.

### 6. Strict Typing & Explicit Classification Invariants
- **Exact Decimal Preservation:** Persisted AUM (`portfolio_size`) and `outstanding_units` must be exact `Decimal` instances (finite and non-negative); `float`, string, integer, or silent float conversions are strictly rejected.
- **Explicit Instrument Type:** Canonical `instrument_type` must be explicit and belong to `TEFAS_RESOLVER_ALLOWED_INSTRUMENT_TYPES`; `None` fails closed as ineligible.
- **TRY-Only Currency Enforcement:** Only observations with canonical `portfolio_size_currency == Currency.TRY` are eligible; non-TRY canonical instruments fail closed.

---

## Phase 16G — PIT-Safe TEFAS Fund Category Authority (`backend/engine/private/fund_category.py`)

- **Separation From Current Metrics:** `fonKategori` remains a raw-snapshot-only field. It is NOT added to `TefasFundCurrentMetricsObservation`, the observation fingerprint, or any economic field (`portfolio_size`, `outstanding_units`, `investor_count`, `reported_current_unit_price`), so category cannot control AUM or current-metrics validity. Phase 16G is a separate derived authority over the raw payload of the snapshot that the existing resolver selects. Neither the provider nor the resolver was modified.
- **Authority Chain:** `resolve_tefas_fund_category(*, query_key, snapshots, mode, as_of)` first calls `PointInTimeMarketDataResolver.resolve_tefas_current_metrics` with the same arguments; snapshot eligibility, conflicts, no-resurrection, the `SYSTEM_AS_OF` `retrieved_at <= as_of` cutoff, selection, and resolution identity are never re-implemented. A non-`SELECTED` underlying status is returned as `TefasFundCategoryUnavailable(reason=UNDERLYING_UNAVAILABLE, underlying_status=<exact status>)` without inspecting any payload.
- **Selected Snapshot Recovery and Raw Payload Integrity:** the selected snapshot is found in the caller-supplied snapshots by exact id (exactly one match) and must agree with the resolution on id, payload hash, `retrieved_at`, and instrument (`canonical_instrument_id == query_key.instrument_id`); otherwise `SELECTED_SNAPSHOT_UNAVAILABLE` and no other snapshot is used. Before `fonKategori` is read, the existing `compute_payload_hash(raw_payload)` must equal both the snapshot's recorded hash and the resolution's snapshot hash (`PAYLOAD_INTEGRITY_FAILURE` otherwise); no new hash implementation exists. Lineage hashes must be lowercase 64-character SHA-256 hex.
- **Payload Shape:** the raw payload must be an exact `str` of JSON with no duplicate object keys (no last-key-wins), a root `dict`, and `resultList` a list with exactly one `dict` row; otherwise `RAW_PAYLOAD_INVALID`. Only `row["fonKategori"]` is read; `kategoriDerece`, `kategoriFonSay`, `pazarPayi`, `fonUnvan`, and `InstrumentType` never substitute for it.
- **Label Semantics:** the canonical label is the raw value after the outer `.strip()` only: no case folding, accent removal, Turkish-character mapping, synonym mapping, abbreviation, or taxonomy (`"Hisse Senedi Fonu"` and `"hisse senedi fonu"` stay different labels). Absent, `null`, or blank values give `CATEGORY_MISSING`; non-string values give `CATEGORY_INVALID`. Missing is never replaced by `"Unknown"`, `"Other"`, an instrument type, or an empty string.
- **InstrumentType Is Not Category Authority:** `InstrumentType.TEFAS_*` values are canonical instrument classifications and capability metadata, not evidence of the exact TEFAS-reported category at a knowledge time. The module does not import or map them, and never infers a category from a fund name or code.
- **`TefasFundCategoryLineage` (four fields):** `snapshot_id`, `snapshot_hash`, `observed_at`, `metrics_resolution_key`. `observed_at` is the time Sentinax retrieved the selected snapshot. It is NOT the economic or legal date the fund entered the category and NOT a TEFAS publication timestamp; no effective date is fabricated. A category-only payload difference (with recomputed hashes) changes the snapshot hash and the metrics resolution key, so the raw snapshot hash provides the binding even though the normalized observation excludes the category.
- **Modes:** `CURRENT_REPORTED` is supported (`as_of` must be `None`); `SYSTEM_AS_OF` is supported through retrieved-at PIT history, and a snapshot retrieved after `as_of` can never appear in a historical result (adding, changing, or corrupting future snapshots leaves the historical category unchanged); `SOURCE_AS_OF` remains unavailable (`UNDERLYING_UNAVAILABLE` with `UNAVAILABLE_SOURCE_AS_OF`) and is never downgraded.
- **No-Resurrection:** if the authoritative snapshot has a missing, blank, or invalid category (or an invalid payload), the result is unavailable; the label of an older snapshot is never restored.
- **Unavailable Results:** `TefasFundCategoryUnavailableReason` has six members (`UNDERLYING_UNAVAILABLE`, `SELECTED_SNAPSHOT_UNAVAILABLE`, `PAYLOAD_INTEGRITY_FAILURE`, `RAW_PAYLOAD_INVALID`, `CATEGORY_MISSING`, `CATEGORY_INVALID`); `UNDERLYING_UNAVAILABLE` requires a non-`SELECTED` underlying status and every other reason requires `SELECTED`. Lineage is attached when the selected snapshot was established (integrity, payload, and category failures) and is `None` otherwise.
- **Not Included:** peer groups, percentiles, ranks, persistence, category benchmarks or averages, scores, and recommendations. Later peer-relative analytics consume `TefasFundCategoryObservation`, never a raw `fonKategori`.
- **Guard Status:** the module is NOT in `PURE_MANIFEST` because it depends on market-data and storage modules outside it; it is clean under G1, G2, G4, and G5, and G3 flags only the four required private imports (asserted in its test suite). No scanner exception was added.
