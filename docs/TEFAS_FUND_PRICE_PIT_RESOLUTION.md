# TEFAS Fund Price Point-in-Time (PIT) Resolution Specification

## 1. Overview & Architectural Scope

The TEFAS Fund Price Point-in-Time Resolver (`PointInTimeMarketDataResolver.resolve_tefas_fund_price`) provides audit-grade, deterministic selection of historical Turkish Investment Fund unit prices (`TefasFundPriceObservation`) ingested from Takasbank/TEFAS public surfaces.

Resolution guarantees:
- **Zero Float Contamination:** All unit prices are preserved as exact, finite `Decimal` objects.
- **Zero Lookahead Leakage:** Strict separation between knowledge time (`retrieved_at`), economic trade date (`trade_date`), and backtest simulation cutoff (`as_of`).
- **UUID-Independent Authority:** Resolution decisions and cryptographic SHA-256 keys depend strictly on economic data and immutable snapshot scopes, never physical random database UUIDs.

---

## 2. Query Model & Authority

```python
@dataclass(frozen=True)
class TefasFundPriceQueryKey:
    instrument_id: UUID
    trade_date: date
    provider_symbol: Optional[str] = None
```

- **Primary Identity Authority:** `(instrument_id, trade_date)` with fixed provider `TEFAS`.
- **Diagnostic Context:** `provider_symbol` (e.g. `"MAC"`, `"NNF"`) is recorded for diagnostic lineage and preflight mismatch checks, but does not control canonical identity.
- **Title Exclusion:** The source title field (`fonUnvan`) is current-metadata-only in TEFAS historical API responses and is strictly excluded from resolution authority and normalized price observation models.
- **Upstream Identity Prerequisite:** The resolver operates strictly on pre-validated observations where canonical `instrument_id` and `currency` are already resolved; it does not perform share-class discrimination.

---

## 3. Resolution Modes

| Mode | Semantics & Behavior | Status on Missing/Naive |
| :--- | :--- | :--- |
| `CURRENT_REPORTED` | Evaluates the latest available authoritative snapshot frontier based on `max(retrieved_at)`. | Returns `NO_SNAPSHOT` if no covering snapshot exists. |
| `SYSTEM_AS_OF` | Filters candidate snapshots strictly by `retrieved_at <= as_of` BEFORE frontier conflict checks and authority selection. | Requires timezone-aware `as_of`; missing/naive returns `INVALID_TEMPORAL_LINEAGE`. |
| `SOURCE_AS_OF` | **Always returns `UNAVAILABLE_SOURCE_AS_OF`**. TEFAS public surfaces provide economic price date (`tarih`), but no microsecond-level first-publication timestamp. | Constant fail-closed status. |

---

## 4. Snapshot Target-Date Coverage Semantics

TEFAS fixed-period API requests (`periyod` in {1, 3, 6, 12, 36, 60} months) do not provide user-specified arbitrary date boundaries.

Therefore, target coverage is established **ONLY** through:
1. **Two-Sided Date Range:** `snapshot.trade_date_range` where `range_start <= target_date <= range_end`.
2. **Exact Target Observation:** Snapshot contains an observation where `obs.trade_date == target_date`.

### Non-Authority Rule for `period_months`:
- A request with `periyod=60` indicates "request up to 60 months". It does **not** guarantee that the fund existed 5 years ago, that every intermediate trading date was published, or that boundary dates are present.
- `period_months` and calendar math (`retrieved_at - 60 months`) **never prove coverage alone**. Actual returned observations and valid range boundaries govern authority.

---

## 5. Temporal Filtering & Isolation Invariants

The resolver enforces a strict temporal evaluation pipeline:
1. **Provider & Instrument Filter:** Match `provider == "TEFAS"` and `instrument_id == query.instrument_id`.
2. **Transport Success Filter:** `http_status == 200` and non-empty `payload_hash`. Failed HTTP attempts (403, 429, 500, timeouts) are discarded and cannot supersede older valid data.
3. **Target Coverage Filter:** Snapshots not covering `query.trade_date` are excluded and cannot poison resolution.
4. **Timezone Awareness Validation:** Evaluated covering snapshots with naive `retrieved_at` timestamps fail closed as `INVALID_TEMPORAL_LINEAGE`. Non-covering or failed naive snapshots are ignored and do not contaminate valid lineages.
5. **SYSTEM_AS_OF Filter:** Restrict to `retrieved_at <= as_of` before evaluating frontier conflict or observation values.

---

## 6. Correction & No-Resurrection Invariants

- **Retrospective Corrections:** If Snapshot B (`retrieved_at = T2`) revises the price for a historical trade date compared to Snapshot A (`retrieved_at = T1`):
  - `CURRENT_REPORTED` selects the revised price from Snapshot B.
  - `SYSTEM_AS_OF` with `as_of < T2` deterministically selects the original price from Snapshot A.
- **True No-Resurrection:** If the newest authoritative covering snapshot's `trade_date_range` encompasses `target_date` but either:
  - Contains no observation row for `target_date`, or
  - Contains an observation marked `INVALID_OBSERVATION`,
  the resolver returns `NO_ELIGIBLE_OBSERVATION`. It **never falls back** to an older snapshot's valid price.
- **Incremental Retention:** A newer short-period snapshot (e.g. `periyod=1` covering only recent weeks) does not cover older dates. Queries for older dates safely retain the older covering 60-month snapshot as authoritative.

---

## 7. Exact Date Semantics (No Approximation)

- The resolver strictly evaluates `trade_date == query.trade_date`.
- There is no nearest-business-day, weekend, or holiday substitution inside the resolver layer.
- Non-trading days within a covered range return `NO_ELIGIBLE_OBSERVATION`. Non-trading days outside covered ranges return `NO_SNAPSHOT`.

---

## 8. Deterministic Resolution Key

Each resolution outcome computes a cryptographic SHA-256 `resolution_key` over canonical parameters:
- `observation_type` (`TEFAS_FUND_PRICE`)
- `mode` (`CURRENT_REPORTED`, `SYSTEM_AS_OF`)
- `as_of` timestamp
- `instrument_id`
- `trade_date`
- Authoritative snapshot `payload_hash`
- `period_months` & `trade_date_range`
- Economic observation fingerprint: `(instrument_id, provider, provider_symbol, trade_date, unit_price, currency, instrument_type, status)`

The resolution key is entirely invariant to physical database UUIDs and input list ordering.

---

## Phase 16A — PIT-Safe TEFAS Fund Price Series (`backend/engine/private/fund_price_series.py`)

- **Purpose:** the single authoritative historical unit-price series contract that later fund analytics must consume, instead of raw TEFAS responses, latest rows, unsorted observations, future snapshots, or silently backfilled observations. It only wraps resolver output; it calculates no returns, volatility, drawdown, VaR/CVaR, Sharpe/Sortino, rankings, scores, or recommendations, and consumes none of the current-metrics fields (`portfolio_size`, `investor_count`, `outstanding_units`, reported current price, `fonKategori`).
- **`TefasFundPricePoint` (frozen, six fields):** `trade_date` (exact `date`, never `datetime`), `unit_price` (exact finite `Decimal` > 0, no float), `currency` (exact `Currency`), `confidence` (exact `DataConfidenceLevel`, taken from the selected observation), `snapshot_retrieved_at` (exact timezone-aware datetime), `resolution_key` (non-empty `str`, the resolver's existing key). The mutable source observation is not retained.
- **`TefasFundPriceGap` (frozen, two fields):** `trade_date` and `status` (any `MarketDataResolutionStatus` except `SELECTED`). A gap means the requested date produced no authoritative SELECTED observation under the requested PIT mode. It does NOT mean price zero, return zero, previous price, next price, a weekend, or a holiday.
- **`TefasFundPriceSeries` (frozen, six fields):** `instrument_id`, `mode`, `as_of`, `requested_dates` (non-empty, exact dates, strictly ascending, unique), `points`, `gaps`. Every requested date appears exactly once in `points` or `gaps`, never both, never neither, and both keep requested-date order. `is_complete` is `len(gaps) == 0`; there is no coverage percentage or minimum-observation threshold yet.
- **Builder:** `build_tefas_fund_price_series(*, instrument_id, trade_dates, snapshots, mode, as_of, provider_symbol=None)`. For each requested date it builds a `TefasFundPriceQueryKey` and calls `PointInTimeMarketDataResolver.resolve_tefas_fund_price`; it never filters snapshots, selects a latest snapshot, or re-implements resolver logic. The snapshot collection is not retained. `provider_symbol` is diagnostic only. Inputs are validated with exact types; dates are never sorted or normalized (callers cannot hide malformed ordering).
- **Modes:** `SYSTEM_AS_OF` requires an exact timezone-aware `as_of` (validated before resolution; no normalization, no current time) and inherits the resolver's future isolation: snapshots retrieved after the cutoff cannot change the series. `CURRENT_REPORTED` requires `as_of is None`. `SOURCE_AS_OF` stays unavailable: the resolver returns `UNAVAILABLE_SOURCE_AS_OF`, so every requested date becomes a gap; it is never downgraded to another mode.
- **No Resurrection (inherited):** when a newer authoritative snapshot covers a date but omits or invalidates the row, the date is a gap (`NO_ELIGIBLE_OBSERVATION`); the older price is never restored. Snapshot and observation conflicts likewise become gaps (`SNAPSHOT_CONFLICT`, `OBSERVATION_CONFLICT`).
- **Missing != Zero, No Fabrication:** no interpolation, forward-fill, backward-fill, or calendar/holiday/weekend inference; the caller supplies the explicit `trade_dates`.
- **Currency Consistency:** all points in one series must share one `Currency`; otherwise construction fails closed with `ValueError("TEFAS fund price series contains inconsistent currencies")`. No FX conversion is performed.
- **Defensive Checks:** a SELECTED result whose observation is missing, not an exact `TefasFundPriceObservation`, or does not match the requested instrument and date fails closed.
- **Guard Status:** the module is deliberately NOT in `PURE_MANIFEST`. It must call `PointInTimeMarketDataResolver`, and the `market_data` modules it imports are not pure-manifest modules (`uuid4` default factories, `datetime.now` defaults, `hashlib`), so G3 flags exactly those three imports and adding it to the manifest would fail the manifest test without a scanner change. The module itself is clean under G1, G2, G4, and G5 (asserted directly in its test suite; G4/G5 also scan the whole private tree).
